"""Freshness, isolation, replay, and queue durability with fake publishers."""

import builtins
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = load("round9_gate", ROOT / "scripts/voice_presence_gate.py")
HOOK = load("round9_hook", ROOT / "scripts/voice-stop-hook.py")
NOW = 1000.0
CMD = b"python3\0/opt/case-presence\0daemon\0"
MISSING = object()


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
class PresenceRound9(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.presence = self.root / "case-presence"
        self.presence.mkdir()
        self.held = self.presence / "held-voice.jsonl"
        self.pid = os.getpid()
        self.start = GATE._proc_starttime(self.pid)
        self.assertIsInstance(self.start, int)

    def state(self, *, stamp=NOW, pid_start=True, hold=True):
        data = {"pid": self.pid, "hold_voice": hold}
        if stamp is not MISSING:
            data["updated_at"] = stamp
        if pid_start:
            data["pid_start"] = self.start
        (self.presence / "state.json").write_text(json.dumps(data))

    def check(self, text="hello", reader=None):
        return GATE.should_hold(
            {"case_presence_gate": True}, text, runtime_dir=str(self.root),
            now=NOW, cmdline_reader=reader or (lambda _pid: CMD),
        )

    def test_non_true_holds_with_matching_start_do_not_queue(self):
        for value in (False, None, 1, "true"):
            with self.subTest(value=value):
                self.state(hold=value)
                self.assertFalse(self.check())
                self.assertFalse(self.held.exists())

    def test_freshness_with_and_without_start(self):
        cases = ((NOW - 10, True), (NOW - 31, False), (NOW + 6, False),
                 (MISSING, False), ("1000", False), (float("nan"), False),
                 (float("inf"), False), (True, False), (NOW - 30, True),
                 (NOW + 5, True))
        for start in (True, False):
            for stamp, expected in cases:
                with self.subTest(start=start, stamp=stamp):
                    self.state(stamp=stamp, pid_start=start)
                    result = GATE.should_hold(
                        {"case_presence_gate": True}, "test", runtime_dir=str(self.root),
                        now=NOW, cmdline_reader=lambda _pid: CMD, queue=False,
                    )
                    self.assertEqual(result, expected)
        self.assertFalse(self.held.exists())

    def test_cmdline_checked_even_when_start_matches(self):
        self.state()
        for cmd in (b"nvim\0/opt/case-presence\0", b"python3\0-c\0pass\0",
                    b"/usr/bin/unrelated\0"):
            with self.subTest(cmd=cmd):
                reader = mock.Mock(return_value=cmd)
                self.assertFalse(self.check(reader=reader))
                reader.assert_called_once_with(self.pid)
                self.assertFalse(self.held.exists())

    def test_fresh_cmdline_identity_and_positive_control(self):
        tail = b"/case-presence"
        truncated = b"x" * (GATE._CMDLINE_READ_MAX - len(tail)) + tail
        self.assertEqual(len(truncated), GATE._CMDLINE_READ_MAX)
        commands = (
            b"",
            b"/usr/local/bin/case-presence-helper\0",
            b"/opt/case-presence/helper\0",
            truncated,
        )
        for stamp in (NOW, NOW - 10):
            for cmd in commands:
                with self.subTest(stamp=stamp, cmd=cmd[:50]):
                    self.state(stamp=stamp)
                    reader = mock.Mock(return_value=cmd)
                    self.assertFalse(self.check(reader=reader))
                    reader.assert_called_once_with(self.pid)
                    self.assertFalse(self.held.exists())
            with self.subTest(stamp=stamp, positive=True):
                self.state(stamp=stamp)
                reader = mock.Mock(return_value=(
                    b"/usr/bin/python3\0/home/aragorn/.local/bin/case-presence\0daemon\0"
                ))
                self.assertTrue(self.check(reader=reader))
                reader.assert_called_once_with(self.pid)
                self.assertTrue(self.held.exists())
                self.held.unlink()

    def test_fresh_cmdline_read_error_fails_open(self):
        self.state()
        reader = mock.Mock(side_effect=OSError(errno.EIO, "I/O error"))
        self.assertFalse(self.check(reader=reader))
        reader.assert_called_once_with(self.pid)
        self.assertFalse(self.held.exists())

    def test_fresh_permission_denied_still_checks_cmdline(self):
        self.state()
        reader = mock.Mock(return_value=b"/usr/bin/unrelated\0")
        with mock.patch.object(GATE.os, "kill", side_effect=PermissionError(errno.EPERM, "denied")):
            self.assertFalse(self.check(reader=reader))
        reader.assert_called_once_with(self.pid)
        self.assertFalse(self.held.exists())

    def test_disabled_gate_uses_no_files(self):
        for cfg in ({}, {"case_presence_gate": False}, {"case_presence_gate": 0},
                    {"case_presence_gate": 1}, {"case_presence_gate": "true"}):
            with self.subTest(cfg=cfg), mock.patch.object(GATE.os, "open", side_effect=AssertionError) as os_open, \
                    mock.patch.object(GATE.os, "read", side_effect=AssertionError) as os_read, \
                    mock.patch.object(builtins, "open", side_effect=AssertionError) as builtin_open, \
                    mock.patch.object(HOOK, "_load_presence_gate", side_effect=AssertionError) as loader:
                self.assertFalse(GATE.should_hold(cfg, "test", runtime_dir=str(self.root)))
                self.assertFalse(HOOK._presence_hold(cfg, "test"))
                self.assertFalse(HOOK._presence_hold_final(cfg, "test"))
                os_open.assert_not_called()
                os_read.assert_not_called()
                builtin_open.assert_not_called()
                loader.assert_not_called()

    def test_disabled_gate_does_not_touch_fifo_or_symlink(self):
        path = self.presence / "state.json"
        for kind in ("fifo", "symlink"):
            with self.subTest(kind=kind):
                if kind == "fifo":
                    os.mkfifo(path)
                else:
                    path.symlink_to(self.root / "missing")
                result = []
                thread = threading.Thread(
                    target=lambda: result.append(GATE.should_hold(
                        {"case_presence_gate": False}, "x", runtime_dir=str(self.root),
                    )),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "gate blocked on state path")
                self.assertEqual(result, [False])
                self.assertFalse(self.held.exists())
                path.unlink()

    def test_main_gate_absent_does_not_load_or_change_path(self):
        payload = json.dumps({"session_id": "round9", "last_assistant_message": "<voice>Hello there.</voice>"})
        original = list(sys.path)
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(HOOK, "load_config", return_value={"tts_enabled": True}), \
                    mock.patch.object(HOOK, "_load_presence_gate", side_effect=AssertionError) as loader, \
                    mock.patch.object(HOOK, "acquire_lock", return_value=mock.Mock()), \
                    mock.patch.object(HOOK, "release_lock"), mock.patch.object(HOOK, "speak"), \
                    mock.patch.object(HOOK, "log"), mock.patch.object(HOOK.sys, "stdin", io.StringIO(payload)), \
                    mock.patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "main"}):
                HOOK.main()
            loader.assert_not_called()
            self.assertEqual(sys.path, original)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_loading_by_path_is_cached_and_does_not_modify_sys_path(self):
        original = list(sys.path)
        with mock.patch.object(HOOK, "_presence_gate_module", None):
            first = HOOK._load_presence_gate()
            self.assertIs(first, HOOK._load_presence_gate())
            self.assertEqual(Path(first.__file__), ROOT / "scripts/voice_presence_gate.py")
        self.assertEqual(sys.path, original)

    def _assert_replay_order(self, decisions, outcomes, expected):
        calls = mock.Mock()
        calls.wait.return_value = (0, False)
        calls.hold.side_effect = decisions
        calls.play.side_effect = outcomes
        cfg = {"case_presence_gate": True}
        argv = ["paplay", "--raw", "--rate=24000", "--channels=1", "--format=s16le"]
        with mock.patch.object(HOOK, "_wait_for_voxtype_idle_status", calls.wait), \
                mock.patch.object(HOOK, "_presence_hold_final", calls.hold), \
                mock.patch.object(HOOK, "_play_one_paplay", calls.play), \
                mock.patch.object(HOOK, "log"):
            HOOK._play_pcm_voxtype_gated(b"\0\0", 24000, 1, cfg=cfg, line_text="x")
        wait = mock.call.wait(max_wait_s=HOOK.VOXTYPE_MAX_WAIT_S)
        hold = mock.call.hold(cfg, "x")
        play = mock.call.play(argv, b"\0\0", True, timeout=30.0)
        self.assertEqual(calls.mock_calls, [dict(wait=wait, hold=hold, play=play)[step] for step in expected])

    def test_final_hold_prevents_initial_playback(self):
        self._assert_replay_order([True], [], ["wait", "hold"])

    def test_dictation_replays_when_presence_stays_clear(self):
        self._assert_replay_order([False, False], ["dictation", "done"],
                                  ["wait", "hold", "play", "wait", "hold", "play"])

    def test_dictation_does_not_replay_after_hold(self):
        self._assert_replay_order([False, True], ["dictation"],
                                  ["wait", "hold", "play", "wait", "hold"])

    def test_failed_write_preserves_queue_and_starts_playback(self):
        self.state()
        previous = b'{"ts": 1, "text": "old"}\n'
        self.held.write_bytes(previous)
        gate = mock.Mock()
        gate.should_hold.side_effect = lambda cfg, text, queue=True: self.check(text)
        with mock.patch.object(GATE, "_write_all", side_effect=OSError("disk full")), \
                mock.patch.object(HOOK, "_load_presence_gate", return_value=gate), \
                mock.patch.object(HOOK, "voxtype_present", return_value=False), \
                mock.patch.object(HOOK, "IS_MACOS", False), \
                mock.patch.object(HOOK.subprocess, "run") as player, mock.patch.object(HOOK, "log"):
            self.assertFalse(self.check("new"))
            HOOK.play_raw_pcm(b"\0\0", 24000, 1, cfg={"case_presence_gate": True}, line_text="new")
        player.assert_called_once()
        self.assertEqual(self.held.read_bytes(), previous)
        self.assertEqual(list(self.presence.glob("*.tmp")), [])

    def test_unicode_separator_is_one_jsonl_record(self):
        self.state()
        self.assertTrue(self.check("one\u2028two\u2029three"))
        content = self.held.read_text()
        self.assertEqual(len(content.splitlines()), 1)
        self.assertEqual(json.loads(content)["text"], "one\u2028two\u2029three")

    def test_concurrent_writers_keep_all_lines_after_replacements(self):
        self.state()
        barrier = threading.Barrier(8)
        errors = []

        def write(index):
            try:
                barrier.wait(timeout=2)
                if not self.check(f"line-{index}"):
                    errors.append(index)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        rows = [json.loads(line)["text"] for line in self.held.read_text().splitlines()]
        self.assertCountEqual(rows, [f"line-{i}" for i in range(8)])


if __name__ == "__main__":
    unittest.main()
