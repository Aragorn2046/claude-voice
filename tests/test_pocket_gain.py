"""Pocket loudness (`pocket_gain_db`) shares the pocket_speed ffmpeg pass."""

import importlib.util
import io
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
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
        for ffmpeg_result in ("rc", "raise", "empty"):
            with self.subTest(ffmpeg_result=ffmpeg_result):
                self.log.reset_mock()
                result, harness = self._shape(1.4, 5.2, ffmpeg_result=ffmpeg_result)
                self.assertEqual(1, len(harness.commands))
                self.assertEqual(BOTH, _filter_of(harness.commands[0]))
                self.assertIs(result, WAV)
                self.log.assert_called_once()

        self.log.reset_mock()
        result, harness = self._shape(1.4, 0, ffmpeg_result="rc")
        self.assertIs(result, WAV)
        self.assertIn("pocket speed 1.4:", self.log.call_args.args[0])
        self.assertIn("playing at 1.0x", self.log.call_args.args[0])

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

    def test_unset_loudness_matches_popup_and_explicit_values_win(self):
        cases = (
            ({}, 1.4, 5.2),
            ({"pocket_speed": 1.0, "pocket_gain_db": 0}, 1.0, 0),
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
