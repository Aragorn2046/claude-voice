import asyncio
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
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
    def test_selected_omnivoice_speaks_original_dutch_before_english_gate(self):
        events = []
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", side_effect=lambda _cfg: (events.append("audible") or (None, True))
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine",
            side_effect=lambda: (events.append("resolver") or ROUTE_ID),
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(
            VOICE_HOOK, "enforce_english_speech", side_effect=AssertionError("English gate ran")
        ), mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, hook_config(), lang_hint="nl")

        self.assertEqual(["audible", "resolver"], events)
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

    def test_lane_failure_uses_translation_pin_path(self):
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
        self.assertEqual("The translated line.", edge.await_args.args[0])

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

    def test_english_or_disabled_or_absent_config_never_resolves(self):
        configs = [
            hook_config(dutch_speech={"enabled": False, "timeout_s": 14}),
            hook_config(),
        ]
        configs[1].pop("dutch_speech")
        configs.append(hook_config())
        for index, cfg in enumerate(configs):
            text = "This is a short English sentence." if index == 2 else DUTCH
            with self.subTest(index=index), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True)
            ), mock.patch.object(
                VOICE_HOOK, "resolve_dutch_speech_engine"
            ) as resolver, mock.patch.object(
                VOICE_HOOK, "enforce_english_speech", return_value="English."
            ), mock.patch.object(
                VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
            ), mock.patch.object(VOICE_HOOK, "log"):
                VOICE_HOOK.speak(text, cfg, lang_hint="en")
            resolver.assert_not_called()

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

    def test_content_voice_override_is_refused_for_dutch_lane(self):
        cfg = hook_config(
            source_machine="custom",
            tts_voice_pocket_en="eva",
            tts_voice_pocket_content=" Aragorn ",
        )
        with mock.patch.dict(VOICE_HOOK.os.environ, {"SHELBY_TTS_POCKET_VOICE": "  ARAGORN "}), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value=ROUTE_ID
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(VOICE_HOOK, "log") as log:
            VOICE_HOOK.speak(DUTCH, cfg, lang_hint="nl")

        self.assertEqual("jarvis", pocket.call_args.args[1])
        self.assertTrue(any("Refused Pocket persona" in c.args[0]
                            for c in log.call_args_list))

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
            VOICE_HOOK, "enforce_english_speech", side_effect=AssertionError("fallback ran")
        ), mock.patch.object(VOICE_HOOK, "log"):
            VOICE_HOOK.speak(DUTCH, hook_config())

        record_drift.assert_called_once_with(DUTCH, None)
        self.assertEqual(DUTCH, pocket.call_args.args[0])

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

    def test_explicit_content_voice_is_refused_without_request(self):
        with mock.patch.object(TTS, "load_config", return_value=self.cli_config()), mock.patch.object(
            TTS, "resolve_dutch_speech_engine"
        ) as resolver, mock.patch(
            "urllib.request.urlopen"
        ) as urlopen, contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(TTS.tts_omnivoice("private line", voice=" ARAGORN "))
        resolver.assert_not_called()
        urlopen.assert_not_called()

    def test_content_cli_exits_nonzero_without_request(self):
        argv = ["tts.py", "--engine", "omnivoice", "--content", "private line"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            TTS, "load_config", return_value=self.cli_config()
        ), mock.patch("urllib.request.urlopen") as urlopen, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                TTS.main()
        self.assertEqual(1, result.exception.code)
        urlopen.assert_not_called()

    def test_explicit_content_voice_cli_exits_nonzero_without_request(self):
        argv = ["tts.py", "--engine", "omnivoice", "--voice", " ARAGORN ", "private line"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            TTS, "load_config", return_value=self.cli_config()
        ), mock.patch("urllib.request.urlopen") as urlopen, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                TTS.main()
        self.assertEqual(1, result.exception.code)
        urlopen.assert_not_called()

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
