import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]
HOOK_PATH = ROOT / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location("voice_stop_hook", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)

GATE_PATH = ROOT / "scripts" / "voice_presence_gate.py"
GATE_SPEC = importlib.util.spec_from_file_location("voice_presence_gate", GATE_PATH)
GATE = importlib.util.module_from_spec(GATE_SPEC)
GATE_SPEC.loader.exec_module(GATE)

NOW = 1_800_000_000.0
PRESENCE_BLOCK = (
    "    # CASE presence gate (gauntlet PRS): hold lines while Aragorn is away.\n"
    "    if _presence_hold(cfg, clean):\n"
    '        log("presence gate: Aragorn away — line held, not spoken")\n'
    "        return\n"
)


def write_state(root, *, updated_at=NOW, pid=None, hold_voice=True, raw=None):
    presence = Path(root) / "case-presence"
    presence.mkdir(exist_ok=True)
    if raw is None:
        raw = json.dumps({
            "updated_at": updated_at,
            "pid": os.getpid() if pid is None else pid,
            "hold_voice": hold_voice,
        })
    (presence / "state.json").write_text(raw, encoding="utf-8")
    return presence


def held_file(root):
    return Path(root) / "case-presence" / "held-voice.jsonl"


def read_held(root):
    return [
        json.loads(line)
        for line in held_file(root).read_text(encoding="utf-8").splitlines()
        if line
    ]


def hold(root, text, cfg=None, now=NOW, cmdline_reader=None):
    if cfg is None:
        cfg = {"case_presence_gate": True}
    return GATE.should_hold(
        cfg,
        text,
        runtime_dir=str(root),
        now=now,
        cmdline_reader=cmdline_reader,
    )


def _reader(raw):
    def read(_pid, raw=raw):
        return raw
    return read


_CASE_PRESENCE_CMDLINE = b"/usr/local/bin/case-presence\0"
_OTHER_CMDLINE = b"/usr/bin/python3\0-m\0pytest\0"


