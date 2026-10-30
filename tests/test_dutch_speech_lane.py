import asyncio
import ast
import builtins
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil
import socket
import socketserver
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import numpy as np


ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import shelby_speech_policy as SPEECH_POLICY
HOOK_PATH = ROOT / "scripts" / ("voice-stop" + "-hook.py")
HOOK_SPEC = importlib.util.spec_from_file_location("voice_stop_hook", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(VOICE_HOOK)

TTS_PATH = ROOT / "scripts" / "tts.py"
TTS_SPEC = importlib.util.spec_from_file_location("shelby_tts", TTS_PATH)
TTS = importlib.util.module_from_spec(TTS_SPEC)
TTS_SPEC.loader.exec_module(TTS)

# scripts/tts.py logs to stderr only. The live hook log is voice-stop-hook.LOG_PATH.
_HOOK_LOG_DIR = None
_HOOK_LOG_PATCH = None
_HOOK_LOG_PATH = None


def setUpModule():
    global _HOOK_LOG_DIR, _HOOK_LOG_PATCH, _HOOK_LOG_PATH
    directory = tempfile.mkdtemp(prefix="claude-tts-test-")
    try:
        log_path = os.path.join(directory, "claude-tts.log")
        patch = mock.patch.object(VOICE_HOOK, "LOG_PATH", log_path)
        patch.start()
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _HOOK_LOG_DIR = directory
    _HOOK_LOG_PATCH = patch
    _HOOK_LOG_PATH = log_path


def tearDownModule():
    global _HOOK_LOG_DIR, _HOOK_LOG_PATCH
    patch = _HOOK_LOG_PATCH
    directory = _HOOK_LOG_DIR
    _HOOK_LOG_PATCH = None
    _HOOK_LOG_DIR = None
    try:
        if patch is not None:
            patch.stop()
    finally:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


def _redirect_hook_log(hook):
    hook.LOG_PATH = _HOOK_LOG_PATH


CASE_O = SPEECH_POLICY.SHELBY_SPEECH_PERSONA
DUTCH = "Dit is een Nederlandse zin die vandaag goed werkt."
ROUTE_ID = "omnivoice:tts"
ENDPOINT = "http://100.77.19.108:8934"
POCKET_ENDPOINT = "http://127.0.0.1:8933"
ENGLISH = "The translated line."
DUTCH_AUDIO = VOICE_HOOK.make_wav(b"\x00" * 1200)
BASE_HOOK_REVISION = "3cfd818"
# main() on this revision already has the VoxType queue and CASE-O. The
# presence allowlist compares against it, not BASE_HOOK_REVISION.
PRESENCE_BASE_REVISION = "7d492e8"
LANE_ONLY_HOOK_SYMBOLS = {
    "_enforce_dutch_lane_english_fallback",
    "_find_model_route_resolver",
    "_format_pocket_gain_db",
    "_is_riff_wav",
    "_is_shelby_dutch_persona",
    "_model_route_resolver_candidates",
    "_omnivoice_fetch",
    "_omnivoice_post_json",
    "_precheck_dutch_audio",
    "_resolve_dutch_lane_voice",
    "_resolve_pocket_voice",
    "bounded_dutch_timeout",
    "is_content_voice",
    "normalize_dutch_persona",
    "pocket_synthesis_timeout_s",
    "remaining_dutch_lane_budget_s",
    "resolve_dutch_speech_engine",
}
# Top-level helpers added by the VoxType speech wait. They are not Dutch-lane
# symbols; the AST lock below allows them and the choke points the gate owns
# (play_raw_pcm, the ElevenLabs Linux branch, SIGTERM, acquire_lock, and main).
# acquire_lock and main now do the pre-lock wait and the FIFO speech queue,
# so their bodies are not compared to the base.
VOXTYPE_HOOK_SYMBOLS = {
    "_LockRefused",
    "_argv0_basename",
    "_charge_wait",
    "_clear_ticket",
    "_dir_is_private",
    "_ensure_private_dir",
    "_feed_pcm",
    "_gone_or_zombie",
    "_is_our_hook",
    "_is_our_hook_linux",
    "_is_our_hook_macos",
    "_is_signallable_paplay",
    "_kill_player",
    "_lock_open_refusal_reason",
    "_log_busy_cap",
    "_maybe_heartbeat",
    "_older_ticket_ahead",
    "_parse_holder_pid",
    "_play_one_paplay",
    "_play_pcm_voxtype_gated",
    "_queue_pid_alive",
    "_read_lock_record",
    "_read_small_regular_file",
    "_register_ticket",
    "_reset_wait_budget",
    "_scan_older_ticket",
    "_shared_cap_s",
    "_status_real_uid",
    "_stop_player",
    "_voice_queue_dir",
    "_voxtype_busy_state",
    "_voxtype_pid_is_dead",
    "_voxtype_state_path",
    "_voxtype_state_text",
    "_voxtype_wait_enabled",
    "_wait_allowance_s",
    "_wait_for_voxtype_idle_status",
    "voxtype_busy",
    "voxtype_present",
    "wait_for_voxtype_idle",
}
# Lock hardening owns the new holder and opener helpers above and _open_lockfile.
VOXTYPE_TOUCHED_HOOK_SYMBOLS = {
    "_open_lockfile",
    "acquire_lock",
    "handle_sigterm",
    "main",
    "play_raw_pcm",
    "speak_elevenlabs_streaming",
}


_VALID_SOUNDFILE = ModuleType("soundfile")
_VALID_SOUNDFILE.read = lambda _source, frames=-1: (np.zeros(1200), 24000)


def add_translation_stub(home):
    script = Path(home) / "42" / "Config" / "scripts" / "tier2-routed.py"
    script.parent.mkdir(parents=True)
    script.touch()
    return script


class _LocalOmniVoiceServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def local_omnivoice_server(mode="happy", body=None, status=200):
    """Serve a deliberately small local HTTP/1.x response for transport tests."""
    requests = []
    body = DUTCH_AUDIO if body is None else body

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request = bytearray()
            self.request.settimeout(2)
            try:
                while b"\r\n\r\n" not in request:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                header_end = request.find(b"\r\n\r\n")
                headers = bytes(request[:header_end]).split(b"\r\n")
                content_length = 0
                for line in headers[1:]:
                    name, separator, value = line.partition(b":")
                    if separator and name.lower() == b"content-length":
                        content_length = int(value.strip())
                        break
                while len(request) - header_end - 4 < content_length:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                requests.append(bytes(request))

                if mode == "never":
                    time.sleep(1.2)
                    return

                code = status if mode != "non_200" else 503
                if mode == "non_riff":
                    response_body = b'{"error":"unavailable"}'
                elif mode == "oversize":
                    response_body = b"RIFF\x00\x00\x00\x00WAVE" + b"x" * len(body)
                else:
                    response_body = body
                response_header = (
                    f"HTTP/1.0 {code} Test\r\n"
                    "Content-Type: audio/wav\r\n"
                    f"Content-Length: {len(response_body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")

                if mode == "trickle_headers":
                    prefix = (
                        f"HTTP/1.0 {code} Test\r\n"
                        "Content-Type: audio/wav\r\n"
                        "X-Slow: "
                    ).encode("ascii")
                    self.request.setsockopt(
                        socket.IPPROTO_TCP,
                        socket.TCP_NODELAY,
                        1,
                    )
                    self.request.sendall(prefix)
                    for byte in b"x" * 50 + b"\r\n\r\n":
                        time.sleep(0.04)
                        self.request.sendall(bytes((byte,)))
                    self.request.sendall(response_body)
                elif mode == "trickle_body":
                    self.request.setsockopt(
                        socket.IPPROTO_TCP,
                        socket.TCP_NODELAY,
                        1,
                    )
                    self.request.sendall(response_header)
                    for offset in range(0, len(response_body), 64):
                        time.sleep(0.04)
                        self.request.sendall(response_body[offset:offset + 64])
                else:
                    self.request.sendall(response_header + response_body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Deadline tests intentionally close while the server is writing.
                return

    server = _LocalOmniVoiceServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield endpoint, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def _is_presence_hold_if(stmt):
    """The exact CASE presence return inserted into voice-stop-hook main()."""
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    if not isinstance(test, ast.Call) or len(test.args) != 2 or test.keywords:
        return False
    func = test.func
    if not isinstance(func, ast.Name) or func.id != "_presence_hold":
        return False
    if [arg.id for arg in test.args if isinstance(arg, ast.Name)] != ["cfg", "clean"]:
        return False
    if len(stmt.body) != 2:
        return False
    call_stmt, ret = stmt.body
    if not isinstance(call_stmt, ast.Expr) or not isinstance(call_stmt.value, ast.Call):
        return False
    call = call_stmt.value
    if not isinstance(call.func, ast.Name) or call.func.id != "log":
        return False
    if len(call.args) != 1 or call.keywords or not isinstance(call.args[0], ast.Constant):
        return False
    if call.args[0].value != "presence gate: Aragorn away — line held, not spoken":
        return False
    return isinstance(ret, ast.Return) and ret.value is None


def hook_config(**updates):
    cfg = {
        "source_machine": "day",
        "tts_engine": "edge",
        "dutch_speech": {"enabled": True, "timeout_s": 14},
    }
    cfg.update(updates)
    return cfg


class DutchSpeechHookTests(unittest.TestCase):
    def setUp(self):
        soundfile_patch = mock.patch.dict(
            sys.modules, {"soundfile": _VALID_SOUNDFILE}
        )
        soundfile_patch.start()
        self.addCleanup(soundfile_patch.stop)

    def test_shared_english_and_playback_functions_match_base_ast(self):
        base_path = HOOK_PATH.relative_to(ROOT).as_posix()
        try:
            result = subprocess.run(
                ["git", "show", f"{BASE_HOOK_REVISION}:{base_path}"],
                cwd=ROOT,
                capture_output=True,
                check=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"git history unavailable for AST identity check: {exc}")

        def named_nodes(source):
            tree = ast.parse(source)
            return {
                node.name: node for node in tree.body
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                )
            }

        base_nodes = named_nodes(result.stdout)
        live_nodes = named_nodes(HOOK_PATH.read_text())
        base_names, live_names = set(base_nodes), set(live_nodes)
        self.assertEqual(set(), base_names - live_names, "base symbols were removed or renamed")

        added_names = live_names - base_names
        # CASE presence hold (gauntlet PRS) adds one helper. It is not a Dutch-lane symbol.
        self.assertEqual(
            LANE_ONLY_HOOK_SYMBOLS | VOXTYPE_HOOK_SYMBOLS | {"_presence_hold"},
            added_names,
        )
        # Skip main and the symbols VoxType rewrote. Both sides already do.
        for name in sorted(base_names - {"speak"} - VOXTYPE_TOUCHED_HOOK_SYMBOLS):
            live_node = live_nodes[name]
            with self.subTest(function=name):
                if name in {"_wav_tempo", "speak_pocket", "_pocket_fetch"}:
                    # pocket_gain_db joins pocket_speed in this ffmpeg pass.
                    # speak_pocket forwards gain_db and retries jarvis once,
                    # only after the server answered.
                    # _pocket_fetch reports that answer (HTTP status or a
                    # short body) versus a transport failure.
                    # _wav_tempo applies gain and retries once without alimiter.
                    continue
                self.assertEqual(
                    ast.dump(base_nodes[name], include_attributes=False),
                    ast.dump(live_node, include_attributes=False),
                )
        try:
            onto = subprocess.run(
                ["git", "show", f"{PRESENCE_BASE_REVISION}:{base_path}"],
                cwd=ROOT,
                capture_output=True,
                check=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"git history unavailable for presence allowlist: {exc}")
        self._assert_main_only_adds_presence_gate(
            named_nodes(onto.stdout)["main"],
            live_nodes["main"],
        )

    def _assert_main_only_adds_presence_gate(self, base_main, live_main):
        """main() may add only the two presence-gate returns; the rest matches baseline.

        The first hold stays the pre-lock return. The post-lock re-check may sit
        in any nested statement list. Exactly one of each is removed, then the
        remainder must match the base AST.
        """

        def is_post_lock_recheck(stmt):
            """``if _presence_hold(cfg, clean): log; release_lock(lock); return``."""
            if not isinstance(stmt, ast.If) or stmt.orelse:
                return False
            test = stmt.test
            if not isinstance(test, ast.Call) or len(test.args) != 2 or test.keywords:
                return False
            func = test.func
            if not isinstance(func, ast.Name) or func.id != "_presence_hold":
                return False
            if [arg.id for arg in test.args if isinstance(arg, ast.Name)] != ["cfg", "clean"]:
                return False
            if len(stmt.body) != 3:
                return False
            log_stmt, release_stmt, ret = stmt.body
            if not isinstance(log_stmt, ast.Expr) or not isinstance(log_stmt.value, ast.Call):
                return False
            log_call = log_stmt.value
            if (
                not isinstance(log_call.func, ast.Name)
                or log_call.func.id != "log"
                or len(log_call.args) != 1
                or log_call.keywords
                or not isinstance(log_call.args[0], ast.Constant)
                or log_call.args[0].value != "presence gate: held after lock wait"
            ):
                return False
            if not isinstance(release_stmt, ast.Expr) or not isinstance(release_stmt.value, ast.Call):
                return False
            release_call = release_stmt.value
            if (
                not isinstance(release_call.func, ast.Name)
                or release_call.func.id != "release_lock"
                or len(release_call.args) != 1
                or release_call.keywords
                or not isinstance(release_call.args[0], ast.Name)
                or release_call.args[0].id != "lock"
            ):
                return False
            return isinstance(ret, ast.Return) and ret.value is None

        def direct_blocks(node):
            if isinstance(node, (
                ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                ast.With, ast.AsyncWith, ast.ExceptHandler,
            )):
                return [node.body]
            if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                return [node.body, node.orelse]
            if isinstance(node, (ast.Try, ast.TryStar)):
                return [
                    node.body,
                    node.orelse,
                    node.finalbody,
                    *[handler.body for handler in node.handlers],
                ]
            if isinstance(node, ast.Match):
                return [case.body for case in node.cases]
            return []

        blocks = []

        def walk(node):
            for block in direct_blocks(node):
                blocks.append(block)
                for stmt in block:
                    walk(stmt)

        walk(live_main)
        presence = []
        recheck = []
        for block in blocks:
            for stmt in block:
                if _is_presence_hold_if(stmt):
                    presence.append(stmt)
                elif is_post_lock_recheck(stmt):
                    recheck.append(stmt)
        self.assertEqual(1, len(presence))
        self.assertEqual(1, len(recheck))
        allowed = {id(presence[0]), id(recheck[0])}
        snapshots = [(block, list(block)) for block in blocks]
        try:
            for block, _original in snapshots:
                block[:] = [stmt for stmt in block if id(stmt) not in allowed]
            self.assertEqual(
                ast.dump(base_main, include_attributes=False),
                ast.dump(live_main, include_attributes=False),
            )
        finally:
            for block, original in snapshots:
                block[:] = original

    def test_hook_loads_without_shared_policy_module_and_pinned_paths_speak(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated_path = Path(directory) / ("voice-stop" + "-hook.py")
            shutil.copyfile(HOOK_PATH, isolated_path)
            spec = importlib.util.spec_from_file_location("isolated_voice_hook", isolated_path)
            isolated_hook = importlib.util.module_from_spec(spec)
            original_import = builtins.__import__
            blocked_imports = []

            def import_without_shared_policy(name, *args, **kwargs):
                if name == "shelby_speech_policy":
                    blocked_imports.append(name)
                    raise ModuleNotFoundError(name)
                return original_import(name, *args, **kwargs)

            with mock.patch("builtins.__import__", side_effect=import_without_shared_policy):
                spec.loader.exec_module(isolated_hook)
                _redirect_hook_log(isolated_hook)

        edge = mock.AsyncMock()
        with mock.patch.object(
            isolated_hook, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            isolated_hook, "enforce_english_speech", side_effect=lambda text, *_args, **_kwargs: ENGLISH
        ), mock.patch.object(
            isolated_hook, "speak_edge", new=edge
        ), mock.patch.object(isolated_hook, "log"):
            isolated_hook.speak("An English line.", hook_config(), lang_hint="en")
            isolated_hook.speak(
                DUTCH, hook_config(dutch_speech={"enabled": False}), lang_hint="nl"
            )

        self.assertEqual([], blocked_imports)
        self.assertEqual(2, edge.await_count)
        self.assertEqual([ENGLISH, ENGLISH], [call.args[0] for call in edge.await_args_list])

    def test_hook_loads_and_falls_back_when_optional_omnivoice_helper_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated_path = Path(directory) / ("voice-stop" + "-hook.py")
            shutil.copyfile(HOOK_PATH, isolated_path)
            spec = importlib.util.spec_from_file_location("isolated_hook_no_omnivoice", isolated_path)
            isolated_hook = importlib.util.module_from_spec(spec)
            original_import = builtins.__import__
            blocked_imports = []

            def import_without_transport(name, *args, **kwargs):
                if name == "omnivoice_http":
                    blocked_imports.append(name)
                    raise ModuleNotFoundError(name)
                return original_import(name, *args, **kwargs)

            with mock.patch("builtins.__import__", side_effect=import_without_transport):
                spec.loader.exec_module(isolated_hook)
                _redirect_hook_log(isolated_hook)

        edge = mock.AsyncMock()
        with mock.patch.object(
            isolated_hook, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            isolated_hook, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            isolated_hook, "_translate_to_english", return_value=ENGLISH
        ), mock.patch.object(
            isolated_hook, "speak_edge", new=edge
        ), mock.patch.object(isolated_hook, "log"):
            isolated_hook.speak(DUTCH, hook_config(), lang_hint="nl")

        self.assertEqual(["omnivoice_http"], blocked_imports)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_lane_delivery_success_is_one_utterance(self):
        remote_target = "http://receiver/tts"
        audio = DUTCH_AUDIO
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=audio
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=True
        ) as send, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
        send.assert_called_once_with(
            audio, remote_target, require_off_lan=False, fallback_target=None
        )
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any("TTS (omnivoice/nl/remote):" in call.args[0]
                            for call in log.call_args_list))

    def test_local_success_returning_none_is_one_utterance(self):
        played_audio = []

        def play_file(path):
            played_audio.append(Path(path).read_bytes())

        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=play_file
        ) as play, mock.patch.object(
            VOICE_HOOK, "_translate_to_english"
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        play.assert_called_once()
        self.assertEqual([DUTCH_AUDIO], played_audio)
        translate.assert_not_called()
        edge.assert_not_awaited()

    def test_enabled_lane_delivers_original_dutch_for_declared_and_undeclared_text(self):
        remote_target = "http://receiver/tts"
        audio = DUTCH_AUDIO
        for hint, expects_drift in (("nl", False), (None, True)):
            with self.subTest(lang_hint=hint), tempfile.TemporaryDirectory() as home:
                with mock.patch.dict(
                    VOICE_HOOK.os.environ,
                    {"HOME": home, "SHELBY_POCKET_TIMEOUT": "27"},
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=audio
                ) as fetch, mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", return_value=True
                ) as send, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english"
                ) as translate, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint=hint)

                fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
                send.assert_called_once_with(
                    audio, remote_target, require_off_lan=False, fallback_target=None
                )
                translate.assert_not_called()
                edge.assert_not_awaited()
                sentinel = Path(home) / ".shelby" / "voice-lang-drift.json"
                self.assertEqual(expects_drift, sentinel.exists())

    def test_enabled_lane_records_undeclared_drift_before_delivery_exception(self):
        remote_target = "http://receiver/tts"
        audio = DUTCH_AUDIO
        for hint, expects_sentinel in (("nl", False), (None, True)):
            with self.subTest(lang_hint=hint), tempfile.TemporaryDirectory() as home:
                sentinel = Path(home) / ".shelby" / "voice-lang-drift.json"
                observed_before_failure = []

                def fail_delivery(*_args, **_kwargs):
                    observed_before_failure.append(sentinel.exists())
                    raise RuntimeError("receiver failed")

                with mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=audio
                ) as fetch, mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", side_effect=fail_delivery
                ) as send, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english"
                ) as translate, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint=hint)

                fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
                send.assert_called_once()
                self.assertEqual([expects_sentinel], observed_before_failure)
                self.assertEqual(expects_sentinel, sentinel.exists())
                translate.assert_not_called()
                edge.assert_not_awaited()

    def test_lane_synthesis_failure_falls_back_and_records_only_undeclared_drift(self):
        for hint, expects_sentinel in (("nl", False), (None, True)):
            with self.subTest(lang_hint=hint), tempfile.TemporaryDirectory() as home:
                edge = mock.AsyncMock()
                sentinel = Path(home) / ".shelby" / "voice-lang-drift.json"
                with mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=None
                ) as fetch, mock.patch.object(
                    VOICE_HOOK, "send_audio_remote"
                ) as send, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
                ) as translate, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(
                    os, "replace", wraps=os.replace
                ) as replace, mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint=hint)

                fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
                send.assert_not_called()
                translate.assert_called_once_with(DUTCH)
                edge.assert_awaited_once()
                self.assertEqual(ENGLISH, edge.await_args.args[0])
                self.assertEqual(expects_sentinel, sentinel.exists())
                self.assertEqual(1 if expects_sentinel else 0, replace.call_count)

    def test_omnivoice_socket_failures_are_bounded_with_hook_fallback_and_cli_failure(self):
        failure_modes = (
            "trickle_headers", "trickle_body", "never", "oversize",
            "non_200", "non_riff",
        )
        for mode in failure_modes:
            with self.subTest(mode=mode), local_omnivoice_server(mode) as (
                endpoint, requests
            ), tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as output_dir:
                edge = mock.AsyncMock()
                translate = mock.Mock(return_value=ENGLISH)
                hook_started = time.monotonic()
                hook_patches = [
                    mock.patch.dict(VOICE_HOOK.DUTCH_ENGINE_ENDPOINTS, {ROUTE_ID: endpoint}),
                    mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}),
                    mock.patch.object(VOICE_HOOK, "bounded_dutch_timeout", return_value=0.35),
                    mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)),
                    mock.patch.object(VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID),
                    mock.patch.object(VOICE_HOOK, "_translate_to_english", translate),
                    mock.patch.object(VOICE_HOOK, "speak_edge", new=edge),
                    mock.patch.object(VOICE_HOOK, "log"),
                ]
                if mode == "oversize":
                    hook_patches.append(
                        mock.patch.object(VOICE_HOOK, "MAX_OMNIVOICE_RESPONSE_BYTES", 1024)
                    )
                with contextlib.ExitStack() as stack:
                    for patcher in hook_patches:
                        stack.enter_context(patcher)
                    VOICE_HOOK.speak(
                        DUTCH,
                        hook_config(dutch_speech={"enabled": True, "timeout_s": 1}),
                        lang_hint="nl",
                    )
                hook_elapsed = time.monotonic() - hook_started

                self.assertLess(hook_elapsed, 0.85)
                translate.assert_called_once_with(DUTCH)
                edge.assert_awaited_once()
                self.assertEqual(ENGLISH, edge.await_args.args[0])

                cfg = {
                    "tts_voice_pocket_en": CASE_O,
                    "tts_voice_pocket_content": "aragorn",
                    "dutch_speech": {"enabled": True, "timeout_s": 1},
                }
                output = str(Path(output_dir) / "failed.wav")
                cli_started = time.monotonic()
                cli_patches = [
                    mock.patch.dict(TTS.DUTCH_ENGINE_ENDPOINTS, {ROUTE_ID: endpoint}),
                    mock.patch.object(TTS, "load_config", return_value=cfg),
                    mock.patch.object(TTS, "bounded_dutch_timeout", return_value=0.35),
                    mock.patch.object(TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID),
                ]
                if mode == "oversize":
                    cli_patches.append(
                        mock.patch.object(TTS, "MAX_OMNIVOICE_RESPONSE_BYTES", 1024)
                    )
                with contextlib.ExitStack() as stack, contextlib.redirect_stderr(io.StringIO()):
                    for patcher in cli_patches:
                        stack.enter_context(patcher)
                    self.assertIsNone(TTS.tts_omnivoice(DUTCH, output_path=output))
                self.assertLess(time.monotonic() - cli_started, 0.85)
                self.assertFalse(Path(output).exists())
                self.assertEqual(2, len(requests))

    def test_omnivoice_socket_happy_path_delivers_one_dutch_utterance(self):
        remote_target = "http://receiver/tts"
        with local_omnivoice_server() as (endpoint, requests):
            edge = mock.AsyncMock()
            with mock.patch.dict(
                VOICE_HOOK.DUTCH_ENGINE_ENDPOINTS, {ROUTE_ID: endpoint}
            ), mock.patch.object(
                VOICE_HOOK, "bounded_dutch_timeout", return_value=1
            ), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                VOICE_HOOK, "_wav_tempo", side_effect=lambda audio, _speed, _gain=0: audio
            ), mock.patch.object(
                VOICE_HOOK, "_pad_wav_tail", side_effect=lambda audio, **_kwargs: audio
            ), mock.patch.object(
                VOICE_HOOK, "send_audio_remote", return_value=True
            ) as send, mock.patch.object(
                VOICE_HOOK, "enforce_english_speech"
            ) as enforce, mock.patch.object(
                VOICE_HOOK, "speak_edge", new=edge
            ), mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

            send.assert_called_once_with(
                DUTCH_AUDIO, remote_target, require_off_lan=False, fallback_target=None
            )
            enforce.assert_not_called()
            edge.assert_not_awaited()
            self.assertEqual(1, len(requests))
            request_line, _, request_tail = requests[0].partition(b"\r\n")
            request_headers, _, request_body = request_tail.partition(b"\r\n\r\n")
            self.assertEqual(b"POST /tts HTTP/1.0", request_line)
            self.assertIn(b"Host: 127.0.0.1:", request_headers)
            self.assertIn(b"Content-Type: application/json", request_headers)
            self.assertIn(
                f"Content-Length: {len(request_body)}".encode(), request_headers
            )
            self.assertIn(b"Connection: close", request_headers)
            self.assertEqual(DUTCH, json.loads(request_body)["text"])

    def test_post_synthesis_pre_delivery_failures_fall_back_to_english_once(self):
        cases = (
            ("tempo transform raises", DUTCH_AUDIO, RuntimeError("tempo failed"), None),
            ("tail transform raises", DUTCH_AUDIO, None, RuntimeError("tail failed")),
            ("empty audio", b"", None, None),
            ("non-RIFF audio", b"not wav" * 200, None, None),
        )
        for name, audio, tempo_error, tail_error in cases:
            for hint, expects_sentinel in (("nl", False), (None, True)):
                with self.subTest(failure=name, lang_hint=hint), tempfile.TemporaryDirectory() as home:
                    edge = mock.AsyncMock()
                    sentinel = Path(home) / ".shelby" / "voice-lang-drift.json"
                    with mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}), mock.patch.object(
                        VOICE_HOOK, "find_audible_path", return_value=(None, True)
                    ), mock.patch.object(
                        VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                    ), mock.patch.object(
                        VOICE_HOOK, "_omnivoice_fetch", return_value=audio
                    ) as fetch, mock.patch.object(
                        VOICE_HOOK, "_wav_tempo",
                        side_effect=tempo_error or (lambda wav, _speed, _gain=0: wav),
                    ), mock.patch.object(
                        VOICE_HOOK, "_pad_wav_tail",
                        side_effect=tail_error or (lambda wav, **_kwargs: wav),
                    ), mock.patch.object(
                        VOICE_HOOK, "send_audio_remote"
                    ) as send, mock.patch.object(
                        VOICE_HOOK, "play_audio_file", return_value=None
                    ) as play, mock.patch.object(
                        VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
                    ) as translate, mock.patch.object(
                        VOICE_HOOK, "speak_edge", new=edge
                    ), mock.patch.object(
                        os, "replace", wraps=os.replace
                    ) as replace, mock.patch.object(VOICE_HOOK, "log"):
                        VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint=hint)

                    fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
                    send.assert_not_called()
                    play.assert_not_called()
                    translate.assert_called_once_with(DUTCH)
                    edge.assert_awaited_once()
                    self.assertEqual(ENGLISH, edge.await_args.args[0])
                    self.assertEqual(expects_sentinel, sentinel.exists())
                    self.assertEqual(1 if expects_sentinel else 0, replace.call_count)

    def test_local_tempfile_staging_failures_fall_back_once(self):
        remote_target = "http://receiver/tts"
        for route in ("local", "remote_failure"):
            for failure in ("create", "write", "close"):
                with self.subTest(route=route, failure=failure), tempfile.TemporaryDirectory() as directory:
                    edge = mock.AsyncMock()
                    translate = mock.Mock(return_value=ENGLISH)
                    partial_path = Path(directory) / "partial.wav"
                    staged_file = mock.MagicMock()
                    staged_file.name = str(partial_path)
                    staged_file.__enter__.return_value = staged_file
                    staged_file.__exit__.return_value = False
                    if failure in ("write", "close"):
                        partial_path.touch()
                    if failure == "write":
                        staged_file.write.side_effect = OSError("disk full")
                    elif failure == "close":
                        staged_file.__exit__.side_effect = OSError("close failed")

                    tempfile_patch = mock.patch.object(
                        VOICE_HOOK.tempfile,
                        "NamedTemporaryFile",
                        side_effect=PermissionError("tmp unwritable")
                        if failure == "create" else None,
                        return_value=None if failure == "create" else staged_file,
                    )
                    with mock.patch.object(
                        VOICE_HOOK,
                        "find_audible_path",
                        return_value=(remote_target, True)
                        if route == "remote_failure" else (None, True),
                    ), mock.patch.object(
                        VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                    ), mock.patch.object(
                        VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                    ) as fetch, mock.patch.object(
                        VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                    ), mock.patch.object(
                        VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                    ), tempfile_patch, mock.patch.object(
                        VOICE_HOOK, "send_audio_remote", return_value=False
                    ) as send, mock.patch.object(
                        VOICE_HOOK, "play_audio_file"
                    ) as play, mock.patch.object(
                        shutil, "which", return_value="/usr/bin/player"
                    ), mock.patch.object(
                        VOICE_HOOK, "_translate_to_english", new=translate
                    ), mock.patch.object(
                        VOICE_HOOK, "speak_edge", new=edge
                    ), mock.patch.object(VOICE_HOOK, "log") as log:
                        VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

                    fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
                    if route == "remote_failure":
                        send.assert_called_once_with(
                            DUTCH_AUDIO, remote_target,
                            require_off_lan=False, fallback_target=None,
                        )
                    else:
                        send.assert_not_called()
                    play.assert_not_called()
                    translate.assert_called_once_with(DUTCH)
                    edge.assert_awaited_once()
                    self.assertEqual(ENGLISH, edge.await_args.args[0])
                    self.assertFalse(partial_path.exists())
                    self.assertTrue(any(
                        "Dutch local pre-check failed" in call.args[0]
                        for call in log.call_args_list
                    ))

    def test_wav_decode_and_soundfile_import_precheck_failures_fall_back_once(self):
        import soundfile

        original_import = builtins.__import__
        for failure in ("decode", "missing soundfile"):
            with self.subTest(failure=failure):
                edge = mock.AsyncMock()
                translate = mock.Mock(return_value=ENGLISH)
                send = mock.Mock()
                play = mock.Mock(return_value=None)
                log = mock.Mock()
                patchers = [
                    mock.patch.object(
                        VOICE_HOOK, "find_audible_path", return_value=(None, True)
                    ),
                    mock.patch.object(
                        VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                    ),
                    mock.patch.object(
                        VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                    ),
                    mock.patch.object(
                        VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                    ),
                    mock.patch.object(
                        VOICE_HOOK, "_pad_wav_tail",
                        side_effect=lambda wav, **_kwargs: wav,
                    ),
                    mock.patch.object(VOICE_HOOK, "send_audio_remote", new=send),
                    mock.patch.object(VOICE_HOOK, "play_audio_file", new=play),
                    mock.patch.object(
                        VOICE_HOOK, "_translate_to_english", translate
                    ),
                    mock.patch.object(VOICE_HOOK, "speak_edge", new=edge),
                    mock.patch.object(VOICE_HOOK, "log", new=log),
                ]
                if failure == "decode":
                    patchers.append(
                        mock.patch.object(
                            soundfile, "read", side_effect=ValueError("bad WAV frames")
                        )
                    )
                else:
                    def import_without_soundfile(name, *args, **kwargs):
                        if name == "soundfile":
                            raise ModuleNotFoundError(name)
                        return original_import(name, *args, **kwargs)

                    patchers.append(
                        mock.patch("builtins.__import__", side_effect=import_without_soundfile)
                    )

                with contextlib.ExitStack() as stack:
                    for patcher in patchers:
                        stack.enter_context(patcher)
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

                send.assert_not_called()
                play.assert_not_called()
                translate.assert_called_once_with(DUTCH)
                edge.assert_awaited_once()
                self.assertEqual(ENGLISH, edge.await_args.args[0])
                self.assertTrue(any(
                    "WAV pre-check failed before delivery" in call.args[0]
                    for call in log.call_args_list
                ))

    def test_remote_false_local_precheck_failure_falls_back_once(self):
        remote_target = "http://receiver/tts"
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            shutil, "which", return_value=None
        ) as which, mock.patch.object(
            VOICE_HOOK, "play_audio_file", return_value=None
        ) as play, mock.patch.object(
            VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        send.assert_called_once()
        which.assert_called_once_with("paplay")
        play.assert_not_called()
        translate.assert_called_once_with(DUTCH)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertTrue(any(
            "Dutch local pre-check failed" in call.args[0]
            for call in log.call_args_list
        ))

    def test_local_playback_exception_after_precheck_stays_silent(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=OSError("player exited after start")
        ) as play, mock.patch.object(
            VOICE_HOOK, "_translate_to_english"
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        play.assert_called_once()
        translate.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any(
            "no English fallback" in call.args[0]
            for call in log.call_args_list
        ))

    def test_known_remote_preplayback_failure_falls_back_to_english_once_without_local_output(self):
        remote_target = "http://receiver/tts"
        audio = DUTCH_AUDIO
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=audio
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            VOICE_HOOK, "_enforce_dutch_lane_english_fallback", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once()
        send.assert_called_once_with(
            audio, remote_target, require_off_lan=False, fallback_target=None
        )
        enforce.assert_called_once_with(DUTCH, "nl")
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertTrue(any("remote delivery failed before playback" in call.args[0]
                            for call in log.call_args_list))

    def test_remote_delivery_false_replays_same_dutch_audio_locally_once_when_enabled(self):
        remote_target = "http://receiver/tts"
        played_audio = []

        def read_audio(path):
            played_audio.append(Path(path).read_bytes())
            return None

        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=read_audio
        ) as play, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
        send.assert_called_once_with(
            DUTCH_AUDIO, remote_target, require_off_lan=False, fallback_target=None
        )
        play.assert_called_once()
        self.assertEqual([DUTCH_AUDIO], played_audio)
        enforce.assert_not_called()
        edge.assert_not_awaited()

    def test_remote_false_local_replay_exception_is_ambiguous_and_stays_silent(self):
        remote_target = "http://receiver/tts"
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=OSError("playback interrupted")
        ) as play, mock.patch.object(
            VOICE_HOOK, "_translate_to_english"
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        send.assert_called_once()
        play.assert_called_once()
        translate.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any(
            "Dutch local fallback not confirmed" in call.args[0]
            and "staying silent" in call.args[0]
            for call in log.call_args_list
        ))

    def test_remote_and_local_delivery_failure_stays_silent_without_translation(self):
        remote_target = "http://receiver/tts"
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=OSError("player failed")
        ) as play, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        send.assert_called_once()
        play.assert_called_once()
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any("Dutch local fallback not confirmed" in call.args[0]
                            for call in log.call_args_list))

    def test_real_remote_send_connect_refusal_uses_lane_local_or_english_fallback(self):
        import requests

        remote_target = "http://receiver/tts"
        for play_local in (True, False):
            with self.subTest(play_local=play_local):
                edge = mock.AsyncMock()
                played = []

                def play_file(path):
                    played.append(Path(path).read_bytes())
                    return None

                with mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(remote_target, play_local)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    shutil, "which", return_value="/usr/bin/paplay"
                ), mock.patch(
                    "requests.post",
                    side_effect=requests.exceptions.ConnectionError("connect refused"),
                ) as post, mock.patch.object(
                    VOICE_HOOK, "play_audio_file", side_effect=play_file
                ) as play, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
                ) as translate, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log") as log:
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

                post.assert_called_once()
                self.assertIn("/reserve", post.call_args.args[0])
                if play_local:
                    play.assert_called_once()
                    self.assertEqual([DUTCH_AUDIO], played)
                    translate.assert_not_called()
                    edge.assert_not_awaited()
                else:
                    play.assert_not_called()
                    translate.assert_called_once_with(DUTCH)
                    edge.assert_awaited_once()
                    self.assertEqual(ENGLISH, edge.await_args.args[0])
                self.assertTrue(any(
                    "Receiver reservation failed" in call.args[0]
                    for call in log.call_args_list
                ))

    def test_real_remote_send_timeout_after_upload_is_ambiguous_and_never_english(self):
        import requests

        remote_target = "http://receiver/tts"
        for play_local in (False, True):
            with self.subTest(play_local=play_local):
                reserved = mock.Mock(status_code=201)
                edge = mock.AsyncMock()
                translate = mock.Mock()
                with mock.patch.object(
                    VOICE_HOOK, "find_audible_path",
                    return_value=(remote_target, play_local),
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail",
                    side_effect=lambda wav, **_kwargs: wav,
                ), mock.patch(
                    "requests.post",
                    side_effect=[reserved, requests.exceptions.ReadTimeout(
                        "response lost after upload"
                    )],
                ) as post, mock.patch(
                    "requests.get",
                    side_effect=requests.exceptions.ReadTimeout("status unavailable"),
                ), mock.patch(
                    "requests.delete",
                    side_effect=requests.exceptions.ConnectionError(
                        "cancel unavailable"
                    ),
                ), mock.patch.object(
                    VOICE_HOOK.time, "sleep"
                ), mock.patch.object(
                    VOICE_HOOK, "play_audio_file", return_value=None
                ) as play, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", new=translate
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log") as log:
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

                self.assertEqual(2, post.call_count)
                self.assertEqual(remote_target, post.call_args_list[1].args[0])
                self.assertEqual(DUTCH_AUDIO, post.call_args_list[1].kwargs["data"])
                play.assert_not_called()
                translate.assert_not_called()
                edge.assert_not_awaited()
                self.assertTrue(any(
                    "Remote delivery outcome ambiguous" in call.args[0]
                    for call in log.call_args_list
                ))
                self.assertTrue(any(
                    "remains ambiguous — suppressing duplicate local play" in call.args[0]
                    for call in log.call_args_list
                ))

    def test_remote_success_obeys_single_playback_contract_when_local_is_available(self):
        remote_target = "http://receiver/tts"
        for pocket_play_both, expected_local_plays in ((False, 0), (True, 1)):
            with self.subTest(pocket_play_both=pocket_play_both):
                played_audio = []
                edge = mock.AsyncMock()
                translate = mock.Mock()

                def play_file(path):
                    played_audio.append(Path(path).read_bytes())

                with mock.patch.object(
                    VOICE_HOOK, "find_audible_path",
                    return_value=(remote_target, True),
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail",
                    side_effect=lambda wav, **_kwargs: wav,
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", return_value=True
                ) as send, mock.patch.object(
                    shutil, "which", return_value="/usr/bin/paplay"
                ), mock.patch.object(
                    VOICE_HOOK, "play_audio_file", side_effect=play_file
                ) as play, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", new=translate
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(
                        DUTCH,
                        hook_config(pocket_play_both=pocket_play_both),
                        lang_hint="nl",
                    )

                send.assert_called_once_with(
                    DUTCH_AUDIO, remote_target,
                    require_off_lan=False, fallback_target=None,
                )
                self.assertEqual(expected_local_plays, play.call_count)
                self.assertEqual(
                    [DUTCH_AUDIO] if expected_local_plays else [], played_audio
                )
                translate.assert_not_called()
                edge.assert_not_awaited()

    def test_real_remote_upload_rejections_use_one_safe_fallback(self):
        statuses = (409, 429, 500)
        remote_target = "http://receiver/tts"
        for status in statuses:
            for play_local in (True, False):
                with self.subTest(status=status, play_local=play_local):
                    import requests

                    edge = mock.AsyncMock()
                    played = []

                    def play_file(path):
                        played.append(Path(path).read_bytes())
                        return None

                    responses = [
                        mock.Mock(status_code=201),
                        mock.Mock(status_code=status),
                    ]
                    with mock.patch.object(
                        VOICE_HOOK, "find_audible_path",
                        return_value=(remote_target, play_local),
                    ), mock.patch.object(
                        VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                    ), mock.patch.object(
                        VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                    ), mock.patch.object(
                        VOICE_HOOK, "_wav_tempo",
                        side_effect=lambda wav, _speed, _gain=0: wav,
                    ), mock.patch.object(
                        VOICE_HOOK, "_pad_wav_tail",
                        side_effect=lambda wav, **_kwargs: wav,
                    ), mock.patch.object(
                        shutil, "which", return_value="/usr/bin/paplay"
                    ), mock.patch(
                        "requests.post", side_effect=responses
                    ) as post, mock.patch.object(
                        VOICE_HOOK, "_cancel_reserved_delivery"
                    ) as cancel, mock.patch.object(
                        VOICE_HOOK, "play_audio_file", side_effect=play_file
                    ) as play, mock.patch.object(
                        VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
                    ) as translate, mock.patch.object(
                        VOICE_HOOK, "speak_edge", new=edge
                    ), mock.patch.object(VOICE_HOOK, "log"):
                        VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

                    self.assertEqual(2, post.call_count)
                    self.assertIn("/reserve", post.call_args_list[0].args[0])
                    self.assertEqual(remote_target, post.call_args_list[1].args[0])
                    self.assertEqual(DUTCH_AUDIO, post.call_args_list[1].kwargs["data"])
                    cancel.assert_called_once()
                    if play_local:
                        play.assert_called_once()
                        self.assertEqual([DUTCH_AUDIO], played)
                        translate.assert_not_called()
                        edge.assert_not_awaited()
                    else:
                        play.assert_not_called()
                        self.assertEqual([], played)
                        translate.assert_called_once_with(DUTCH)
                        edge.assert_awaited_once()
                        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_real_remote_success_can_also_play_locally_when_requested(self):
        remote_target = "http://receiver/tts"
        edge = mock.AsyncMock()
        played = []

        def play_file(path):
            played.append(Path(path).read_bytes())
            return None

        responses = [mock.Mock(status_code=201), mock.Mock(status_code=200)]
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch(
            "requests.post", side_effect=responses
        ) as post, mock.patch.object(
            VOICE_HOOK, "_poll_delivery_status", return_value=True
        ) as poll, mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=play_file
        ) as play, mock.patch.object(
            VOICE_HOOK, "_translate_to_english"
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(
                DUTCH,
                hook_config(pocket_play_both=True),
                lang_hint="nl",
            )

        self.assertEqual(2, post.call_count)
        self.assertIn("/reserve", post.call_args_list[0].args[0])
        self.assertEqual(remote_target, post.call_args_list[1].args[0])
        poll.assert_called_once()
        play.assert_called_once()
        self.assertEqual([DUTCH_AUDIO], played)
        translate.assert_not_called()
        edge.assert_not_awaited()

    def test_lane_pre_delivery_failure_falls_back_once_for_edge_and_pocket(self):
        remote_target = "http://receiver/tts"

        async def edge_delivery(_text, _voice, _speed, **kwargs):
            VOICE_HOOK.send_audio_remote(
                b"English audio", kwargs["remote_target"],
                require_off_lan=kwargs["remote_requires_off_lan"],
                fallback_target=kwargs["remote_fallback_target"],
            )

        for engine in ("edge", "pocket"):
            with self.subTest(engine=engine):
                fetches = []
                edge = mock.AsyncMock(side_effect=edge_delivery)

                def dutch_fetch(*args):
                    fetches.append(("omnivoice", args))
                    return None

                def pocket_fetch(*args):
                    fetches.append(("pocket", args))
                    return DUTCH_AUDIO

                cfg = hook_config(
                    tts_engine=engine,
                    pocket_tts_url=POCKET_ENDPOINT,
                )
                with mock.patch.dict(
                    VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", side_effect=dutch_fetch
                ), mock.patch.object(
                    VOICE_HOOK, "_pocket_fetch", side_effect=pocket_fetch
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", return_value=True
                ) as send, mock.patch.object(
                    VOICE_HOOK, "_enforce_dutch_lane_english_fallback", return_value=ENGLISH
                ) as enforce, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

                expected_fetches = [
                    ("omnivoice", (DUTCH, CASE_O, ENDPOINT, 14)),
                ]
                if engine == "pocket":
                    expected_fetches.append(
                        ("pocket", (ENGLISH, CASE_O, POCKET_ENDPOINT, 27))
                    )
                self.assertEqual(expected_fetches, fetches)
                enforce.assert_called_once_with(DUTCH, "nl")
                send.assert_called_once()
                if engine == "edge":
                    edge.assert_awaited_once()
                    self.assertEqual(ENGLISH, edge.await_args.args[0])
                else:
                    edge.assert_not_awaited()

    def test_missing_local_player_is_preplayback_and_falls_back_once(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", True
        ), mock.patch.object(
            shutil, "which", return_value=None
        ) as which, mock.patch.object(
            VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        which.assert_called_once_with("afplay")
        translate.assert_called_once_with(DUTCH)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertTrue(any(
            "Dutch local pre-check failed" in call.args[0]
            for call in log.call_args_list
        ))

    def test_local_playback_exception_does_not_fallback_after_synthesis(self):
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            shutil, "which", return_value="/usr/bin/paplay"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=OSError("player failed")
        ), mock.patch.object(
            VOICE_HOOK, "_enforce_dutch_lane_english_fallback", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any(
            call.args[0] == "TTS (omnivoice/nl): delivery not confirmed; no English fallback (avoid double speech)"
            for call in log.call_args_list
        ))

    def test_disabled_and_absent_config_match_baseline_traces(self):
        base_path = "scripts/voice-stop-hook.py"
        result = subprocess.run(
            ["git", "show", f"{BASE_HOOK_REVISION}:{base_path}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
        base_tree = ast.parse(result.stdout)
        baseline_speak_node = next(
            node for node in base_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "speak"
        )
        baseline_namespace = VOICE_HOOK.__dict__.copy()
        exec(
            compile(
                ast.Module(body=[baseline_speak_node], type_ignores=[]),
                base_path,
                "exec",
            ),
            baseline_namespace,
        )
        baseline_speak = baseline_namespace["speak"]

        remote_target = "http://receiver/tts"
        origin_target = "http://origin/tts"
        english_text = "This is an English sentence."
        config_cases = (
            ("absent", None),
            ("disabled", {"enabled": False, "timeout_s": 14}),
        )
        route_cases = {
            "remote": (remote_target, False, {}),
            "local": (None, True, {}),
            "remote_and_local": (remote_target, True, {}),
            "off_lan": (remote_target, False, {
                "off_lan_target": remote_target,
                "origin_target": origin_target,
            }),
        }

        def run_trace(speak_impl, namespace, dutch_config, engine, route,
                      text, hint, voice_config=None):
            route_target, route_local, presence_policy = route_cases[route]
            cfg = hook_config(
                tts_engine=engine,
                pocket_tts_url=POCKET_ENDPOINT,
                pocket_speed=1.2,
                source_machine="day",
            )
            if presence_policy:
                cfg["dusk_presence_routing"] = presence_policy
            if voice_config:
                cfg.update(voice_config)
            if dutch_config is None:
                cfg.pop("dutch_speech")
            else:
                cfg["dutch_speech"] = dutch_config

            events = []

            def find_path_spy(observed_cfg):
                normalized_cfg = dict(observed_cfg)
                normalized_cfg.pop("dutch_speech", None)
                events.append(("find_audible_path", (normalized_cfg,), {}))
                return route_target, route_local

            def detect_language_spy(spoken):
                events.append(("detect_language", (spoken,), {}))
                return "nl" if spoken == DUTCH else "en"

            def enforce_spy(spoken, lang_hint=None):
                events.append(("enforce_english_speech", (spoken, lang_hint), {}))
                return ENGLISH if spoken == DUTCH else spoken

            def speak_pocket_spy(*args, **kwargs):
                events.append(("speak_pocket", args, kwargs))
                return False

            async def speak_edge_spy(*args, **kwargs):
                events.append(("speak_edge", args, kwargs))

            resolver = mock.Mock()
            replacements = {
                "find_audible_path": find_path_spy,
                "detect_language": detect_language_spy,
                "resolve_dutch_speech_engine": resolver,
                "enforce_english_speech": enforce_spy,
                "speak_pocket": speak_pocket_spy,
                "speak_edge": speak_edge_spy,
                "log": lambda _message: None,
            }
            with mock.patch.dict(namespace, replacements), mock.patch.dict(
                VOICE_HOOK.os.environ, {}, clear=True
            ):
                speak_impl(text, cfg, lang_hint=hint)
            return events, resolver

        def shelby_persona_of(trace):
            return [args[1] for name, args, _kwargs in trace if name == "speak_pocket"]

        def without_shelby_persona(trace):
            rewritten = []
            for name, args, kwargs in trace:
                if name == "speak_pocket":
                    args = (args[0], "<shelby-persona>", *args[2:])
                    # Baseline speak() has no loudness argument. Gain is covered
                    # by the pocket_gain_db tests; drop it so the rest stays aligned.
                    kwargs = {key: value for key, value in kwargs.items() if key != "gain_db"}
                rewritten.append((name, args, kwargs))
            return rewritten

        for engine in ("pocket", "edge"):
            for route in route_cases:
                for text, hint in (
                    (english_text, "en"),
                    (DUTCH, "nl"),
                    (DUTCH, None),  # undeclared Dutch still follows the English pin
                ):
                    traces = {}
                    for config_name, dutch_config in config_cases:
                        with self.subTest(
                            engine=engine, route=route, text=text,
                            lang_hint=hint, config=config_name,
                        ):
                            baseline, baseline_resolver = run_trace(
                                baseline_speak, baseline_namespace, dutch_config,
                                engine, route, text, hint,
                            )
                            candidate, candidate_resolver = run_trace(
                                VOICE_HOOK.speak, VOICE_HOOK.__dict__, dutch_config,
                                engine, route, text, hint,
                            )
                            baseline_resolver.assert_not_called()
                            candidate_resolver.assert_not_called()
                            if engine == "pocket":
                                # Baseline still forces JARVIS. CASE-O is the default now.
                                self.assertEqual(["jarvis"], shelby_persona_of(baseline))
                                self.assertEqual([CASE_O], shelby_persona_of(candidate))
                            self.assertEqual(
                                without_shelby_persona(baseline),
                                without_shelby_persona(candidate),
                            )
                            self.assertEqual(
                                ["speak_pocket", "speak_edge"] if engine == "pocket"
                                else ["speak_edge"],
                                [event[0] for event in candidate
                                 if event[0].startswith("speak_")],
                            )
                            traces[config_name] = (baseline, candidate)

                    self.assertEqual(traces["absent"][0], traces["disabled"][0])
                    self.assertEqual(traces["absent"][1], traces["disabled"][1])

        # Content-prefix refusal is the other intentional difference from the
        # baseline. The historical speak() passed a configured content voice through.
        content_voice_config = {
            "source_machine": "other",
            "tts_voice_pocket_en": "aragorn-ss",
            "tts_voice_pocket_content": "aragorn",
        }
        baseline, baseline_resolver = run_trace(
            baseline_speak, baseline_namespace, None, "pocket", "local",
            english_text, "en", content_voice_config,
        )
        candidate, candidate_resolver = run_trace(
            VOICE_HOOK.speak, VOICE_HOOK.__dict__, None, "pocket", "local",
            english_text, "en", content_voice_config,
        )
        baseline_resolver.assert_not_called()
        candidate_resolver.assert_not_called()

        def pocket_voice(trace):
            return next(event[1][1] for event in trace if event[0] == "speak_pocket")

        def normalize_content_voice_exception(trace):
            return [
                (
                    name,
                    (args[0], "<approved-content-voice-hardening>", *args[2:]),
                    {key: value for key, value in kwargs.items() if key != "gain_db"},
                )
                if name == "speak_pocket" else (name, args, kwargs)
                for name, args, kwargs in trace
            ]

        self.assertEqual("aragorn-ss", pocket_voice(baseline))
        self.assertEqual(CASE_O, pocket_voice(candidate))
        self.assertEqual(
            normalize_content_voice_exception(baseline),
            normalize_content_voice_exception(candidate),
        )

    def test_dutch_synthesis_timeout_is_clamped_and_fallback_room_is_reserved(self):
        cfg = hook_config(dutch_speech={"enabled": True, "timeout_s": 45})
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=None
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_enforce_dutch_lane_english_fallback", return_value=ENGLISH
        ), mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")
        self.assertEqual(15, fetch.call_args.args[3])

    def test_speak_tracks_elapsed_resolver_and_synthesis_budget(self):
        cases = (
            ("resolver leaves bounded synthesis time", 8.0, 10.0, True),
            ("resolver exhausts fallback reserve", 50.0, None, False),
        )
        for name, resolver_elapsed, expected_timeout, expect_fetch in cases:
            with self.subTest(case=name):
                clock_value = [0.0]
                fetches = []
                edge = mock.AsyncMock()

                def monotonic():
                    return clock_value[0]

                def resolve():
                    clock_value[0] += resolver_elapsed
                    return ROUTE_ID

                def fetch(text, voice, endpoint, timeout):
                    fetches.append((text, voice, endpoint, timeout))
                    clock_value[0] += timeout
                    return None

                clock = SimpleNamespace(
                    monotonic=monotonic,
                    time=time.time,
                    strftime=time.strftime,
                )
                translate = mock.Mock(return_value=ENGLISH)
                with mock.patch.dict(
                    VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
                ), mock.patch.object(
                    VOICE_HOOK, "time", clock
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", side_effect=resolve
                ) as resolver, mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", side_effect=fetch
                ) as synthesize, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", translate
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(
                        DUTCH,
                        hook_config(dutch_speech={"enabled": True, "timeout_s": 14}),
                        lang_hint="nl",
                    )

                resolver.assert_called_once()
                translate.assert_called_once_with(DUTCH)
                edge.assert_awaited_once()
                self.assertEqual(ENGLISH, edge.await_args.args[0])
                if expect_fetch:
                    synthesize.assert_called_once()
                    self.assertEqual(
                        [(DUTCH, CASE_O, ENDPOINT, expected_timeout)], fetches
                    )
                    self.assertEqual(resolver_elapsed + expected_timeout, clock_value[0])
                else:
                    synthesize.assert_not_called()
                    self.assertEqual([], fetches)
                    self.assertEqual(resolver_elapsed, clock_value[0])

    def test_resolver_failure_falls_back_before_synthesis(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=None
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch"
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_enforce_dutch_lane_english_fallback", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl")
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_content_voice_override_is_refused_for_dutch_lane(self):
        for override in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            with tempfile.TemporaryDirectory() as home:
                cfg = hook_config(
                    source_machine="custom",
                    tts_voice_pocket_en="eva",
                    tts_voice_pocket_content="aragorn",
                )
                played_audio = []

                def play_file(path):
                    played_audio.append(Path(path).read_bytes())

                with self.subTest(override=override), mock.patch.dict(
                    VOICE_HOOK.os.environ,
                    {"HOME": home, "SHELBY_TTS_POCKET_VOICE": override},
                    clear=True,
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                ) as fetch, mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    shutil, "which", return_value="/usr/bin/paplay"
                ), mock.patch.object(
                    VOICE_HOOK, "play_audio_file", side_effect=play_file
                ) as play, mock.patch.object(
                    VOICE_HOOK, "_translate_to_english"
                ) as translate, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
                    VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

                fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
                play.assert_called_once()
                self.assertEqual([DUTCH_AUDIO], played_audio)
                translate.assert_not_called()
                edge.assert_not_awaited()
                self.assertTrue(any("Refused Pocket persona" in c.args[0]
                                    for c in log.call_args_list))

    def test_configured_content_prefix_is_refused_on_dutch_lane(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = hook_config(
                source_machine="custom",
                tts_voice_pocket_en="eva",
                tts_voice_pocket_content="VoiceClone",
            )
            played_audio = []

            def play_file(path):
                played_audio.append(Path(path).read_bytes())

            with mock.patch.dict(
                VOICE_HOOK.os.environ,
                {"HOME": home, "SHELBY_TTS_POCKET_VOICE": " voiceclone-nl "},
                clear=True,
            ), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
            ) as fetch, mock.patch.object(
                VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav
            ), mock.patch.object(
                VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
            ), mock.patch.object(
                shutil, "which", return_value="/usr/bin/paplay"
            ), mock.patch.object(
                VOICE_HOOK, "play_audio_file", side_effect=play_file
            ) as play, mock.patch.object(
                VOICE_HOOK, "_translate_to_english"
            ) as translate, mock.patch.object(
                VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
            ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

            fetch.assert_called_once_with(DUTCH, CASE_O, ENDPOINT, 14)
            play.assert_called_once()
            self.assertEqual([DUTCH_AUDIO], played_audio)
            translate.assert_not_called()
            edge.assert_not_awaited()

    def test_dutch_lane_accepts_exact_allowlist_after_normalization(self):
        cfg = hook_config(source_machine="custom", tts_voice_pocket_en="jarvis")
        cases = (
            ("case-o", "case-o"),
            ("CASE-O ", "case-o"),
            ("jarvis", "jarvis"),
            ("JARVIS ", "jarvis"),
            ("jarvis-app-dynamic", "jarvis-app-dynamic"),
            ("jarvis-app-focus", "jarvis-app-focus"),
            ("jarvis-app-lively", "jarvis-app-lively"),
            ("jarvis-studio-v2", "jarvis-studio-v2"),
            ("eva", "eva"),
            ("codex", "codex"),
        )
        for requested, expected in cases:
            with self.subTest(requested=requested), mock.patch.dict(
                VOICE_HOOK.os.environ, {"SHELBY_TTS_POCKET_VOICE": requested}
            ):
                self.assertEqual(expected, VOICE_HOOK._resolve_dutch_lane_voice(cfg))
                self.assertTrue(SPEECH_POLICY.is_shelby_pocket_persona(requested, cfg))

    def test_dutch_lane_replaces_unknown_and_content_personas_with_case_o(self):
        cfg = hook_config(
            source_machine="custom",
            tts_voice_pocket_en="jarvis",
            tts_voice_pocket_content="VoiceClone",
        )
        for requested in (
            "me", "voices/aragorn.wav", "hf://voices/private", "aragorn2",
            "voiceclone-nl", "arag\u00f8rn", "eva-studio", "codex-custom",
            "jarvis/../aragorn", "jarvis-aragorn", "aragorn",
            "JARVIS/../x", "jarvis:hf://x", "JARVIS-VoiceClone",
        ):
            with self.subTest(requested=requested), mock.patch.dict(
                VOICE_HOOK.os.environ, {"SHELBY_TTS_POCKET_VOICE": requested}
            ):
                self.assertEqual(CASE_O, VOICE_HOOK._resolve_dutch_lane_voice(cfg))
                self.assertFalse(SPEECH_POLICY.is_shelby_pocket_persona(requested, cfg))

    def test_hook_and_shared_policy_constants_match(self):
        for name in (
            "OVERALL_DUTCH_BUDGET_S",
            "DUTCH_RESOLVER_TIMEOUT_S",
            "ENGLISH_PIN_TRANSLATION_TIMEOUT_S",
            "DEFAULT_POCKET_TIMEOUT_S",
            "MAX_DUTCH_TIMEOUT_S",
            "DEFAULT_DUTCH_TIMEOUT_S",
            "MIN_DUTCH_TIMEOUT_S",
            "MIN_DUTCH_LANE_BUDGET_S",
            "SHELBY_SPEECH_PERSONA",
            "SHELBY_DUTCH_PERSONAS",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(SPEECH_POLICY, name), getattr(VOICE_HOOK, name))
        with mock.patch.dict(VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}):
            for config in (None, {}, {"timeout_s": 4}, {"timeout_s": 60},
                           {"timeout_s": "bad"}):
                for elapsed in (0, 20, 45):
                    with self.subTest(config=config, elapsed=elapsed):
                        self.assertEqual(
                            SPEECH_POLICY.bounded_dutch_timeout(config, elapsed),
                            VOICE_HOOK.bounded_dutch_timeout(config, elapsed),
                        )

    def test_authoritative_machines_without_config_use_case_o(self):
        for source in ("day", "dawn", "dusk"):
            cfg = hook_config(source_machine=source)
            self.assertNotIn("tts_voice_pocket_en", cfg)
            with self.subTest(source=source), mock.patch.dict(
                VOICE_HOOK.os.environ, {}, clear=True
            ):
                self.assertEqual(CASE_O, VOICE_HOOK._resolve_pocket_voice(cfg))
                self.assertEqual(CASE_O, VOICE_HOOK._resolve_dutch_lane_voice(cfg))

    def test_day_config_jarvis_switches_back_to_jarvis(self):
        for source in ("day", "dawn", "dusk"):
            cfg = hook_config(source_machine=source, tts_voice_pocket_en="jarvis")
            with self.subTest(source=source), mock.patch.dict(
                VOICE_HOOK.os.environ, {}, clear=True
            ):
                self.assertEqual("jarvis", VOICE_HOOK._resolve_pocket_voice(cfg))
                self.assertEqual("jarvis", VOICE_HOOK._resolve_dutch_lane_voice(cfg))

    def test_authoritative_content_config_never_speaks_as_aragorn(self):
        cases = (
            {"tts_voice_pocket_en": "aragorn"},
            {"tts_voice_pocket_en": "aragorn-ss"},
            {
                "tts_voice_pocket_en": "voiceclone-nl",
                "tts_voice_pocket_content": "VoiceClone",
            },
        )
        for updates in cases:
            cfg = hook_config(source_machine="day", **updates)
            with self.subTest(voice=updates["tts_voice_pocket_en"]), mock.patch.dict(
                VOICE_HOOK.os.environ, {}, clear=True
            ):
                for resolver in (
                    VOICE_HOOK._resolve_pocket_voice,
                    VOICE_HOOK._resolve_dutch_lane_voice,
                ):
                    resolved = resolver(cfg)
                    self.assertEqual(CASE_O, resolved)
                    self.assertFalse(str(resolved).startswith("aragorn"))

    def test_authoritative_garbage_config_uses_case_o(self):
        for garbage in ("foo", "", "arag\u00f8rn"):
            cfg = hook_config(source_machine="day", tts_voice_pocket_en=garbage)
            with self.subTest(garbage=garbage), mock.patch.dict(
                VOICE_HOOK.os.environ, {}, clear=True
            ):
                self.assertEqual(CASE_O, VOICE_HOOK._resolve_pocket_voice(cfg))
                self.assertEqual(CASE_O, VOICE_HOOK._resolve_dutch_lane_voice(cfg))

    def test_env_override_cannot_beat_authoritative_persona_on_day(self):
        cases = (
            ({}, "jarvis-studio-v2", CASE_O),
            ({}, "eva", CASE_O),
            ({}, "aragorn", CASE_O),
            ({"tts_voice_pocket_en": "jarvis"}, "case-o", "jarvis"),
            ({"tts_voice_pocket_en": "jarvis"}, "eva", "jarvis"),
            ({"tts_voice_pocket_en": "case-o"}, "jarvis", CASE_O),
        )
        for updates, override, expected in cases:
            cfg = hook_config(source_machine="day", **updates)
            with self.subTest(override=override, config=updates), mock.patch.dict(
                VOICE_HOOK.os.environ, {"SHELBY_TTS_POCKET_VOICE": override}, clear=True
            ):
                self.assertEqual(expected, VOICE_HOOK._resolve_pocket_voice(cfg))
                self.assertEqual(expected, VOICE_HOOK._resolve_dutch_lane_voice(cfg))

    def test_pocket_and_dutch_resolvers_agree_on_day_and_case(self):
        rows = (
            ("day", {}, None, CASE_O),
            ("day", {"tts_voice_pocket_en": "foo"}, None, CASE_O),
            ("day", {"tts_voice_pocket_en": " JARVIS "}, None, "jarvis"),
            ("day", {"tts_voice_pocket_en": "jarvis"}, "eva", "jarvis"),
            ("day", {"tts_voice_pocket_en": "jarvis"}, "foo", "jarvis"),
            ("day", {}, "jarvis", CASE_O),
            ("day", {"tts_voice_pocket_en": "aragorn"}, "eva", CASE_O),
            ("day", {"tts_voice_pocket_en": "jarvis/../aragorn"}, None, CASE_O),
            ("case", {"tts_voice_pocket_en": "foo"}, None, CASE_O),
            ("case", {"tts_voice_pocket_en": " JARVIS "}, None, "jarvis"),
            ("case", {"tts_voice_pocket_en": "case-o"}, "jarvis", "jarvis"),
            ("case", {"tts_voice_pocket_en": "jarvis"}, "eva", "eva"),
            ("case", {"tts_voice_pocket_en": "jarvis"}, "foo", CASE_O),
            ("case", {"tts_voice_pocket_en": "jarvis"}, " JARVIS ", "jarvis"),
            ("case", {"tts_voice_pocket_en": "aragorn"}, None, CASE_O),
            ("case", {"tts_voice_pocket_en": "Aragorn-SS"}, None, CASE_O),
            ("case", {"tts_voice_pocket_en": "jarvis/../aragorn"}, None, CASE_O),
            ("case", {}, "foo", CASE_O),
            ("case", {}, " JARVIS ", "jarvis"),
            ("CASE", {"tts_voice_pocket_en": "eva"}, None, "eva"),
        )
        for source, updates, override, expected in rows:
            cfg = hook_config(source_machine=source, **updates)
            env = {} if override is None else {"SHELBY_TTS_POCKET_VOICE": override}
            with self.subTest(source=source, config=updates, override=override), mock.patch.dict(
                VOICE_HOOK.os.environ, env, clear=True
            ), mock.patch.object(VOICE_HOOK, "log"):
                self.assertEqual(expected, VOICE_HOOK._resolve_pocket_voice(cfg))
                self.assertEqual(expected, VOICE_HOOK._resolve_dutch_lane_voice(cfg))

    def test_case_refuses_foo_in_one_line_and_normalizes_jarvis(self):
        refused = hook_config(source_machine="case", tts_voice_pocket_en="foo")
        with mock.patch.dict(VOICE_HOOK.os.environ, {}, clear=True), mock.patch.object(
            VOICE_HOOK, "log"
        ) as log:
            self.assertEqual(CASE_O, VOICE_HOOK._resolve_pocket_voice(refused))
            self.assertEqual(1, log.call_count)
            self.assertNotIn("\n", log.call_args.args[0])
            self.assertIn("Refused Pocket persona", log.call_args.args[0])
            self.assertIn("foo", log.call_args.args[0])
            self.assertIn(CASE_O, log.call_args.args[0])
            log.reset_mock()
            self.assertEqual(CASE_O, VOICE_HOOK._resolve_dutch_lane_voice(refused))
            self.assertEqual(1, log.call_count)
            self.assertNotIn("\n", log.call_args.args[0])

        normalized = hook_config(source_machine="case", tts_voice_pocket_en=" JARVIS ")
        with mock.patch.dict(VOICE_HOOK.os.environ, {}, clear=True), mock.patch.object(
            VOICE_HOOK, "log"
        ) as log:
            self.assertEqual("jarvis", VOICE_HOOK._resolve_pocket_voice(normalized))
            self.assertEqual("jarvis", VOICE_HOOK._resolve_dutch_lane_voice(normalized))
            self.assertFalse(any("Refused" in call.args[0] for call in log.call_args_list))

    def test_case_o_is_a_shelby_pocket_persona(self):
        self.assertEqual("case-o", SPEECH_POLICY.SHELBY_SPEECH_PERSONA)
        self.assertEqual("case-o", VOICE_HOOK.SHELBY_SPEECH_PERSONA)
        self.assertIn("case-o", SPEECH_POLICY.SHELBY_DUTCH_PERSONAS)
        self.assertTrue(SPEECH_POLICY.is_shelby_pocket_persona("case-o"))
        self.assertTrue(SPEECH_POLICY.is_shelby_pocket_persona(" CASE-O "))
        self.assertTrue(VOICE_HOOK._is_shelby_dutch_persona("case-o", {}))

    def test_content_voice_config_is_refused_on_english_pocket_branch(self):
        for voice in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            with tempfile.TemporaryDirectory() as home:
                cfg = hook_config(
                    source_machine="custom",
                    tts_engine="pocket",
                    tts_voice_pocket_en=voice,
                    pocket_tts_url=POCKET_ENDPOINT,
                    dutch_speech={"enabled": False},
                )
                with self.subTest(voice=voice), mock.patch.dict(
                    VOICE_HOOK.os.environ, {"HOME": home}, clear=True
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine"
                ) as resolver, mock.patch.object(
                    VOICE_HOOK, "enforce_english_speech", return_value="English line."
                ), mock.patch.object(
                    VOICE_HOOK, "speak_pocket", return_value=True
                ) as pocket, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak("This is an English line.", cfg, lang_hint="en")

                resolver.assert_not_called()
                pocket.assert_called_once_with(
                    "English line.", CASE_O, remote_target=None,
                    play_local=True, base_url=POCKET_ENDPOINT, fallback_local=True,
                    tail_ms=1000, remote_requires_off_lan=False,
                    remote_fallback_target=None, speed=1.0, gain_db=0,
                )
                edge.assert_not_awaited()



    def test_hook_resolver_uses_exact_safe_subprocess_contract(self):
        completed = subprocess.CompletedProcess(
            ["resolver"], 0, stdout=f"{ROUTE_ID}\n", stderr=""
        )
        with mock.patch.object(
            VOICE_HOOK, "_find_model_route_resolver", return_value="/resolver"
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(ROUTE_ID, VOICE_HOOK.resolve_dutch_speech_engine())

        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        for option, value in (("--consumer", "shelby-audio"), ("--modality", "speech-nl")):
            self.assertEqual(value, argv[argv.index(option) + 1])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(VOICE_HOOK.DUTCH_RESOLVER_TIMEOUT_S, run.call_args.kwargs["timeout"])

    def test_hook_resolver_outcomes_and_unmapped_routes(self):
        cases = (
            ("timeout", subprocess.TimeoutExpired(["resolver"], 3), None),
            ("missing executable", FileNotFoundError("resolver"), None),
            ("nonzero", subprocess.CompletedProcess([], 1, "", "failed"), None),
            ("empty", subprocess.CompletedProcess([], 0, "", ""), None),
            ("disabled rung", subprocess.CompletedProcess([], 0, "disabled rung\n", ""), None),
            ("pocket route", subprocess.CompletedProcess([], 0, "pocket:tts\n", ""), "pocket:tts"),
            ("pocket-tts route", subprocess.CompletedProcess([], 0, "pocket-tts:tts\n", ""), "pocket-tts:tts"),
        )
        for name, outcome, expected in cases:
            def run_result(*_args, **_kwargs):
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            with self.subTest(outcome=name), mock.patch.object(
                VOICE_HOOK, "_find_model_route_resolver", return_value="/resolver"
            ), mock.patch.object(
                VOICE_HOOK.subprocess, "run", side_effect=run_result
            ):
                self.assertEqual(expected, VOICE_HOOK.resolve_dutch_speech_engine())

        with mock.patch.object(
            VOICE_HOOK, "_find_model_route_resolver", return_value=None
        ), mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
            self.assertIsNone(VOICE_HOOK.resolve_dutch_speech_engine())
        run.assert_not_called()

    def test_resolver_rejections_fall_back_to_one_english_delivery(self):
        cases = (
            ("resolver missing", None, None, 0),
            ("timeout", "/resolver", subprocess.TimeoutExpired(["resolver"], 3), 1),
            ("missing executable", "/resolver", FileNotFoundError("resolver"), 1),
            ("nonzero", "/resolver", subprocess.CompletedProcess([], 1, "", "failed"), 1),
            ("empty", "/resolver", subprocess.CompletedProcess([], 0, "", ""), 1),
            ("disabled rung", "/resolver", subprocess.CompletedProcess([], 0, "disabled rung\n", ""), 1),
            ("pocket route", "/resolver", subprocess.CompletedProcess([], 0, "pocket:tts\n", ""), 1),
        )
        for name, resolver_path, outcome, expected_resolver_calls in cases:
            for hint, expects_sentinel in (("nl", False), (None, True)):
                with self.subTest(outcome=name, lang_hint=hint), tempfile.TemporaryDirectory() as home:
                    add_translation_stub(home)
                    resolver_calls = []
                    translation_calls = []

                    def run(argv, **kwargs):
                        if "--consumer" in argv:
                            resolver_calls.append((argv, kwargs))
                            if isinstance(outcome, BaseException):
                                raise outcome
                            return outcome
                        translation_calls.append((argv, kwargs))
                        return subprocess.CompletedProcess(argv, 0, ENGLISH, "")

                    edge = mock.AsyncMock()
                    sentinel = Path(home) / ".shelby" / "voice-lang-drift.json"
                    with mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}), mock.patch.object(
                        VOICE_HOOK, "_find_model_route_resolver", return_value=resolver_path
                    ), mock.patch.object(
                        VOICE_HOOK.subprocess, "run", side_effect=run
                    ), mock.patch.object(
                        VOICE_HOOK, "find_audible_path", return_value=(None, True)
                    ), mock.patch.object(
                        VOICE_HOOK, "_omnivoice_fetch"
                    ) as fetch, mock.patch.object(
                        VOICE_HOOK, "speak_edge", new=edge
                    ), mock.patch.object(
                        os, "replace", wraps=os.replace
                    ) as replace, mock.patch.object(VOICE_HOOK, "log"):
                        VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint=hint)

                    self.assertEqual(expected_resolver_calls, len(resolver_calls))
                    self.assertEqual(1, len(translation_calls))
                    self.assertIn("tier2-fast", translation_calls[0][0])
                    self.assertEqual(
                        VOICE_HOOK.ENGLISH_PIN_TRANSLATION_TIMEOUT_S,
                        translation_calls[0][1]["timeout"],
                    )
                    fetch.assert_not_called()
                    edge.assert_awaited_once()
                    self.assertEqual(ENGLISH, edge.await_args.args[0])
                    self.assertEqual(expects_sentinel, sentinel.exists())
                    self.assertEqual(1 if expects_sentinel else 0, replace.call_count)

    def test_english_text_skips_enabled_dutch_resolver(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "_find_model_route_resolver"
        ) as resolver_path, mock.patch.object(
            VOICE_HOOK.subprocess, "run"
        ) as run, mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(
                "This is an English line.", hook_config(), lang_hint="en"
            )

        resolver_path.assert_not_called()
        run.assert_not_called()
        edge.assert_awaited_once()
        self.assertEqual("This is an English line.", edge.await_args.args[0])

    def test_pre_delivery_failures_translate_once_and_keep_fallback_budget(self):
        cases = (
            ("resolver timeout", subprocess.TimeoutExpired(["resolver"], 3), None),
            ("synthesis timeout", ROUTE_ID, TimeoutError("read timed out")),
            ("synthesis transport failure", ROUTE_ID, OSError("connection refused")),
        )
        for name, resolver_outcome, fetch_error in cases:
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as home:
                add_translation_stub(home)
                resolver_calls = []
                translation_calls = []
                fetch_calls = []

                def run(argv, **kwargs):
                    if "--consumer" in argv:
                        resolver_calls.append((argv, kwargs))
                        if isinstance(resolver_outcome, BaseException):
                            raise resolver_outcome
                        return subprocess.CompletedProcess(
                            argv, 0, f"{ROUTE_ID}\n", ""
                        )
                    translation_calls.append((argv, kwargs))
                    return subprocess.CompletedProcess(argv, 0, ENGLISH, "")

                def fetch(text, voice, endpoint, timeout):
                    fetch_calls.append((text, voice, endpoint, timeout))
                    if fetch_error is not None:
                        raise fetch_error
                    return None

                edge = mock.AsyncMock()
                with mock.patch.dict(
                    VOICE_HOOK.os.environ,
                    {"HOME": home, "SHELBY_POCKET_TIMEOUT": "27"},
                ), mock.patch.object(
                    VOICE_HOOK, "_find_model_route_resolver", return_value="/resolver"
                ), mock.patch.object(
                    VOICE_HOOK.subprocess, "run", side_effect=run
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", side_effect=fetch
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(
                        DUTCH,
                        hook_config(dutch_speech={"enabled": True, "timeout_s": 45}),
                        lang_hint="nl",
                    )

                self.assertEqual(1, len(translation_calls))
                self.assertEqual(
                    VOICE_HOOK.ENGLISH_PIN_TRANSLATION_TIMEOUT_S,
                    translation_calls[0][1]["timeout"],
                )
                edge.assert_awaited_once()
                self.assertEqual(ENGLISH, edge.await_args.args[0])
                self.assertEqual(1, len(resolver_calls))
                if fetch_error is None:
                    self.assertEqual([], fetch_calls)
                else:
                    self.assertEqual(1, len(fetch_calls))
                    self.assertLessEqual(
                        fetch_calls[0][3], VOICE_HOOK.MAX_DUTCH_TIMEOUT_S
                    )
                self.assertFalse(
                    (Path(home) / ".shelby" / "voice-lang-drift.json").exists()
                )

    def test_disabled_path_writes_sentinel_with_real_drift_recorder(self):
        cfg = hook_config(dutch_speech={"enabled": False, "timeout_s": 14})
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine"
            ) as resolver, mock.patch.object(
                VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
            ), mock.patch.object(
                VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
            ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak(DUTCH, cfg)

            resolver.assert_not_called()
            edge.assert_awaited_once()
            sentinel = Path(home) / ".shelby" / "voice-lang-drift.json"
            payload = json.loads(sentinel.read_text())
            self.assertEqual("dutch", payload["kind"])
            self.assertEqual(DUTCH[:160], payload["sample"])

    def test_disabled_lane_keeps_current_drift_recording(self):
        cfg = hook_config(dutch_speech={"enabled": False, "timeout_s": 14})
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(VOICE_HOOK, "resolve_dutch_speech_engine") as resolver, mock.patch.object(
            VOICE_HOOK, "_record_lang_drift"
        ) as record_drift, mock.patch.object(
            VOICE_HOOK, "_translate_to_english", return_value="Pinned English."
        ), mock.patch.object(VOICE_HOOK, "speak_edge", new=edge), mock.patch.object(
            VOICE_HOOK, "log"
        ):
            VOICE_HOOK.speak(DUTCH, cfg)

        resolver.assert_not_called()
        record_drift.assert_called_once_with(DUTCH, None)
        self.assertEqual("Pinned English.", edge.await_args.args[0])


class DutchSpeechCliTests(unittest.TestCase):
    def cli_config(self):
        return {
            "tts_voice_pocket_en": "jarvis",
            "tts_voice_pocket_codex": "jarvis",
            "tts_voice_pocket_content": "aragorn",
            "dutch_speech": {"enabled": False, "timeout_s": 11},
        }

    def test_cli_resolver_uses_same_catalog_route_and_timeout(self):
        completed = mock.Mock(returncode=0, stdout="omnivoice:tts\n", stderr="")
        with mock.patch.object(
            TTS, "_find_model_route_resolver", return_value="/resolver"
        ), mock.patch.object(TTS.subprocess, "run", return_value=completed) as run:
            self.assertEqual(ROUTE_ID, TTS.resolve_dutch_speech_engine())
        argv = run.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual("shelby-audio", argv[argv.index("--consumer") + 1])
        self.assertEqual("speech-nl", argv[argv.index("--modality") + 1])
        self.assertEqual(TTS.DUTCH_RESOLVER_TIMEOUT_S, run.call_args.kwargs["timeout"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_cli_resolver_failures_and_disabled_rungs(self):
        cases = (
            ("timeout", subprocess.TimeoutExpired(["resolver"], 3), None),
            ("missing executable", FileNotFoundError("resolver"), None),
            ("nonzero", subprocess.CompletedProcess([], 1, "", "failed"), None),
            ("empty", subprocess.CompletedProcess([], 0, "", ""), None),
            ("disabled rung", subprocess.CompletedProcess([], 0, "disabled rung\n", ""), None),
            ("pocket route", subprocess.CompletedProcess([], 0, "pocket:tts\n", ""), "pocket:tts"),
            ("pocket-tts route", subprocess.CompletedProcess([], 0, "pocket-tts:tts\n", ""), "pocket-tts:tts"),
        )
        for name, outcome, expected in cases:
            def run_result(*_args, **_kwargs):
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            with self.subTest(outcome=name), mock.patch.object(
                TTS, "_find_model_route_resolver", return_value="/resolver"
            ), mock.patch.object(TTS.subprocess, "run", side_effect=run_result):
                self.assertEqual(expected, TTS.resolve_dutch_speech_engine())

        with mock.patch.object(TTS, "_find_model_route_resolver", return_value=None), mock.patch.object(
            TTS.subprocess, "run"
        ) as run:
            self.assertIsNone(TTS.resolve_dutch_speech_engine())
        run.assert_not_called()

    def test_content_use_is_refused_without_resolver_or_request(self):
        with mock.patch.object(TTS, "load_config", return_value=self.cli_config()), mock.patch.object(
            TTS, "resolve_dutch_speech_engine"
        ) as resolver, mock.patch.object(
            TTS, "_omnivoice_post_json"
        ) as post, contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(TTS.tts_omnivoice("private line", content=True))
        resolver.assert_not_called()
        post.assert_not_called()

    def test_explicit_content_voice_is_refused_and_resolved_to_case_o(self):
        cfg = self.cli_config()
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(TTS, "load_config", return_value=cfg), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ) as resolver, mock.patch.object(
                TTS, "_omnivoice_post_json", return_value=DUTCH_AUDIO
            ) as post, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    output,
                    TTS.tts_omnivoice("private line", voice=" ARAGORN ", output_path=output),
                )
        resolver.assert_called_once_with()
        self.assertEqual(CASE_O, json.loads(post.call_args.args[1])["voice"])

    def test_content_voice_prefix_variants_are_refused_and_resolved_to_case_o(self):
        cfg = self.cli_config()
        for voice in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "speech.wav")
                with mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    TTS, "_omnivoice_post_json", return_value=DUTCH_AUDIO
                ) as post, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        output,
                        TTS.tts_omnivoice("private line", voice=voice, output_path=output),
                    )
                self.assertEqual(CASE_O, json.loads(post.call_args.args[1])["voice"])

    def test_configured_content_prefix_is_refused_and_resolved_to_case_o(self):
        cfg = self.cli_config()
        cfg["tts_voice_pocket_content"] = "VoiceClone"
        cfg["tts_voice_pocket_en"] = "voiceclone-nl"
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(TTS, "load_config", return_value=cfg), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                TTS, "_omnivoice_post_json", return_value=DUTCH_AUDIO
            ) as post, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    output, TTS.tts_omnivoice("private line", output_path=output)
                )
        self.assertEqual(CASE_O, json.loads(post.call_args.args[1])["voice"])

    def test_content_cli_exits_nonzero_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forbidden_calls = root / "forbidden-calls.txt"
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(TTS_PATH, scripts / "tts.py")
            shutil.copyfile(SCRIPTS / "shelby_speech_policy.py",
                            scripts / "shelby_speech_policy.py")
            shutil.copyfile(SCRIPTS / "omnivoice_http.py",
                            scripts / "omnivoice_http.py")
            (scripts / "config.json").write_text(json.dumps({
                "engine_fallback": {"omnivoice": "edge"},
            }))
            (root / "sitecustomize.py").write_text(
                "import socket, subprocess\n"
                f"marker_path = {str(forbidden_calls)!r}\n"
                "def forbidden(*args, **kwargs):\n"
                "    with open(marker_path, 'a') as marker:\n"
                "        marker.write('called\\n')\n"
                "    raise RuntimeError('network or resolver call forbidden in CLI test')\n"
                "subprocess.run = forbidden\n"
                "socket.create_connection = forbidden\n"
            )
            env = {
                "HOME": str(root / "home"),
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(root),
            }
            completed = subprocess.run(
                [sys.executable, str(scripts / "tts.py"), "--engine", "omnivoice",
                 "--content", "private line"],
                cwd=root, env=env, capture_output=True, text=True, timeout=10,
            )
            recorded_calls = forbidden_calls.read_text() if forbidden_calls.exists() else ""

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("OmniVoice weights are CC-BY-NC: never for content", completed.stderr)
        self.assertNotIn("network or resolver call forbidden", completed.stderr)
        self.assertEqual("", recorded_calls)

    def test_explicit_content_voice_cli_passes_the_requested_voice(self):
        for voice in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            argv = [
                "tts.py", "--engine", "omnivoice", "--voice", voice,
                "--no-play", "private line",
            ]
            with self.subTest(voice=voice), mock.patch.object(
                sys, "argv", argv
            ), mock.patch.object(
                TTS, "load_config", return_value=self.cli_config()
            ), mock.patch.object(
                TTS, "tts_omnivoice", return_value="/tmp/speech.wav"
            ) as omnivoice, contextlib.redirect_stderr(io.StringIO()):
                TTS.main()
            omnivoice.assert_called_once_with(
                "private line", voice, None, content=False
            )

    def test_omnivoice_uses_exact_allowlist_and_case_o_for_other_names(self):
        cfg = self.cli_config()
        cfg["tts_voice_pocket_eva"] = "eva"
        cases = (
            ("case-o", CASE_O),
            ("CASE-O ", CASE_O),
            ("jarvis", "jarvis"),
            ("JARVIS ", "jarvis"),
            ("jarvis-app-dynamic", "jarvis-app-dynamic"),
            ("jarvis-app-focus", "jarvis-app-focus"),
            ("jarvis-app-lively", "jarvis-app-lively"),
            ("jarvis-studio-v2", "jarvis-studio-v2"),
            ("eva", "eva"),
            ("codex", "codex"),
            ("aragorn2", CASE_O),
            ("me", CASE_O),
            ("jarvis/../aragorn", CASE_O),
            ("jarvis-aragorn", CASE_O),
            ("JARVIS/../x", CASE_O),
            ("jarvis:hf://x", CASE_O),
            ("JARVIS-VoiceClone", CASE_O),
            ("aragorn", CASE_O),
            ("voices/aragorn.wav", CASE_O),
            ("hf://voices/private", CASE_O),
            ("arag\u00f8rn", CASE_O),
        )
        for voice, expected in cases:
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "speech.wav")
                with mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    TTS, "_omnivoice_post_json", return_value=DUTCH_AUDIO
                ) as post, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        output,
                        TTS.tts_omnivoice(
                            "private Dutch line", voice=voice, output_path=output
                        ),
                    )
                self.assertEqual(expected, json.loads(post.call_args.args[1])["voice"])

    def test_omnivoice_failure_does_not_fall_back_to_edge_with_dutch(self):
        cfg = self.cli_config()
        cfg["engine_fallback"] = {"omnivoice": "edge"}
        edge = mock.AsyncMock()
        with mock.patch.object(TTS, "load_config", return_value=cfg), mock.patch.object(
            TTS, "resolve_dutch_speech_engine", return_value=None
        ), mock.patch.object(
            TTS, "tts_edge", new=edge
        ), contextlib.redirect_stderr(io.StringIO()):
            result = TTS.speak(DUTCH, engine="omnivoice", play=False)

        self.assertIsNone(result)
        edge.assert_not_awaited()

    def test_tts_omnivoice_timeout_is_bounded(self):
        cases = ((-1, 1), (0.5, 1), (8.25, 8.25), (50, 15), ("invalid", 15))
        for configured, expected in cases:
            cfg = self.cli_config()
            cfg["dutch_speech"] = {"enabled": True, "timeout_s": configured}
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    TTS, "_omnivoice_post_json", return_value=DUTCH_AUDIO
                ) as post:
                    output = str(Path(directory) / "speech.wav")
                    self.assertEqual(output, TTS.tts_omnivoice(
                        "Dit is privé.", output_path=output
                    ))
                self.assertEqual(expected, post.call_args.args[2])

    def test_tts_omnivoice_posts_to_routed_endpoint_with_shelby_voice(self):
        with local_omnivoice_server() as (endpoint, requests):
            with tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "speech.wav")
                with mock.patch.dict(TTS.DUTCH_ENGINE_ENDPOINTS, {ROUTE_ID: endpoint}), mock.patch.object(
                    TTS, "load_config", return_value=self.cli_config()
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ):
                    result = TTS.tts_omnivoice("Dit is privé.", output_path=output)
                self.assertEqual(output, result)
                self.assertEqual(DUTCH_AUDIO, Path(output).read_bytes())

        self.assertEqual(1, len(requests))
        request_line, _, request_tail = requests[0].partition(b"\r\n")
        headers, _, request_body = request_tail.partition(b"\r\n\r\n")
        self.assertEqual(b"POST /tts HTTP/1.0", request_line)
        self.assertIn(b"Host: 127.0.0.1:", headers)
        self.assertIn(b"Content-Type: application/json", headers)
        self.assertIn(f"Content-Length: {len(request_body)}".encode(), headers)
        self.assertIn(b"Connection: close", headers)
        self.assertEqual("jarvis", json.loads(request_body)["voice"])
        self.assertEqual("Dit is privé.", json.loads(request_body)["text"])

    def test_omnivoice_refusal_paths_fall_back_to_case_o(self):
        cfg = {
            "tts_voice_pocket_en": "jarvis",
            "tts_voice_pocket_content": "VoiceClone",
        }
        cases = (
            ("aragorn", "content"),
            ("aragorn-ss", "content"),
            ("voiceclone-nl", "content"),
            ("foo", "non-Shelby"),
            ("me", "non-Shelby"),
            ("arag\u00f8rn", "non-Shelby"),
        )
        for candidate, kind in cases:
            stderr = io.StringIO()
            with self.subTest(candidate=candidate, kind=kind), contextlib.redirect_stderr(stderr):
                resolved = TTS._resolve_omnivoice_voice(cfg, candidate)
                self.assertEqual(CASE_O, resolved)
                self.assertIn(CASE_O, stderr.getvalue())
                self.assertNotIn("using jarvis", stderr.getvalue())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(CASE_O, TTS._resolve_omnivoice_voice({}, None))
        self.assertEqual("", stderr.getvalue())

    def _pocket_response(self, payload):
        class Response:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return Response()

    def test_english_pocket_refuses_content_family_and_unknown_personas(self):
        payload = b"R" * 1200
        refused = ("aragorn", "Aragorn-SS", "jarvis/../aragorn", "foo")
        for configured in refused:
            for explicit in (None, configured):
                cfg = self.cli_config()
                cfg["tts_voice_pocket_en"] = configured
                captured = {}

                def urlopen(req, timeout=None, _captured=captured):
                    _captured["body"] = req.data
                    return self._pocket_response(payload)

                stderr = io.StringIO()
                with self.subTest(configured=configured, explicit=explicit), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=urlopen
                ), contextlib.redirect_stderr(stderr):
                    output = str(Path(directory) / "speech.wav")
                    result = TTS.tts_pocket(
                        "Hello from Shelby.",
                        voice=explicit,
                        output_path=output,
                        content=False,
                    )
                    self.assertEqual(output, result)
                    self.assertEqual(CASE_O, json.loads(captured["body"])["voice"])
                    lines = stderr.getvalue().splitlines()
                    self.assertEqual(1, len(lines))
                    self.assertIn(CASE_O, lines[0])
                    self.assertIn(configured, lines[0])
                    self.assertNotIn("\n", lines[0])

    def test_english_pocket_accepts_shelby_personas_and_keeps_content_mode(self):
        payload = b"R" * 1200
        cases = (
            ({"tts_voice_pocket_en": " JARVIS "}, None, False, "jarvis"),
            ({"tts_voice_pocket_en": "case-o"}, "eva", False, "eva"),
            ({"tts_voice_pocket_en": "aragorn"}, None, True, "aragorn"),
            ({"tts_voice_pocket_en": "jarvis"}, "Aragorn-SS", True, "Aragorn-SS"),
        )
        for updates, explicit, content, expected in cases:
            cfg = self.cli_config()
            cfg.update(updates)
            captured = {}

            def urlopen(req, timeout=None, _captured=captured):
                _captured["body"] = req.data
                return self._pocket_response(payload)

            stderr = io.StringIO()
            with self.subTest(explicit=explicit, content=content), tempfile.TemporaryDirectory() as directory, mock.patch.object(
                TTS, "load_config", return_value=cfg
            ), mock.patch(
                "urllib.request.urlopen", side_effect=urlopen
            ), contextlib.redirect_stderr(stderr):
                output = str(Path(directory) / "speech.wav")
                result = TTS.tts_pocket(
                    "Hello from Shelby.",
                    voice=explicit,
                    output_path=output,
                    content=content,
                )
                self.assertEqual(output, result)
                self.assertEqual(expected, json.loads(captured["body"])["voice"])
                self.assertEqual("", stderr.getvalue())

    def test_cli_persona_failure_retries_jarvis_once_then_edge(self):
        import urllib.error

        calls = []

        def urlopen(req, timeout=None):
            voice = json.loads(req.data.decode())["voice"]
            calls.append((voice, timeout))
            if voice not in {"case-o", "jarvis"}:
                raise AssertionError(f"content voice used: {voice}")
            raise urllib.error.HTTPError(req.full_url, 503, "down", None, None)

        cfg = self.cli_config()
        cfg["tts_engine"] = "pocket"
        cfg["tts_voice_pocket_en"] = "case-o"
        cfg["tts_voice_pocket_content"] = "VoiceClone"
        cfg["pocket_tts_url"] = "http://127.0.0.1:8933"
        cfg["engine_fallback"] = {"pocket": "edge"}
        stderr = io.StringIO()
        edge = mock.AsyncMock(return_value=None)
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"SHELBY_POCKET_TIMEOUT": "60"}
        ), mock.patch.object(
            TTS, "load_config", return_value=cfg
        ), mock.patch(
            "urllib.request.urlopen", side_effect=urlopen
        ), mock.patch.object(
            TTS, "tts_edge", edge
        ), contextlib.redirect_stderr(stderr):
            result = TTS.speak(
                "Hello from Shelby.",
                engine="pocket",
                play=False,
                output_path=str(Path(directory) / "speech.wav"),
            )
        self.assertIsNone(result)
        self.assertEqual([("case-o", 60), ("jarvis", 60)], calls)
        self.assertEqual(
            1,
            stderr.getvalue().count(
                "pocket: persona case-o unavailable on http://127.0.0.1:8933, using jarvis"
            ),
        )
        edge.assert_awaited_once()
        self.assertEqual("Hello from Shelby.", edge.await_args.args[0])

    def test_cli_empty_audio_retries_jarvis_once_with_the_same_timeout(self):
        wav = b"R" * 1200
        calls = []

        def urlopen(req, timeout=None):
            voice = json.loads(req.data.decode())["voice"]
            calls.append((voice, timeout))
            payload = b"" if voice == "case-o" else wav
            if voice not in {"case-o", "jarvis"}:
                raise AssertionError(f"content voice used: {voice}")
            return self._pocket_response(payload)

        cfg = self.cli_config()
        cfg["tts_voice_pocket_en"] = "case-o"
        cfg["tts_voice_pocket_content"] = "aragorn"
        cfg["pocket_tts_url"] = "http://127.0.0.1:8933"
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"SHELBY_POCKET_TIMEOUT": "60"}
        ), mock.patch.object(
            TTS, "load_config", return_value=cfg
        ), mock.patch(
            "urllib.request.urlopen", side_effect=urlopen
        ), contextlib.redirect_stderr(stderr):
            output = str(Path(directory) / "speech.wav")
            result = TTS.tts_pocket("Hello from Shelby.", output_path=output)
            self.assertEqual(output, result)
            self.assertEqual(wav, Path(output).read_bytes())
        self.assertEqual([("case-o", 60), ("jarvis", 60)], calls)
        self.assertEqual(
            1,
            stderr.getvalue().count(
                "pocket: persona case-o unavailable on http://127.0.0.1:8933, using jarvis"
            ),
        )

    def test_cli_content_and_jarvis_failures_do_not_add_a_fallback_voice(self):
        import urllib.error

        def fail_open(req, timeout=None, calls=None):
            voice = json.loads(req.data.decode())["voice"]
            calls.append((voice, timeout))
            raise urllib.error.URLError("down")

        cfg = self.cli_config()
        cfg["pocket_tts_url"] = "http://127.0.0.1:8933"
        cfg["tts_voice_pocket_content"] = "VoiceClone"
        stderr = io.StringIO()
        jarvis_calls = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"SHELBY_POCKET_TIMEOUT": "60"}
        ), mock.patch.object(
            TTS, "load_config", return_value=cfg
        ), mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: fail_open(req, timeout, jarvis_calls),
        ), contextlib.redirect_stderr(stderr):
            output = str(Path(directory) / "speech.wav")
            result = TTS.tts_pocket("Hello from Shelby.", output_path=output)
        self.assertIsNone(result)
        self.assertEqual([("jarvis", 60)], jarvis_calls)
        self.assertNotIn("using jarvis", stderr.getvalue())

        content_calls = []
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"SHELBY_POCKET_TIMEOUT": "60"}
        ), mock.patch.object(
            TTS, "load_config", return_value=cfg
        ), mock.patch(
            "urllib.request.urlopen",
            side_effect=lambda req, timeout=None: fail_open(req, timeout, content_calls),
        ), contextlib.redirect_stderr(stderr):
            output = str(Path(directory) / "speech.wav")
            result = TTS.tts_pocket(
                "Hello from Shelby.", output_path=output, content=True
            )
        self.assertIsNone(result)
        self.assertEqual([("voiceclone", 60)], content_calls)
        self.assertNotIn("jarvis", [voice for voice, _timeout in content_calls])
        self.assertNotIn("using jarvis", stderr.getvalue())

    def test_cli_transport_failure_skips_jarvis_retry(self):
        import urllib.error

        cases = {
            "timeout": lambda: TimeoutError("timed out"),
            "socket-timeout": lambda: socket.timeout("timed out"),
            "urlerror-timeout": lambda: urllib.error.URLError(TimeoutError("timed out")),
            "connection-refused": lambda: urllib.error.URLError(
                ConnectionRefusedError(111, "Connection refused")
            ),
            "connection-reset": lambda: urllib.error.URLError(
                ConnectionResetError(104, "Connection reset by peer")
            ),
            "dns": lambda: urllib.error.URLError(
                socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            ),
        }
        cfg = self.cli_config()
        cfg["tts_engine"] = "pocket"
        cfg["tts_voice_pocket_en"] = "case-o"
        cfg["tts_voice_pocket_content"] = "VoiceClone"
        cfg["pocket_tts_url"] = "http://127.0.0.1:8933"
        cfg["engine_fallback"] = {"pocket": "edge"}
        for name, make_failure in cases.items():
            with self.subTest(failure=name):
                calls = []
                failure = make_failure()

                def urlopen(req, timeout=None, _failure=failure):
                    voice = json.loads(req.data.decode())["voice"]
                    calls.append((voice, timeout))
                    if voice == "jarvis":
                        raise AssertionError("jarvis retry on a transport failure")
                    raise _failure

                stderr = io.StringIO()
                edge = mock.AsyncMock(return_value=None)
                with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                    os.environ, {"SHELBY_POCKET_TIMEOUT": "60"}
                ), mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=urlopen
                ), mock.patch.object(
                    TTS, "tts_edge", edge
                ), contextlib.redirect_stderr(stderr):
                    result = TTS.speak(
                        "Hello from Shelby.",
                        engine="pocket",
                        play=False,
                        output_path=str(Path(directory) / "speech.wav"),
                    )
                self.assertIsNone(result)
                self.assertEqual([("case-o", 60)], calls)
                self.assertNotIn("using jarvis", stderr.getvalue())
                edge.assert_awaited_once()
                self.assertEqual("Hello from Shelby.", edge.await_args.args[0])

    def test_cli_answered_failure_retries_jarvis_once(self):
        import urllib.error

        wav = b"R" * 1200
        cases = {
            "http-404": lambda: urllib.error.HTTPError(
                "http://127.0.0.1:8933/tts", 404, "missing", None, None
            ),
            "http-500": lambda: urllib.error.HTTPError(
                "http://127.0.0.1:8933/tts", 500, "error", None, None
            ),
            "short-body": lambda: b"x" * 999,
        }
        cfg = self.cli_config()
        cfg["tts_voice_pocket_en"] = "case-o"
        cfg["tts_voice_pocket_content"] = "aragorn"
        cfg["pocket_tts_url"] = "http://127.0.0.1:8933"
        for name, make_failure in cases.items():
            with self.subTest(failure=name):
                calls = []
                failure = make_failure()

                def urlopen(req, timeout=None, _failure=failure):
                    voice = json.loads(req.data.decode())["voice"]
                    calls.append((voice, timeout))
                    if voice not in {"case-o", "jarvis"}:
                        raise AssertionError(f"content voice used: {voice}")
                    if voice == "jarvis":
                        return self._pocket_response(wav)
                    if isinstance(_failure, BaseException):
                        raise _failure
                    return self._pocket_response(_failure)

                stderr = io.StringIO()
                with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                    os.environ, {"SHELBY_POCKET_TIMEOUT": "60"}
                ), mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=urlopen
                ), contextlib.redirect_stderr(stderr):
                    output = str(Path(directory) / "speech.wav")
                    result = TTS.tts_pocket("Hello from Shelby.", output_path=output)
                    self.assertEqual(output, result)
                    self.assertEqual(wav, Path(output).read_bytes())
                self.assertEqual([("case-o", 60), ("jarvis", 60)], calls)
                self.assertEqual(
                    1,
                    stderr.getvalue().count(
                        "pocket: persona case-o unavailable on http://127.0.0.1:8933, using jarvis"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
