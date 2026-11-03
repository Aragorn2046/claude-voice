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
            "SHELBY_VOICE_QUEUE_DIR": self.queue_dir,
            "SHELBY_VOXTYPE_STATE_FILE": self.state_path,
            "CLAUDE_SESSION_TYPE": "main",
        }
        VOICE_HOOK._voiceq_reservation = None
        VOICE_HOOK._voiceq_sunk = False
        VOICE_HOOK._voiceq_fail_logged = False
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
        with mock.patch.dict(
            os.environ,
            {"SHELBY_VOICEQ_SOCKET": "", "XDG_RUNTIME_DIR": ""},
        ):
            self.assertIsNone(VOICE_HOOK._voiceq_socket_path())

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
        self.assertEqual([req.get("op") for req in server.requests], ["reserve", "submit"])
        submit = server.requests[1]
        self.assertEqual(submit["id"], "42")
        self.assertEqual(submit["wav"], self.wav_path)
        self.assertEqual(server.submit_info["exists"], True)
        self.assertGreater(server.submit_info["size"], 44)
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
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
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 3.5)
        self.assertEqual([req.get("op") for req in server.requests], ["reserve"])
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertFalse(os.path.exists(self.wav_path))

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
        self.assertLess(elapsed, 0.8)
        self.assertEqual([req.get("op") for req in server.requests], ["reserve"])
        self._assert_paplay(result)
        self.assertEqual(len(self._voiceq_lines()), 1)

    def test_malformed_reply_uses_today_path(self):
        self._enable_flag()
        replies = [
            b"not-json\n",
            b"[1, 2]\n",
            b'{"ok": true}\n',
            b'{"ok": true, "id": "1", "wav": "relative.wav"}\n',
            b'{"ok": true, "id": "", "wav": "/tmp/voiceq-empty-id.wav"}\n',
            b'{"ok": true, "id": 1, "wav": "not-absolute"}\n',
        ]
        for raw in replies:
            with self.subTest(raw=raw):
                Path(self.log_path).write_text("", encoding="utf-8")
                if os.path.exists(self.sock_path):
                    os.unlink(self.sock_path)
                if os.path.exists(self.lock_path):
                    os.unlink(self.lock_path)
                server = self._server(lambda _req, raw=raw: raw)
                try:
                    result = self._run(VOICE, on_speak=self._play_local(server))
                    self.assertEqual([req.get("op") for req in server.requests], ["reserve"])
                    self._assert_paplay(result)
                    self.assertEqual(len(self._voiceq_lines()), 1)
                    self.assertFalse(os.path.exists(self.wav_path))
                finally:
                    server.close()

    def test_submit_failure_plays_locally(self):
        self._enable_flag()

        def on_submit(_req):
            return {"ok": False}

        server = self._accepting_server(on_submit=on_submit)
        result = self._run(VOICE, on_speak=self._play_local(server))
        self.assertEqual(
            [req.get("op") for req in server.requests],
            ["reserve", "submit", "cancel"],
        )
        self.assertEqual(server.requests[1]["id"], "42")
        self.assertEqual(server.requests[1]["wav"], self.wav_path)
        self.assertEqual(server.requests[2]["id"], "42")
        self.assertGreater(server.submit_info["size"], 44)
        self._assert_wav(self.wav_path, PCM, RATE, CHANNELS)
        result["run"].assert_called_once()
        argv = result["run"].call_args.args[0]
        self.assertEqual(argv[0], "paplay")
        self.assertEqual(result["run"].call_args.kwargs.get("input"), PCM)
        result["popen"].assert_not_called()
        # Synthesis itself takes no lock. The fallback takes today's lock.
        self.assertEqual(self.during["lock_text"], "")
        self.assertEqual(len(result["locks"]), 1)
        _args, kwargs = result["locks"][0]
        self.assertEqual(kwargs.get("queue_wait_s"), 0)
        self.assertIs(kwargs.get("graceful_steal"), False)
        self.assertEqual(self.during["ops"], ["reserve"])
        self.assertEqual(len(self._voiceq_lines()), 1)
        self.assertIn("submit failed", self._voiceq_lines()[0])
        self.assertEqual(result["waits"], [])

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

    def _arm_reservation(self):
        VOICE_HOOK._voiceq_reservation = {"id": "42", "wav": self.wav_path}
        VOICE_HOOK._voiceq_sunk = False
        VOICE_HOOK._voiceq_fail_logged = False

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


if __name__ == "__main__":
    unittest.main()
