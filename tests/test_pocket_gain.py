"""Pocket loudness (`pocket_gain_db`) shares the pocket_speed ffmpeg pass."""

import importlib.util
import io
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest
import urllib.error
import wave
from pathlib import Path
from unittest import mock


HOOK_PATH = Path(__file__).parents[1] / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location("voice_stop_hook_gain", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)

WAV = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVE" + b"source-marker" + (b"\x00" * 48)
SHAPED = b"RIFF" + b"WAVE" + b"shaped-audio" + (b"\x00" * 64)
LIMITER = "alimiter=limit=0.97:level=0:latency=1"
SPEED_ONLY = "atempo=1.400"
GAIN_ONLY = f"volume=5.2dB,{LIMITER}"
BOTH = f"atempo=1.400,volume=5.2dB,{LIMITER}"
NEGATIVE_GAIN = "volume=-5.2dB"


def _ffmpeg_available():
    if any(os.path.exists(path) for path in (
        "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg",
    )):
        return True
    return shutil.which("ffmpeg") is not None


def _sine_wav(seconds=0.5, freq=440.0, amplitude=0.4, rate=24000, click_amplitude=None):
    frame_count = int(seconds * rate)
    # An 8-sample click starting 2 ms before the end sits inside the final 3 ms.
    click_at = frame_count - int(0.002 * rate)
    click_len = 8
    pcm = bytearray()
    for index in range(frame_count):
        value = amplitude * math.sin(2 * math.pi * freq * index / rate)
        if (
            click_amplitude is not None
            and click_at <= index < min(frame_count, click_at + click_len)
        ):
            value = click_amplitude if (index - click_at) % 2 == 0 else -click_amplitude
        sample = int(round(max(-1.0, min(1.0, value)) * 32767))
        pcm += struct.pack("<h", sample)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(pcm))
    return buffer.getvalue()


