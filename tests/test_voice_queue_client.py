"""CASE voice-queue client on the Claude stop hook. No real audio, no daemon."""

import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock


HOOK_PATH = Path(__file__).parents[1] / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location("voice_stop_hook_voiceq", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)

# Stereo 16-bit frames so the WAV header has to carry rate and channels.
PCM = b"\x11\x22\x33\x44" * 80
RATE = 16000
CHANNELS = 2
VOICE = '<voice lang="en">The build finished cleanly.</voice>'


class FakeVoiceq:
    """One-thread Unix socket. reply_fn returns a dict, raw bytes, or None to hang."""

    def __init__(self, path, reply_fn):
        self.path = path
        self.reply_fn = reply_fn
        self.requests = []
        self.connections = 0
        self.submit_info = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._handlers = []
        self._closed = False
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.connections += 1
            handler = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            with self._lock:
                self._handlers.append(handler)
            handler.start()

    def _handle(self, conn):
        try:
            conn.settimeout(2.0)
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            try:
                req = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                req = {"_raw": line.decode("utf-8", "replace")}
            with self._lock:
                self.requests.append(req)
            if req.get("op") == "submit" and isinstance(req.get("wav"), str):
                wav = req["wav"]
                info = {"exists": os.path.isfile(wav), "size": 0}
                if info["exists"]:
                    info["size"] = os.path.getsize(wav)
                with self._lock:
                    self.submit_info = info
            reply = self.reply_fn(req)
            if reply is None:
                self._stop.wait(6.0)
                return
            if isinstance(reply, (bytes, bytearray)):
                conn.sendall(reply)
            else:
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            handler.join(timeout=2)
        try:
            os.unlink(self.path)
        except OSError:
            pass


class VoiceQueueClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.flag_path = os.path.join(self.root, "voice-queue.enabled")
        self.sock_path = os.path.join(self.root, "vq.sock")
        self.lock_path = os.path.join(self.root, "sonia-tts.lock")
        self.log_path = os.path.join(self.root, "claude-tts.log")
        self.queue_dir = os.path.join(self.root, "tickets")
        self.state_path = os.path.join(self.root, "voxtype-state")
        self.wav_dir = os.path.join(self.root, "incoming")
        os.mkdir(self.queue_dir)
        os.mkdir(self.wav_dir)
        self.wav_path = os.path.join(self.wav_dir, "42.wav")
        self.env = {
            "SHELBY_VOICE_QUEUE_FLAG": self.flag_path,
            "SHELBY_VOICEQ_SOCKET": self.sock_path,
            "SHELBY_VOICEQ_STATE_DIR": self.root,
            "SHELBY_VOICE_QUEUE_DIR": self.queue_dir,
            "SHELBY_VOXTYPE_STATE_FILE": self.state_path,
            "CLAUDE_SESSION_TYPE": "main",
        }
        VOICE_HOOK._voiceq_reservation = None
        VOICE_HOOK._voiceq_sunk = False
        VOICE_HOOK._voiceq_fail_logged = False
        VOICE_HOOK._voiceq_remote_routed = False
        VOICE_HOOK._ticket_path = None
        VOICE_HOOK._ticket_stamp_ns = None
        VOICE_HOOK._queue_mode = False
        self.during = {}

    def _enable_flag(self):
        with open(self.flag_path, "w", encoding="utf-8") as handle:
            handle.write("on\n")

    def _write_state(self, word):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write(word)

    def _log(self):
        try:
            return Path(self.log_path).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _voiceq_lines(self):
        return [line for line in self._log().splitlines() if "voiceq:" in line]

    def _server(self, reply_fn):
        server = FakeVoiceq(self.sock_path, reply_fn)
        self.addCleanup(server.close)
        return server

    def _accepting_server(self, on_submit=None):
        wav_path = self.wav_path

        def reply(req):
            if req.get("op") == "reserve":
                return {"ok": True, "id": "42", "wav": wav_path}
            if req.get("op") == "submit":
                if on_submit is not None:
                    return on_submit(req)
                return {"ok": True}
            return {"ok": True}

        return self._server(reply)

    def _run(self, assistant, cfg=None, on_speak=None, env=None, loads_spy=None):
        if cfg is None:
            cfg = {"tts_enabled": True, "remote_audio": False, "voxtype_wait": True}
        payload = json.dumps({
            "last_assistant_message": assistant,
            "session_id": "voiceq-test",
        })
        result = {
            "spoken": [],
            "locks": [],
            "waits": [],
        }
        real_lock = VOICE_HOOK.acquire_lock
        real_loads = VOICE_HOOK.json.loads

        def spy_lock(*args, **kwargs):
            result["locks"].append((args, kwargs))
            return real_lock(*args, **kwargs)

        def spy_wait(*args, **kwargs):
            result["waits"].append((args, kwargs))
            return (0.0, False)

        def speak(clean, hook_cfg, lang_hint=None):
            result["spoken"].append(clean)
            if on_speak is None:
                raise AssertionError("speak called")
            on_speak(clean, hook_cfg, lang_hint)

        def spy_loads(raw):
            if loads_spy is not None:
                loads_spy(raw)
            return real_loads(raw)

        run = mock.Mock()
        popen = mock.Mock()
        previous = signal.getsignal(signal.SIGTERM)
        use_env = dict(self.env if env is None else env)
        try:
            with mock.patch.dict(os.environ, use_env), \
                    mock.patch.object(VOICE_HOOK, "LOG_PATH", self.log_path), \
                    mock.patch.object(VOICE_HOOK, "LOCKFILE_PATH", self.lock_path), \
                    mock.patch.object(VOICE_HOOK, "load_config", return_value=cfg), \
                    mock.patch.object(VOICE_HOOK, "_record_missing_voice_block"), \
                    mock.patch.object(VOICE_HOOK, "acquire_lock", side_effect=spy_lock), \
                    mock.patch.object(
                        VOICE_HOOK, "_wait_for_voxtype_idle_status", side_effect=spy_wait,
                    ), \
                    mock.patch.object(VOICE_HOOK, "speak", side_effect=speak), \
                    mock.patch.object(VOICE_HOOK.json, "loads", side_effect=spy_loads), \
                    mock.patch.object(VOICE_HOOK.subprocess, "run", run), \
                    mock.patch.object(VOICE_HOOK.subprocess, "Popen", popen), \
                    mock.patch.object(VOICE_HOOK.sys, "stdin", io.StringIO(payload)):
                VOICE_HOOK.main()
        finally:
            signal.signal(signal.SIGTERM, previous)
        result["run"] = run
        result["popen"] = popen
        return result

    def _play_local(self, server=None):
        def on_speak(_clean, _cfg, _lang=None):
            self.during = {
                "lock_text": (
                    Path(self.lock_path).read_text(encoding="utf-8")
                    if os.path.exists(self.lock_path) else ""
                ),
                "ticket": VOICE_HOOK._ticket_path,
                "queue": os.listdir(self.queue_dir),
                "ops": [req.get("op") for req in (server.requests if server else [])],
            }
            VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)
        return on_speak

    def _assert_paplay(self, result):
        result["run"].assert_called_once()
        argv = result["run"].call_args.args[0]
        self.assertEqual(argv[0], "paplay")
        self.assertIn(f"--rate={RATE}", argv)
        self.assertIn(f"--channels={CHANNELS}", argv)
        self.assertIn("--format=s16le", argv)
        self.assertEqual(result["run"].call_args.kwargs.get("input"), PCM)
        self.assertTrue(result["locks"])
        self.assertIn(str(os.getpid()), self.during.get("lock_text", ""))
        result["popen"].assert_not_called()

    def _assert_no_local_play(self, result):
        result["run"].assert_not_called()
        result["popen"].assert_not_called()
        self.assertEqual(result["locks"], [])
        self.assertEqual(result["waits"], [])
        self.assertFalse(os.path.exists(self.lock_path))
        self.assertEqual(os.listdir(self.queue_dir), [])

    def test_flag_absent_does_not_touch_socket(self):
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.requests, [])
        self.assertEqual(result["spoken"], ["The build finished cleanly."])
        self._assert_paplay(result)
        self.assertEqual(self._voiceq_lines(), [])
        self.assertFalse(os.path.exists(self.wav_path))

    def test_macos_with_flag_uses_today_path(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        with mock.patch.object(VOICE_HOOK, "IS_MACOS", True):
            result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.requests, [])
        result["run"].assert_called()
        self.assertEqual(result["run"].call_args.args[0][0], "afplay")
        self.assertTrue(result["locks"])
        self.assertEqual(self._voiceq_lines(), [])

    def test_wsl_with_flag_uses_today_path(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        with mock.patch.object(VOICE_HOOK, "_voiceq_is_wsl", return_value=True):
            result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.requests, [])
        self._assert_paplay(result)
        self.assertEqual(self._voiceq_lines(), [])

    def test_wsl_detection_reads_proc_version(self):
        real_open = open

        def opener(text):
            def fake(path, *args, **kwargs):
                if path == "/proc/version":
                    return io.StringIO(text)
                return real_open(path, *args, **kwargs)
            return fake

        with mock.patch("builtins.open", opener("Linux 5.15.0-microsoft-standard-WSL2")):
            self.assertTrue(VOICE_HOOK._voiceq_is_wsl())
        with mock.patch("builtins.open", opener("Linux version 6.6.87.2-microsoft-standard-WSL2")):
            self.assertTrue(VOICE_HOOK._voiceq_is_wsl())
        with mock.patch("builtins.open", opener("Linux version 7.2.8-10-omarchy")):
            self.assertFalse(VOICE_HOOK._voiceq_is_wsl())
        with mock.patch("builtins.open", opener("")):
            self.assertFalse(VOICE_HOOK._voiceq_is_wsl())

    def test_macos_returns_before_proc_version_and_flag(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        real_open = open
        real_exists = os.path.exists

        def opener(path, *args, **kwargs):
            if path == "/proc/version":
                raise AssertionError("/proc/version read on macOS")
            return real_open(path, *args, **kwargs)

        def spy_exists(path, *args, **kwargs):
            if path == self.flag_path:
                raise AssertionError("flag stat on macOS")
            return real_exists(path, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK, "IS_MACOS", True), \
                mock.patch("builtins.open", side_effect=opener), \
                mock.patch("os.path.exists", side_effect=spy_exists):
            result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.requests, [])
        result["run"].assert_called()
        self.assertEqual(result["run"].call_args.args[0][0], "afplay")

    def test_wsl_returns_before_flag_stat(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        real_exists = os.path.exists

        def spy_exists(path, *args, **kwargs):
            if path == self.flag_path:
                raise AssertionError("flag stat on WSL")
            return real_exists(path, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK, "_voiceq_is_wsl", return_value=True), \
                mock.patch("os.path.exists", side_effect=spy_exists):
            result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.requests, [])
        self._assert_paplay(result)

    def test_unreadable_proc_version_is_not_case(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        real_open = open
        real_exists = os.path.exists

        def opener(path, *args, **kwargs):
            if path == "/proc/version":
                raise OSError(errno.EACCES, "unreadable")
            return real_open(path, *args, **kwargs)

        def spy_exists(path, *args, **kwargs):
            if path == self.flag_path:
                raise AssertionError("flag stat when /proc/version is unreadable")
            return real_exists(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=opener), \
                mock.patch("os.path.exists", side_effect=spy_exists):
            self.assertIsNone(VOICE_HOOK._voiceq_proc_version())
            result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(server.connections, 0)
        self.assertEqual(server.requests, [])
        self._assert_paplay(result)

    def test_default_flag_and_socket_paths(self):
        kept = {
            key: os.environ[key]
            for key in ("HOME", "USER", "PATH", "XDG_RUNTIME_DIR")
            if key in os.environ
        }
        with mock.patch.dict(os.environ, kept, clear=True):
            self.assertEqual(
                VOICE_HOOK._voiceq_flag_path(),
                os.path.expanduser("~/.config/shelby/voice-queue.enabled"),
            )
            runtime = os.environ.get("XDG_RUNTIME_DIR")
            self.assertTrue(runtime)
            self.assertEqual(
                VOICE_HOOK._voiceq_socket_path(),
                os.path.join(runtime, "shelby-voiceq.sock"),
            )
            self.assertEqual(
                VOICE_HOOK._voiceq_state_dir(),
                os.path.expanduser("~/.local/state/shelby-voiceq"),
            )
        with mock.patch.dict(
            os.environ,
            {
                "SHELBY_VOICEQ_SOCKET": "",
                "XDG_RUNTIME_DIR": "",
                "SHELBY_VOICEQ_STATE_DIR": "",
            },
        ):
            self.assertIsNone(VOICE_HOOK._voiceq_socket_path())
            self.assertEqual(
                VOICE_HOOK._voiceq_state_dir(),
                os.path.expanduser("~/.local/state/shelby-voiceq"),
            )

    def test_queue_path_reserves_writes_wav_and_submits(self):
        self._enable_flag()
        self._write_state("recording")
        seen = {}
        server = self._accepting_server()

        def loads_spy(raw):
            text = raw if isinstance(raw, str) else ""
            if "last_assistant_message" not in text:
                return
            seen["ops_at_parse"] = [req.get("op") for req in server.requests]
            seen["reserve"] = dict(server.requests[0]) if server.requests else None

        started = time.monotonic()
        result = self._run(
            VOICE,
            on_speak=self._play_local(server),
            loads_spy=loads_spy,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0)
        self.assertEqual(seen["ops_at_parse"], ["reserve"])
        self.assertEqual(seen["reserve"]["source"], "claude")
        self.assertEqual(seen["reserve"]["label"], "claude")
        self.assertEqual(seen["reserve"]["op"], "reserve")
        self._assert_key(seen["reserve"])
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "submit"])
        submit = server.requests[1]
        self.assertEqual(submit["id"], "42")
        self.assertEqual(submit["wav"], self.wav_path)
        self.assertEqual(server.submit_info["exists"], True)
        self.assertGreater(server.submit_info["size"], 44)
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
        self.assertEqual(os.stat(self.wav_path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(self.wav_path + ".part"))
        self._assert_no_local_play(result)
        self.assertIsNone(self.during["ticket"])
        self.assertEqual(self.during["queue"], [])
        self.assertEqual(self.during["ops"], ["reserve"])
        self.assertEqual(self._voiceq_lines(), [])
        self.assertIsNone(VOICE_HOOK._voiceq_reservation)

    def test_no_voice_block_cancels(self):
        self._enable_flag()
        server = self._accepting_server()
        result = self._run("plain prose with no voice block")
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "cancel"])
        self.assertEqual(server.requests[1]["id"], "42")
        self.assertEqual(result["spoken"], [])
        self._assert_no_local_play(result)
        self.assertFalse(os.path.exists(self.wav_path))

    def test_tts_disabled_cancels(self):
        self._enable_flag()
        server = self._accepting_server()
        result = self._run(VOICE, cfg={
            "tts_enabled": False, "remote_audio": False, "voxtype_wait": True,
        })
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "cancel"])
        self.assertEqual(server.requests[1]["id"], "42")
        self.assertEqual(result["spoken"], [])
        self._assert_no_local_play(result)

    def test_empty_after_sanitize_cancels(self):
        self._enable_flag()
        assistant = "<voice>/var/tmp/only-a-path</voice>"
        shaped = VOICE_HOOK.extract_voice_block(assistant)
        self.assertEqual(VOICE_HOOK.sanitize_for_speech(shaped), "")
        server = self._accepting_server()
        result = self._run(assistant)
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "cancel"])
        self.assertEqual(result["spoken"], [])
        self._assert_no_local_play(result)

    def test_synthesis_failure_cancels(self):
        self._enable_flag()
        server = self._accepting_server()

        def boom(_clean, _cfg, _lang=None):
            raise RuntimeError("synth failed")

        result = self._run(VOICE, on_speak=boom)
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "cancel"])
        self.assertEqual(result["spoken"], ["The build finished cleanly."])
        self._assert_no_local_play(result)
        self.assertIn("TTS error:", self._log())
        self.assertFalse(os.path.exists(self.wav_path))

    def test_reserve_timeout_uses_today_path(self):
        self._enable_flag()

        def reply(req):
            if req.get("op") == "reserve":
                return None
            return {"ok": True}

        server = self._server(reply)
        started = time.monotonic()
        result = self._run(VOICE, on_speak=self._play_local(server))
        elapsed = time.monotonic() - started
        reserves = [req for req in server.requests if req.get("op") == "reserve"]
        self.assertEqual(len(reserves), 3)
        self.assertGreaterEqual(elapsed, 2.5)
        self.assertLess(elapsed, 5.0)
        key = self._assert_same_keys(reserves)
        self.assertEqual(server.requests[-1].get("op"), "cancel")
        self.assertEqual(server.requests[-1].get("key"), key)
        self.assertNotIn("id", server.requests[-1])
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("reserve failed", self._voiceq_lines()[0])
        self.assertFalse(os.path.exists(self.wav_path))

    def test_reserve_slower_than_one_second_retries_same_key_without_paplay(self):
        self._enable_flag()
        state = {"n": 0}

        def reply(req):
            if req.get("op") == "reserve":
                state["n"] += 1
                if state["n"] == 1:
                    time.sleep(1.4)
                    return {"ok": True, "id": "42", "wav": self.wav_path}
                return {"ok": True, "id": "42", "wav": self.wav_path}
            return {"ok": True}

        server = self._server(reply)
        started = time.monotonic()
        result = self._run(VOICE, on_speak=self._play_local(server))
        elapsed = time.monotonic() - started
        reserves = [req for req in server.requests if req.get("op") == "reserve"]
        self.assertEqual(len(reserves), 2)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 3.0)
        self._assert_same_keys(reserves)
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "reserve", "submit"])
        self.assertEqual(server.requests[-1]["id"], "42")
        self.assertEqual(server.requests[-1]["wav"], self.wav_path)
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
        self._assert_no_local_play(result)

    def test_reserve_busy_is_definite_and_does_not_retry(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": False, "error": "busy"})
        started = time.monotonic()
        result = self._run(VOICE, on_speak=self._play_local(server))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["op"], "reserve")
        self.assertEqual(server.requests[0]["error"] if "error" in server.requests[0] else None, None)
        self._assert_key(server.requests[0])
        self._assert_paplay(result)
        self.assertIn("reserve failed", self._voiceq_lines()[0])

    def test_reserve_refused_is_definite_and_does_not_retry(self):
        self._enable_flag()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.sock_path)
        sock.listen(1)
        sock.close()
        self.addCleanup(lambda: os.path.exists(self.sock_path) and os.unlink(self.sock_path))
        connects = {"n": 0}
        real_socket = socket.socket

        class Counting(real_socket):
            def connect(self, addr):
                connects["n"] += 1
                return super().connect(addr)

        started = time.monotonic()
        with mock.patch("socket.socket", Counting):
            result = self._run(VOICE, on_speak=self._play_local())
        elapsed = time.monotonic() - started
        self.assertEqual(connects["n"], 1)
        self.assertLess(elapsed, 0.8)
        self._assert_paplay(result)
        self.assertIn("reserve failed", self._voiceq_lines()[0])

    def test_socket_missing_uses_today_path(self):
        self._enable_flag()
        missing = os.path.join(self.root, "no-such.sock")
        env = dict(self.env)
        env["SHELBY_VOICEQ_SOCKET"] = missing
        started = time.monotonic()
        result = self._run(VOICE, on_speak=self._play_local(), env=env)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8)
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("reserve failed", self._voiceq_lines()[0])

    def test_missing_default_socket_uses_today_path(self):
        self._enable_flag()
        xdg = os.path.join(self.root, "xdg")
        os.mkdir(xdg)
        # A server on the override path must stay idle: empty override means default.
        server = self._server(lambda _req: {"ok": True, "id": "42", "wav": self.wav_path})
        env = dict(self.env)
        env["SHELBY_VOICEQ_SOCKET"] = ""
        env["XDG_RUNTIME_DIR"] = xdg
        result = self._run(VOICE, on_speak=self._play_local(), env=env)
        self.assertEqual(server.connections, 0)
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)

    def test_reserve_not_ok_uses_today_path(self):
        self._enable_flag()
        server = self._server(lambda _req: {"ok": False, "id": "42", "wav": self.wav_path})
        result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual([req.get("op") for req in server.requests], ["reserve"])
        self._assert_key(server.requests[0])
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)

    def test_oversized_reply_uses_today_path(self):
        self._enable_flag()
        blob = b"{" + (b"x" * 9000)
        self.assertNotIn(b"\n", blob)
        server = self._server(lambda _req: blob)
        started = time.monotonic()
        result = self._run(VOICE, on_speak=self._play_local(server))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        reserves = [req for req in server.requests if req.get("op") == "reserve"]
        self.assertEqual(len(reserves), 3)
        key = self._assert_same_keys(reserves)
        self.assertEqual(server.requests[-1].get("op"), "cancel")
        self.assertEqual(server.requests[-1].get("key"), key)
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)

    def test_malformed_reply_uses_today_path(self):
        self._enable_flag()
        # A complete ok:true with a bad id or wav cancels once. Garbage and
        # a non-object are ambiguous, so the same key is retried.
        definite_bad = [
            b'{"ok": true}\n',
            b'{"ok": true, "id": "1", "wav": "relative.wav"}\n',
            b'{"ok": true, "id": "", "wav": "/tmp/voiceq-empty-id.wav"}\n',
            b'{"ok": true, "id": 1, "wav": "not-absolute"}\n',
        ]
        ambiguous = [
            b"not-json\n",
            b"[1, 2]\n",
        ]
        for raw in definite_bad + ambiguous:
            with self.subTest(raw=raw):
                Path(self.log_path).write_text("", encoding="utf-8")
                if os.path.exists(self.sock_path):
                    os.unlink(self.sock_path)
                if os.path.exists(self.lock_path):
                    os.unlink(self.lock_path)
                server = self._server(lambda _req, raw=raw: raw)
                try:
                    result = self._run(VOICE, on_speak=self._play_local(server))
                    reserves = [req for req in server.requests if req.get("op") == "reserve"]
                    if raw in ambiguous:
                        self.assertEqual(len(reserves), 3)
                    else:
                        self.assertEqual(len(reserves), 1)
                    key = self._assert_same_keys(reserves)
                    self.assertEqual(server.requests[-1].get("op"), "cancel")
                    if raw in (b'{"ok": true}\n', b'{"ok": true, "id": "", "wav": "/tmp/voiceq-empty-id.wav"}\n'):
                        self.assertEqual(server.requests[-1].get("key"), key)
                        self.assertNotIn("id", server.requests[-1])
                    elif raw in ambiguous:
                        self.assertEqual(server.requests[-1].get("key"), key)
                    else:
                        self.assertEqual(server.requests[-1].get("id"), "1")
                    self._assert_paplay(result)
                    self.assertEqual(len(self._voiceq_lines()), 1)
                    self.assertFalse(os.path.exists(self.wav_path))
                finally:
                    server.close()

    def test_submit_rejected_does_not_play_while_daemon_answers(self):
        self._enable_flag()

        def on_submit(_req):
            return {"ok": False}

        server = self._accepting_server(on_submit=on_submit)
        seen = {}
        with self._patch_gated(seen):
            result = self._run(VOICE, on_speak=self._play_local(server))
        ops = [req.get("op") for req in server.requests]
        self.assertEqual(ops[0], "reserve")
        self.assertEqual(ops[1:-1], ["submit", "submit", "submit", "submit"])
        self.assertEqual(ops[-1], "cancel")
        for req in server.requests[1:-1]:
            self.assertEqual(req["id"], "42")
            self.assertEqual(req["wav"], self.wav_path)
        self.assertEqual(server.requests[-1]["id"], "42")
        self.assertGreater(server.submit_info["size"], 44)
        self.assertFalse(os.path.exists(self.wav_path))
        self.assertFalse(os.path.exists(self.wav_path + ".part"))
        self.assertEqual(seen, {})
        self._assert_no_local_play(result)
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("not playing locally", self._voiceq_lines()[0])
        self.assertEqual(self._voiceq_private_names(), [])

    def test_remote_audio_cancels_and_plays_locally(self):
        self._enable_flag()
        server = self._accepting_server()
        result = self._run(
            VOICE,
            cfg={"tts_enabled": True, "remote_audio": True, "voxtype_wait": True},
            on_speak=self._play_local(server),
        )
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "cancel"])
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("remote_audio", self._voiceq_lines()[0])
        self.assertFalse(os.path.exists(self.wav_path))

    def test_elevenlabs_stream_writes_wav_instead_of_paplay(self):
        self._arm_reservation()
        chunks = [b"\x01\x02", b"", b"\x03\x04"]
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = lambda chunk_size=4096: iter(chunks)
        requests_mod = mock.Mock()
        requests_mod.post.return_value = response
        try:
            with mock.patch.dict(sys.modules, {"requests": requests_mod}), \
                    mock.patch.object(VOICE_HOOK, "voxtype_present", return_value=False), \
                    mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen, \
                    mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
                VOICE_HOOK.speak_elevenlabs_streaming("hi", "voice", "model", "key")
            popen.assert_not_called()
            run.assert_not_called()
            self._assert_wav(self.wav_path, b"\x01\x02\x03\x04", 24000, 1)
            self.assertTrue(VOICE_HOOK._voiceq_sunk)
        finally:
            VOICE_HOOK._voiceq_reservation = None
            VOICE_HOOK._voiceq_sunk = False

    def test_elevenlabs_voxtype_branch_skips_gated_player(self):
        self._arm_reservation()
        chunks = [b"\x05\x06", b"\x07\x08"]
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = lambda chunk_size=4096: iter(chunks)
        requests_mod = mock.Mock()
        requests_mod.post.return_value = response
        try:
            with mock.patch.dict(sys.modules, {"requests": requests_mod}), \
                    mock.patch.object(VOICE_HOOK, "voxtype_present", return_value=True), \
                    mock.patch.object(
                        VOICE_HOOK, "_play_pcm_voxtype_gated",
                        side_effect=AssertionError("gated player"),
                    ), \
                    mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen:
                VOICE_HOOK.speak_elevenlabs_streaming("hi", "voice", "model", "key")
            popen.assert_not_called()
            self._assert_wav(self.wav_path, b"\x05\x06\x07\x08", 24000, 1)
        finally:
            VOICE_HOOK._voiceq_reservation = None
            VOICE_HOOK._voiceq_sunk = False

    def test_reserve_rejects_noncanonical_wav_and_nondigit_id(self):
        elsewhere = os.path.join(self.root, "42.wav")
        cases = [
            {"ok": True, "id": "42", "wav": elsewhere},
            {"ok": True, "id": "42", "wav": os.path.join(self.root, "audio", "42.wav")},
            {"ok": True, "id": "42", "wav": os.path.join(self.wav_dir, "99.wav")},
            {"ok": True, "id": "42a", "wav": os.path.join(self.wav_dir, "42a.wav")},
            {"ok": True, "id": "../42", "wav": os.path.join(self.root, "42.wav")},
            {"ok": True, "id": "42", "wav": os.path.join(self.wav_dir, "042.wav")},
            {"ok": True, "id": -1, "wav": os.path.join(self.wav_dir, "-1.wav")},
        ]
        for reply in cases:
            with self.subTest(reply=reply):
                self._assert_reserve_rejected(reply)

    def test_integer_id_reserves_canonical_wav(self):
        self._enable_flag()

        def reply(req):
            if req.get("op") == "reserve":
                return {"ok": True, "id": 42, "wav": self.wav_path}
            return {"ok": True}

        server = self._server(reply)
        result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "submit"])
        self.assertEqual(server.requests[1]["id"], "42")
        self.assertEqual(server.requests[1]["wav"], self.wav_path)
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
        self.assertEqual(os.stat(self.wav_path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(self.wav_path + ".part"))
        self._assert_no_local_play(result)

    def test_sink_writes_part_via_dir_fd_then_renames(self):
        self._arm_reservation()
        opens = []
        events = []
        real_open = os.open
        real_rename = os.rename
        real_fsync = os.fsync

        def spy_open(path, flags, mode=0o777, *, dir_fd=None):
            opens.append((path, flags, mode, dir_fd))
            events.append(("open", path))
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def spy_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            events.append(("rename", src, dst, src_dir_fd, dst_dir_fd))
            return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def spy_fsync(fd):
            events.append(("fsync", fd))
            return real_fsync(fd)

        with mock.patch("os.open", side_effect=spy_open), \
                mock.patch("os.rename", side_effect=spy_rename), \
                mock.patch("os.fsync", side_effect=spy_fsync), \
                mock.patch.object(VOICE_HOOK.subprocess, "run") as run, \
                mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen:
            VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertTrue(VOICE_HOOK._voiceq_sunk)
        part = [item for item in opens if item[0] == "42.wav.part"]
        self.assertEqual(len(part), 1)
        _path, flags, mode, dir_fd = part[0]
        required = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        self.assertEqual(flags, required)
        self.assertEqual(mode & 0o777, 0o600)
        self.assertIsInstance(dir_fd, int)
        renames = [item for item in events if item[0] == "rename"]
        self.assertEqual(
            renames, [("rename", "42.wav.part", "42.wav", dir_fd, dir_fd)],
        )
        fsync_at = next(i for i, item in enumerate(events) if item[0] == "fsync")
        rename_at = next(i for i, item in enumerate(events) if item[0] == "rename")
        self.assertLess(fsync_at, rename_at)
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
        self.assertEqual(os.stat(self.wav_path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(self.wav_path + ".part"))
        dir_open = [item for item in opens if item[0] == self.wav_dir]
        self.assertEqual(len(dir_open), 1)
        dir_flags = dir_open[0][1]
        self.assertTrue(dir_flags & os.O_DIRECTORY)
        self.assertTrue(dir_flags & os.O_NOFOLLOW)
        self.assertTrue(dir_flags & os.O_CLOEXEC)
        self.assertFalse(dir_flags & os.O_WRONLY)
        self.assertFalse(dir_flags & os.O_RDWR)

    def test_existing_part_is_not_replaced(self):
        self._arm_reservation()
        part = self.wav_path + ".part"
        with open(part, "wb") as handle:
            handle.write(b"keep-me")
        with mock.patch.object(VOICE_HOOK.subprocess, "run") as run, \
                mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen:
            VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(Path(part).read_bytes(), b"keep-me")
        self.assertFalse(os.path.exists(self.wav_path))
        self.assertTrue(VOICE_HOOK._voiceq_reservation.get("failed"))
        self.assertFalse(VOICE_HOOK._voiceq_sunk)

    def test_part_symlink_is_not_followed(self):
        self._arm_reservation()
        outside = os.path.join(self.root, "outside.wav")
        os.symlink(outside, self.wav_path + ".part")
        with mock.patch.object(VOICE_HOOK.subprocess, "run") as run, \
                mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen:
            VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(os.path.exists(outside))
        self.assertTrue(os.path.islink(self.wav_path + ".part"))
        self.assertFalse(os.path.exists(self.wav_path))
        self.assertTrue(VOICE_HOOK._voiceq_reservation.get("failed"))

    def test_sink_failure_does_not_fall_through_to_a_player(self):
        for macos in (False, True):
            with self.subTest(macos=macos):
                if not os.path.isdir(self.wav_dir):
                    os.mkdir(self.wav_dir)
                self._arm_reservation()
                os.rmdir(self.wav_dir)
                with mock.patch.object(VOICE_HOOK, "IS_MACOS", macos), \
                        mock.patch.object(VOICE_HOOK.subprocess, "run") as run, \
                        mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen:
                    VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)
                run.assert_not_called()
                popen.assert_not_called()
                self.assertTrue(VOICE_HOOK._voiceq_reservation.get("failed"))
                self.assertFalse(VOICE_HOOK._voiceq_sunk)
                os.mkdir(self.wav_dir)

    def test_wav_write_fails_twice_cancels_without_paplay(self):
        self._enable_flag()
        server = self._accepting_server()
        writes = {"n": 0}

        def boom(*_args, **_kwargs):
            writes["n"] += 1
            raise OSError("disk full")

        def on_speak(_clean, _cfg, _lang=None):
            VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)

        with mock.patch.object(VOICE_HOOK, "_voiceq_write_wav", side_effect=boom):
            result = self._run(VOICE, on_speak=on_speak)
        self.assertEqual(writes["n"], 2)
        self.assertEqual(result["spoken"], ["The build finished cleanly."])
        self.assertEqual(
            [req.get("op") for req in server.requests], ["reserve", "cancel"],
        )
        self.assertEqual(server.requests[1]["id"], "42")
        self._assert_no_local_play(result)
        self.assertIn("wav write failed", self._log())
        self.assertFalse(os.path.exists(self.wav_path))

    def test_wav_write_retries_once_then_submits(self):
        self._enable_flag()
        server = self._accepting_server()
        real = VOICE_HOOK._voiceq_write_wav
        writes = {"n": 0}

        def flaky(*args, **kwargs):
            writes["n"] += 1
            if writes["n"] == 1:
                raise OSError("transient")
            return real(*args, **kwargs)

        def on_speak(_clean, _cfg, _lang=None):
            VOICE_HOOK.play_raw_pcm(PCM, RATE, CHANNELS)

        with mock.patch.object(VOICE_HOOK, "_voiceq_write_wav", side_effect=flaky):
            result = self._run(VOICE, on_speak=on_speak)
        self.assertEqual(writes["n"], 2)
        self.assertEqual(
            [req.get("op") for req in server.requests], ["reserve", "submit"],
        )
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
        self._assert_no_local_play(result)

    def test_speak_without_sink_cancels_without_local_play(self):
        self._enable_flag()
        server = self._accepting_server()
        calls = {"n": 0}

        def on_speak(_clean, _cfg, _lang=None):
            calls["n"] += 1

        result = self._run(VOICE, on_speak=on_speak)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            [req.get("op") for req in server.requests], ["reserve", "cancel"],
        )
        self._assert_no_local_play(result)
        self.assertFalse(os.path.exists(self.wav_path))
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("no audio captured", self._voiceq_lines()[0])

    def test_remote_route_is_complete_without_local_play(self):
        self._enable_flag()
        server = self._accepting_server()
        calls = {"n": 0}

        def on_speak(_clean, _cfg, _lang=None):
            calls["n"] += 1
            VOICE_HOOK._voiceq_remote_routed = True

        result = self._run(VOICE, on_speak=on_speak)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            [req.get("op") for req in server.requests], ["reserve", "cancel"],
        )
        self._assert_no_local_play(result)
        self.assertIn("remote route complete", self._voiceq_lines()[0])

    def test_submit_timeout_without_the_wav_does_not_play(self):
        self._enable_flag()

        def reply(req):
            if req.get("op") == "reserve":
                return {"ok": True, "id": "42", "wav": self.wav_path}
            if req.get("op") == "submit":
                try:
                    os.unlink(req["wav"])
                except OSError:
                    pass
                return None
            return {"ok": True}

        server = self._server(reply)
        seen = {}
        started = time.monotonic()
        with self._patch_gated(seen):
            result = self._run(VOICE, on_speak=self._play_local(server))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 3.5)
        self.assertEqual(
            [req.get("op") for req in server.requests], ["reserve", "submit"],
        )
        self.assertEqual(seen, {})
        result["run"].assert_not_called()
        result["popen"].assert_not_called()
        self.assertEqual(result["locks"], [])
        self.assertEqual(result["waits"], [])
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("daemon owns", self._voiceq_lines()[0])
        self.assertEqual(self._voiceq_private_names(), [])

    def test_submit_timeout_does_not_play_when_daemon_may_be_up(self):
        self._enable_flag()

        def reply(req):
            if req.get("op") == "reserve":
                return {"ok": True, "id": "42", "wav": self.wav_path}
            if req.get("op") == "submit":
                return None
            return {"ok": True}

        server = self._server(reply)
        seen = {}
        started = time.monotonic()
        with self._patch_gated(seen):
            result = self._run(VOICE, on_speak=self._play_local(server))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 3.0)
        self.assertLess(elapsed, 7.0)
        ops = [req.get("op") for req in server.requests]
        self.assertEqual(ops[0], "reserve")
        self.assertEqual(ops[1:-1], ["submit"] * 4)
        self.assertEqual(ops[-1], "cancel")
        self.assertEqual(seen, {})
        self._assert_no_local_play(result)
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("not playing locally", self._voiceq_lines()[0])
        self.assertFalse(os.path.exists(self.wav_path))
        self.assertEqual(self._voiceq_private_names(), [])

    def test_daemon_down_after_reserve_plays_locally_once(self):
        self._enable_flag()
        self._write_state("idle")
        os.chmod(self.queue_dir, 0o700)
        server = self._accepting_server()
        seen = {}
        ops = []
        closed = {"done": False}
        real_rpc = VOICE_HOOK._voiceq_rpc

        def spy(payload, timeout=None):
            ops.append(payload.get("op"))
            if payload.get("op") == "submit" and not closed["done"]:
                closed["done"] = True
                server.close()
            return real_rpc(payload, timeout=timeout)

        with self._patch_gated(seen), mock.patch.object(VOICE_HOOK, "_voiceq_rpc", spy):
            result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(
            ops, ["reserve", "submit", "submit", "submit", "submit", "cancel"],
        )
        self.assertEqual(seen["calls"], 1)
        self.assertEqual(seen["pcm"], PCM)
        self.assertEqual(seen["srate"], RATE)
        self.assertEqual(seen["channels"], CHANNELS)
        self.assertFalse(seen["wav_exists"])
        self.assertIsNone(seen["reservation"])
        self.assertIn(str(os.getpid()), seen["lock_text"])
        self.assertEqual(result["spoken"], ["The build finished cleanly."])
        result["run"].assert_not_called()
        result["popen"].assert_not_called()
        self.assertEqual(len(result["locks"]), 1)
        _args, kwargs = result["locks"][0]
        self.assertIs(kwargs.get("graceful_steal"), True)
        self.assertIn("playing locally", self._voiceq_lines()[0])
        self.assertFalse(os.path.exists(self.wav_path))
        self.assertEqual(self._voiceq_private_names(), [])

    def test_ok_reply_with_wrong_path_cancels_and_uses_today_path(self):
        elsewhere = os.path.join(self.root, "42.wav")
        self._assert_reserve_rejected({"ok": True, "id": "42", "wav": elsewhere})

    def test_reclaim_rename_stays_inside_incoming(self):
        self._enable_flag()
        server = self._accepting_server(on_submit=lambda _req: {"ok": False})
        opens = []
        renames = []
        links = []
        real_open = os.open
        real_rename = os.rename
        real_link = os.link

        def spy_open(path, flags, mode=0o777, *, dir_fd=None):
            opens.append((path, flags, mode, dir_fd))
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def spy_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            renames.append((src, dst, src_dir_fd, dst_dir_fd))
            return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def spy_link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True):
            links.append((src, dst, src_dir_fd, dst_dir_fd, follow_symlinks))
            return real_link(
                src, dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with mock.patch("os.open", side_effect=spy_open), \
                mock.patch("os.rename", side_effect=spy_rename), \
                mock.patch("os.link", side_effect=spy_link):
            result = self._run(VOICE, on_speak=self._play_local(server))
        self._assert_no_local_play(result)
        priv = [item for item in opens if str(item[0]).startswith(".voiceq-")]
        self.assertGreaterEqual(len(priv), 1)
        _path, flags, mode, dir_fd = priv[0]
        required = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        self.assertEqual(flags & required, required)
        self.assertEqual(mode & 0o777, 0o600)
        self.assertIsInstance(dir_fd, int)
        claim_renames = [
            item for item in renames
            if item[0] == "42.wav" and str(item[1]).startswith(".voiceq-")
        ]
        self.assertGreaterEqual(len(claim_renames), 1)
        _src, _dst, src_fd, dst_fd = claim_renames[0]
        self.assertIsInstance(src_fd, int)
        self.assertEqual(src_fd, dst_fd)
        self.assertTrue(any(
            str(src).startswith(".voiceq-") and dst == "42.wav" and follow is False
            for src, dst, _sfd, _dfd, follow in links
        ))
        self.assertFalse(any(
            os.path.dirname(os.path.join(self.root, name)) == self.root
            and name.startswith(".voiceq-")
            for name in os.listdir(self.root)
        ))
        self.assertEqual(self._voiceq_private_names(), [])

    def _assert_reserve_rejected(self, reply):
        self._enable_flag()
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)
        if os.path.exists(self.lock_path):
            os.unlink(self.lock_path)
        Path(self.log_path).write_text("", encoding="utf-8")
        VOICE_HOOK._voiceq_reservation = None
        VOICE_HOOK._voiceq_sunk = False
        VOICE_HOOK._voiceq_fail_logged = False
        VOICE_HOOK._voiceq_remote_routed = False
        server = self._server(lambda _req, reply=reply: reply)
        try:
            result = self._run(VOICE, on_speak=self._play_local(server))
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(server.requests[0].get("op"), "reserve")
            key = self._assert_key(server.requests[0])
            self.assertEqual(server.requests[1].get("op"), "cancel")
            rid = VOICE_HOOK._voiceq_parse_id(reply.get("id"))
            if rid:
                self.assertEqual(server.requests[1].get("id"), rid)
            else:
                self.assertEqual(server.requests[1].get("key"), key)
                self.assertNotIn("id", server.requests[1])
            self._assert_paplay(result)
            self.assertEqual(len(self._voiceq_lines()), 1)
            self.assertIn("reserve failed", self._voiceq_lines()[0])
            self.assertFalse(os.path.exists(self.wav_path))
        finally:
            server.close()

    def _voiceq_private_names(self):
        names = []
        for base in (self.root, self.wav_dir):
            try:
                found = os.listdir(base)
            except OSError:
                continue
            names.extend(name for name in found if name.startswith(".voiceq-"))
        return names

    def _assert_key(self, req):
        key = req.get("key")
        self.assertIsInstance(key, str)
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        return key

    def _assert_same_keys(self, reserves):
        self.assertTrue(reserves)
        key = self._assert_key(reserves[0])
        for req in reserves[1:]:
            self.assertEqual(req.get("key"), key)
        return key

    def _patch_gated(self, seen):
        def gated(pcm, srate, channels, timeout=30.0):
            seen["pcm"] = bytes(pcm)
            seen["srate"] = srate
            seen["channels"] = channels
            seen["calls"] = seen.get("calls", 0) + 1
            seen["lock_text"] = (
                Path(self.lock_path).read_text(encoding="utf-8")
                if os.path.exists(self.lock_path) else ""
            )
            seen["wav_exists"] = os.path.exists(self.wav_path)
            seen["reservation"] = VOICE_HOOK._voiceq_reservation
            seen["private"] = self._voiceq_private_names()

        return mock.patch.object(
            VOICE_HOOK, "_play_pcm_voxtype_gated", side_effect=gated,
        )

    def _arm_reservation(self):
        VOICE_HOOK._voiceq_reservation = {"id": "42", "wav": self.wav_path}
        VOICE_HOOK._voiceq_sunk = False
        VOICE_HOOK._voiceq_fail_logged = False
        VOICE_HOOK._voiceq_remote_routed = False

    def _assert_wav(self, path, pcm, rate, channels):
        raw = Path(path).read_bytes()
        self.assertTrue(raw.startswith(b"RIFF"))
        self.assertEqual(raw[8:12], b"WAVE")
        with wave.open(path, "rb") as reader:
            self.assertEqual(reader.getnchannels(), channels)
            self.assertEqual(reader.getsampwidth(), 2)
            self.assertEqual(reader.getframerate(), rate)
            self.assertEqual(reader.getcomptype(), "NONE")
            frames = reader.readframes(reader.getnframes())
        self.assertEqual(frames, pcm)
        self.assertEqual(len(frames) % (2 * channels), 0)

    def _remote_accept(self, *_args, **_kwargs):
        return VOICE_HOOK._voiceq_remote_success()

    def test_edge_remote_success_does_not_reach_the_sink(self):
        self._arm_reservation()

        class Communicate:
            def __init__(self, *_args, **_kwargs):
                pass

            async def save(self, path):
                with open(path, "wb") as handle:
                    handle.write(b"mp3")

        edge = mock.Mock()
        edge.Communicate = Communicate
        import numpy as np
        soundfile = mock.Mock()
        soundfile.read.return_value = (np.array([0.1, -0.1]), 24000)
        try:
            with mock.patch.dict(sys.modules, {"edge_tts": edge, "soundfile": soundfile}), \
                    mock.patch.object(
                        VOICE_HOOK, "send_audio_remote", side_effect=self._remote_accept,
                    ), \
                    mock.patch.object(VOICE_HOOK, "play_raw_pcm") as play:
                VOICE_HOOK.asyncio.run(VOICE_HOOK.speak_edge(
                    "hi", "en-GB-SoniaNeural", "+0%",
                    remote_target="http://127.0.0.1:9/tts",
                ))
            play.assert_not_called()
            self.assertTrue(VOICE_HOOK._voiceq_remote_routed)
            self.assertFalse(VOICE_HOOK._voiceq_sunk)
            self.assertFalse(os.path.exists(self.wav_path))
        finally:
            self._arm_reservation()

    def test_elevenlabs_remote_success_does_not_reach_the_sink(self):
        self._arm_reservation()
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = lambda chunk_size=4096: iter([b"\x01\x00"])
        requests_mod = mock.Mock()
        requests_mod.post.return_value = response
        try:
            with mock.patch.dict(sys.modules, {"requests": requests_mod}), \
                    mock.patch.object(
                        VOICE_HOOK, "send_audio_remote", side_effect=self._remote_accept,
                    ), \
                    mock.patch.object(VOICE_HOOK, "play_raw_pcm") as play:
                VOICE_HOOK.speak_elevenlabs_streaming(
                    "hi", "voice", "model", "key",
                    remote_target="http://127.0.0.1:9/tts",
                )
            play.assert_not_called()
            self.assertTrue(VOICE_HOOK._voiceq_remote_routed)
            self.assertFalse(os.path.exists(self.wav_path))
        finally:
            self._arm_reservation()

    def test_pocket_remote_success_does_not_reach_the_sink(self):
        self._arm_reservation()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(b"\x00\x01" * 600)
        try:
            with mock.patch.object(VOICE_HOOK, "_pocket_fetch", return_value=buf.getvalue()), \
                    mock.patch.object(
                        VOICE_HOOK, "send_audio_remote", side_effect=self._remote_accept,
                    ), \
                    mock.patch.object(VOICE_HOOK, "play_audio_file") as play:
                ok = VOICE_HOOK.speak_pocket(
                    "hi", "aragorn", remote_target="http://127.0.0.1:9/tts",
                    play_local=False, speed=1.0, gain_db=0, tail_ms=0,
                )
            self.assertTrue(ok)
            play.assert_not_called()
            self.assertTrue(VOICE_HOOK._voiceq_remote_routed)
            self.assertFalse(os.path.exists(self.wav_path))
        finally:
            self._arm_reservation()

    def test_play_audio_file_reaches_the_sink(self):
        """Pocket and Dutch local playback share this helper."""
        self._arm_reservation()
        import numpy as np
        data = np.array([0.25, -0.25], dtype=np.float32)
        soundfile = mock.Mock()
        soundfile.read.return_value = (data, 16000)
        try:
            with mock.patch.dict(sys.modules, {"soundfile": soundfile}):
                VOICE_HOOK.play_audio_file(os.path.join(self.root, "clip.wav"))
            self.assertTrue(VOICE_HOOK._voiceq_sunk)
            pcm = (data * 32767).astype(np.int16).tobytes()
            self._assert_wav(self.wav_path, pcm, 16000, 1)
        finally:
            self._arm_reservation()

    def test_no_audible_path_does_not_sink_or_route(self):
        self._arm_reservation()
        try:
            with mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, False),
            ):
                VOICE_HOOK.speak("hello", {"tts_engine": "edge", "tts_enabled": True})
            self.assertFalse(VOICE_HOOK._voiceq_sunk)
            self.assertFalse(VOICE_HOOK._voiceq_remote_routed)
            self.assertFalse(os.path.exists(self.wav_path))
        finally:
            self._arm_reservation()


if __name__ == "__main__":
    unittest.main()
