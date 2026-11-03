import builtins
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import voice_session_gate as gate

HOOK_PATH = SCRIPTS / "voice-stop-hook.py"
HOOK_SPEC = importlib.util.spec_from_file_location("voice_stop_hook_gate", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(HOOK_SPEC)
HOOK_SPEC.loader.exec_module(VOICE_HOOK)

SID = "abcdef0123456789abcdef0123456789"
CO_NAME = "Day \u00b7 co-shelby-y"
HEAVY = "http://127.0.0.1:8933"
LIGHT = "http://127.0.0.1:8936"


def _pcm_wav(frames: bytes) -> bytes:
    """Mono 16-bit 16 kHz PCM WAV. ``frames`` is raw little-endian samples."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames)
    return buffer.getvalue()


def _silent_wav() -> bytes:
    # 0.1 s of silence: 1600 frames × 2 bytes.
    return _pcm_wav(b"\x00\x00" * 1600)


WAV = _silent_wav()


def _env(home, **extra):
    env = {"HOME": home, "PATH": "/usr/bin:/bin"}
    env.update(extra)
    return env


def _write_entry(directory, filename, entry):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(entry), encoding="utf-8")
    return path


def _co_entry(sid=SID, **extra):
    entry = {
        "sessionId": sid,
        "name": CO_NAME,
        "nameSource": "user",
        "pid": 111,
        "kind": "interactive",
        "entrypoint": "cli",
    }
    entry.update(extra)
    return entry


class VoiceSessionGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def decide(self, session_id=SID, env=None, pid_alive=None, **kwargs):
        if env is None:
            env = _env(self.home)
        if pid_alive is None:
            pid_alive = {111, 222}.__contains__
        return gate.decide(
            session_id, env=env, home=self.home, pid_alive=pid_alive, **kwargs
        )

    def test_registry_name_in_claude_sessions(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        _write_entry(sessions, "session.json", _co_entry())
        decision = self.decide()
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual(
            "voice-gate allow reason=registry_name host=day sid=abcdef01",
            decision.log_line("day", SID),
        )
        (sessions / "session.json").unlink()
        for name in (
            "co-shelby-y",
            "Dawn \u00b7 co-shelby-x",
            "Host2 \u00b7 co-shelby-z",
        ):
            with self.subTest(name=name):
                entry = _co_entry()
                entry["name"] = name
                _write_entry(sessions, "plain.json", entry)
                decision = self.decide()
                self.assertTrue(decision.allow)
                self.assertEqual("registry_name", decision.reason)

    def test_registry_name_in_claude_accounts(self):
        sessions = Path(self.home) / ".claude-accounts" / "shelby" / "sessions"
        _write_entry(sessions, "session.json", _co_entry())
        decision = self.decide()
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_registry_name_via_claude_config_dir(self):
        config_dir = Path(self.home) / "elsewhere"
        _write_entry(config_dir / "sessions", "session.json", _co_entry())
        decision = self.decide(env=_env(self.home, CLAUDE_CONFIG_DIR=str(config_dir)))
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_registry_directory_order_and_dedup(self):
        config_dir = Path(self.home) / "elsewhere"
        _write_entry(config_dir / "sessions", "a.json", {
            "sessionId": SID,
            "pid": 222,
            "name": "Dusk \u00b7 gauntlet-BKP",
            "tmux": "gauntlet-BKP:@31.%31",
        })
        _write_entry(Path(self.home) / ".claude" / "sessions", "b.json", _co_entry())
        decision = self.decide(env=_env(self.home, CLAUDE_CONFIG_DIR=str(config_dir)))
        self.assertFalse(decision.allow)
        self.assertEqual("registry_conflict", decision.reason)

        claude = Path(self.home) / ".claude"
        with mock.patch("voice_session_gate.os.listdir", wraps=os.listdir) as listed:
            decision = self.decide(env=_env(self.home, CLAUDE_CONFIG_DIR=str(claude)))
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual(1, listed.call_count)

    def test_registry_gauntlet_denied(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
            "pid": 111,
            "name": "Dusk \u00b7 gauntlet-BKP",
            "tmux": "gauntlet-BKP:@31.%31",
        })
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_user_name_does_not_use_tmux(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
            "pid": 111,
            "name": "Day \u00b7 Tailscale and Wisperflow installation",
            "nameSource": "user",
            "tmux": "co-shelby-y:@1.%1",
        })
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_missing_name_source_denies_co_name(self):
        entry = _co_entry()
        entry.pop("nameSource")
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", entry)
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_derived_name_source_denies_co_name(self):
        _write_entry(
            Path(self.home) / ".claude" / "sessions",
            "session.json",
            _co_entry(nameSource="derived"),
        )
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_derived_name_with_co_tmux_denies(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
            "pid": 111,
            "name": "workspace-28",
            "nameSource": "derived",
            "tmux": "co-shelby-y:@119.%119",
        })
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_no_entry_with_tmux_pane_denies_not_registered(self):
        decision = self.decide(env=_env(self.home, TMUX_PANE="%119"))
        self.assertFalse(decision.allow)
        self.assertEqual("not_registered", decision.reason)

    def test_renamed_session_field_is_not_registered(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "session_id": SID,
            "name": CO_NAME,
            "nameSource": "user",
            "entrypoint": "cli",
            "tmux": "co-shelby-y",
        })
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("not_registered", decision.reason)

    def test_live_co_shape_allows(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
            "pid": 111,
            "name": "Day \u00b7 co-shelby-y",
            "nameSource": "user",
            "entrypoint": "cli",
            "kind": "interactive",
            "tmux": "co-shelby-y",
        })
        decision = self.decide(env=_env(
            self.home,
            CLAUDE_CODE_ENTRYPOINT="cli",
            CLAUDE_CODE_SESSION_ATTENDED="1",
            CLAUDE_CODE_CHILD_SESSION="1",
        ))
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_near_miss_names_denied(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        for name in (
            "co-merge-97",
            "co-turn-chain12",
            "Day \u00b7 co-shelby-y extra",
            "Day.co-shelby-y",
            "Day \u00b7 co-shelby-yy",
            "Day \u00b7 co-shelby-gauntlet",
            "co-shelby-notes",
            "Day \u00b7 co-shelby-Y",
            "Day \u00b7 co-shelby-",
            "A \u00b7 B \u00b7 co-shelby-y",
            "Day . co-shelby-y",
        ):
            with self.subTest(name=name):
                _write_entry(sessions, "session.json", {
                    "sessionId": SID,
                    "pid": 111,
                    "name": name,
                    "nameSource": "user",
                    "entrypoint": "cli",
                    "kind": "interactive",
                })
                decision = self.decide()
                self.assertFalse(decision.allow)
                self.assertEqual("registry_not_co", decision.reason)

    def test_entrypoint_denies_before_registry(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", _co_entry())
        decision = self.decide(env=_env(self.home, CLAUDE_CODE_ENTRYPOINT="sdk-cli"))
        self.assertFalse(decision.allow)
        self.assertEqual("entrypoint", decision.reason)

    def test_hard_env_denies(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", _co_entry())
        cases = (
            ({"CLAUDE_CODE_SESSION_ATTENDED": "0"}, "unattended"),
            ({"CLAUDE_CODE_SHELBY_WORKER": "1"}, "worker"),
            ({"SHELBY_DELEGATED": "1"}, "delegated"),
            ({"SHELBY_CODE_WORKER_DEPTH": "1"}, "worker_depth"),
            ({"CLAUDE_SESSION_TYPE": "headless"}, "session_type"),
            ({"CLAUDE_SESSION_TYPE": "cron"}, "session_type"),
            ({"CLAUDE_SESSION_TYPE": "spinoff"}, "session_type"),
        )
        for extra, reason in cases:
            with self.subTest(reason=reason, extra=extra):
                decision = self.decide(env=_env(self.home, **extra))
                self.assertFalse(decision.allow)
                self.assertEqual(reason, decision.reason)

    def test_child_session_flag_does_not_deny(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", _co_entry())
        decision = self.decide(env=_env(
            self.home,
            CLAUDE_CODE_CHILD_SESSION="1",
            CLAUDE_CODE_ENTRYPOINT="cli",
            CLAUDE_CODE_SESSION_ATTENDED="1",
        ))
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_open_spy_skips_key_files_and_denies_bad_entries(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        co = _co_entry()
        (sessions / "broken.json").write_text("{", encoding="utf-8")
        (sessions / "list.json").write_text(json.dumps([co]), encoding="utf-8")
        (sessions / "null.json").write_text("null", encoding="utf-8")
        huge = {"pad": "x" * 70000}
        huge_path = sessions / "huge.json"
        huge_path.write_text(json.dumps(huge), encoding="utf-8")
        self.assertGreater(huge_path.stat().st_size, 65536)
        key_path = sessions / "4242.deadbeef.key"
        key_path.write_text(json.dumps(co), encoding="utf-8")
        outside = Path(self.home) / "real.json"
        outside.write_text(json.dumps(co), encoding="utf-8")
        (sessions / "link.json").symlink_to(outside)

        opened = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            opened.append((os.path.basename(os.fspath(path)), flags))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("voice_session_gate.os.open", side_effect=spy):
            decision = self.decide()
        self.assertFalse(decision.allow)
        # huge.json is too_large, which outranks the unreadable siblings.
        self.assertEqual("registry_too_large", decision.reason)
        self.assertFalse(decision.reason.startswith("error:"))
        names = [name for name, _flags in opened]
        self.assertNotIn("4242.deadbeef.key", names)
        self.assertTrue(opened)
        self.assertIn("huge.json", names)
        self.assertIn("link.json", names)
        for name, flags in opened:
            self.assertTrue(flags & os.O_NOFOLLOW, name)
            self.assertTrue(flags & os.O_NONBLOCK, name)

    def test_bad_session_id(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", _co_entry())
        for session_id in ("", "../../etc", "abc", "abc/defgh", "a" * 65):
            with self.subTest(session_id=session_id):
                decision = self.decide(session_id=session_id)
                self.assertFalse(decision.allow)
                self.assertEqual("bad_session_id", decision.reason)
        self.assertEqual(
            "voice-gate deny reason=bad_session_id host=dusk sid=-",
            gate.Decision(False, "bad_session_id").log_line("dusk", ""),
        )

    def test_speak_policy(self):
        self.assertEqual("all", gate.speak_policy({}))
        self.assertEqual("all", gate.speak_policy({"speak_sessions": "all"}))
        self.assertEqual("all", gate.speak_policy({"speak_sessions": None}))
        self.assertEqual("co_only", gate.speak_policy({"speak_sessions": "co_only"}))
        self.assertEqual("co_only", gate.speak_policy({"speak_sessions": "co-only"}))
        self.assertEqual("co_only", gate.speak_policy({"speak_sessions": 1}))
        self.assertEqual(gate.POLICY_ALL, "all")
        self.assertEqual(gate.POLICY_CO_ONLY, "co_only")

    def test_decide_never_raises(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        with mock.patch("voice_session_gate.os.listdir", side_effect=RuntimeError("boom")):
            decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("error:RuntimeError", decision.reason)

        with mock.patch("voice_session_gate.os.scandir", side_effect=RuntimeError("boom")):
            decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("error:RuntimeError", decision.reason)

    def test_cli_explain(self):
        _write_entry(
            Path(self.home) / ".claude" / "sessions",
            f"{os.getpid()}.json",
            _co_entry(pid=os.getpid()),
        )
        script = str(SCRIPTS / "voice_session_gate.py")
        proc = subprocess.run(
            [sys.executable, script, "--explain", "--session-id", SID, "--home", self.home],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(
            {
                "allow": True,
                "reason": "registry_name",
                "session_id": SID,
                "detail": "",
                "entrypoint": None,
                "attended": None,
            },
            json.loads(proc.stdout),
        )

        usage = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        self.assertEqual(2, usage.returncode)
        self.assertIn("--explain", usage.stderr)
        self.assertIn("--env-file", usage.stderr)
        self.assertNotIn("--tmux-pane", usage.stderr)

        denied = subprocess.run(
            [
                sys.executable, script, "--explain", "--session-id", SID,
                "--home", self.home, "--env", "CLAUDE_CODE_SHELBY_WORKER=1",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )
        self.assertEqual(0, denied.returncode, denied.stderr)
        self.assertEqual(
            {
                "allow": False,
                "reason": "worker",
                "session_id": SID,
                "detail": "",
                "entrypoint": None,
                "attended": None,
            },
            json.loads(denied.stdout),
        )

    def _explain(self, *args):
        script = str(SCRIPTS / "voice_session_gate.py")
        return subprocess.run(
            [sys.executable, script, "--explain", "--session-id", SID, "--home", self.home, *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
        )

    def test_explain_env_file(self):
        _write_entry(
            Path(self.home) / ".claude" / "sessions",
            f"{os.getpid()}.json",
            _co_entry(pid=os.getpid()),
        )
        env_file = Path(self.home) / "gate.env"
        env_file.write_text(
            "\n# comment\n   \nCLAUDE_CODE_SESSION_ATTENDED=0\n# trailing\n",
            encoding="utf-8",
        )
        denied = self._explain("--env-file", str(env_file))
        self.assertEqual(0, denied.returncode, denied.stderr)
        payload = json.loads(denied.stdout)
        self.assertFalse(payload["allow"])
        self.assertEqual("unattended", payload["reason"])
        self.assertEqual("0", payload["attended"])
        self.assertIsNone(payload["entrypoint"])
        self.assertEqual("", payload["detail"])
        for key in ("detail", "entrypoint", "attended"):
            self.assertIn(key, payload)

        env_file.write_text(
            "# comment\n\nCLAUDE_CODE_ENTRYPOINT=cli\nCLAUDE_CODE_SESSION_ATTENDED=0\n",
            encoding="utf-8",
        )
        overridden = self._explain(
            "--env-file", str(env_file),
            "--env", "CLAUDE_CODE_SESSION_ATTENDED=1",
        )
        self.assertEqual(0, overridden.returncode, overridden.stderr)
        payload = json.loads(overridden.stdout)
        self.assertTrue(payload["allow"])
        self.assertEqual("registry_name", payload["reason"])
        self.assertEqual("cli", payload["entrypoint"])
        self.assertEqual("1", payload["attended"])
        self.assertIn("detail", payload)

        for text in ("NOEQUALS\n", "=value\n", " =novalue\n"):
            with self.subTest(text=text):
                env_file.write_text(text, encoding="utf-8")
                bad = self._explain("--env-file", str(env_file))
                self.assertEqual(2, bad.returncode)
                self.assertIn("usage:", bad.stderr)

        missing = self._explain("--env-file", str(Path(self.home) / "missing.env"))
        self.assertEqual(2, missing.returncode)
        self.assertIn("usage:", missing.stderr)

        directory = self._explain("--env-file", self.home)
        self.assertEqual(2, directory.returncode)
        self.assertIn("usage:", directory.stderr)

        oversized = Path(self.home) / "big.env"
        oversized.write_bytes(b"A=1\n" + b"B" * 65536)
        self.assertGreater(oversized.stat().st_size, 65536)
        huge = self._explain("--env-file", str(oversized))
        self.assertEqual(2, huge.returncode)
        self.assertIn("usage:", huge.stderr)

    def test_dead_producer_bad_record_is_ignored(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "111.json", _co_entry(pid=111))
        bad = sessions / "999.json"
        bad.write_bytes(b"x" * 70000)
        dead = lambda pid: pid == 111
        alive = lambda pid: pid in {111, 999}

        decision = self.decide(pid_alive=dead)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual("", decision.detail)

        decision = self.decide(pid_alive=alive)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_too_large", decision.reason)

        bad.write_text("{", encoding="utf-8")
        decision = self.decide(pid_alive=dead)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual("", decision.detail)

        decision = self.decide(pid_alive=alive)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def test_non_digit_and_pid_one_stay_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "111.json", _co_entry(pid=111))
        (sessions / "x.json").write_text("{", encoding="utf-8")
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

        (sessions / "x.json").unlink()
        (sessions / "1.json").write_text("{", encoding="utf-8")
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def test_dead_producer_fifo_does_not_block_and_allows(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        os.mkfifo(sessions / "999.json")
        _write_entry(sessions, "y.json", _co_entry(pid=111))
        box = {}

        def run():
            try:
                box["decision"] = self.decide(pid_alive=lambda pid: pid == 111)
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "decide blocked on dead producer fifo")
        if "error" in box:
            raise box["error"]
        decision = box["decision"]
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_detail_names_offender_and_log_line_sanitizes_env(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        (sessions / "x.json").write_bytes(b"x" * 70000)
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertEqual("registry_too_large", decision.reason)
        self.assertTrue(decision.detail.startswith("~/"), decision.detail)
        self.assertTrue(decision.detail.endswith("x.json:too_large"), decision.detail)
        self.assertLessEqual(len(decision.detail), 160)

        (sessions / "a.json").write_text("{", encoding="utf-8")
        (sessions / "m.json").write_bytes(b"y" * 70000)
        ordered = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertEqual("registry_too_large", ordered.reason)
        self.assertTrue(ordered.detail.endswith("m.json:too_large"), ordered.detail)
        self.assertNotIn("a.json", ordered.detail)

        for path in (sessions / "x.json", sessions / "a.json", sessions / "m.json"):
            path.unlink()
        weird = "a b\nc.json"
        (sessions / weird).write_text("{", encoding="utf-8")
        dirty = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertEqual("registry_unreadable", dirty.reason)
        self.assertIn("a?b?c.json:unreadable", dirty.detail)
        self.assertNotIn(" ", dirty.detail)
        self.assertNotIn("\n", dirty.detail)
        self.assertTrue(dirty.detail.startswith("~/"), dirty.detail)

        plain = gate.Decision(True, "registry_name")
        self.assertEqual(
            "voice-gate allow reason=registry_name host=day sid=abcdef01",
            plain.log_line("day", SID),
        )
        self.assertEqual(
            "voice-gate deny reason=bad_session_id host=dusk sid=-",
            gate.Decision(False, "bad_session_id").log_line("dusk", ""),
        )
        leaked = plain.log_line("day", SID, env={
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "OTHER": "secret",
        })
        self.assertEqual(
            "voice-gate allow reason=registry_name host=day sid=abcdef01 ep=cli att=1",
            leaked,
        )
        self.assertNotIn("secret", leaked)
        self.assertNotIn("detail=", leaked)

        messy = plain.log_line("day", SID, env={
            "CLAUDE_CODE_ENTRYPOINT": "cli extra\n",
            "CLAUDE_CODE_SESSION_ATTENDED": "1 2",
        })
        self.assertIn(" ep=cli?extra? att=1?2", messy)
        self.assertNotIn("\n", messy)
        self.assertNotIn("cli extra", messy)
        self.assertNotIn("1 2", messy)

        capped = plain.log_line("day", SID, env={
            "CLAUDE_CODE_ENTRYPOINT": "E" * 50,
            "CLAUDE_CODE_SESSION_ATTENDED": "A" * 50,
        })
        ep = capped.split(" ep=", 1)[1].split(" att=", 1)[0]
        att = capped.split(" att=", 1)[1]
        self.assertEqual("E" * 32, ep)
        self.assertEqual("A" * 32, att)

        named = dirty.log_line("day", SID, env={
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ATTENDED": "1",
            "OTHER": "secret",
        })
        self.assertIn(" detail=", named)
        self.assertLess(named.index(" detail="), named.index(" ep="))
        self.assertIn(" ep=cli att=1", named)
        self.assertNotIn("secret", named)
        self.assertNotIn("\n", named)

        messy_ids = plain.log_line("Day\nX", "ab\ncd\x1b[0mzz")
        self.assertEqual(
            "voice-gate allow reason=registry_name host=Day?X sid=ab?cd??0",
            messy_ids,
        )
        self.assertNotIn("\n", messy_ids)
        self.assertNotIn("\x1b", messy_ids)
        bad_line = gate.Decision(False, "bad_session_id").log_line(
            "Day\nX", "ab\ncd\x1b[0mzz"
        )
        self.assertEqual(
            "voice-gate deny reason=bad_session_id host=Day?X sid=ab?cd??0",
            bad_line,
        )
        self.assertEqual([bad_line], bad_line.splitlines())

    def test_dead_pid_co_entry_is_not_registered(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: False)
        self.assertFalse(decision.allow)
        self.assertEqual("not_registered", decision.reason)

    def test_dead_co_plus_live_co_allows(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        _write_entry(sessions, "111.json", _co_entry(pid=111))
        _write_entry(sessions, "222.json", _co_entry(pid=222))
        decision = self.decide(pid_alive=lambda pid: pid == 222)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_live_co_and_live_gauntlet_conflict(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        _write_entry(sessions, "111.json", _co_entry(pid=111))
        _write_entry(sessions, "222.json", {
            "sessionId": SID,
            "pid": 222,
            "name": "Dusk \u00b7 gauntlet-BKP",
            "nameSource": "user",
            "entrypoint": "cli",
            "kind": "interactive",
        })
        decision = self.decide(pid_alive=lambda pid: pid in {111, 222})
        self.assertFalse(decision.allow)
        self.assertEqual("registry_conflict", decision.reason)

    def test_sdk_cli_entrypoint_is_not_co(self):
        _write_entry(
            Path(self.home) / ".claude" / "sessions",
            "111.json",
            _co_entry(pid=111, entrypoint="sdk-cli"),
        )
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_missing_kind_is_not_co(self):
        entry = _co_entry(pid=111)
        entry.pop("kind")
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", entry)
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_filename_pid_when_field_absent(self):
        entry = _co_entry()
        entry.pop("pid")
        _write_entry(Path(self.home) / ".claude" / "sessions", "222.json", entry)
        seen = []

        def alive(pid):
            seen.append(pid)
            return pid == 222

        decision = self.decide(pid_alive=alive)
        self.assertEqual([222], seen)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_non_digit_filename_without_pid_is_skipped(self):
        entry = _co_entry()
        entry.pop("pid")
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", entry)
        seen = []

        def alive(pid):
            seen.append(pid)
            return True

        decision = self.decide(pid_alive=alive)
        self.assertEqual([], seen)
        self.assertFalse(decision.allow)
        self.assertEqual("not_registered", decision.reason)

    def test_fifo_does_not_block_and_is_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        os.mkfifo(sessions / "x.json")
        _write_entry(sessions, "y.json", _co_entry(pid=111))
        box = {}

        def run():
            box["decision"] = self.decide(pid_alive=lambda pid: pid == 111)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "decide blocked on fifo")
        decision = box["decision"]
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def test_symlink_json_is_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        outside = Path(self.home) / "real.json"
        outside.write_text(json.dumps(_co_entry(pid=111)), encoding="utf-8")
        (sessions / "x.json").symlink_to(outside)
        decision = self.decide(pid_alive=lambda pid: True)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def _write_session_id_past_cap(self, directory, filename, size=70000, pad=66000):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        head = b'{"pad":"'
        padding = b"x" * pad
        mid = b'","sessionId":"' + SID.encode("ascii") + b'","tail":"'
        end = b'"}'
        fixed = len(head) + len(padding) + len(mid) + len(end)
        self.assertGreaterEqual(size, fixed)
        payload = head + padding + mid + (b"y" * (size - fixed)) + end
        self.assertEqual(size, len(payload))
        key_at = payload.find(b'"sessionId"')
        self.assertGreater(key_at, pad)
        self.assertEqual(-1, payload.find(b"sessionId", 0, pad))
        self.assertGreater(key_at, gate._MAX_REGISTRY_BYTES)
        path = directory / filename
        path.write_bytes(payload)
        self.assertEqual(size, path.stat().st_size)
        return path

    def test_session_id_after_cap_with_live_co_is_too_large(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        self._write_session_id_past_cap(sessions, "huge.json")
        seen = []

        def alive(pid):
            seen.append(pid)
            return pid == 111

        decision = self.decide(pid_alive=alive)
        self.assertEqual([], seen)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_too_large", decision.reason)

    def test_unicode_escaped_session_id_parses_as_conflict(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "co.json", _co_entry(pid=111))
        escaped = "".join("\\u%04x" % ord(ch) for ch in SID)
        raw = (
            '{"sessionId":"' + escaped + '",'
            '"pid":222,'
            '"name":"Dusk \\u00b7 gauntlet-BKP",'
            '"tmux":"gauntlet-BKP:@31.%31"}'
        )
        encoded = raw.encode("utf-8")
        self.assertLess(len(encoded), 1024)
        self.assertNotIn(SID.encode("ascii"), encoded)
        parsed = json.loads(raw)
        self.assertEqual(SID, parsed["sessionId"])
        self.assertEqual(222, parsed["pid"])
        self.assertEqual("Dusk \u00b7 gauntlet-BKP", parsed["name"])
        (sessions / "gauntlet.json").write_bytes(encoded)
        decision = self.decide(pid_alive=lambda pid: pid in {111, 222})
        self.assertFalse(decision.allow)
        self.assertEqual("registry_conflict", decision.reason)

    def test_oversized_file_without_session_is_too_large(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        payload = json.dumps({"pad": "x" * 70000})
        self.assertNotIn("sessionId", payload)
        self.assertNotIn(SID, payload)
        path = sessions / "huge.json"
        path.write_text(payload, encoding="utf-8")
        self.assertGreaterEqual(path.stat().st_size, 70000)
        seen = []

        def alive(pid):
            seen.append(pid)
            return pid == 111

        decision = self.decide(pid_alive=alive)
        self.assertEqual([], seen)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_too_large", decision.reason)

    def _write_oversized_match(self, directory, filename):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"sessionId": SID, "pad": "x" * 70000})
        path = directory / filename
        path.write_text(payload, encoding="utf-8")
        self.assertGreater(path.stat().st_size, 65536)
        self.assertIn(SID.encode("ascii"), path.read_bytes()[:65537])
        return path

    def _clear_registry_files(self, *directories):
        for directory in directories:
            directory = Path(directory)
            if not directory.exists():
                continue
            for child in directory.iterdir():
                if child.is_symlink() or child.is_file():
                    child.unlink()

    def test_live_co_plus_oversized_match_denies_any_order(self):
        claude = Path(self.home) / ".claude" / "sessions"
        accounts = Path(self.home) / ".claude-accounts" / "a" / "sessions"
        name_orders = (
            ("a-huge.json", "z-live.json"),
            ("z-huge.json", "a-live.json"),
        )
        for huge_name, live_name in name_orders:
            with self.subTest(where="one-dir", huge=huge_name, live=live_name):
                self._clear_registry_files(claude, accounts)
                ordered = sorted((huge_name, live_name))
                if huge_name.startswith("a-"):
                    self.assertEqual([huge_name, live_name], ordered)
                else:
                    self.assertEqual([live_name, huge_name], ordered)
                self._write_oversized_match(claude, huge_name)
                _write_entry(claude, live_name, _co_entry(pid=111))
                seen = []

                def alive(pid, seen=seen):
                    seen.append(pid)
                    return pid == 111

                decision = self.decide(pid_alive=alive)
                self.assertEqual([], seen)
                self.assertFalse(decision.allow)
                self.assertEqual("registry_too_large", decision.reason)

        # Default scan is ~/.claude/sessions then ~/.claude-accounts/*/sessions.
        # CLAUDE_CONFIG_DIR reverses that by putting the accounts dir first.
        splits = (
            ("default", None, "claude"),
            ("default", None, "accounts"),
            ("accounts-first", str(accounts.parent), "claude"),
            ("accounts-first", str(accounts.parent), "accounts"),
        )
        for scan, config_dir, huge_at in splits:
            with self.subTest(scan=scan, huge_at=huge_at):
                self._clear_registry_files(claude, accounts)
                huge_dir = claude if huge_at == "claude" else accounts
                live_dir = accounts if huge_at == "claude" else claude
                self._write_oversized_match(huge_dir, "huge.json")
                _write_entry(live_dir, "111.json", _co_entry(pid=111))
                env = _env(self.home)
                if config_dir is not None:
                    env = _env(self.home, CLAUDE_CONFIG_DIR=config_dir)
                seen = []

                def alive(pid, seen=seen):
                    seen.append(pid)
                    return True

                decision = self.decide(env=env, pid_alive=alive)
                self.assertEqual([], seen)
                self.assertFalse(decision.allow)
                self.assertEqual("registry_too_large", decision.reason)

    def test_deeply_nested_json_over_cap_is_too_large(self):
        # "[" * 100000 is a regular file of 100000 bytes, so the size rule
        # denies with registry_too_large before any JSON parse.
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "a.json").write_text("[" * 100000, encoding="utf-8")
        self.assertGreater((sessions / "a.json").stat().st_size, 65536)
        _write_entry(sessions, "b.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_too_large", decision.reason)

    def test_deeply_nested_json_under_cap_is_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        nested = "[" * 5000 + "]" * 5000
        self.assertLess(len(nested.encode("utf-8")), 65536)
        # The gate's decoder must handle recursion failure as unreadable.
        (sessions / "a.json").write_text(nested, encoding="utf-8")
        _write_entry(sessions, "b.json", _co_entry(pid=111))
        raised = []
        real_loads = json.loads

        def spy(text, *args, **kwargs):
            try:
                return real_loads(text, *args, **kwargs)
            except RecursionError as exc:
                raised.append((text, exc))
                raise

        with mock.patch("voice_session_gate.json.loads", side_effect=spy):
            decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)
        self.assertTrue(raised)
        self.assertTrue(any(text == nested for text, _exc in raised))
        self.assertTrue(all(isinstance(exc, RecursionError) for _text, exc in raised))

    def test_invalid_utf8_registry_file_is_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "bad.json").write_bytes(b"\xff\xfe{")
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def test_json_list_is_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "x.json").write_text("[]", encoding="utf-8")
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def test_directory_named_json_is_unreadable(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "x.json").mkdir()
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

    def test_vanished_json_during_scan_is_ignored(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        _write_entry(sessions, "live.json", _co_entry(pid=111))
        chosen = sessions / "gone.json"
        chosen.write_text("{}", encoding="utf-8")
        real_open = os.open
        raised = []

        def spy(path, flags, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == "gone.json":
                raised.append(os.fspath(path))
                raise FileNotFoundError(2, "No such file or directory", path)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("voice_session_gate.os.open", side_effect=spy):
            decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertEqual(["gone.json"], [os.path.basename(path) for path in raised])
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def _chmod_restore(self, path, mode=0o755):
        def restore():
            if path.exists():
                path.chmod(mode)
        self.addCleanup(restore)

    @unittest.skipIf(os.geteuid() == 0, "mode 000 does not deny root")
    def test_unreadable_accounts_tree_denies(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        accounts = Path(self.home) / ".claude-accounts"
        accounts.mkdir()
        self._chmod_restore(accounts)
        accounts.chmod(0)
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)
        self.assertTrue(
            decision.detail.endswith(".claude-accounts:unreadable"),
            decision.detail,
        )

    @unittest.skipIf(os.geteuid() == 0, "mode 000 does not deny root")
    def test_unreadable_account_sessions_stat_denies(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        account = Path(self.home) / ".claude-accounts" / "a"
        account.mkdir(parents=True)
        self._chmod_restore(account)
        account.chmod(0)
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)
        self.assertTrue(decision.detail.endswith("a/sessions:unreadable"), decision.detail)

    def test_missing_accounts_tree_allows(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        self.assertFalse((Path(self.home) / ".claude-accounts").exists())
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    def test_account_name_that_is_a_file_is_skipped(self):
        accounts = Path(self.home) / ".claude-accounts"
        accounts.mkdir()
        (accounts / "a").write_text("not-a-directory", encoding="utf-8")
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)

    @unittest.skipIf(os.geteuid() == 0, "mode 000 does not deny root")
    def test_unreadable_sessions_listdir_denies(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        self._chmod_restore(sessions)
        sessions.chmod(0)
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)
        self.assertTrue(decision.detail.endswith("sessions:unreadable"), decision.detail)

    def test_live_bad_record_retried_once_then_allows(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "111.json").write_text("{}", encoding="utf-8")
        calls = {"n": 0}

        def fake_read(path, session_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None, "unreadable"
            return _co_entry(pid=111), "ok"

        sleeps = []
        with mock.patch("voice_session_gate._read_registry_record", side_effect=fake_read), \
                mock.patch("voice_session_gate._RETRY_SLEEP", side_effect=sleeps.append):
            decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual([0.05], sleeps)
        self.assertEqual(2, calls["n"])

    def test_live_bad_record_still_unreadable_after_retry(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "111.json").write_text("{", encoding="utf-8")
        sleeps = []

        def fake_read(path, session_id):
            return None, "unreadable"

        with mock.patch("voice_session_gate._read_registry_record", side_effect=fake_read), \
                mock.patch("voice_session_gate._RETRY_SLEEP", side_effect=sleeps.append):
            decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)
        self.assertEqual([0.05], sleeps)

    def test_two_bad_files_sleep_once(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "111.json").write_text("{", encoding="utf-8")
        (sessions / "222.json").write_text("{", encoding="utf-8")
        seen = []
        sleeps = []

        def fake_read(path, session_id):
            seen.append(os.path.basename(path))
            return None, "unreadable"

        with mock.patch("voice_session_gate._read_registry_record", side_effect=fake_read), \
                mock.patch("voice_session_gate._RETRY_SLEEP", side_effect=sleeps.append):
            decision = self.decide(pid_alive=lambda pid: pid in {111, 222})
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)
        self.assertEqual([0.05], sleeps)
        self.assertEqual(["111.json", "222.json", "111.json", "222.json"], seen)

    def test_clean_registry_does_not_sleep(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        sleeps = []
        with mock.patch("voice_session_gate._RETRY_SLEEP", side_effect=sleeps.append):
            decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual([], sleeps)

    def test_child_sdk_unattended_denies(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "111.json", _co_entry(pid=111))
        decision = self.decide(env=_env(
            self.home,
            CLAUDE_CODE_CHILD_SESSION="1",
            CLAUDE_CODE_ENTRYPOINT="sdk-cli",
            CLAUDE_CODE_SESSION_ATTENDED="0",
        ))
        self.assertFalse(decision.allow)
        self.assertIn(decision.reason, ("entrypoint", "unattended"))

    def test_explain_ignores_ambient_env_and_rejects_fifo(self):
        _write_entry(
            Path(self.home) / ".claude" / "sessions",
            f"{os.getpid()}.json",
            _co_entry(pid=os.getpid()),
        )
        script = str(SCRIPTS / "voice_session_gate.py")
        env = os.environ.copy()
        env["CLAUDE_CODE_SESSION_ATTENDED"] = "0"
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"
        proc = subprocess.run(
            [sys.executable, script, "--explain", "--session-id", SID, "--home", self.home],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=10,
            env=env,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["allow"])
        self.assertEqual("registry_name", payload["reason"])
        self.assertIsNone(payload["entrypoint"])
        self.assertIsNone(payload["attended"])

        fifo = Path(self.home) / "gate.fifo"
        os.mkfifo(fifo)
        blocked = self._explain("--env-file", str(fifo))
        self.assertEqual(2, blocked.returncode, blocked.stderr)
        self.assertIn("usage:", blocked.stderr)


class VoiceHookGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._old_sigterm = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, self._old_sigterm)

    def run_main(self, cfg, message, session_id=SID, extra_env=None):
        payload = json.dumps({
            "session_id": session_id,
            "last_assistant_message": message,
        })
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "CLAUDE_SESSION_TYPE": "main"}
        if extra_env:
            env.update(extra_env)
        speak = mock.Mock()
        missing = mock.Mock()
        drift = mock.Mock()
        writer = mock.Mock()
        lock = mock.Mock(return_value=object())
        log = mock.Mock()
        patches = (
            mock.patch.object(VOICE_HOOK, "load_config", return_value=cfg),
            mock.patch.object(VOICE_HOOK, "speak", speak),
            mock.patch.object(VOICE_HOOK, "_record_missing_voice_block", missing),
            mock.patch.object(VOICE_HOOK, "_record_lang_drift", drift),
            mock.patch.object(VOICE_HOOK, "_record_voice_defect", writer),
            mock.patch.object(VOICE_HOOK, "acquire_lock", lock),
            mock.patch.object(VOICE_HOOK, "release_lock"),
            mock.patch.object(VOICE_HOOK, "log", log),
            mock.patch.object(VOICE_HOOK.sys, "stdin", io.StringIO(payload)),
            mock.patch.dict(os.environ, env, clear=True),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        VOICE_HOOK.main()
        return speak, missing, log, lock, drift, writer

    def test_missing_policy_speaks_without_importing_gate(self):
        saved = sys.modules.pop("voice_session_gate", None)
        self.addCleanup(lambda: sys.modules.__setitem__("voice_session_gate", saved) if saved is not None else None)
        speak, missing, _log, _lock, _drift, _writer = self.run_main(
            {"tts_enabled": True, "source_machine": "day"},
            "<voice>Ready now.</voice>",
        )
        speak.assert_called_once()
        missing.assert_not_called()
        self.assertNotIn("voice_session_gate", sys.modules)

    def _assert_denied_session_skips_side_effects(self, message):
        speak, missing, log, lock, drift, writer = self.run_main(
            {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
            message,
        )
        lock.assert_not_called()
        speak.assert_not_called()
        missing.assert_not_called()
        drift.assert_not_called()
        writer.assert_not_called()
        lines = [call.args[0] for call in log.call_args_list if call.args]
        self.assertTrue(any("voice-gate deny" in line for line in lines), lines)
        self.assertTrue(any("sid=abcdef01" in line for line in lines), lines)

    def test_co_only_denied_with_voice_block_skips_side_effects(self):
        self._assert_denied_session_skips_side_effects("<voice>Ready now.</voice>")

    def test_co_only_denied_without_voice_block_skips_side_effects(self):
        self._assert_denied_session_skips_side_effects("No voice block in this prose.")

    def test_co_only_registered_session_speaks(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", f"{os.getpid()}.json", {
            "sessionId": SID,
            "pid": os.getpid(),
            "name": "Day \u00b7 co-shelby-y",
            "nameSource": "user",
            "entrypoint": "cli",
            "kind": "interactive",
            "tmux": "co-shelby-y",
        })
        speak, missing, log, lock, _drift, _writer = self.run_main(
            {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
            "<voice>Ready now.</voice>",
        )
        lock.assert_called_once()
        speak.assert_called_once()
        missing.assert_not_called()
        lines = [call.args[0] for call in log.call_args_list if call.args]
        self.assertTrue(any("voice-gate allow" in line and "registry_name" in line for line in lines), lines)

    def test_session_may_speak_logs_entrypoint_and_attended(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", f"{os.getpid()}.json", {
            "sessionId": SID,
            "pid": os.getpid(),
            "name": "Day \u00b7 co-shelby-y",
            "nameSource": "user",
            "entrypoint": "cli",
            "kind": "interactive",
        })
        _speak, _missing, log, _lock, _drift, _writer = self.run_main(
            {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
            "<voice>Ready now.</voice>",
            extra_env={
                "CLAUDE_CODE_ENTRYPOINT": "cli",
                "CLAUDE_CODE_SESSION_ATTENDED": "1",
                "OTHER": "secret",
            },
        )
        lines = [call.args[0] for call in log.call_args_list if call.args]
        gate_lines = [line for line in lines if line.startswith("voice-gate ")]
        self.assertEqual(1, len(gate_lines), lines)
        self.assertIn(" ep=cli att=1", gate_lines[0])
        self.assertNotIn("secret", gate_lines[0])
        self.assertNotIn("OTHER", gate_lines[0])

    def test_gate_import_failure_fails_closed(self):
        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "voice_session_gate":
                raise ImportError("blocked")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded):
            speak, missing, log, _lock, _drift, _writer = self.run_main(
                {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
                "<voice>Ready now.</voice>",
            )
        speak.assert_not_called()
        missing.assert_not_called()
        lines = [call.args[0] for call in log.call_args_list if call.args]
        self.assertTrue(any("gate_unavailable" in line for line in lines), lines)
        self.assertTrue(any(line.startswith("voice-gate deny") for line in lines), lines)

    def test_child_shape_through_main_skips_side_effects(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", f"{os.getpid()}.json", {
            "sessionId": SID,
            "pid": os.getpid(),
            "name": CO_NAME,
            "nameSource": "user",
            "entrypoint": "cli",
            "kind": "interactive",
        })
        speak, missing, log, lock, drift, writer = self.run_main(
            {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
            "<voice>Ready now.</voice>",
            extra_env={
                "CLAUDE_CODE_CHILD_SESSION": "1",
                "CLAUDE_CODE_ENTRYPOINT": "sdk-cli",
                "CLAUDE_CODE_SESSION_ATTENDED": "0",
            },
        )
        lock.assert_not_called()
        speak.assert_not_called()
        missing.assert_not_called()
        drift.assert_not_called()
        writer.assert_not_called()
        lines = [call.args[0] for call in log.call_args_list if call.args]
        self.assertTrue(any("voice-gate deny" in line for line in lines), lines)

    def test_open_policy_without_gate_in_fresh_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "voice-stop-hook.py"
            dest.write_bytes(HOOK_PATH.read_bytes())
            code = (
                "import importlib.util, sys\n"
                "path = sys.argv[1]\n"
                "spec = importlib.util.spec_from_file_location('fresh_hook', path)\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "results = [\n"
                "    mod._session_may_speak({}),\n"
                "    mod._session_may_speak({'speak_sessions': None}),\n"
                "    mod._session_may_speak({'speak_sessions': 'all'}),\n"
                "]\n"
                "print(results[0], results[1], results[2], "
                "'voice_session_gate' in sys.modules)\n"
            )
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(dest)],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tmp,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp},
            )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("True True True False", proc.stdout.strip())

    def test_policy_rollback_without_restart_matches_baseline(self):
        config_path = Path(self.home) / "config.json"
        message = "<voice>Ready now.</voice>"
        payload = json.dumps({
            "session_id": SID,
            "last_assistant_message": message,
        })
        open_cfg = {"tts_enabled": True, "source_machine": "day"}
        closed_cfg = dict(open_cfg, speak_sessions="co_only")
        speak = mock.Mock()
        decide = mock.Mock(wraps=gate.decide)
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "CLAUDE_SESSION_TYPE": "main"}

        def run():
            with mock.patch.object(VOICE_HOOK.sys, "stdin", io.StringIO(payload)):
                VOICE_HOOK.main()

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(VOICE_HOOK, "CONFIG_PATH", str(config_path)))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "speak", speak))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "acquire_lock", return_value=object()))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "release_lock"))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "log"))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "_record_missing_voice_block"))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "_record_lang_drift"))
            stack.enter_context(mock.patch.object(VOICE_HOOK, "_record_voice_defect"))
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(gate, "decide", decide))

            config_path.write_text(json.dumps(open_cfg), encoding="utf-8")
            run()
            baseline = (speak.call_args.args, speak.call_args.kwargs)
            speak.reset_mock()
            decide.reset_mock()

            config_path.write_text(json.dumps(closed_cfg), encoding="utf-8")
            run()
            speak.assert_not_called()
            self.assertGreaterEqual(decide.call_count, 1)
            decide.reset_mock()

            config_path.write_text(json.dumps(open_cfg), encoding="utf-8")
            run()
            speak.assert_called_once()
            decide.assert_not_called()
            self.assertEqual(baseline, (speak.call_args.args, speak.call_args.kwargs))


class LightSpeechUrlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def pocket_cfg(self, **updates):
        cfg = {
            "tts_engine": "pocket",
            "source_machine": "day",
            "pocket_tts_url": HEAVY,
            "pocket_speed": 1.0,
            "dutch_speech": {"enabled": False},
        }
        cfg.update(updates)
        return cfg

    def marker_dir(self):
        directory = Path(self.home) / "voice-tmp"
        directory.mkdir(exist_ok=True)
        return directory

    def marker_path(self):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return self.marker_dir() / f"claude-voice-light-down-{uid}.json"

    def run_speak(self, cfg, responses, pocket_timeout="27", logs=None, now=None,
                  preserve_marker=False, voices=None):
        calls = []
        pending = list(responses)
        self.played_audio = []
        if not preserve_marker and self.marker_path().exists():
            self.marker_path().unlink()

        def fetch(_text, voice, base_url, timeout, **_kwargs):
            calls.append((base_url, timeout))
            if voices is not None:
                voices.append(voice)
            if not pending:
                return None
            result = pending.pop(0)
            return result, result is not None

        def play(path):
            self.played_audio.append(Path(path).read_bytes())

        def capture(msg):
            if logs is not None:
                logs.append(msg)

        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
        }
        if pocket_timeout is not None:
            env["SHELBY_POCKET_TIMEOUT"] = pocket_timeout
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(VOICE_HOOK.tempfile, "gettempdir", return_value=str(self.marker_dir())),
            mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)),
            mock.patch.object(VOICE_HOOK, "_pocket_fetch", side_effect=fetch),
            mock.patch.object(VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed, _gain=0: wav),
            mock.patch.object(VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav),
            mock.patch.object(VOICE_HOOK, "play_audio_file", side_effect=play),
            mock.patch.object(VOICE_HOOK, "send_audio_remote"),
            mock.patch.object(VOICE_HOOK, "speak_edge", new=mock.AsyncMock()),
            mock.patch.object(VOICE_HOOK, "speak_kokoro", return_value=False),
            mock.patch.object(VOICE_HOOK, "speak_elevenlabs_streaming"),
            mock.patch.object(VOICE_HOOK, "log", side_effect=capture),
        ]
        if now is not None:
            patches.append(mock.patch.object(VOICE_HOOK.time, "time", return_value=now))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            VOICE_HOOK.speak("Ready now.", cfg)
        return calls

    def test_speech_url_falls_back_to_heavy(self):
        cfg = self.pocket_cfg(pocket_tts_url_speech=LIGHT)
        voices = []
        calls = self.run_speak(cfg, [None, WAV], voices=voices)
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)
        self.assertEqual(["case-o", "case-o"], voices)
        self.assertEqual([WAV], self.played_audio)

    def test_explicit_persona_is_used_by_both_servers(self):
        for persona in ("case-o", "jarvis"):
            with self.subTest(persona=persona):
                voices = []
                cfg = self.pocket_cfg(pocket_tts_url_speech=LIGHT, tts_voice_pocket_en=persona)
                self.run_speak(cfg, [None, WAV], voices=voices)
                self.assertEqual([persona, persona], voices)

    def test_unset_speech_url_keeps_heavy_timeout(self):
        calls = self.run_speak(self.pocket_cfg(), [None, WAV])
        self.assertEqual([(HEAVY, 27)], calls)

    def test_unset_speech_url_uses_legacy_default_timeout(self):
        # speak_pocket uses int(os.environ["SHELBY_POCKET_TIMEOUT"] or "27").
        calls = self.run_speak(self.pocket_cfg(), [WAV], pocket_timeout=None)
        self.assertEqual([(HEAVY, 27)], calls)

    def test_unset_speech_url_passes_no_timeout_kwargs(self):
        seen = {}

        def fake_speak_pocket(*_args, **kwargs):
            seen["kwargs"] = kwargs
            return True

        env = {"HOME": self.home, "PATH": "/usr/bin:/bin"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(
                mock.patch.object(
                    VOICE_HOOK, "find_audible_path", return_value=(None, True)
                )
            )
            stack.enter_context(
                mock.patch.object(
                    VOICE_HOOK, "speak_pocket", side_effect=fake_speak_pocket
                )
            )
            stack.enter_context(mock.patch.object(VOICE_HOOK, "log"))
            VOICE_HOOK.speak("Ready now.", self.pocket_cfg())
        kwargs = seen["kwargs"]
        self.assertEqual({"remote_target": None, "play_local": True,
                          "base_url": "http://127.0.0.1:8933", "fallback_local": True,
                          "tail_ms": 1000, "remote_requires_off_lan": False,
                          "remote_fallback_target": None, "speed": 1.0, "gain_db": 0}, kwargs)
        self.assertEqual(HEAVY, kwargs["base_url"])
        for key in (
            "first_timeout_s",
            "primary_timeout_s",
            "fallback_base_url",
            "fallback_timeout_s",
            "on_primary_failure",
        ):
            self.assertNotIn(key, kwargs)

    def test_speech_url_same_as_heavy_matches_unset_kwargs(self):
        def capture(cfg):
            seen = {}

            def fake_speak_pocket(*_args, **kwargs):
                seen["kwargs"] = kwargs
                return True

            env = {"HOME": self.home, "PATH": "/usr/bin:/bin"}
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
                stack.enter_context(
                    mock.patch.object(
                        VOICE_HOOK, "find_audible_path", return_value=(None, True)
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        VOICE_HOOK, "speak_pocket", side_effect=fake_speak_pocket
                    )
                )
                stack.enter_context(mock.patch.object(VOICE_HOOK, "log"))
                stack.enter_context(
                    mock.patch.object(VOICE_HOOK, "speak_edge", new=mock.AsyncMock())
                )
                stack.enter_context(
                    mock.patch.object(VOICE_HOOK, "speak_kokoro", return_value=False)
                )
                stack.enter_context(
                    mock.patch.object(VOICE_HOOK, "speak_elevenlabs_streaming")
                )
                VOICE_HOOK.speak("Ready now.", cfg)
            return seen["kwargs"]

        unset = capture(self.pocket_cfg())
        same = capture(self.pocket_cfg(pocket_tts_url_speech=HEAVY + "/"))
        self.assertEqual(unset, same)
        self.assertEqual(HEAVY, same["base_url"])
        for key in (
            "first_timeout_s",
            "primary_timeout_s",
            "fallback_base_url",
            "fallback_timeout_s",
            "on_primary_failure",
        ):
            self.assertNotIn(key, same)

    def test_speech_timeout_clamps_and_rejects_bad_values(self):
        clamped = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_timeout_s=99,
            ),
            [None, WAV],
            pocket_timeout="5",
        )
        self.assertEqual([(LIGHT, 60), (HEAVY, 45)], clamped)

        bad = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_timeout_s="nope",
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], bad)

        low = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_timeout_s=0,
            ),
            [WAV],
        )
        self.assertEqual([(LIGHT, 1)], low)

    def test_light_timeout_is_exact_when_pocket_timeout_unset(self):
        fallback = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_timeout_s=40,
            ),
            [None, WAV],
            pocket_timeout=None,
        )
        self.assertEqual([(LIGHT, 40), (HEAVY, 45)], fallback)

        same = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=HEAVY,
                pocket_tts_speech_timeout_s=40,
            ),
            [WAV],
            pocket_timeout=None,
        )
        # Equal to the heavy URL, so the speech timeout is ignored and the
        # legacy default (SHELBY_POCKET_TIMEOUT unset → 27) is used.
        self.assertEqual([(HEAVY, 27)], same)

        default = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [WAV],
            pocket_timeout=None,
        )
        self.assertEqual([(LIGHT, 12)], default)

    def test_fallback_timeout_override_clamp_and_bad_value(self):
        overridden = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_fallback_timeout_s=60,
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 60)], overridden)

        coerced = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_fallback_timeout_s="60",
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 60)], coerced)

        high = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_fallback_timeout_s=99,
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 90)], high)

        low = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_fallback_timeout_s=0,
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 1)], low)

        bad = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_fallback_timeout_s="nope",
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], bad)

        flagged = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_fallback_timeout_s=True,
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], flagged)

    def test_light_failure_writes_suppress_marker(self):
        now = 1_800_000_000.0
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
            now=now,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)
        marker = self.marker_path()
        self.assertTrue(marker.is_file())
        self.assertEqual(0o600, marker.stat().st_mode & 0o777)
        data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(LIGHT, data["url"])
        self.assertEqual(now + 120, data["until"])

    def test_suppressed_marker_fetches_heavy_only(self):
        now = 1_800_000_000.0
        until = now + 30
        self.marker_path().write_text(
            json.dumps({"url": LIGHT, "until": until}),
            encoding="utf-8",
        )
        logs = []
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [WAV],
            now=now,
            logs=logs,
            preserve_marker=True,
        )
        self.assertEqual([(HEAVY, 45)], calls)
        stamp = time.strftime("%H:%M:%S", time.localtime(until))
        self.assertIn(
            f"pocket light lane suppressed until {stamp} — heavy only",
            logs,
        )

    def test_light_success_leaves_no_marker(self):
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [WAV],
        )
        self.assertEqual([(LIGHT, 12)], calls)
        self.assertFalse(self.marker_path().exists())

    def test_expired_or_current_marker_fetches_light_first(self):
        now = 1_800_000_000.0
        for until in (now - 1, now):
            with self.subTest(until=until):
                self.marker_path().write_text(
                    json.dumps({"url": LIGHT, "until": until}),
                    encoding="utf-8",
                )
                calls = self.run_speak(
                    self.pocket_cfg(pocket_tts_url_speech=LIGHT),
                    [WAV],
                    now=now,
                    preserve_marker=True,
                )
                self.assertEqual([(LIGHT, 12)], calls)

    def test_until_equal_to_suppress_window_fetches_heavy_only(self):
        now = 1_800_000_000.0
        suppress_s = 120
        self.marker_path().write_text(
            json.dumps({"url": LIGHT, "until": now + suppress_s}),
            encoding="utf-8",
        )
        calls = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_suppress_s=suppress_s,
            ),
            [WAV],
            now=now,
            preserve_marker=True,
        )
        self.assertEqual([(HEAVY, 45)], calls)

    def test_marker_for_other_url_is_not_suppressed(self):
        now = 1_800_000_000.0
        self.marker_path().write_text(
            json.dumps({"url": "http://127.0.0.1:9", "until": now + 500}),
            encoding="utf-8",
        )
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
            now=now,
            preserve_marker=True,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)

    def test_corrupt_marker_is_not_suppressed(self):
        self.marker_path().write_text("{not json", encoding="utf-8")
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
            preserve_marker=True,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)

    def test_suppress_s_zero_writes_no_marker(self):
        calls = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_suppress_s=0,
            ),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)
        self.assertFalse(self.marker_path().exists())

    def test_fifo_marker_is_not_suppressed_and_does_not_block(self):
        os.mkfifo(self.marker_path())
        box = {}

        def run():
            try:
                box["calls"] = self.run_speak(
                    self.pocket_cfg(pocket_tts_url_speech=LIGHT),
                    [None, WAV],
                    preserve_marker=True,
                )
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "speak blocked on fifo marker")
        if "error" in box:
            raise box["error"]
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], box["calls"])

    def test_symlink_marker_is_not_suppressed(self):
        now = 1_800_000_000.0
        real = self.marker_dir() / "armed.json"
        real.write_text(
            json.dumps({"url": LIGHT, "until": now + 30}),
            encoding="utf-8",
        )
        before = (real.read_bytes(), real.stat().st_mode)
        os.symlink(real, self.marker_path())
        voices = []
        started = time.monotonic()
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
            now=now,
            preserve_marker=True,
            voices=voices,
        )
        self.assertLessEqual(time.monotonic() - started, 1.75)
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)
        self.assertEqual(["case-o", "case-o"], voices)
        self.assertEqual([WAV], self.played_audio)
        self.assertEqual(before, (real.read_bytes(), real.stat().st_mode))
        self.assertFalse(self.marker_path().is_symlink())

    def test_oversized_marker_is_not_suppressed(self):
        now = 1_800_000_000.0
        body = json.dumps({"url": LIGHT, "until": now + 30}).encode("utf-8")
        padded = body + b" " * (5000 - len(body))
        self.assertEqual(5000, len(padded))
        # Trailing spaces keep the whole file valid JSON, so only the size cap
        # can refuse it.
        self.assertEqual({"url": LIGHT, "until": now + 30}, json.loads(padded))
        self.marker_path().write_bytes(padded)
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
            now=now,
            preserve_marker=True,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)

    def test_until_beyond_suppress_window_is_not_suppressed(self):
        now = 1_800_000_000.0
        self.marker_path().write_text(
            json.dumps({"url": LIGHT, "until": now + 10000}),
            encoding="utf-8",
        )
        calls = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_suppress_s=120,
            ),
            [None, WAV],
            now=now,
            preserve_marker=True,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)

    def test_suppress_s_zero_ignores_armed_marker(self):
        now = 1_800_000_000.0
        marker = self.marker_path()
        marker.write_text(
            json.dumps({"url": LIGHT, "until": now + 30}),
            encoding="utf-8",
        )
        before = marker.read_bytes()
        calls = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_suppress_s=0,
            ),
            [None, WAV],
            now=now,
            preserve_marker=True,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)
        self.assertEqual(before, marker.read_bytes())

    def test_light_timeout_arms_marker_then_suppresses(self):
        import http.server

        heavy_wav = WAV
        light_wav = _pcm_wav(b"\x02\x00" * 1600)
        self.assertGreaterEqual(len(heavy_wav), 1000)
        self.assertGreaterEqual(len(light_wav), 1000)
        self.assertNotEqual(light_wav, heavy_wav)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.server.hits.append(self.path)
                delay = self.server.delay
                if delay:
                    time.sleep(delay)
                payload = self.server.payload
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    return

            def log_message(self, fmt, *args):
                return

        def start(delay, payload):
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            httpd.daemon_threads = True
            httpd.hits = []
            httpd.delay = delay
            httpd.payload = payload
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            def stop():
                httpd.shutdown()
                httpd.server_close()
                thread.join(2)

            self.addCleanup(stop)
            return httpd

        light = start(5, light_wav)
        heavy = start(0, heavy_wav)
        light_url = f"http://127.0.0.1:{light.server_address[1]}"
        heavy_url = f"http://127.0.0.1:{heavy.server_address[1]}"
        cfg = self.pocket_cfg(
            pocket_tts_url=heavy_url,
            pocket_tts_url_speech=light_url,
            pocket_tts_speech_timeout_s=1,
            pocket_tail_ms=0,
        )
        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "SHELBY_POCKET_TIMEOUT": "27",
        }
        played = []

        def play(path):
            played.append(Path(path).read_bytes())

        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                VOICE_HOOK.tempfile, "gettempdir", return_value=str(self.marker_dir())
            ),
            mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)),
            mock.patch.object(VOICE_HOOK, "play_audio_file", side_effect=play),
            mock.patch.object(VOICE_HOOK, "send_audio_remote"),
            mock.patch.object(VOICE_HOOK, "speak_edge", new=mock.AsyncMock()),
            mock.patch.object(VOICE_HOOK, "speak_kokoro", return_value=False),
            mock.patch.object(VOICE_HOOK, "speak_elevenlabs_streaming"),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            started = time.monotonic()
            VOICE_HOOK.speak("Ready now.", cfg)
            elapsed = time.monotonic() - started
            self.assertLessEqual(elapsed, 2.0)
            self.assertEqual(len(light.hits), 1)
            self.assertIn("/tts", heavy.hits)
            self.assertEqual([heavy_wav], played)
            marker = self.marker_path()
            self.assertTrue(marker.is_file())
            data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(light_url, data["url"])
            self.assertAlmostEqual(data["until"], time.time() + 120, delta=4)
            light_hits = len(light.hits)
            heavy_hits = len(heavy.hits)
            VOICE_HOOK.speak("Ready again.", cfg)
            self.assertEqual(len(light.hits) - light_hits, 0)
            self.assertEqual(light_hits, len(light.hits))
            self.assertGreater(len(heavy.hits), heavy_hits)
            self.assertEqual([heavy_wav, heavy_wav], played)


if __name__ == "__main__":
    unittest.main()
