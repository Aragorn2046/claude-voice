import builtins
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
    entry = {"sessionId": sid, "name": CO_NAME, "nameSource": "user"}
    entry.update(extra)
    return entry


class VoiceSessionGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def decide(self, session_id=SID, env=None, **kwargs):
        if env is None:
            env = _env(self.home)
        return gate.decide(session_id, env=env, home=self.home, **kwargs)

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
            "name": "Dusk \u00b7 gauntlet-BKP",
            "tmux": "gauntlet-BKP:@31.%31",
        })
        _write_entry(Path(self.home) / ".claude" / "sessions", "b.json", _co_entry())
        decision = self.decide(env=_env(self.home, CLAUDE_CONFIG_DIR=str(config_dir)))
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

        claude = Path(self.home) / ".claude"
        with mock.patch("voice_session_gate.os.listdir", wraps=os.listdir) as listed:
            decision = self.decide(env=_env(self.home, CLAUDE_CONFIG_DIR=str(claude)))
        self.assertTrue(decision.allow)
        self.assertEqual("registry_name", decision.reason)
        self.assertEqual(1, listed.call_count)

    def test_registry_gauntlet_denied(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
            "name": "Dusk \u00b7 gauntlet-BKP",
            "tmux": "gauntlet-BKP:@31.%31",
        })
        decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("registry_not_co", decision.reason)

    def test_user_name_does_not_use_tmux(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
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
            "name": "Day \u00b7 co-shelby-y",
            "nameSource": "user",
            "entrypoint": "cli",
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
                    "name": name,
                    "nameSource": "user",
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

    def test_ignores_unreadable_and_non_json_entries(self):
        sessions = Path(self.home) / ".claude" / "sessions"
        sessions.mkdir(parents=True)
        co = _co_entry()
        (sessions / "broken.json").write_text("{", encoding="utf-8")
        (sessions / "list.json").write_text(json.dumps([co]), encoding="utf-8")
        (sessions / "null.json").write_text("null", encoding="utf-8")
        huge = dict(co)
        huge["pad"] = "x" * 70000
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
        real_open = open

        def spy(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=spy):
            decision = self.decide()
        self.assertFalse(decision.allow)
        self.assertEqual("not_registered", decision.reason)
        names = [os.path.basename(path) for path in opened]
        self.assertNotIn("4242.deadbeef.key", names)
        self.assertNotIn("link.json", names)
        self.assertNotIn("huge.json", names)
        self.assertFalse(decision.reason.startswith("error:"))

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
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", _co_entry())
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
        log = mock.Mock()
        patches = (
            mock.patch.object(VOICE_HOOK, "load_config", return_value=cfg),
            mock.patch.object(VOICE_HOOK, "speak", speak),
            mock.patch.object(VOICE_HOOK, "_record_missing_voice_block", missing),
            mock.patch.object(VOICE_HOOK, "acquire_lock", return_value=object()),
            mock.patch.object(VOICE_HOOK, "release_lock"),
            mock.patch.object(VOICE_HOOK, "log", log),
            mock.patch.object(VOICE_HOOK.sys, "stdin", io.StringIO(payload)),
            mock.patch.dict(os.environ, env, clear=True),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        VOICE_HOOK.main()
        return speak, missing, log

    def test_missing_policy_speaks_without_importing_gate(self):
        saved = sys.modules.pop("voice_session_gate", None)
        self.addCleanup(lambda: sys.modules.__setitem__("voice_session_gate", saved) if saved is not None else None)
        speak, missing, _log = self.run_main(
            {"tts_enabled": True, "source_machine": "day"},
            "<voice>Ready now.</voice>",
        )
        speak.assert_called_once()
        missing.assert_not_called()
        self.assertNotIn("voice_session_gate", sys.modules)

    def test_co_only_non_co_skips_speak_and_missing_block(self):
        speak, missing, log = self.run_main(
            {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
            "No voice block in this prose.",
        )
        speak.assert_not_called()
        missing.assert_not_called()
        lines = [call.args[0] for call in log.call_args_list if call.args]
        self.assertTrue(any("voice-gate deny" in line for line in lines), lines)
        self.assertTrue(any("sid=abcdef01" in line for line in lines), lines)

    def test_co_only_registered_session_speaks(self):
        _write_entry(Path(self.home) / ".claude" / "sessions", "session.json", {
            "sessionId": SID,
            "name": "Day \u00b7 co-shelby-y",
            "nameSource": "user",
            "entrypoint": "cli",
            "tmux": "co-shelby-y",
        })
        speak, missing, log = self.run_main(
            {"speak_sessions": "co_only", "tts_enabled": True, "source_machine": "day"},
            "<voice>Ready now.</voice>",
        )
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
            speak, missing, log = self.run_main(
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

    def run_speak(self, cfg, responses, pocket_timeout="27"):
        calls = []
        pending = list(responses)

        def fetch(_text, _voice, base_url, timeout):
            calls.append((base_url, timeout))
            if not pending:
                return None
            return pending.pop(0)

        env = {
            "HOME": self.home,
            "PATH": "/usr/bin:/bin",
            "SHELBY_POCKET_TIMEOUT": pocket_timeout,
        }
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(VOICE_HOOK, "find_audible_path", return_value=(None, True)), \
                mock.patch.object(VOICE_HOOK, "_pocket_fetch", side_effect=fetch), \
                mock.patch.object(VOICE_HOOK, "_wav_tempo", side_effect=lambda wav, _speed: wav), \
                mock.patch.object(VOICE_HOOK, "_pad_wav_tail", side_effect=lambda wav, **_kwargs: wav), \
                mock.patch.object(VOICE_HOOK, "play_audio_file"), \
                mock.patch.object(VOICE_HOOK, "send_audio_remote"), \
                mock.patch.object(VOICE_HOOK, "speak_edge", new=mock.AsyncMock()), \
                mock.patch.object(VOICE_HOOK, "speak_kokoro", return_value=False), \
                mock.patch.object(VOICE_HOOK, "log"):
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


if __name__ == "__main__":
    unittest.main()
