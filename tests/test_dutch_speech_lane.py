import asyncio
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
import unittest
from unittest import mock


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


DUTCH = "Dit is een Nederlandse zin die vandaag goed werkt."
ROUTE_ID = "omnivoice:tts"
ENDPOINT = "http://100.77.19.108:8934"
POCKET_ENDPOINT = "http://127.0.0.1:8933"
ENGLISH = "The translated line."
DUTCH_AUDIO = VOICE_HOOK.make_wav(b"\x00" * 1200)


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


def hook_config(**updates):
    cfg = {
        "source_machine": "day",
        "tts_engine": "edge",
        "dutch_speech": {"enabled": True, "timeout_s": 14},
    }
    cfg.update(updates)
    return cfg


class DutchSpeechHookTests(unittest.TestCase):
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
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
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

        fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
        send.assert_called_once_with(
            audio, remote_target, require_off_lan=False, fallback_target=None
        )
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any("TTS (omnivoice/nl/remote):" in call.args[0]
                            for call in log.call_args_list))

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
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
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

                fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
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
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
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

                fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
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

                fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
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
                    "tts_voice_pocket_en": "jarvis",
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
                VOICE_HOOK, "_wav_tempo", side_effect=lambda audio, _speed: audio
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
                        side_effect=tempo_error or (lambda wav, _speed: wav),
                    ), mock.patch.object(
                        VOICE_HOOK, "_pad_wav_tail",
                        side_effect=tail_error or (lambda wav, **_kwargs: wav),
                    ), mock.patch.object(
                        VOICE_HOOK, "send_audio_remote"
                    ) as send, mock.patch.object(
                        VOICE_HOOK, "play_audio_file"
                    ) as play, mock.patch.object(
                        VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
                    ) as translate, mock.patch.object(
                        VOICE_HOOK, "speak_edge", new=edge
                    ), mock.patch.object(
                        os, "replace", wraps=os.replace
                    ) as replace, mock.patch.object(VOICE_HOOK, "log"):
                        VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint=hint)

                    fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
                    send.assert_not_called()
                    play.assert_not_called()
                    translate.assert_called_once_with(DUTCH)
                    edge.assert_awaited_once()
                    self.assertEqual(ENGLISH, edge.await_args.args[0])
                    self.assertEqual(expects_sentinel, sentinel.exists())
                    self.assertEqual(1 if expects_sentinel else 0, replace.call_count)

    def test_local_tempfile_failure_is_pre_delivery_and_falls_back_once(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK.tempfile, "NamedTemporaryFile", side_effect=PermissionError("tmp unwritable")
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote"
        ) as send, mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ) as play, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
        send.assert_not_called()
        play.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_local_tempfile_write_failure_is_pre_delivery_and_falls_back_once(self):
        edge = mock.AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            partial_path = Path(directory) / "partial.wav"
            partial_path.touch()
            staged_file = mock.MagicMock()
            staged_file.name = str(partial_path)
            staged_file.__enter__.return_value = staged_file
            staged_file.__exit__.return_value = False
            staged_file.write.side_effect = OSError("disk full")

            with mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
            ) as fetch, mock.patch.object(
                VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
            ), mock.patch.object(
                VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
            ), mock.patch.object(
                VOICE_HOOK.tempfile, "NamedTemporaryFile", return_value=staged_file
            ), mock.patch.object(
                VOICE_HOOK, "send_audio_remote"
            ) as send, mock.patch.object(
                VOICE_HOOK, "play_audio_file"
            ) as play, mock.patch.object(
                VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
            ) as enforce, mock.patch.object(
                VOICE_HOOK, "speak_edge", new=edge
            ), mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

            self.assertFalse(partial_path.exists())

        fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
        send.assert_not_called()
        play.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

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
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once()
        send.assert_called_once_with(
            audio, remote_target, require_off_lan=False, fallback_target=None
        )
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertTrue(any("remote delivery failed before playback" in call.args[0]
                            for call in log.call_args_list))

    def test_remote_delivery_false_replays_same_dutch_audio_locally_once_when_enabled(self):
        remote_target = "http://receiver/tts"
        played_audio = []

        def read_audio(path):
            played_audio.append(Path(path).read_bytes())
            return True

        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=read_audio
        ) as play, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
        send.assert_called_once_with(
            DUTCH_AUDIO, remote_target, require_off_lan=False, fallback_target=None
        )
        play.assert_called_once()
        self.assertEqual([DUTCH_AUDIO], played_audio)
        enforce.assert_not_called()
        edge.assert_not_awaited()

    def test_remote_and_local_delivery_failure_stays_silent_without_translation(self):
        remote_target = "http://receiver/tts"
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            VOICE_HOOK, "play_audio_file", return_value=False
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
                    return True

                with mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(remote_target, play_local)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
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
        reserved = mock.Mock(status_code=201)
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch(
            "requests.post",
            side_effect=[reserved, requests.exceptions.ReadTimeout("response lost after upload")],
        ) as post, mock.patch(
            "requests.get", side_effect=requests.exceptions.ReadTimeout("status unavailable")
        ), mock.patch(
            "requests.delete", side_effect=requests.exceptions.ConnectionError("cancel unavailable")
        ), mock.patch.object(
            VOICE_HOOK.time, "sleep"
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ) as play, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        self.assertEqual(2, post.call_count)
        self.assertEqual(remote_target, post.call_args_list[1].args[0])
        self.assertEqual(DUTCH_AUDIO, post.call_args_list[1].kwargs["data"])
        play.assert_not_called()
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any(
            "Remote delivery outcome ambiguous" in call.args[0]
            for call in log.call_args_list
        ))
        self.assertTrue(any(
            "remains ambiguous — suppressing duplicate local play" in call.args[0]
            for call in log.call_args_list
        ))

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
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", return_value=True
                ) as send, mock.patch.object(
                    VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
                ) as enforce, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

                expected_fetches = [
                    ("omnivoice", (DUTCH, "jarvis", ENDPOINT, 14)),
                ]
                if engine == "pocket":
                    expected_fetches.append(
                        ("pocket", (ENGLISH, "jarvis", POCKET_ENDPOINT, 27))
                    )
                self.assertEqual(expected_fetches, fetches)
                enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
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
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", True
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "Popen", side_effect=FileNotFoundError("afplay missing")
        ) as spawn, mock.patch.object(
            VOICE_HOOK, "_translate_to_english", return_value=ENGLISH
        ) as translate, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        spawn.assert_called_once()
        translate.assert_called_once_with(DUTCH)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertTrue(any(
            "local preparation failed before delivery" in call.args[0]
            for call in log.call_args_list
        ))

    def test_local_delivery_false_does_not_fallback_after_synthesis(self):
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", return_value=False
        ), mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
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

    def test_disabled_and_absent_config_match_base_call_sequence(self):
        remote_target = "http://receiver/tts"
        english_text = "This is an English sentence."
        audio = b"audio bytes"
        config_cases = (
            ("absent", None),
            ("disabled", {"enabled": False, "timeout_s": 14}),
        )
        pocket_kwargs = {
            "remote_target": remote_target,
            "play_local": False,
            "base_url": POCKET_ENDPOINT,
            "fallback_local": False,
            "tail_ms": 1000,
            "remote_requires_off_lan": False,
            "remote_fallback_target": None,
            "speed": 1.2,
        }
        original_enforce = VOICE_HOOK.enforce_english_speech
        original_speak_pocket = VOICE_HOOK.speak_pocket

        for config_name, dutch_config in config_cases:
            for text, hint in ((english_text, "en"), (DUTCH, "nl")):
                cfg = hook_config(
                    tts_engine="pocket", pocket_tts_url=POCKET_ENDPOINT,
                    pocket_speed=1.2,
                )
                if config_name == "absent":
                    cfg.pop("dutch_speech")
                else:
                    cfg["dutch_speech"] = dutch_config
                events = []

                def record(name, result):
                    def call(*args, **kwargs):
                        events.append((name, args, kwargs))
                        return result
                    return call

                def enforce_spy(*args, **kwargs):
                    events.append(("enforce_english_speech", args, kwargs))
                    return original_enforce(*args, **kwargs)

                def speak_pocket_spy(*args, **kwargs):
                    events.append(("speak_pocket", args, kwargs))
                    return original_speak_pocket(*args, **kwargs)

                spoken_text = text
                expected = [
                    ("find_audible_path", (cfg,), {}),
                    ("enforce_english_speech", (text, hint), {}),
                ]
                if hint == "nl":
                    expected.extend([
                        ("_record_lang_drift", (DUTCH, "nl"), {}),
                        ("_translate_to_english", (DUTCH,), {}),
                    ])
                    spoken_text = ENGLISH
                expected.extend([
                    ("speak_pocket", (spoken_text, "jarvis"), pocket_kwargs),
                    ("_pocket_fetch", (spoken_text, "jarvis", POCKET_ENDPOINT, 27), {}),
                    ("send_audio_remote", (audio, remote_target), {
                        "require_off_lan": False,
                        "fallback_target": None,
                    }),
                ])

                with self.subTest(config=config_name, text=text), mock.patch.dict(
                    VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path",
                    side_effect=record("find_audible_path", (remote_target, False)),
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine"
                ) as resolver, mock.patch.object(
                    VOICE_HOOK, "enforce_english_speech", side_effect=enforce_spy
                ), mock.patch.object(
                    VOICE_HOOK, "_record_lang_drift", side_effect=record("_record_lang_drift", None)
                ), mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", side_effect=record("_translate_to_english", ENGLISH)
                ), mock.patch.object(
                    VOICE_HOOK, "speak_pocket", side_effect=speak_pocket_spy
                ), mock.patch.object(
                    VOICE_HOOK, "_pocket_fetch", side_effect=record("_pocket_fetch", audio)
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
                ), mock.patch.object(
                    VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", side_effect=record("send_audio_remote", True)
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(text, cfg, lang_hint=hint)

                self.assertEqual(expected, events)
                resolver.assert_not_called()
                edge.assert_not_awaited()

    def test_dutch_synthesis_timeout_is_clamped_and_fallback_room_is_reserved(self):
        cfg = hook_config(dutch_speech={"enabled": True, "timeout_s": 45})
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=None
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ), mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")
        self.assertEqual(15, fetch.call_args.args[3])

        with mock.patch.object(
            VOICE_HOOK, "OVERALL_DUTCH_BUDGET_S", 40
        ), mock.patch.object(
            VOICE_HOOK, "pocket_synthesis_timeout_s", return_value=27
        ):
            self.assertIsNone(VOICE_HOOK.bounded_dutch_timeout(
                {"timeout_s": 14}
            ))

    def test_resolver_failure_falls_back_before_synthesis(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=None
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch"
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_content_voice_override_is_refused_for_dutch_lane(self):
        for override in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            cfg = hook_config(
                source_machine="custom",
                tts_voice_pocket_en="eva",
                tts_voice_pocket_content="aragorn",
            )
            with self.subTest(override=override), mock.patch.dict(
                VOICE_HOOK.os.environ, {"SHELBY_TTS_POCKET_VOICE": override}
            ), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
            ) as fetch, mock.patch.object(
                VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
            ), mock.patch.object(
                VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
            ), mock.patch.object(
                VOICE_HOOK, "play_audio_file"
            ), mock.patch.object(VOICE_HOOK, "log") as log:
                VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

            fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
            self.assertTrue(any("Refused Pocket persona" in c.args[0]
                                for c in log.call_args_list))

    def test_configured_content_prefix_is_refused_on_dutch_lane(self):
        cfg = hook_config(
            source_machine="custom",
            tts_voice_pocket_en="eva",
            tts_voice_pocket_content="VoiceClone",
        )
        with mock.patch.dict(
            VOICE_HOOK.os.environ,
            {"SHELBY_TTS_POCKET_VOICE": " voiceclone-nl "},
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=DUTCH_AUDIO
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)

    def test_dutch_lane_accepts_exact_allowlist_after_normalization(self):
        cfg = hook_config(source_machine="custom", tts_voice_pocket_en="jarvis")
        cases = (
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

    def test_dutch_lane_replaces_unknown_and_content_personas_with_jarvis(self):
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
                self.assertEqual("jarvis", VOICE_HOOK._resolve_dutch_lane_voice(cfg))
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

    def test_content_voice_config_is_refused_on_english_pocket_branch(self):
        for voice in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            cfg = hook_config(
                source_machine="custom",
                tts_engine="pocket",
                tts_voice_pocket_en=voice,
                pocket_tts_url=POCKET_ENDPOINT,
                dutch_speech={"enabled": False},
            )
            with self.subTest(voice=voice), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine"
            ) as resolver, mock.patch.object(
                VOICE_HOOK, "enforce_english_speech", return_value="English line."
            ), mock.patch.object(
                VOICE_HOOK, "speak_pocket", return_value=True
            ) as pocket, mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak("This is an English line.", cfg, lang_hint="en")
            resolver.assert_not_called()
            self.assertEqual("jarvis", pocket.call_args.args[1])



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
                    synth_timeout = 0
                else:
                    self.assertEqual(1, len(fetch_calls))
                    synth_timeout = fetch_calls[0][3]
                    self.assertLessEqual(
                        synth_timeout, VOICE_HOOK.MAX_DUTCH_TIMEOUT_S
                    )
                total_reserved = (
                    VOICE_HOOK.DUTCH_RESOLVER_TIMEOUT_S
                    + max(
                        synth_timeout,
                        VOICE_HOOK.MAX_DUTCH_TIMEOUT_S if not fetch_calls else 0,
                    )
                    + VOICE_HOOK.ENGLISH_PIN_TRANSLATION_TIMEOUT_S
                    + VOICE_HOOK.pocket_synthesis_timeout_s()
                )
                self.assertLessEqual(
                    total_reserved, VOICE_HOOK.OVERALL_DUTCH_BUDGET_S
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

    def test_explicit_content_voice_is_refused_and_resolved_to_jarvis(self):
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
        self.assertEqual("jarvis", json.loads(post.call_args.args[1])["voice"])

    def test_content_voice_prefix_variants_are_refused_and_resolved_to_jarvis(self):
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
                self.assertEqual("jarvis", json.loads(post.call_args.args[1])["voice"])

    def test_configured_content_prefix_is_refused_and_resolved_to_jarvis(self):
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
        self.assertEqual("jarvis", json.loads(post.call_args.args[1])["voice"])

    def test_content_cli_exits_nonzero_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
                "def forbidden(*args, **kwargs):\n"
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

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("OmniVoice weights are CC-BY-NC: never for content", completed.stderr)
        self.assertNotIn("network or resolver call forbidden", completed.stderr)

    def test_explicit_content_voice_cli_resolves_to_jarvis(self):
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

    def test_omnivoice_uses_exact_allowlist_and_jarvis_for_other_names(self):
        cfg = self.cli_config()
        cfg["tts_voice_pocket_eva"] = "eva"
        cases = (
            ("jarvis", "jarvis"),
            ("JARVIS ", "jarvis"),
            ("jarvis-app-dynamic", "jarvis-app-dynamic"),
            ("jarvis-app-focus", "jarvis-app-focus"),
            ("jarvis-app-lively", "jarvis-app-lively"),
            ("jarvis-studio-v2", "jarvis-studio-v2"),
            ("eva", "eva"),
            ("codex", "codex"),
            ("aragorn2", "jarvis"),
            ("me", "jarvis"),
            ("jarvis/../aragorn", "jarvis"),
            ("jarvis-aragorn", "jarvis"),
            ("JARVIS/../x", "jarvis"),
            ("jarvis:hf://x", "jarvis"),
            ("JARVIS-VoiceClone", "jarvis"),
            ("aragorn", "jarvis"),
            ("voices/aragorn.wav", "jarvis"),
            ("hf://voices/private", "jarvis"),
            ("arag\u00f8rn", "jarvis"),
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


if __name__ == "__main__":
    unittest.main()
