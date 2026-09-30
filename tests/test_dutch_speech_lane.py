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
import urllib.error
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


def add_translation_stub(home):
    script = Path(home) / "42" / "Config" / "scripts" / "tier2-routed.py"
    script.parent.mkdir(parents=True)
    script.touch()
    return script


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.offset = 0
        self.read_requests = []

    def __enter__(self):
        self.offset = 0
        return self

    def __exit__(self, *_args):
        return False

    def read(self, requested_size=-1):
        self.read_requests.append(requested_size)
        if requested_size is None or requested_size < 0:
            requested_size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + requested_size]
        self.offset += len(chunk)
        return chunk


class TricklingResponse(FakeResponse):
    def __init__(self, body, clock, seconds_per_read=0.6, bytes_per_read=1200):
        super().__init__(body)
        self.clock = clock
        self.seconds_per_read = seconds_per_read
        self.bytes_per_read = bytes_per_read
        self.requested_sizes = []

    def read(self, requested_size=-1):
        self.clock[0] += self.seconds_per_read
        self.requested_sizes.append(requested_size)
        if requested_size is not None and requested_size >= 0:
            requested_size = min(requested_size, self.bytes_per_read)
        return super().read(requested_size)


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

    def test_lane_delivery_success_is_one_utterance(self):
        remote_target = "http://receiver/tts"
        audio = b"Dutch audio"
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=audio
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
        audio = b"Dutch audio"
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
                    VOICE_HOOK, "_pocket_fetch", return_value=audio
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
        audio = b"Dutch audio"
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
                    VOICE_HOOK, "_pocket_fetch", return_value=audio
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

    def test_lane_synthesis_failure_falls_back_to_english_once(self):
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=None
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "send_audio_remote"
        ) as send, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once_with(DUTCH, "jarvis", ENDPOINT, 14)
        send.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_lane_delivery_false_after_synthesis_logs_and_does_not_fallback(self):
        remote_target = "http://receiver/tts"
        audio = b"Dutch audio"
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=audio
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        fetch.assert_called_once()
        send.assert_called_once_with(
            audio, remote_target, require_off_lan=False, fallback_target=None
        )
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any(
            call.args[0] == "TTS (omnivoice/nl): delivery not confirmed; no English fallback (avoid double speech)"
            for call in log.call_args_list
        ))

    def test_local_delivery_failure_does_not_fallback_after_synthesis(self):
        audio = b"Dutch audio"
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=audio
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=FileNotFoundError("player missing")
        ) as play, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        play.assert_called_once()
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertTrue(any(
            call.args[0] == "TTS (omnivoice/nl): delivery not confirmed; no English fallback (avoid double speech)"
            for call in log.call_args_list
        ))

    def test_local_delivery_false_does_not_fallback_after_synthesis(self):
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=b"Dutch audio"
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", return_value=False
        ), mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
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
            VOICE_HOOK, "_pocket_fetch", return_value=None
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
            VOICE_HOOK, "_pocket_fetch"
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
                VOICE_HOOK, "_pocket_fetch", return_value=b"Dutch audio"
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
            VOICE_HOOK, "_pocket_fetch", return_value=b"Dutch audio"
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
            with self.subTest(outcome=name), tempfile.TemporaryDirectory() as home:
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
                with mock.patch.dict(VOICE_HOOK.os.environ, {"HOME": home}), mock.patch.object(
                    VOICE_HOOK, "_find_model_route_resolver", return_value=resolver_path
                ), mock.patch.object(
                    VOICE_HOOK.subprocess, "run", side_effect=run
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "_pocket_fetch"
                ) as fetch, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

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
                self.assertFalse(
                    (Path(home) / ".shelby" / "voice-lang-drift.json").exists()
                )

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
            (
                "synthesis HTTP error",
                ROUTE_ID,
                urllib.error.HTTPError(f"{ENDPOINT}/tts", 503, "busy", None, None),
            ),
        )
        for name, resolver_outcome, synth_error in cases:
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as home:
                add_translation_stub(home)
                resolver_calls = []
                translation_calls = []

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
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=synth_error
                ) as urlopen, mock.patch.object(
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
                if synth_error is None:
                    urlopen.assert_not_called()
                    synth_timeout = 0
                else:
                    urlopen.assert_called_once()
                    synth_timeout = urlopen.call_args.kwargs["timeout"]
                    self.assertLessEqual(
                        synth_timeout, VOICE_HOOK.MAX_DUTCH_TIMEOUT_S
                    )
                total_reserved = (
                    VOICE_HOOK.DUTCH_RESOLVER_TIMEOUT_S
                    + max(synth_timeout, VOICE_HOOK.MAX_DUTCH_TIMEOUT_S if synth_error is None else 0)
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
        ) as resolver, mock.patch(
            "urllib.request.urlopen"
        ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(TTS.tts_omnivoice("private line", content=True))
        resolver.assert_not_called()
        urlopen.assert_not_called()

    def test_explicit_content_voice_is_refused_and_resolved_to_jarvis(self):
        response = FakeResponse(b"RIFF" + b"0" * 1200)
        cfg = self.cli_config()
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(TTS, "load_config", return_value=cfg), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ) as resolver, mock.patch(
                "urllib.request.urlopen", return_value=response
            ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    output,
                    TTS.tts_omnivoice("private line", voice=" ARAGORN ", output_path=output),
                )
        resolver.assert_called_once_with()
        self.assertEqual("jarvis", json.loads(urlopen.call_args.args[0].data)["voice"])

    def test_content_voice_prefix_variants_are_refused_and_resolved_to_jarvis(self):
        cfg = self.cli_config()
        for voice in ("Aragorn", " aragorn-ss ", "ARAGORN2"):
            response = FakeResponse(b"RIFF" + b"0" * 1200)
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "speech.wav")
                with mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch(
                    "urllib.request.urlopen", return_value=response
                ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        output,
                        TTS.tts_omnivoice("private line", voice=voice, output_path=output),
                    )
                self.assertEqual("jarvis", json.loads(urlopen.call_args.args[0].data)["voice"])

    def test_configured_content_prefix_is_refused_and_resolved_to_jarvis(self):
        cfg = self.cli_config()
        cfg["tts_voice_pocket_content"] = "VoiceClone"
        cfg["tts_voice_pocket_en"] = "voiceclone-nl"
        response = FakeResponse(b"RIFF" + b"0" * 1200)
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(TTS, "load_config", return_value=cfg), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch(
                "urllib.request.urlopen", return_value=response
            ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    output, TTS.tts_omnivoice("private line", output_path=output)
                )
        self.assertEqual("jarvis", json.loads(urlopen.call_args.args[0].data)["voice"])

    def test_content_cli_exits_nonzero_without_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copyfile(TTS_PATH, scripts / "tts.py")
            shutil.copyfile(SCRIPTS / "shelby_speech_policy.py",
                            scripts / "shelby_speech_policy.py")
            (scripts / "config.json").write_text(json.dumps({
                "engine_fallback": {"omnivoice": "edge"},
            }))
            (root / "sitecustomize.py").write_text(
                "import subprocess, urllib.request\n"
                "def forbidden(*args, **kwargs):\n"
                "    raise RuntimeError('network or resolver call forbidden in CLI test')\n"
                "subprocess.run = forbidden\n"
                "urllib.request.urlopen = forbidden\n"
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
        response = FakeResponse(b"RIFF" + b"0" * 1200)
        for voice, expected in cases:
            with self.subTest(voice=voice), tempfile.TemporaryDirectory() as directory:
                output = str(Path(directory) / "speech.wav")
                with mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch(
                    "urllib.request.urlopen", return_value=response
                ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(
                        output,
                        TTS.tts_omnivoice(
                            "private Dutch line", voice=voice, output_path=output
                        ),
                    )
                self.assertEqual(expected, json.loads(urlopen.call_args.args[0].data)["voice"])

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
        response = FakeResponse(b"RIFF" + b"0" * 1200)
        cases = ((-1, 1), (0.5, 1), (8.25, 8.25), (50, 15), ("invalid", 15))
        for configured, expected in cases:
            cfg = self.cli_config()
            cfg["dutch_speech"] = {"enabled": True, "timeout_s": configured}
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(
                    TTS, "load_config", return_value=cfg
                ), mock.patch.object(
                    TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
                ), mock.patch(
                    "urllib.request.urlopen", return_value=response
                ) as urlopen:
                    output = str(Path(directory) / "speech.wav")
                    self.assertEqual(output, TTS.tts_omnivoice(
                        "Dit is privé.", output_path=output
                    ))
                self.assertEqual(expected, urlopen.call_args.kwargs["timeout"])

    def test_tts_omnivoice_enforces_one_deadline_across_trickling_reads(self):
        clock = [0.0]
        response = TricklingResponse(b"RIFF" + b"0" * 3000, clock)
        cfg = self.cli_config()
        cfg["dutch_speech"] = {"enabled": True, "timeout_s": 1}
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(
                TTS, "load_config", return_value=cfg
            ), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                TTS.time, "monotonic", side_effect=lambda: clock[0]
            ), mock.patch(
                "urllib.request.urlopen", return_value=response
            ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(
                    TTS.tts_omnivoice("Dit is privé.", output_path=output)
                )

            self.assertFalse(Path(output).exists())
        self.assertEqual(1, urlopen.call_args.kwargs["timeout"])
        self.assertEqual(2, len(response.read_requests))
        self.assertTrue(response.requested_sizes)
        self.assertTrue(all(
            size == TTS.OMNIVOICE_RESPONSE_CHUNK_SIZE
            for size in response.requested_sizes
        ))

    def test_tts_omnivoice_rejects_response_over_byte_cap(self):
        response = FakeResponse(
            b"RIFF" + b"0" * TTS.MAX_OMNIVOICE_RESPONSE_BYTES
        )
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(
                TTS, "load_config", return_value=self.cli_config()
            ), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch(
                "urllib.request.urlopen", return_value=response
            ), contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(
                    TTS.tts_omnivoice("Dit is privé.", output_path=output)
                )
            self.assertFalse(Path(output).exists())

    def test_tts_omnivoice_posts_to_routed_endpoint_with_shelby_voice(self):
        response = FakeResponse(b"RIFF" + b"0" * 1200)
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "speech.wav")
            with mock.patch.object(
                TTS, "load_config", return_value=self.cli_config()
            ), mock.patch.object(
                TTS, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch(
                "urllib.request.urlopen", return_value=response
            ) as urlopen:
                result = TTS.tts_omnivoice("Dit is privé.", output_path=output)
            self.assertEqual(output, result)
            self.assertTrue(Path(output).read_bytes().startswith(b"RIFF"))

        request = urlopen.call_args.args[0]
        self.assertEqual(f"{ENDPOINT}/tts", request.full_url)
        self.assertEqual("jarvis", json.loads(request.data)["voice"])
        self.assertEqual(11, urlopen.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
