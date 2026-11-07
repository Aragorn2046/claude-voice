"""Final presence check, immediately before local playback.

Fakes only. No audio device, no network, no real case-presence daemon.
"""

import builtins
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

import numpy as np


ROOT = Path(__file__).parents[1]
HOOK_PATH = ROOT / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location(
    "voice_stop_hook_presence_final", HOOK_PATH,
)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)

SENTENCE = "The build finished cleanly."
HELD_LOG = "presence gate: held before playback"
PCM = b"\x00\x00" * 16


def _soundfile():
    """soundfile is not installed here. Playback only needs read()."""
    module = ModuleType("soundfile")

    def read(*_args, **_kwargs):
        return (np.zeros(16, dtype=np.float64), 24000)

    module.read = read
    return module


def _holding_gate():
    calls = []

    def should_hold(cfg, text, queue=True):
        calls.append((cfg, text, queue))
        return True

    gate = ModuleType("voice_presence_gate")
    gate.should_hold = should_hold
    gate.calls = calls
    return gate


class PresenceFinalCheckTest(unittest.TestCase):
    def _cfg(self, **extra):
        cfg = {
            "tts_enabled": True,
            "case_presence_gate": True,
            "tts_engine": "pocket",
            "source_machine": "day",
            "tts_voice_pocket_en": "case-o",
            "pocket_tts_url": "http://127.0.0.1:8933",
        }
        cfg.update(extra)
        return cfg

    def _play_through_main(self, cfg, decide=None, break_import=False):
        """Speak one line through main() up to the local player.

        Pocket synthesis and soundfile are fakes. ``decide`` is should_hold.
        ``break_import`` makes importing the gate raise.
        """
        payload = json.dumps({
            "session_id": "presence-final",
            "last_assistant_message": f"<voice>{SENTENCE}</voice>",
        })
        players = []
        queued = []
        calls = []
        logs = []
        gate_imports = []
        lock = mock.Mock(name="lock")
        release = mock.Mock()

        def should_hold(seen_cfg, text, queue=True):
            held = decide(seen_cfg, text, queue=queue)
            calls.append((seen_cfg, text, queue, held))
            if held and queue:
                queued.append(text)
            return held

        def run(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv and argv[0] in {"paplay", "afplay"}:
                players.append(list(argv))
            completed = mock.Mock()
            completed.returncode = 0
            return completed

        def popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and argv:
                players.append(list(argv))
            proc = mock.Mock()
            proc.poll.return_value = 0
            proc.returncode = 0
            proc.wait.return_value = 0
            proc.stdin = mock.Mock()
            return proc

        gate = ModuleType("voice_presence_gate")
        gate.should_hold = should_hold
        modules = {"soundfile": _soundfile()}
        if not break_import:
            modules["voice_presence_gate"] = gate

        real_import = builtins.__import__

        def load_gate():
            gate_imports.append("voice_presence_gate")
            if break_import:
                raise RuntimeError("gate down")
            return gate

        wav = VOICE_HOOK.make_wav(b"\x00\x00" * 200)
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(
                os.environ, {
                    "CLAUDE_SESSION_TYPE": "main",
                    "SHELBY_VOXTYPE_STATE_FILE": "/tmp/presence-final-no-such-state",
                },
            ), mock.patch.object(
                VOICE_HOOK, "load_config", return_value=cfg,
            ), mock.patch.object(
                VOICE_HOOK, "acquire_lock", return_value=lock,
            ), mock.patch.object(
                VOICE_HOOK, "release_lock", release,
            ), mock.patch.object(
                VOICE_HOOK, "log", side_effect=logs.append,
            ), mock.patch.object(
                VOICE_HOOK, "find_audible_path", return_value=(None, True),
            ), mock.patch.object(
                VOICE_HOOK, "enforce_english_speech", return_value="TRANSLATED",
            ), mock.patch.object(
                VOICE_HOOK, "_pocket_fetch", return_value=wav,
            ), mock.patch.object(
                VOICE_HOOK, "_wav_tempo",
                side_effect=lambda audio, speed, gain_db=0: audio,
            ), mock.patch.object(
                VOICE_HOOK, "voxtype_present", return_value=False,
            ), mock.patch.object(
                VOICE_HOOK, "IS_MACOS", False,
            ), mock.patch.object(
                VOICE_HOOK.subprocess, "run", side_effect=run,
            ), mock.patch.object(
                VOICE_HOOK.subprocess, "Popen", side_effect=popen,
            ), mock.patch.object(
                VOICE_HOOK.sys, "stdin", io.StringIO(payload),
            ), mock.patch.object(
                VOICE_HOOK, "_load_presence_gate", side_effect=load_gate,
            ):
                VOICE_HOOK.main()
        finally:
            signal.signal(signal.SIGTERM, previous)

        return SimpleNamespace(
            players=players,
            queued=queued,
            calls=calls,
            logs=logs,
            release=release,
            lock=lock,
            gate_imports=gate_imports,
        )

    def test_hold_flips_after_lock_skips_player_and_queues_once(self):
        answers = iter((False, False, True))

        def decide(_cfg, _text, queue=True):
            return next(answers)

        result = self._play_through_main(self._cfg(), decide)
        self.assertEqual(
            [held for _cfg, _text, _queue, held in result.calls],
            [False, False, True],
        )
        self.assertEqual(
            [text for _cfg, text, _queue, _held in result.calls],
            [SENTENCE, SENTENCE, SENTENCE],
        )
        self.assertTrue(all(queue for _cfg, _text, queue, _held in result.calls))
        self.assertEqual(result.queued, [SENTENCE])
        self.assertEqual(result.players, [])
        self.assertIn(HELD_LOG, result.logs)
        result.release.assert_called_once_with(result.lock)

    def test_hold_stays_false_starts_player_once_and_queues_nothing(self):
        def decide(_cfg, _text, queue=True):
            return False

        result = self._play_through_main(self._cfg(), decide)
        self.assertEqual(len(result.calls), 3)
        self.assertTrue(all(not held for _cfg, _text, _queue, held in result.calls))
        self.assertEqual(result.queued, [])
        self.assertEqual(len(result.players), 1)
        self.assertEqual(result.players[0][0], "paplay")
        self.assertNotIn(HELD_LOG, result.logs)
        result.release.assert_called_once_with(result.lock)

    def test_gate_module_raises_fail_open_and_player_starts(self):
        result = self._play_through_main(self._cfg(), break_import=True)
        # Two pre-playback checks plus the final one. All three fail open.
        self.assertEqual(result.gate_imports, ["voice_presence_gate"] * 3)
        self.assertEqual(result.queued, [])
        self.assertEqual(len(result.players), 1)
        self.assertEqual(result.players[0][0], "paplay")
        self.assertNotIn(HELD_LOG, result.logs)
        result.release.assert_called_once_with(result.lock)

    def test_gate_off_does_not_call_should_hold_and_player_starts(self):
        cases = (
            {"case_presence_gate": False},
            {},
        )
        for overrides in cases:
            with self.subTest(cfg=overrides):
                imported = []
                real_import = builtins.__import__

                def tracking(name, *args, **kwargs):
                    if name == "voice_presence_gate":
                        imported.append(name)
                    return real_import(name, *args, **kwargs)

                def boom(*_args, **_kwargs):
                    raise AssertionError("should_hold was called")

                gate = ModuleType("voice_presence_gate")
                gate.should_hold = boom
                cfg = overrides
                with mock.patch.dict(
                    sys.modules, {"voice_presence_gate": gate},
                ), mock.patch(
                    "builtins.__import__", side_effect=tracking,
                ), mock.patch.object(
                    VOICE_HOOK, "IS_MACOS", False,
                ), mock.patch.object(
                    VOICE_HOOK, "voxtype_present", return_value=False,
                ), mock.patch.object(
                    VOICE_HOOK.subprocess, "run",
                ) as run:
                    VOICE_HOOK.play_raw_pcm(PCM, 24000, 1, cfg=cfg, line_text=SENTENCE)
                self.assertEqual(imported, [])
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][0], "paplay")

    def _assert_held_once(self, gate, cfg, line_text="clean line"):
        self.assertEqual(gate.calls, [(cfg, line_text, True)])

    def test_voxtype_gated_paplay_not_started_when_held(self):
        cfg = {"case_presence_gate": True}
        gate = _holding_gate()
        order = []

        def wait(*_args, **_kwargs):
            order.append("wait")
            return (0.0, False)

        real_should_hold = gate.should_hold

        def should_hold(seen_cfg, text, queue=True):
            order.append("hold")
            return real_should_hold(seen_cfg, text, queue=queue)

        gate.should_hold = should_hold
        logs = []
        with mock.patch.object(VOICE_HOOK, "_load_presence_gate", return_value=gate), mock.patch.object(
            VOICE_HOOK, "_wait_for_voxtype_idle_status", side_effect=wait,
        ), mock.patch.object(
            VOICE_HOOK, "_play_one_paplay",
        ) as play, mock.patch.object(
            VOICE_HOOK, "log", side_effect=logs.append,
        ):
            VOICE_HOOK._play_pcm_voxtype_gated(
                PCM, 24000, 1, cfg=cfg, line_text="clean line",
            )
        self.assertEqual(order, ["wait", "hold"])
        play.assert_not_called()
        self._assert_held_once(gate, cfg)
        self.assertIn(HELD_LOG, logs)

    def test_raw_paplay_not_started_when_held(self):
        cfg = {"case_presence_gate": True}
        gate = _holding_gate()
        logs = []
        with mock.patch.object(VOICE_HOOK, "_load_presence_gate", return_value=gate), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", False,
        ), mock.patch.object(
            VOICE_HOOK, "voxtype_present", return_value=False,
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run",
        ) as run, mock.patch.object(
            VOICE_HOOK, "log", side_effect=logs.append,
        ):
            VOICE_HOOK.play_raw_pcm(PCM, 24000, 1, cfg=cfg, line_text="clean line")
        run.assert_not_called()
        self._assert_held_once(gate, cfg)
        self.assertIn(HELD_LOG, logs)

    def test_streaming_paplay_not_started_when_held(self):
        cfg = {"case_presence_gate": True}
        gate = _holding_gate()
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = lambda chunk_size=4096: iter([b"\x01\x02"])
        requests_mod = mock.Mock()
        requests_mod.post.return_value = response
        logs = []
        with mock.patch.dict(
            sys.modules, {"requests": requests_mod},
        ), mock.patch.object(VOICE_HOOK, "_load_presence_gate", return_value=gate), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", False,
        ), mock.patch.object(
            VOICE_HOOK, "voxtype_present", return_value=False,
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "Popen",
        ) as popen, mock.patch.object(
            VOICE_HOOK, "log", side_effect=logs.append,
        ):
            VOICE_HOOK.speak_elevenlabs_streaming(
                "engine text", "voice", "model", "key",
                cfg=cfg, line_text="clean line",
            )
        requests_mod.post.assert_called_once()
        response.iter_content.assert_not_called()
        popen.assert_not_called()
        self._assert_held_once(gate, cfg)
        self.assertIn(HELD_LOG, logs)

    def test_macos_raw_afplay_not_started_when_held(self):
        cfg = {"case_presence_gate": True}
        gate = _holding_gate()
        logs = []
        with mock.patch.object(VOICE_HOOK, "_load_presence_gate", return_value=gate), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", True,
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run",
        ) as run, mock.patch.object(
            VOICE_HOOK, "log", side_effect=logs.append,
        ):
            VOICE_HOOK.play_raw_pcm(PCM, 24000, 1, cfg=cfg, line_text="clean line")
        run.assert_not_called()
        self._assert_held_once(gate, cfg)
        self.assertIn(HELD_LOG, logs)

    def test_macos_file_player_not_started_when_held(self):
        cfg = {"case_presence_gate": True}
        gate = _holding_gate()
        logs = []
        with mock.patch.object(VOICE_HOOK, "_load_presence_gate", return_value=gate), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", True,
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run",
        ) as run, mock.patch.object(
            VOICE_HOOK, "log", side_effect=logs.append,
        ):
            VOICE_HOOK.play_audio_file(
                "/tmp/presence-final-no-such-file.wav",
                cfg=cfg, line_text="clean line",
            )
        run.assert_not_called()
        self._assert_held_once(gate, cfg)
        self.assertIn(HELD_LOG, logs)

    def test_linux_wav_player_not_started_when_held(self):
        cfg = {"case_presence_gate": True}
        gate = _holding_gate()
        logs = []
        with mock.patch.dict(
            sys.modules, {"soundfile": _soundfile()},
        ), mock.patch.object(VOICE_HOOK, "_load_presence_gate", return_value=gate), mock.patch.object(
            VOICE_HOOK, "IS_MACOS", False,
        ), mock.patch.object(
            VOICE_HOOK, "voxtype_present", return_value=False,
        ), mock.patch.object(
            VOICE_HOOK.subprocess, "run",
        ) as run, mock.patch.object(
            VOICE_HOOK, "log", side_effect=logs.append,
        ):
            VOICE_HOOK.play_audio_file(
                "/tmp/presence-final-no-such-file.wav",
                cfg=cfg, line_text="clean line",
            )
        run.assert_not_called()
        self._assert_held_once(gate, cfg)
        self.assertIn(HELD_LOG, logs)


if __name__ == "__main__":
    unittest.main()
