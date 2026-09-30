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


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.offset = 0

    def __enter__(self):
        self.offset = 0
        return self

    def __exit__(self, *_args):
        return False

    def read(self, requested_size=-1):
        if requested_size is None or requested_size < 0:
            requested_size = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + requested_size]
        self.offset += len(chunk)
        return chunk


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
        enforce.assert_called_once_with(DUTCH, "nl")
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
        enforce.assert_called_once_with(DUTCH, "nl")
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
        self.assertEqual(3, run.call_args.kwargs["timeout"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertIn("speech-nl", run.call_args.args[0])

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