class VoicePresenceGateTests(unittest.TestCase):
    def test_gate_off_is_false_without_reading_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, hold_voice=True)
            with mock.patch("builtins.open", side_effect=AssertionError("state was read")):
                for value in (False, None, 0, 1, "true"):
                    with self.subTest(value=value):
                        self.assertFalse(GATE.should_hold(
                            {"case_presence_gate": value},
                            "hello",
                            runtime_dir=tmp,
                            now=NOW,
                        ))
            self.assertFalse(held_file(tmp).exists())

    def test_fresh_state_holds_and_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW, pid=os.getpid(), hold_voice=True)
            self.assertTrue(hold(tmp, "Aragorn is away."))
            rows = read_held(tmp)
            self.assertEqual(rows, [{"ts": NOW, "text": "Aragorn is away."}])
            self.assertEqual(held_file(tmp).stat().st_mode & 0o777, 0o600)

    def test_hold_voice_false_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in (False, None, 1, "true"):
                with self.subTest(value=value):
                    write_state(tmp, hold_voice=True, raw=json.dumps({
                        "updated_at": NOW,
                        "pid": os.getpid(),
                        "hold_voice": value,
                    }))
                    self.assertFalse(hold(tmp, "nope"))
            self.assertFalse(held_file(tmp).exists())

    def test_stale_updated_at_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW - 20)
            self.assertFalse(hold(tmp, "stale"))
            self.assertFalse(held_file(tmp).exists())

    def test_fresh_hold_does_not_read_cmdline(self):
        def reader(pid):
            raise AssertionError(f"cmdline read for fresh pid {pid}")

        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW, pid=os.getpid(), hold_voice=True)
            self.assertTrue(hold(tmp, "fresh", cmdline_reader=reader))
            self.assertEqual(read_held(tmp), [{"ts": NOW, "text": "fresh"}])

    def test_stale_60s_alive_case_presence_holds(self):
        cmdlines = (
            _CASE_PRESENCE_CMDLINE,
            b"python3\0/opt/case-presence\0",
        )
        for raw in cmdlines:
            with self.subTest(raw=raw):
                seen = []

                def reader(pid, raw=raw):
                    seen.append(pid)
                    return raw

                with tempfile.TemporaryDirectory() as tmp:
                    write_state(
                        tmp, updated_at=NOW - 60, pid=os.getpid(), hold_voice=True,
                    )
                    self.assertTrue(hold(tmp, "still away", cmdline_reader=reader))
                    self.assertEqual(seen, [os.getpid()])
                    self.assertEqual(
                        read_held(tmp), [{"ts": NOW, "text": "still away"}],
                    )

    def test_just_stale_and_180s_case_presence_hold(self):
        self.assertEqual(GATE._STALE_ALIVE_MAX_S, 180.0)
        for age in (15.5, 180.0):
            with self.subTest(age=age):
                with tempfile.TemporaryDirectory() as tmp:
                    write_state(
                        tmp,
                        updated_at=NOW - age,
                        pid=os.getpid(),
                        hold_voice=True,
                    )
                    self.assertTrue(hold(
                        tmp,
                        "within window",
                        cmdline_reader=_reader(_CASE_PRESENCE_CMDLINE),
                    ))

    def test_stale_60s_other_cmdline_speaks(self):
        cmdlines = (
            _OTHER_CMDLINE,
            b"/usr/local/bin/case-presence-helper\0",
            b"/opt/case-presence/helper\0",
            b"",
        )
        for raw in cmdlines:
            with self.subTest(raw=raw):
                with tempfile.TemporaryDirectory() as tmp:
                    write_state(
                        tmp, updated_at=NOW - 60, pid=os.getpid(), hold_voice=True,
                    )
                    self.assertFalse(hold(
                        tmp, "speak", cmdline_reader=_reader(raw),
                    ))
                    self.assertFalse(held_file(tmp).exists())

    def test_stale_200s_speaks_even_if_case_presence_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW - 200, pid=os.getpid(), hold_voice=True)
            self.assertFalse(hold(
                tmp,
                "too old",
                cmdline_reader=_reader(_CASE_PRESENCE_CMDLINE),
            ))
            self.assertFalse(held_file(tmp).exists())

    def test_stale_hold_voice_false_speaks(self):
        for value in (False, 1, "true"):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    write_state(tmp, hold_voice=True, raw=json.dumps({
                        "updated_at": NOW - 60,
                        "pid": os.getpid(),
                        "hold_voice": value,
                    }))
                    self.assertFalse(hold(
                        tmp,
                        "present",
                        cmdline_reader=_reader(_CASE_PRESENCE_CMDLINE),
                    ))
                    self.assertFalse(held_file(tmp).exists())

    def test_stale_dead_pid_speaks(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW - 60, pid=os.getpid(), hold_voice=True)
            with mock.patch.object(GATE.os, "kill", side_effect=ProcessLookupError):
                self.assertFalse(hold(
                    tmp,
                    "dead",
                    cmdline_reader=_reader(_CASE_PRESENCE_CMDLINE),
                ))
            self.assertFalse(held_file(tmp).exists())

    def test_stale_unreadable_cmdline_speaks(self):
        def unreadable(pid):
            raise OSError(f"cannot read cmdline of {pid}")

        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW - 60, pid=os.getpid(), hold_voice=True)
            self.assertFalse(hold(tmp, "speak", cmdline_reader=unreadable))
            self.assertFalse(held_file(tmp).exists())

    def test_truncated_cmdline_tail_is_not_case_presence(self):
        cap = GATE._CMDLINE_READ_MAX
        self.assertEqual(cap, 4096)
        # The cut lands so the partial tail's basename is case-presence.
        # That fragment is incomplete and must not hold.
        head = b"/usr/bin/python3\0"
        partial = b"/bin/case-presence"
        raw = head + (b"z" * (cap - len(head) - len(partial))) + partial
        self.assertEqual(len(raw), cap)
        self.assertFalse(raw.endswith(b"\0"))
        self.assertEqual(os.path.basename(raw.split(b"\0")[-1]), b"case-presence")
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW - 60, pid=os.getpid(), hold_voice=True)
            self.assertFalse(hold(tmp, "cut", cmdline_reader=_reader(raw)))
            self.assertFalse(held_file(tmp).exists())

        # A complete case-presence argument before the cut still counts.
        complete = b"/usr/local/bin/case-presence\0"
        kept = complete + (b"y" * (cap - len(complete)))
        self.assertEqual(len(kept), cap)
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW - 60, pid=os.getpid(), hold_voice=True)
            self.assertTrue(hold(tmp, "kept", cmdline_reader=_reader(kept)))

    def test_proc_cmdline_read_is_capped_at_4096(self):
        pid = os.getpid()
        requested = []

        def fake_open(path, flags):
            self.assertEqual(path, f"/proc/{pid}/cmdline")
            self.assertEqual(flags, os.O_RDONLY | os.O_CLOEXEC)
            return 42

        def fake_read(fd, size):
            self.assertEqual(fd, 42)
            requested.append(size)
            if len(requested) == 1:
                return b"a" * 100
            if len(requested) == 2:
                return b"b" * 50
            return b""

        with mock.patch.object(GATE.os, "open", side_effect=fake_open), \
                mock.patch.object(GATE.os, "read", side_effect=fake_read), \
                mock.patch.object(GATE.os, "close", return_value=None) as close:
            blob = GATE._read_proc_cmdline(pid)
        # Each read asks only for the bytes still under the 4096 cap,
        # including the final call that hits EOF.
        self.assertEqual(requested, [4096, 4096 - 100, 4096 - 150])
        self.assertEqual(blob, (b"a" * 100) + (b"b" * 50))
        self.assertLessEqual(len(blob), 4096)
        close.assert_called_once_with(42)

    def test_self_cmdline_read_matches_proc(self):
        pid = os.getpid()
        blob = GATE._read_proc_cmdline(pid)
        with open(f"/proc/{pid}/cmdline", "rb", buffering=0) as handle:
            expected = handle.read(4096)
        self.assertEqual(blob, expected)
        self.assertLessEqual(len(blob), 4096)
        self.assertTrue(blob)
        names = [os.path.basename(arg) for arg in blob.split(b"\0") if arg]
        self.assertNotIn(b"case-presence", names)

    def test_future_updated_at_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, updated_at=NOW + 60)
            self.assertFalse(hold(tmp, "future"))
            self.assertFalse(held_file(tmp).exists())

    def test_updated_at_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = (
                (NOW - 15, True),
                (NOW - 15.5, False),
                (NOW + 5, True),
                (NOW + 5.5, False),
            )
            for updated_at, expected in cases:
                with self.subTest(updated_at=updated_at, expected=expected):
                    write_state(tmp, updated_at=updated_at)
                    self.assertEqual(hold(tmp, "edge"), expected)

    def test_dead_pid_does_not_hold(self):
        pid = _reaped_child_pid()
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, pid=pid)
            self.assertFalse(hold(tmp, "dead"))
            self.assertFalse(held_file(tmp).exists())

    def test_permission_error_counts_as_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, pid=os.getpid())
            with mock.patch.object(GATE.os, "kill", side_effect=PermissionError):
                self.assertTrue(hold(tmp, "alive but unsignalable"))
            self.assertEqual(read_held(tmp)[0]["text"], "alive but unsignalable")

    def test_non_int_pid_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            for raw in (
                '{"updated_at": %s, "pid": true, "hold_voice": true}' % NOW,
                '{"updated_at": %s, "pid": 1.5, "hold_voice": true}' % NOW,
                '{"updated_at": %s, "pid": "1", "hold_voice": true}' % NOW,
            ):
                with self.subTest(raw=raw):
                    write_state(tmp, raw=raw)
                    self.assertFalse(hold(tmp, "bad pid"))
            self.assertFalse(held_file(tmp).exists())

    def test_missing_file_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(hold(tmp, "missing"))
            self.assertFalse(held_file(tmp).exists())

    def test_garbage_json_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, raw="{not json")
            self.assertFalse(hold(tmp, "garbage"))
            self.assertFalse(held_file(tmp).exists())

    def test_json_list_does_not_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, raw="[1, 2, 3]")
            self.assertFalse(hold(tmp, "list"))
            self.assertFalse(held_file(tmp).exists())

    def test_queue_bounded_to_newest_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            cfg = {"case_presence_gate": True, "case_presence_held_max": 20}
            for index in range(25):
                self.assertTrue(hold(tmp, f"line-{index}", cfg=cfg))
            texts = [row["text"] for row in read_held(tmp)]
            self.assertEqual(texts, [f"line-{index}" for index in range(5, 25)])

    def test_custom_and_bad_held_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            cfg = {"case_presence_gate": True, "case_presence_held_max": 3}
            for index in range(5):
                self.assertTrue(hold(tmp, f"n{index}", cfg=cfg))
            self.assertEqual(
                [row["text"] for row in read_held(tmp)],
                ["n2", "n3", "n4"],
            )

        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            # 999 is outside 1..200, so the queue falls back to 20 rather than clamping.
            cfg = {"case_presence_gate": True, "case_presence_held_max": 999}
            for index in range(25):
                self.assertTrue(hold(tmp, f"b{index}", cfg=cfg))
            self.assertEqual(
                [row["text"] for row in read_held(tmp)],
                [f"b{index}" for index in range(5, 25)],
            )

        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            cfg = {"case_presence_gate": True, "case_presence_held_max": True}
            for index in range(3):
                self.assertTrue(hold(tmp, f"t{index}", cfg=cfg))
            self.assertEqual(
                [row["text"] for row in read_held(tmp)],
                ["t0", "t1", "t2"],
            )

    def test_text_truncated_to_300(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            text = "y" * 350
            self.assertTrue(hold(tmp, text))
            stored = read_held(tmp)[0]["text"]
            self.assertEqual(stored, "y" * 300)
            self.assertEqual(len(stored), 300)

    def test_symlinked_held_file_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_state(root)
            outside = root / "outside.jsonl"
            outside.write_text("sentinel\n", encoding="utf-8")
            link = held_file(root)
            link.symlink_to(outside)
            self.assertTrue(hold(root, "do not leak"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), str(outside))

    def test_symlinked_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            runtime.mkdir()
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            (runtime / "case-presence").symlink_to(elsewhere, target_is_directory=True)
            (elsewhere / "state.json").write_text(json.dumps({
                "updated_at": NOW,
                "pid": os.getpid(),
                "hold_voice": True,
            }), encoding="utf-8")
            self.assertTrue(hold(runtime, "stay put"))
            self.assertFalse((elsewhere / "held-voice.jsonl").exists())

    def test_foreign_directory_owner_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            foreign = os.getuid() + 1
            real_fstat = GATE.os.fstat

            def fake_fstat(fd):
                info = real_fstat(fd)
                return mock.Mock(st_mode=info.st_mode, st_uid=foreign)

            with mock.patch.object(GATE.os, "fstat", side_effect=fake_fstat):
                self.assertTrue(hold(tmp, "not our dir"))
            self.assertFalse(held_file(tmp).exists())

    def test_append_failure_still_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            with mock.patch.object(GATE.os, "open", side_effect=OSError("refused")):
                self.assertTrue(hold(tmp, "dropped"))
            self.assertFalse(held_file(tmp).exists())

    def test_unexpected_error_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp, raw="{}")
            with mock.patch.object(GATE.json, "load", side_effect=RuntimeError("boom")):
                self.assertFalse(hold(tmp, "speak"))
            self.assertFalse(held_file(tmp).exists())

    def test_xdg_runtime_dir_is_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_state(tmp)
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertTrue(GATE.should_hold(
                    {"case_presence_gate": True},
                    "from xdg",
                    now=NOW,
                ))
            self.assertEqual(read_held(tmp)[0]["text"], "from xdg")

    def test_example_config_has_presence_keys(self):
        example = json.loads(
            (ROOT / "scripts" / "config.example.json").read_text(encoding="utf-8")
        )
        self.assertIs(example["case_presence_gate"], False)
        self.assertEqual(example["case_presence_held_max"], 20)

    def test_main_source_has_presence_block_before_lock(self):
        source = HOOK_PATH.read_text(encoding="utf-8")
        self.assertIn(PRESENCE_BLOCK, source)
        sanitize_at = source.index("clean = sanitize_for_speech(voice_text)")
        block_at = source.index(PRESENCE_BLOCK)
        lock_at = source.index("lock = acquire_lock()", block_at)
        self.assertLess(sanitize_at, block_at)
        self.assertLess(block_at, lock_at)
        between = source[sanitize_at:block_at]
        self.assertIn("if not clean:", between)
        self.assertNotIn("acquire_lock", between)

    def test_presence_hold_fail_open(self):
        with mock.patch.dict(sys.modules, {"voice_presence_gate": None}):
            self.assertFalse(VOICE_HOOK._presence_hold({"case_presence_gate": True}, "hi"))

        broken = mock.Mock()
        broken.should_hold.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, {"voice_presence_gate": broken}):
            self.assertFalse(VOICE_HOOK._presence_hold({"case_presence_gate": True}, "hi"))

        gate = mock.Mock()
        gate.should_hold.return_value = True
        with mock.patch.dict(sys.modules, {"voice_presence_gate": gate}):
            self.assertTrue(VOICE_HOOK._presence_hold({"case_presence_gate": True}, "hi"))
        gate.should_hold.assert_called_once_with({"case_presence_gate": True}, "hi")

    def test_main_returns_before_acquire_lock_when_holding(self):
        payload = json.dumps({
            "session_id": "presence-test",
            "last_assistant_message": "<voice>Held for presence.</voice>",
        })
        logs = []
        acquire = mock.Mock(side_effect=AssertionError("acquire_lock was called"))
        speak = mock.Mock(side_effect=AssertionError("speak was called"))
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(
                VOICE_HOOK, "load_config", return_value={"tts_enabled": True}
            ), mock.patch.object(
                VOICE_HOOK, "_presence_hold", return_value=True
            ) as presence, mock.patch.object(
                VOICE_HOOK, "acquire_lock", acquire
            ), mock.patch.object(
                VOICE_HOOK, "speak", speak
            ), mock.patch.object(
                VOICE_HOOK, "log", side_effect=logs.append
            ), mock.patch.object(
                VOICE_HOOK.sys, "stdin", io.StringIO(payload)
            ), mock.patch.dict(
                os.environ, {"CLAUDE_SESSION_TYPE": "main"}
            ):
                VOICE_HOOK.main()
        finally:
            signal.signal(signal.SIGTERM, previous)

        presence.assert_called_once()
        cfg, text = presence.call_args.args
        self.assertIs(cfg.get("tts_enabled"), True)
        self.assertIn("Held for presence", text)
        acquire.assert_not_called()
        speak.assert_not_called()
        self.assertIn(
            "presence gate: Aragorn away — line held, not spoken",
            logs,
        )


def _reaped_child_pid():
    for _ in range(5):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = proc.pid
        proc.wait(timeout=5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            continue
    raise AssertionError("could not observe a reaped child pid")


if __name__ == "__main__":
    unittest.main()