def _wav_stats(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        width = reader.getsampwidth()
        channels = reader.getnchannels()
        frames = reader.readframes(reader.getnframes())
    if width != 2:
        raise AssertionError(f"expected 16-bit PCM, got sample width {width}")
    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    if channels > 1:
        samples = samples[::channels]
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    return rms, peak


class _FfmpegHarness:
    def __init__(self, result="ok"):
        self.result = result
        self.commands = []
        self.inputs = []

    def __call__(self, cmd, input=None, capture_output=None, timeout=None):
        self.commands.append(list(cmd))
        self.inputs.append(input)
        out = cmd[-1]
        if self.result == "raise":
            raise OSError("ffmpeg failed")
        if self.result == "rc":
            return subprocess.CompletedProcess(cmd, 7, b"", b"nope")
        if self.result == "empty":
            with open(out, "wb") as handle:
                handle.write(b"")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        with open(out, "wb") as handle:
            handle.write(SHAPED)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


def _filter_of(cmd):
    return cmd[cmd.index("-filter:a") + 1]


class PocketGainTests(unittest.TestCase):
    def setUp(self):
        # Keep the suite off the real speech log.
        self.log_patch = mock.patch.object(VOICE_HOOK, "log")
        self.log = self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def _shape(self, speed, gain=0, ffmpeg_result="ok", wav=WAV):
        harness = _FfmpegHarness(ffmpeg_result)
        with mock.patch.object(VOICE_HOOK.subprocess, "run", side_effect=harness):
            result = VOICE_HOOK._wav_tempo(wav, speed, gain)
        return result, harness

    def test_speed_only_filter(self):
        result, harness = self._shape(1.4)
        self.assertEqual([SPEED_ONLY], [_filter_of(cmd) for cmd in harness.commands])
        self.assertEqual(WAV, harness.inputs[0])
        self.assertEqual(SHAPED, result)
        self.assertNotIn("volume=", SPEED_ONLY)
        self.assertNotIn("alimiter", SPEED_ONLY)
        self.log.assert_not_called()

        # Historical two-argument callers stay on the same filter.
        harness = _FfmpegHarness()
        with mock.patch.object(VOICE_HOOK.subprocess, "run", side_effect=harness):
            result = VOICE_HOOK._wav_tempo(WAV, 1.4)
        self.assertEqual([SPEED_ONLY], [_filter_of(cmd) for cmd in harness.commands])
        self.assertEqual(SHAPED, result)
        self.log.assert_not_called()

    def test_gain_only_filter(self):
        result, harness = self._shape(1.0, 5.2)
        self.assertEqual([GAIN_ONLY], [_filter_of(cmd) for cmd in harness.commands])
        self.assertEqual(SHAPED, result)
        self.log.assert_not_called()

    def test_speed_and_gain_share_one_pass(self):
        result, harness = self._shape(1.4, 5.2)
        self.assertEqual(1, len(harness.commands))
        self.assertEqual(BOTH, _filter_of(harness.commands[0]))
        self.assertEqual(SHAPED, result)
        self.log.assert_not_called()

    def test_negative_gain_has_no_limiter(self):
        _result, harness = self._shape(1.0, -5.2)
        self.assertEqual([NEGATIVE_GAIN], [_filter_of(cmd) for cmd in harness.commands])

    def test_neither_speed_nor_gain_skips_ffmpeg(self):
        for speed, gain in ((1.0, 0), (1.0, 0.0), (1.005, 0), (0, 0), ("0", "0")):
            with self.subTest(speed=speed, gain=gain):
                self.log.reset_mock()
                result, harness = self._shape(speed, gain)
                self.assertEqual([], harness.commands)
                self.assertIs(result, WAV)
                self.log.assert_not_called()

    def test_bad_gain_becomes_zero(self):
        for gain in ("loud", None, "", "5.2dB"):
            with self.subTest(gain=gain):
                self.log.reset_mock()
                result, harness = self._shape(1.4, gain)
                self.assertEqual([SPEED_ONLY], [_filter_of(cmd) for cmd in harness.commands])
                self.assertEqual(SHAPED, result)
                self.assertEqual(1, self.log.call_count)
                self.assertIn("not a number", self.log.call_args.args[0])
                self.assertIn("0 dB", self.log.call_args.args[0])

        self.log.reset_mock()
        result, harness = self._shape(1.0, "loud")
        self.assertEqual([], harness.commands)
        self.assertIs(result, WAV)
        self.assertEqual(1, self.log.call_count)

    def test_out_of_range_gain_becomes_zero(self):
        for gain in (20.01, -20.01, 21, -21, 1e6, float("inf"), float("-inf"), float("nan")):
            with self.subTest(gain=gain):
                self.log.reset_mock()
                result, harness = self._shape(1.0, gain)
                self.assertEqual([], harness.commands)
                self.assertIs(result, WAV)
                self.assertEqual(1, self.log.call_count)
                self.assertIn("outside -20..+20", self.log.call_args.args[0])
                self.assertIn("0 dB", self.log.call_args.args[0])

        for gain, expected in (
            (20, f"volume=20dB,{LIMITER}"),
            (-20, "volume=-20dB"),
        ):
            with self.subTest(gain=gain, bound="inclusive"):
                self.log.reset_mock()
                _result, harness = self._shape(1.0, gain)
                self.assertEqual([expected], [_filter_of(cmd) for cmd in harness.commands])
                self.log.assert_not_called()

    def test_ffmpeg_failure_returns_original_bytes(self):
        retry_filter = "atempo=1.400,volume=5.2dB"
        for ffmpeg_result in ("rc", "raise", "empty"):
            with self.subTest(ffmpeg_result=ffmpeg_result):
                self.log.reset_mock()
                result, harness = self._shape(1.4, 5.2, ffmpeg_result=ffmpeg_result)
                self.assertEqual(
                    [BOTH, retry_filter],
                    [_filter_of(cmd) for cmd in harness.commands],
                )
                self.assertIs(result, WAV)
                self.assertEqual(2, self.log.call_count)
                fallback, give_up = (call.args[0] for call in self.log.call_args_list)
                self.assertNotIn("\n", fallback)
                self.assertIn("alimiter failed", fallback)
                self.assertIn("retrying without alimiter", fallback)
                self.assertIn("playing unchanged", give_up)

        self.log.reset_mock()
        result, harness = self._shape(1.4, 0, ffmpeg_result="rc")
        self.assertEqual(1, len(harness.commands))
        self.assertIs(result, WAV)
        self.assertEqual(1, self.log.call_count)
        self.assertIn("pocket speed 1.4:", self.log.call_args.args[0])
        self.assertIn("playing at 1.0x", self.log.call_args.args[0])

        self.log.reset_mock()
        result, harness = self._shape(1.0, -5.2, ffmpeg_result="rc")
        self.assertEqual(
            [NEGATIVE_GAIN],
            [_filter_of(cmd) for cmd in harness.commands],
        )
        self.assertIs(result, WAV)
        self.assertEqual(1, self.log.call_count)
        self.assertIn("playing unchanged", self.log.call_args.args[0])

    def test_limiter_failure_retries_once_without_alimiter(self):
        class Harness:
            def __init__(self):
                self.commands = []

            def __call__(self, cmd, input=None, capture_output=None, timeout=None):
                self.commands.append(list(cmd))
                self.assert_timeout = timeout
                out = cmd[-1]
                if "alimiter" in _filter_of(cmd):
                    return subprocess.CompletedProcess(cmd, 7, b"", b"nope")
                with open(out, "wb") as handle:
                    handle.write(SHAPED)
                return subprocess.CompletedProcess(cmd, 0, b"", b"")

        for speed, gain, second in (
            (1.4, 5.2, "atempo=1.400,volume=5.2dB"),
            (1.0, 5.2, "volume=5.2dB"),
        ):
            with self.subTest(speed=speed, gain=gain):
                self.log.reset_mock()
                harness = Harness()
                with mock.patch.object(VOICE_HOOK.subprocess, "run", side_effect=harness):
                    result = VOICE_HOOK._wav_tempo(WAV, speed, gain)
                self.assertEqual(SHAPED, result)
                self.assertEqual(2, len(harness.commands))
                self.assertEqual(10, harness.assert_timeout)
                self.assertIn("alimiter", _filter_of(harness.commands[0]))
                self.assertEqual(second, _filter_of(harness.commands[1]))
                self.assertNotIn("alimiter", second)
                self.log.assert_called_once()
                line = self.log.call_args.args[0]
                self.assertNotIn("\n", line)
                self.assertIn("retrying without alimiter", line)

    def test_real_ffmpeg_gain_raises_rms_without_clipping(self):
        if not _ffmpeg_available():
            self.skipTest("ffmpeg is not installed")
        source = _sine_wav()
        shaped = VOICE_HOOK._wav_tempo(source, 1.0, 5.2)
        self.log.assert_not_called()
        in_rms, in_peak = _wav_stats(source)
        out_rms, out_peak = _wav_stats(shaped)
        delta_db = 20 * math.log10(out_rms / in_rms)
        self.assertGreater(delta_db, 5.2 - 1.5)
        self.assertLess(delta_db, 5.2 + 1.5)
        self.assertLess(out_peak, 32767)
        self.assertGreater(out_peak, in_peak)

    def test_real_ffmpeg_positive_gain_keeps_duration_tail_and_level(self):
        if not _ffmpeg_available():
            self.skipTest("ffmpeg is not installed")
        source = _sine_wav(seconds=0.25, click_amplitude=0.95)
        shaped = VOICE_HOOK._wav_tempo(source, 1.0, 5.2)
        self.log.assert_not_called()

        def frames_of(wav_bytes):
            with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
                return reader.getframerate(), reader.getnframes(), reader.readframes(reader.getnframes())

        in_rate, in_frames, in_pcm = frames_of(source)
        out_rate, out_frames, out_pcm = frames_of(shaped)
        self.assertEqual(in_rate, out_rate)
        # Sample-count duration within 1 ms.
        self.assertLessEqual(abs(out_frames - in_frames) * 1000, in_rate)

        def samples_of(pcm):
            return struct.unpack("<" + "h" * (len(pcm) // 2), pcm)

        in_samples = samples_of(in_pcm)
        out_samples = samples_of(out_pcm)
        tail_count = max(1, int(in_rate * 0.003))
        in_tail_peak = max(abs(sample) for sample in in_samples[-tail_count:])
        out_tail_peak = max(abs(sample) for sample in out_samples[-tail_count:])
        body_peak = max(abs(sample) for sample in out_samples[: len(out_samples) // 2])
        self.assertGreater(in_tail_peak, 30000)
        self.assertGreater(out_tail_peak, 30000)
        self.assertGreater(out_tail_peak, body_peak)

        in_rms, _in_peak = _wav_stats(source)
        out_rms, _out_peak = _wav_stats(shaped)
        delta_db = 20 * math.log10(out_rms / in_rms)
        self.assertGreater(delta_db, 5.2 - 1.5)
        self.assertLess(delta_db, 5.2 + 1.5)

    def test_speak_pocket_forwards_gain_into_the_same_shaper(self):
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 400)
        with mock.patch.object(VOICE_HOOK, "_pocket_fetch", return_value=wav), \
                mock.patch.object(
                    VOICE_HOOK, "_wav_tempo",
                    side_effect=lambda audio, speed, gain_db=0: audio,
                ) as shape, \
                mock.patch.object(VOICE_HOOK, "play_audio_file"):
            self.assertTrue(VOICE_HOOK.speak_pocket(
                "Hello.", "case-o", play_local=True, speed=1.4, gain_db=5.2,
            ))
        shape.assert_called_once_with(wav, 1.4, 5.2)

    def test_english_and_dutch_callers_pass_configured_gain(self):
        english_cfg = {
            "source_machine": "day",
            "tts_engine": "pocket",
            "dutch_speech": {"enabled": False},
            "pocket_tts_url": "http://127.0.0.1:8933",
            "pocket_speed": 1.4,
            "pocket_gain_db": 5.2,
        }
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "speak_pocket", return_value=True
        ) as pocket, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ):
            VOICE_HOOK.speak("The build finished cleanly.", english_cfg, lang_hint="en")
        self.assertEqual(1.4, pocket.call_args.kwargs["speed"])
        self.assertEqual(5.2, pocket.call_args.kwargs["gain_db"])

        dutch_cfg = {
            "source_machine": "day",
            "tts_engine": "edge",
            "dutch_speech": {"enabled": True, "timeout_s": 14},
            "pocket_speed": 1.4,
            "pocket_gain_db": 5.2,
        }
        audio = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=("http://receiver/tts", False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value="omnivoice:tts"
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=audio
        ), mock.patch.object(
            VOICE_HOOK, "_wav_tempo",
            side_effect=lambda wav, speed, gain_db=0: wav,
        ) as shape, mock.patch.object(
            VOICE_HOOK, "_precheck_dutch_audio"
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=True
        ), mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ):
            VOICE_HOOK.speak("Dit is een Nederlandse zin.", dutch_cfg, lang_hint="nl")
        shape.assert_called_once_with(audio, 1.4, 5.2)

    def test_unset_loudness_stays_at_unity_and_explicit_values_win(self):
        cases = (
            ({}, 1.0, 0),
            ({"pocket_speed": 1.0, "pocket_gain_db": 0}, 1.0, 0),
            ({"pocket_speed": 1.4, "pocket_gain_db": 5.2}, 1.4, 5.2),
        )
        for updates, speed, gain in cases:
            with self.subTest(updates=updates):
                english_cfg = {
                    "source_machine": "day",
                    "tts_engine": "pocket",
                    "dutch_speech": {"enabled": False},
                    "pocket_tts_url": "http://127.0.0.1:8933",
                }
                english_cfg.update(updates)
                with mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                ), mock.patch.object(
                    VOICE_HOOK, "speak_pocket", return_value=True
                ) as pocket, mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ):
                    VOICE_HOOK.speak("The build finished cleanly.", english_cfg, lang_hint="en")
                self.assertEqual(speed, pocket.call_args.kwargs["speed"])
                self.assertEqual(gain, pocket.call_args.kwargs["gain_db"])

                dutch_cfg = {
                    "source_machine": "day",
                    "tts_engine": "edge",
                    "dutch_speech": {"enabled": True, "timeout_s": 14},
                }
                dutch_cfg.update(updates)
                audio = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
                with mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=("http://receiver/tts", False)
                ), mock.patch.object(
                    VOICE_HOOK, "resolve_dutch_speech_engine", return_value="omnivoice:tts"
                ), mock.patch.object(
                    VOICE_HOOK, "_omnivoice_fetch", return_value=audio
                ), mock.patch.object(
                    VOICE_HOOK, "_wav_tempo",
                    side_effect=lambda wav, wav_speed, gain_db=0: wav,
                ) as shape, mock.patch.object(
                    VOICE_HOOK, "_precheck_dutch_audio"
                ), mock.patch.object(
                    VOICE_HOOK, "send_audio_remote", return_value=True
                ), mock.patch.object(
                    VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
                ):
                    VOICE_HOOK.speak("Dit is een Nederlandse zin.", dutch_cfg, lang_hint="nl")
                shape.assert_called_once_with(audio, speed, gain)

    def test_example_config_keeps_unity_defaults_and_voxtype_wait(self):
        example = json.loads(
            (Path(__file__).parents[1] / "scripts" / "config.example.json").read_text()
        )
        self.assertEqual(1.0, example["pocket_speed"])
        self.assertEqual(0, example["pocket_gain_db"])
        self.assertIs(True, example["voxtype_wait"])

    def test_unset_keys_skip_ffmpeg_on_english_and_dutch_callers(self):
        self.assertEqual(1.0, VOICE_HOOK.DEFAULT_POCKET_SPEED)
        self.assertEqual(0, VOICE_HOOK.DEFAULT_POCKET_GAIN_DB)
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
        english_cfg = {
            "source_machine": "day",
            "tts_engine": "pocket",
            "dutch_speech": {"enabled": False},
            "pocket_tts_url": "http://127.0.0.1:8933",
            "tts_voice_pocket_content": "aragorn",
        }
        with mock.patch.dict(
            VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=wav
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run"
        ) as run, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ):
            VOICE_HOOK.speak("The build finished cleanly.", english_cfg, lang_hint="en")
        run.assert_not_called()
        fetch.assert_called_once_with(
            "The build finished cleanly.", "case-o", "http://127.0.0.1:8933", 27
        )

        dutch_cfg = {
            "source_machine": "day",
            "tts_engine": "edge",
            "dutch_speech": {"enabled": True, "timeout_s": 14},
            "tts_voice_pocket_content": "aragorn",
        }
        with mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=("http://receiver/tts", False)
        ), mock.patch.object(
            VOICE_HOOK, "resolve_dutch_speech_engine", return_value="omnivoice:tts"
        ), mock.patch.object(
            VOICE_HOOK, "_omnivoice_fetch", return_value=wav
        ), mock.patch.object(
            VOICE_HOOK, "_precheck_dutch_audio"
        ), mock.patch.object(
            VOICE_HOOK, "send_audio_remote", return_value=True
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run"
        ) as run, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=mock.AsyncMock()
        ):
            VOICE_HOOK.speak("Dit is een Nederlandse zin.", dutch_cfg, lang_hint="nl")
        run.assert_not_called()

    def _persona_lines(self):
        return [
            call.args[0] for call in self.log.call_args_list
            if call.args and str(call.args[0]).startswith("pocket: persona")
        ]

    def test_successful_shelby_persona_is_not_retried(self):
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
        with mock.patch.dict(
            VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", return_value=wav
        ) as fetch, mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ):
            self.assertTrue(VOICE_HOOK.speak_pocket(
                "Hello.", "case-o", play_local=True, speed=1.0, gain_db=0,
            ))
        fetch.assert_called_once_with("Hello.", "case-o", "http://127.0.0.1:8933", 27)
        self.assertEqual([], self._persona_lines())

    def test_persona_fetch_failure_retries_jarvis_once_on_the_same_server(self):
        primary = "http://127.0.0.1:8933"
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
        failures = {
            "http": urllib.error.HTTPError(primary + "/tts", 404, "missing", None, None),
            "empty": b"",
            "short": b"not-audio",
        }
        for name, failure in failures.items():
            with self.subTest(failure=name):
                self.log.reset_mock()
                calls = []

                def urlopen(req, timeout=None, _failure=failure):
                    voice = json.loads(req.data.decode())["voice"]
                    calls.append((voice, req.full_url, timeout))
                    if voice == "aragorn":
                        raise AssertionError("content voice used")
                    if voice == "case-o":
                        if isinstance(_failure, BaseException):
                            raise _failure
                        return mock.Mock(read=lambda: _failure)
                    if voice == "jarvis":
                        return mock.Mock(read=lambda: wav)
                    raise AssertionError(voice)

                with mock.patch.dict(
                    VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=urlopen
                ), mock.patch.object(
                    VOICE_HOOK, "play_audio_file"
                ):
                    self.assertTrue(VOICE_HOOK.speak_pocket(
                        "Hello.", "case-o", play_local=True, base_url=primary,
                        fallback_base_url="http://127.0.0.1:8999",
                        speed=1.0, gain_db=0,
                    ))
                self.assertEqual(
                    [("case-o", primary + "/tts", 27), ("jarvis", primary + "/tts", 27)],
                    calls,
                )
                self.assertEqual(
                    [f"pocket: persona case-o unavailable on {primary}, using jarvis"],
                    self._persona_lines(),
                )

    def test_jarvis_retry_is_once_then_the_next_server_and_edge(self):
        primary = "http://127.0.0.1:8933"
        fallback = "http://100.77.19.108:8933"
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
        calls = []

        def fetch(text, voice, url, timeout):
            calls.append((voice, url, timeout))
            if voice not in {"case-o", "jarvis"}:
                raise AssertionError(f"content voice used: {voice}")
            if (voice, url) == ("case-o", fallback):
                return wav
            return None

        with mock.patch.dict(
            VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", side_effect=fetch
        ), mock.patch.object(
            VOICE_HOOK, "play_audio_file"
        ):
            self.assertTrue(VOICE_HOOK.speak_pocket(
                "Hello.", "case-o", play_local=True, base_url=primary,
                fallback_base_url=fallback, speed=1.0, gain_db=0,
            ))
        self.assertEqual(
            [
                ("case-o", primary, 27),
                ("jarvis", primary, 27),
                ("case-o", fallback, 27),
            ],
            calls,
        )
        self.assertEqual(
            [f"pocket: persona case-o unavailable on {primary}, using jarvis"],
            self._persona_lines(),
        )

        calls.clear()
        self.log.reset_mock()
        edge = mock.AsyncMock()
        cfg = {
            "source_machine": "day",
            "tts_engine": "pocket",
            "dutch_speech": {"enabled": False},
            "pocket_tts_url": primary,
            "tts_voice_pocket_en": "case-o",
            "tts_voice_pocket_content": "VoiceClone",
            "engine_fallback": {"pocket": "edge"},
        }
        with mock.patch.dict(
            VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}, clear=False
        ), mock.patch.object(
            VOICE_HOOK, "find_audible_path", return_value=(None, True)
        ), mock.patch.object(
            VOICE_HOOK, "_pocket_fetch", side_effect=lambda *_args, **_kwargs: None
        ) as fetch_mock, mock.patch.object(
            VOICE_HOOK, "speak_edge", new=edge
        ):
            VOICE_HOOK.speak("The build finished cleanly.", cfg, lang_hint="en")
        self.assertEqual(
            [
                ("The build finished cleanly.", "case-o", primary, 27),
                ("The build finished cleanly.", "jarvis", primary, 27),
            ],
            [call.args for call in fetch_mock.call_args_list],
        )
        self.assertNotIn("VoiceClone", [call.args[1] for call in fetch_mock.call_args_list])
        self.assertEqual(
            [f"pocket: persona case-o unavailable on {primary}, using jarvis"],
            self._persona_lines(),
        )
        edge.assert_awaited_once()
        self.assertEqual("The build finished cleanly.", edge.await_args.args[0])

    def test_jarvis_and_content_voices_do_not_gain_an_extra_fallback(self):
        primary = "http://127.0.0.1:8933"
        fallback = "http://100.77.19.108:8933"

        def run(voice):
            calls = []

            def fetch(_text, requested, url, timeout):
                calls.append((requested, url, timeout))
                return None

            self.log.reset_mock()
            with mock.patch.dict(
                VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
            ), mock.patch.object(
                VOICE_HOOK, "_pocket_fetch", side_effect=fetch
            ):
                self.assertFalse(VOICE_HOOK.speak_pocket(
                    "Hello.", voice, play_local=False, base_url=primary,
                    fallback_base_url=fallback, speed=1.0, gain_db=0,
                ))
            return calls

        self.assertEqual(
            [("jarvis", primary, 27), ("jarvis", fallback, 27)],
            run("jarvis"),
        )
        self.assertEqual([], self._persona_lines())
        content_calls = run("aragorn")
        self.assertEqual(
            [("aragorn", primary, 27), ("aragorn", fallback, 27)],
            content_calls,
        )
        self.assertEqual([], self._persona_lines())
        self.assertNotIn("jarvis", [voice for voice, _url, _timeout in content_calls])

    def test_transport_failure_skips_jarvis_and_tries_the_next_server(self):
        primary = "http://127.0.0.1:8933"
        fallback = "http://100.77.19.108:8933"
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
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
        for name, make_failure in cases.items():
            with self.subTest(failure=name):
                self.log.reset_mock()
                calls = []
                failure = make_failure()

                def urlopen(req, timeout=None, _failure=failure):
                    voice = json.loads(req.data.decode())["voice"]
                    calls.append((voice, req.full_url, timeout))
                    if voice == "jarvis":
                        raise AssertionError("jarvis retry on a transport failure")
                    if req.full_url.startswith(fallback):
                        return mock.Mock(read=lambda: wav)
                    raise _failure

                with mock.patch.dict(
                    VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=urlopen
                ), mock.patch.object(
                    VOICE_HOOK, "play_audio_file"
                ):
                    self.assertTrue(VOICE_HOOK.speak_pocket(
                        "Hello.", "case-o", play_local=True, base_url=primary,
                        fallback_base_url=fallback, speed=1.0, gain_db=0,
                    ))
                self.assertEqual(
                    [
                        ("case-o", primary + "/tts", 27),
                        ("case-o", fallback + "/tts", 27),
                    ],
                    calls,
                )
                self.assertEqual([], self._persona_lines())

    def test_answered_failure_retries_jarvis_once_on_the_same_server(self):
        primary = "http://127.0.0.1:8933"
        fallback = "http://100.77.19.108:8933"
        wav = VOICE_HOOK.make_wav(b"\x01\x00" * 600)
        cases = {
            "http-404": lambda: urllib.error.HTTPError(
                primary + "/tts", 404, "missing", None, None
            ),
            "http-500": lambda: urllib.error.HTTPError(
                primary + "/tts", 500, "error", None, None
            ),
            "short-body": lambda: b"x" * 999,
        }
        for name, make_failure in cases.items():
            with self.subTest(failure=name):
                self.log.reset_mock()
                calls = []
                failure = make_failure()

                def urlopen(req, timeout=None, _failure=failure):
                    voice = json.loads(req.data.decode())["voice"]
                    calls.append((voice, req.full_url, timeout))
                    if voice == "aragorn":
                        raise AssertionError("content voice used")
                    if req.full_url.startswith(fallback):
                        raise AssertionError("next server used after an answer")
                    if voice == "case-o":
                        if isinstance(_failure, BaseException):
                            raise _failure
                        return mock.Mock(read=lambda: _failure)
                    if voice == "jarvis":
                        return mock.Mock(read=lambda: wav)
                    raise AssertionError(voice)

                with mock.patch.dict(
                    VOICE_HOOK.os.environ, {"SHELBY_POCKET_TIMEOUT": "27"}
                ), mock.patch(
                    "urllib.request.urlopen", side_effect=urlopen
                ), mock.patch.object(
                    VOICE_HOOK, "play_audio_file"
                ):
                    self.assertTrue(VOICE_HOOK.speak_pocket(
                        "Hello.", "case-o", play_local=True, base_url=primary,
                        fallback_base_url=fallback, speed=1.0, gain_db=0,
                    ))
                self.assertEqual(
                    [("case-o", primary + "/tts", 27), ("jarvis", primary + "/tts", 27)],
                    calls,
                )
                self.assertEqual(
                    [f"pocket: persona case-o unavailable on {primary}, using jarvis"],
                    self._persona_lines(),
                )


class PocketGainLogIsolationTests(unittest.TestCase):
    def test_failure_log_does_not_touch_the_real_speech_log(self):
        real_log = Path("/tmp/claude-tts.log")
        before = real_log.stat().st_size if real_log.exists() else None
        harness = _FfmpegHarness("raise")
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "claude-tts.log"
            with mock.patch.object(VOICE_HOOK, "LOG_PATH", str(log_path)), \
                    mock.patch.object(VOICE_HOOK.subprocess, "run", side_effect=harness):
                result = VOICE_HOOK._wav_tempo(WAV, 1.4, 5.2)
            self.assertIs(result, WAV)
            self.assertIn("playing unchanged", log_path.read_text())
        after = real_log.stat().st_size if real_log.exists() else None
        self.assertEqual(before, after)
