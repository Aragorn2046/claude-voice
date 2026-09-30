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


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body


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

    def test_selected_omnivoice_speaks_once_without_running_english_fallback(self):
        events = []
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", side_effect=lambda _cfg: (events.append("audible") or (None, True))
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine",
            side_effect=lambda: (events.append("resolver") or ROUTE_ID),
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        self.assertEqual(["audible", "resolver"], events)
        pocket.assert_called_once()
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertEqual(DUTCH, pocket.call_args.args[0])
        self.assertEqual("jarvis", pocket.call_args.args[1])
        self.assertEqual(ENDPOINT, pocket.call_args.kwargs["base_url"])
        self.assertEqual(14, pocket.call_args.kwargs["timeout_s"])
        self.assertNotIn("fallback_base_url", pocket.call_args.kwargs)
        self.assertTrue(any("TTS (omnivoice/nl/local):" in c.args[0]
                            for c in log.call_args_list))

    def test_other_catalog_id_uses_translation_pin_path(self):
        edge = mock.AsyncMock()
        with mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value="pocket-tts:tts"
        ), mock.patch.object(VOICE_HOOK, "speak_pocket") as pocket, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value="The translated line.",
        ) as enforce, mock.patch.object(VOICE_HOOK, "speak_edge", new=edge), mock.patch.object(
            VOICE_HOOK, "log"
        ):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        pocket.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        self.assertEqual("The translated line.", edge.await_args.args[0])

    def test_lane_failure_never_sends_raw_dutch_to_edge(self):
        edge = mock.AsyncMock()
        with mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(VOICE_HOOK, "speak_pocket", return_value=False) as pocket, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value="The translated line.",
        ) as enforce, mock.patch.object(VOICE_HOOK, "speak_edge", new=edge), mock.patch.object(
            VOICE_HOOK, "log"
        ):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        self.assertEqual(ENDPOINT, pocket.call_args.kwargs["base_url"])
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertNotEqual(DUTCH, edge.await_args.args[0])

    def test_lane_exception_uses_english_pocket_fallback_once(self):
        cfg = hook_config(
            tts_engine="pocket", pocket_tts_url=POCKET_ENDPOINT, pocket_speed=1.2,
        )
        pocket = mock.Mock(side_effect=[ConnectionError("lane down"), True])
        edge = mock.AsyncMock()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", new=pocket
        ), mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        self.assertEqual(2, pocket.call_count)
        self.assertEqual((DUTCH, "jarvis"), pocket.call_args_list[0].args)
        self.assertEqual(ENDPOINT, pocket.call_args_list[0].kwargs["base_url"])
        self.assertEqual((ENGLISH, "jarvis"), pocket.call_args_list[1].args)
        self.assertEqual(POCKET_ENDPOINT, pocket.call_args_list[1].kwargs["base_url"])
        self.assertEqual(27, pocket.call_args_list[1].kwargs["timeout_s"])
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_not_awaited()

    def test_undeclared_dutch_lane_failure_records_drift_once_then_falls_back(self):
        cfg = hook_config(tts_engine="pocket", pocket_tts_url=POCKET_ENDPOINT)
        pocket = mock.Mock(side_effect=[False, True])
        with mock.patch.object(
            VOICE_HOOK, "detect_language", return_value="nl"
        ), mock.patch.object(
            VOICE_HOOK, "is_dutch_speech", return_value=True
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", new=pocket
        ), mock.patch.object(
            VOICE_HOOK, "_record_lang_drift"
        ) as record_drift, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg)

        record_drift.assert_called_once_with(DUTCH, None)
        enforce.assert_called_once_with(DUTCH, None, record_drift=False)
        self.assertEqual(2, pocket.call_count)
        self.assertEqual(DUTCH, pocket.call_args_list[0].args[0])
        self.assertEqual(ENGLISH, pocket.call_args_list[1].args[0])

    def test_resolver_failures_fall_back_without_lane_request(self):
        cases = (
            ("timeout", subprocess.TimeoutExpired("resolver", 3)),
            ("nonzero", mock.Mock(returncode=4, stdout="", stderr="policy disabled")),
            ("missing", None),
        )
        for name, outcome in cases:
            with self.subTest(name=name):
                edge = mock.AsyncMock()
                run = mock.Mock()
                if name == "timeout":
                    run.side_effect = outcome
                elif name == "nonzero":
                    run.return_value = outcome
                finder_result = None if name == "missing" else "/home/arago/scripts/shelby-model-route.py"
                with mock.patch.object(
                    VOICE_HOOK, "_find_model_route_resolver", return_value=finder_result
                ), mock.patch.object(VOICE_HOOK.subprocess, "run", run), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(VOICE_HOOK, "speak_pocket") as pocket, mock.patch.object(
                    VOICE_HOOK, "enforce_english_speech", return_value="Pinned English.",
                ) as enforce, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=edge
                ), mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

                pocket.assert_not_called()
                enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
                self.assertEqual("Pinned English.", edge.await_args.args[0])
                if name == "missing":
                    run.assert_not_called()
                else:
                    self.assertEqual(3, run.call_args.kwargs["timeout"])
                    self.assertFalse(run.call_args.kwargs["shell"])

    def test_resolver_reads_catalog_id_with_hard_timeout(self):
        completed = mock.Mock(returncode=0, stdout="omnivoice:tts\n", stderr="")
        with mock.patch.object(
            VOICE_HOOK, "_find_model_route_resolver", return_value="/resolver"
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(ROUTE_ID, VOICE_HOOK.resolve_dutch_speech_engine())

        argv = run.call_args.args[0]
        self.assertEqual("--consumer", argv[2])
        self.assertEqual("shelby-audio", argv[3])
        self.assertEqual("--modality", argv[4])
        self.assertEqual("speech-nl", argv[5])
        self.assertEqual("--target", argv[6])
        self.assertEqual("catalog", argv[7])
        self.assertEqual(["--field", "catalog_id"], argv[8:10])
        self.assertEqual(3, run.call_args.kwargs["timeout"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_resolver_rejects_unparseable_catalog_output(self):
        completed = mock.Mock(returncode=0, stdout="active route: omnivoice:tts", stderr="")
        with mock.patch.object(
            VOICE_HOOK, "_find_model_route_resolver", return_value="/resolver"
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run", return_value=completed
        ), mock.patch.object(VOICE_HOOK, "log"):
            self.assertIsNone(VOICE_HOOK.resolve_dutch_speech_engine())

    def test_disabled_and_absent_config_match_pre_lane_dutch_call_sequence(self):
        config_cases = (
            ("absent", None),
            ("non_dict", None),
            ("empty", {}),
            ("disabled", {"enabled": False, "timeout_s": 14}),
            ("string_false", {"enabled": "false", "timeout_s": 14}),
        )
        original_enforce = VOICE_HOOK.enforce_english_speech

        for config_name, dutch_config in config_cases:
            for hint in (None, "nl"):
                cfg = hook_config(
                    tts_engine="pocket", pocket_tts_url=POCKET_ENDPOINT,
                    pocket_speed=1.2,
                )
                if config_name == "absent":
                    cfg.pop("dutch_speech")
                else:
                    cfg["dutch_speech"] = dutch_config
                events = []

                def event(name, result):
                    def record(*args, **kwargs):
                        events.append((name, args, kwargs))
                        return result
                    return record

                def enforce_spy(*args, **kwargs):
                    events.append(("enforce_english_speech", args, kwargs))
                    return original_enforce(*args, **kwargs)

                with self.subTest(config=config_name, hint=hint), mock.patch.object(
                    VOICE_HOOK, "detect_language", side_effect=event("detect_language", "nl")
                ), mock.patch.object(
                    VOICE_HOOK, "find_audible_path", side_effect=event("find_audible_path", (None, True))
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine"
                ) as resolver, mock.patch.object(
                    VOICE_HOOK, "enforce_english_speech", side_effect=enforce_spy
                ), mock.patch.object(
                    VOICE_HOOK, "_record_lang_drift", side_effect=event("_record_lang_drift", None)
                ), mock.patch.object(
                    VOICE_HOOK, "_translate_to_english", side_effect=event("_translate_to_english", ENGLISH)
                ), mock.patch.object(
                    VOICE_HOOK, "speak_pocket", side_effect=event("speak_pocket", True)
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ) as edge, mock.patch.object(VOICE_HOOK, "log"):
                    VOICE_HOOK.speak(DUTCH, cfg, lang_hint=hint)

                expected = []
                if hint is None:
                    expected.append(("detect_language", (DUTCH,), {}))
                expected.extend([
                    ("find_audible_path", (cfg,), {}),
                    ("enforce_english_speech", (DUTCH, hint), {}),
                    ("_record_lang_drift", (DUTCH, hint), {}),
                    ("_translate_to_english", (DUTCH,), {}),
                    ("speak_pocket", (ENGLISH, "jarvis"), {
                        "remote_target": None,
                        "play_local": True,
                        "base_url": POCKET_ENDPOINT,
                        "fallback_local": True,
                        "tail_ms": 1000,
                        "remote_requires_off_lan": False,
                        "remote_fallback_target": None,
                        "speed": 1.2,
                    }),
                ])
                self.assertEqual(expected, events)
                resolver.assert_not_called()
                edge.assert_not_awaited()

    def test_muted_turn_never_resolves(self):
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, False)
        ), mock.patch.object(VOICE_HOOK, "resolve_dutch_speech_engine") as resolver, mock.patch.object(
            VOICE_HOOK, "speak_pocket"
        ) as pocket, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")
        resolver.assert_not_called()
        pocket.assert_not_called()

    def test_pocket_timeout_override_and_environment_default(self):
        with mock.patch.object(VOICE_HOOK, "_pocket_fetch", return_value=None) as fetch, mock.patch.dict(
            VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
        ):
            self.assertFalse(VOICE_HOOK.speak_pocket("Test.", "jarvis", timeout_s=9))
            self.assertEqual(9, fetch.call_args.args[3])
            self.assertFalse(VOICE_HOOK.speak_pocket("Test.", "jarvis"))
            self.assertEqual(27, fetch.call_args.args[3])

    def test_dutch_lane_timeout_is_bounded_to_one_through_fifteen_seconds(self):
        cases = ((-2, 1), (0.5, 1), (9.25, 9.25), (45, 15), ("bad", 15))
        for configured, expected in cases:
            cfg = hook_config(dutch_speech={"enabled": True, "timeout_s": configured})
            with self.subTest(configured=configured), mock.patch.object(
                VOICE_HOOK, "pocket_synthesis_timeout_s", return_value=27
            ), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
            ), mock.patch.object(
                VOICE_HOOK, "speak_pocket", return_value=True
            ) as pocket, mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")
            self.assertEqual(expected, pocket.call_args.kwargs["timeout_s"])

    def test_slow_lane_preserves_translation_and_pocket_reserve_then_speaks(self):
        overall_budget = 2.2
        resolver_reserve = 0.05
        translation_reserve = 0.2
        pocket_reserve = 0.3
        events = []
        cfg = hook_config(
            tts_engine="pocket", pocket_tts_url=POCKET_ENDPOINT,
            dutch_speech={"enabled": True, "timeout_s": 10},
        )

        def resolve_slowly():
            time.sleep(resolver_reserve)
            events.append("resolver")
            return ROUTE_ID

        def pocket_lane_and_fallback(text, _voice, **kwargs):
            if kwargs["base_url"] == ENDPOINT:
                events.append(("lane", kwargs["timeout_s"]))
                time.sleep(kwargs["timeout_s"])
                return VOICE_HOOK.POCKET_RESULT_FALLBACK_SAFE
            events.append(("fallback", text, kwargs["timeout_s"]))
            time.sleep(kwargs["timeout_s"])
            return True

        def translate_and_pin(text, lang_hint, record_drift=True):
            events.append(("translate", text, lang_hint, record_drift))
            time.sleep(translation_reserve)
            return ENGLISH

        start = time.monotonic()
        with mock.patch.object(
            VOICE_HOOK, "OVERALL_DUTCH_BUDGET_S", overall_budget
        ), mock.patch.object(
            VOICE_HOOK, "DUTCH_RESOLVER_TIMEOUT_S", resolver_reserve
        ), mock.patch.object(
            VOICE_HOOK, "ENGLISH_PIN_TRANSLATION_TIMEOUT_S", translation_reserve
        ), mock.patch.object(
            VOICE_HOOK, "MAX_DUTCH_TIMEOUT_S", 1.1
        ), mock.patch.object(
            VOICE_HOOK, "MIN_DUTCH_LANE_BUDGET_S", 0.1
        ), mock.patch.object(
            VOICE_HOOK, "pocket_synthesis_timeout_s", return_value=pocket_reserve
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", side_effect=resolve_slowly
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", side_effect=pocket_lane_and_fallback
        ), mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", side_effect=translate_and_pin
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        elapsed = time.monotonic() - start
        self.assertLessEqual(elapsed, overall_budget)
        self.assertEqual("resolver", events[0])
        self.assertEqual(("lane", 1.1), events[1])
        self.assertEqual(("translate", DUTCH, "nl", False), events[2])
        self.assertEqual(("fallback", ENGLISH, pocket_reserve), events[3])

    def test_lane_is_skipped_when_fallback_reserve_leaves_less_than_floor(self):
        cfg = hook_config(
            tts_engine="pocket", pocket_tts_url=POCKET_ENDPOINT,
            dutch_speech={"enabled": True, "timeout_s": 14},
        )
        with mock.patch.object(
            VOICE_HOOK, "OVERALL_DUTCH_BUDGET_S", 40
        ), mock.patch.object(
            VOICE_HOOK, "pocket_synthesis_timeout_s", return_value=27
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine"
        ) as resolver, mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        resolver.assert_not_called()
        pocket.assert_called_once()
        self.assertEqual(POCKET_ENDPOINT, pocket.call_args.kwargs["base_url"])
        self.assertEqual(27, pocket.call_args.kwargs["timeout_s"])

    def test_remote_only_receiver_refusal_uses_one_english_fallback(self):
        remote_target = "http://receiver/tts"
        cfg = hook_config()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(remote_target, False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=b"audio bytes"
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda audio, _speed: audio
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda audio, **_kwargs: audio
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=False
        ) as send_remote, mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ) as play_local, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        send_remote.assert_called_once()
        play_local.assert_not_called()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])
        self.assertEqual(remote_target, edge.await_args.kwargs["remote_target"])

    def test_missing_local_player_uses_one_english_fallback(self):
        cfg = hook_config()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=b"audio bytes"
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda audio, _speed: audio
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda audio, **_kwargs: audio
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file", side_effect=FileNotFoundError("paplay missing")
        ) as play_local, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value=ENGLISH
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        play_local.assert_called_once()
        enforce.assert_called_once_with(DUTCH, "nl", record_drift=False)
        edge.assert_awaited_once()
        self.assertEqual(ENGLISH, edge.await_args.args[0])

    def test_mid_stream_playback_exception_does_not_trigger_a_second_utterance(self):
        cfg = hook_config()
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=b"audio bytes"
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo", side_effect=lambda audio, _speed: audio
        ), mock.patch.object(
            VOICE_HOOK, "_pad_wav_tail", side_effect=lambda audio, **_kwargs: audio
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file",
            side_effect=subprocess.TimeoutExpired("paplay", 30),
        ), mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        enforce.assert_not_called()
        edge.assert_not_awaited()

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
                VOICE_HOOK, "speak_pocket", return_value=True
            ) as pocket, mock.patch.object(VOICE_HOOK, "log") as log:
                VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

            pocket.assert_called_once()
            self.assertEqual("jarvis", pocket.call_args.args[1])
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
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        self.assertEqual("jarvis", pocket.call_args.args[1])

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

    def test_declared_dutch_has_no_drift_even_when_lane_falls_back(self):
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=False
        ), mock.patch.object(
            VOICE_HOOK, "_record_lang_drift"
        ) as record_drift, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", return_value="Pinned English."
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        record_drift.assert_not_called()
        self.assertFalse(enforce.call_args.kwargs["record_drift"])

    def test_undeclared_dutch_records_drift_and_uses_lane(self):
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(
            VOICE_HOOK, "_record_lang_drift"
        ) as record_drift, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech"
        ) as enforce, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ) as edge, mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config())

        record_drift.assert_called_once_with(DUTCH, None)
        enforce.assert_not_called()
        edge.assert_not_awaited()
        self.assertEqual(DUTCH, pocket.call_args.args[0])
        pocket.assert_called_once()

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
