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
WAV = b"RIFF" + (b"\x00" * 2000)


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
        self.addCleanup(lambda: key_path.chmod(0o644) if key_path.exists() else None)
        key_path.chmod(0)
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

        with mock.patch("voice_session_gate.glob.glob", side_effect=OSError("boom")):
            decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("error:OSError", decision.reason)

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
            {"allow": True, "reason": "registry_name", "session_id": SID},
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
            {"allow": False, "reason": "worker", "session_id": SID},
            json.loads(denied.stdout),
        )

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
        nested = "[" * 10000
        self.assertLess(len(nested.encode("utf-8")), 65536)
        (sessions / "a.json").write_text(nested, encoding="utf-8")
        _write_entry(sessions, "b.json", _co_entry(pid=111))
        decision = self.decide(pid_alive=lambda pid: pid == 111)
        self.assertFalse(decision.allow)
        self.assertEqual("registry_unreadable", decision.reason)

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


class VoiceHookGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._old_sigterm = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, self._old_sigterm)

    def run_main(self, cfg, message, session_id=SID):
        payload = json.dumps({
            "session_id": session_id,
            "last_assistant_message": message,
        })
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "CLAUDE_SESSION_TYPE": "main"}
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
                  preserve_marker=False):
        calls = []
        pending = list(responses)
        if not preserve_marker and self.marker_path().exists():
            self.marker_path().unlink()

        def fetch(_text, _voice, base_url, timeout):
            calls.append((base_url, timeout))
            if not pending:
                return None
            return pending.pop(0)

        def capture(msg):
            if logs is not None:
                logs.append(msg)

        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "SHELBY_POCKET_TIMEOUT": pocket_timeout,
        }
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(VOICE_HOOK.tempfile, "gettempdir", return_value=str(self.marker_dir())),
            mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)),
            mock.patch.object(VOICE_HOOK, "_pocket_fetch", side_effect=fetch),
            mock.patch.object(VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav),
            mock.patch.object(VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav),
            mock.patch.object(VOICE_HOOK, "play_audio_file"),
            mock.patch.object(VOICE_HOOK, "send_audio_remote"),
            mock.patch.object(VOICE_HOOK, "speak_edge", new=mock.AsyncMock()),
            mock.patch.object(VOICE_HOOK, "speak_kokoro", return_value=False),
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
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)

    def test_unset_speech_url_keeps_heavy_timeout(self):
        calls = self.run_speak(self.pocket_cfg(), [None, WAV])
        self.assertEqual([(HEAVY, 27)], calls)

    def test_speech_url_same_as_heavy_has_no_fallback(self):
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=HEAVY),
            [None, WAV],
        )
        self.assertEqual([(HEAVY, 12)], calls)

    def test_speech_timeout_clamps_and_rejects_bad_values(self):
        clamped = self.run_speak(
            self.pocket_cfg(
                pocket_tts_url_speech=LIGHT,
                pocket_tts_speech_timeout_s=99,
            ),
            [None, WAV],
            pocket_timeout="80",
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
        os.symlink(real, self.marker_path())
        calls = self.run_speak(
            self.pocket_cfg(pocket_tts_url_speech=LIGHT),
            [None, WAV],
            now=now,
            preserve_marker=True,
        )
        self.assertEqual([(LIGHT, 12), (HEAVY, 45)], calls)

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

        wav = b"RIFF" + (b"\x00" * 2000)
        self.assertGreaterEqual(len(wav), 1000)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.server.hits.append(self.path)
                delay = self.server.delay
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(wav)))
                    self.end_headers()
                    self.wfile.write(wav)
                except OSError:
                    return

            def log_message(self, fmt, *args):
                return

        def start(delay):
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            httpd.daemon_threads = True
            httpd.hits = []
            httpd.delay = delay
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()

            def stop():
                httpd.shutdown()
                httpd.server_close()
                thread.join(2)

            self.addCleanup(stop)
            return httpd

        light = start(5)
        heavy = start(0)
        light_url = f"http://127.0.0.1:{light.server_address[1]}"
        heavy_url = f"http://127.0.0.1:{heavy.server_address[1]}"
        cfg = self.pocket_cfg(
            pocket_tts_url=heavy_url,
            pocket_tts_url_speech=light_url,
            pocket_tts_speech_timeout_s=1,
        )
        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "SHELBY_POCKET_TIMEOUT": "27",
        }
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                VOICE_HOOK.tempfile, "gettempdir", return_value=str(self.marker_dir())
            ),
            mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)),
            mock.patch.object(VOICE_HOOK, "play_audio_file"),
            mock.patch.object(VOICE_HOOK, "send_audio_remote"),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            started = time.monotonic()
            VOICE_HOOK.speak("Ready now.", cfg)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 4.0)
            self.assertIn("/tts", heavy.hits)
            marker = self.marker_path()
            self.assertTrue(marker.is_file())
            data = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(light_url, data["url"])
            self.assertAlmostEqual(data["until"], time.time() + 120, delta=4)
            light_hits = len(light.hits)
            heavy_hits = len(heavy.hits)
            VOICE_HOOK.speak("Ready again.", cfg)
            self.assertEqual(light_hits, len(light.hits))
            self.assertGreater(len(heavy.hits), heavy_hits)


if __name__ == "__main__":
    unittest.main()
