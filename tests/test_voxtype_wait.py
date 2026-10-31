"""VoxType speech-wait gate. No real audio: paplay is a temp stand-in."""

import ast
import contextlib
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


HOOK_PATH = Path(__file__).parents[1] / "scripts" / "voice-stop-hook.py"
SPEC = importlib.util.spec_from_file_location("voice_stop_hook_voxtype", HOOK_PATH)
VOICE_HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VOICE_HOOK)

PCM = b"\x00" * 4800
PAPLAY_ARGV = ["paplay", "--raw", "--rate=24000", "--channels=1", "--format=s16le"]
FAKE_PAPLAY = r"""
import os
import sys
import time

log_path = os.environ["FAKE_PAPLAY_LOG"]
play_s = float(os.environ.get("FAKE_PLAY_S", "1.0"))


def append(line):
    fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


append(f"start {os.getpid()}")
sys.stdin.buffer.read()
time.sleep(play_s)
append("end")
"""

LINUX_PROCS = unittest.skipUnless(
    sys.platform.startswith("linux"),
    "Linux process integration",
)


class _NullStdin:
    def write(self, data):
        return len(data)

    def close(self):
        return None


class _ExitedOnStopPoll:
    """Looks alive to the play loop, then already exited once stop runs."""

    def __init__(self, code):
        self.code = code
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self.stdin = _NullStdin()
        self._in_stop = False

    def poll(self):
        if self._in_stop:
            self.returncode = self.code
            return self.code
        return None

    def wait(self, timeout=None):
        if self.returncode is not None or self._in_stop:
            self.returncode = self.code
            return self.code
        raise subprocess.TimeoutExpired(PAPLAY_ARGV, 0.2 if timeout is None else timeout)

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True


class VoxTypeWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.root = root
        self.vox_dir = os.path.join(root, "voxtype")
        os.makedirs(self.vox_dir)
        self.state_path = os.path.join(self.vox_dir, "state")
        self.pid_path = os.path.join(self.vox_dir, "pid")
        self.lock_path = os.path.join(root, "sonia-tts.lock")
        self.log_path = os.path.join(root, "claude-tts.log")
        self.paplay_log = os.path.join(root, "paplay.log")
        self.bin_dir = os.path.join(root, "bin")
        os.makedirs(self.bin_dir)
        self.paplay_path = os.path.join(self.bin_dir, "paplay")
        script = "#!" + sys.executable + "\n" + FAKE_PAPLAY
        Path(self.paplay_path).write_text(script)
        os.chmod(self.paplay_path, 0o755)
        Path(self.lock_path).write_text("")
        self.pcm = PCM
        self.threads = []

        env = {
            "SHELBY_VOXTYPE_STATE_FILE": self.state_path,
            "FAKE_PAPLAY_LOG": self.paplay_log,
            "FAKE_PLAY_S": "0.1",
            "PATH": self.bin_dir + os.pathsep + os.environ.get("PATH", ""),
        }
        patchers = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(VOICE_HOOK, "LOCKFILE_PATH", self.lock_path),
            mock.patch.object(VOICE_HOOK, "LOG_PATH", self.log_path),
            mock.patch.object(VOICE_HOOK, "IS_MACOS", False),
            mock.patch.object(VOICE_HOOK, "_VOXTYPE_WAIT_ENABLED", True),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.assertTrue(os.environ.get("SHELBY_VOXTYPE_STATE_FILE"))
        self.assertEqual(os.environ["SHELBY_VOXTYPE_STATE_FILE"], self.state_path)
        self.assertNotIn("/run/user/", self.state_path)
        self.assertFalse(os.path.exists(self.state_path))
        self.assertEqual(VOICE_HOOK._voxtype_state_path(), self.state_path)
        self.assertEqual(VOICE_HOOK.LOCKFILE_PATH, self.lock_path)
        self.assertEqual(VOICE_HOOK.LOG_PATH, self.log_path)
        self.assertEqual(shutil.which("paplay"), self.paplay_path)
        VOICE_HOOK._current_player = None

    def tearDown(self):
        try:
            self.write_state("idle")
        except OSError:
            pass
        for pid in self.player_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for thread in self.threads:
            thread.join(timeout=2)

    def write_state(self, text):
        path = Path(self.state_path)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(text.encode())
        os.replace(tmp, path)

    def write_pid(self, pid):
        Path(self.pid_path).write_bytes(str(pid).encode())

    def log_text(self):
        try:
            return Path(self.log_path).read_text()
        except FileNotFoundError:
            return ""

    def _play_lines(self):
        try:
            return Path(self.paplay_log).read_text().splitlines()
        except FileNotFoundError:
            return []

    def starts(self):
        return [line for line in self._play_lines() if line.startswith("start ")]

    def ends(self):
        return [line for line in self._play_lines() if line == "end"]

    def player_pids(self):
        return [int(line.split()[1]) for line in self.starts()]

    def pid_running(self, pid):
        """True while pid is executing. Zombies and reaped pids are not running."""
        if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
            try:
                info = os.waitid(
                    os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                info = None
            else:
                if info is not None:
                    return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_starts(self, count, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.starts()) >= count:
                return True
            time.sleep(0.01)
        return False

    def start_play(self):
        errors = []
        # Patch on this thread. The worker only calls the already-patched run.
        patcher = mock.patch.object(
            VOICE_HOOK.subprocess, "run",
            side_effect=AssertionError("ungated subprocess.run"),
        )
        patcher.start()
        stopped = False

        def stop_patch():
            nonlocal stopped
            if stopped:
                return
            stopped = True
            patcher.stop()

        self.addCleanup(stop_patch)

        def run():
            try:
                VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.errors = errors
        self.threads.append(thread)
        thread.start()
        return thread

    def finish(self, thread, timeout):
        thread.join(timeout)
        if thread.is_alive():
            self.write_state("idle")
            for pid in self.player_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            thread.join(3)
            self.fail(f"playback still running after {timeout}s")

    @contextlib.contextmanager
    def forbid_voxtype_io(self):
        real_exists = os.path.exists
        real_open = open
        real_os_open = os.open

        def forbidden(path):
            text = os.fspath(path)
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            if (
                text in (self.state_path, self.pid_path)
                or text.endswith("/voxtype/state")
                or text.endswith("/voxtype/pid")
                or ("/run/user/" in text and "voxtype" in text)
            ):
                raise AssertionError(f"voxtype path accessed: {text}")

        def guarded_exists(path):
            forbidden(path)
            return real_exists(path)

        def guarded_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                forbidden(file)
            return real_open(file, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            if isinstance(path, (str, bytes, os.PathLike)):
                forbidden(path)
            return real_os_open(path, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK.os.path, "exists", guarded_exists), \
                mock.patch("builtins.open", guarded_open), \
                mock.patch.object(VOICE_HOOK.os, "open", guarded_os_open):
            yield

    def dead_pid(self):
        for _ in range(20):
            proc = subprocess.Popen(["/bin/true"])
            proc.wait()
            try:
                os.kill(proc.pid, 0)
            except ProcessLookupError:
                return proc.pid
        self.fail("could not observe a reaped pid")

    def test_absent_state_is_zero_cost_and_uses_today_path(self):
        self.assertFalse(os.path.exists(self.state_path))
        self.assertFalse(VOICE_HOOK.voxtype_busy())
        started = time.perf_counter()
        waited = VOICE_HOOK.wait_for_voxtype_idle()
        self.assertEqual(waited, 0.0)
        self.assertLess((time.perf_counter() - started) * 1000, 5)
        self.assertNotIn("voxtype:", self.log_text())

        missing = os.path.join(self.root, "absent", "state")
        with mock.patch.dict(os.environ, {"SHELBY_VOXTYPE_STATE_FILE": missing}):
            total = 0.0
            started = time.perf_counter()
            for _ in range(1000):
                total += VOICE_HOOK.wait_for_voxtype_idle()
            elapsed = time.perf_counter() - started
        self.assertEqual(total, 0.0)
        self.assertLess((elapsed / 1000) * 1e3, 0.05)

        with mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        run.assert_called_once_with(
            PAPLAY_ARGV, input=self.pcm, capture_output=True, timeout=30,
        )
        self.assertEqual(self.starts(), [])

    @LINUX_PROCS
    def test_idle_starts_player_immediately(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "0.1"
        started = time.monotonic()
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 0.3))
        self.assertLess(time.monotonic() - started, 0.3)
        self.finish(thread, 2)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1)
        self.assertNotIn("voxtype: waiting", self.log_text())

    def _assert_holds_then_plays(self, state):
        self.write_state(state)
        os.environ["FAKE_PLAY_S"] = "0.05"
        thread = self.start_play()
        time.sleep(0.6)
        self.assertEqual(self.starts(), [])
        self.write_state("idle")
        flipped = time.monotonic()
        self.assertTrue(self.wait_starts(1, 0.4), self.log_text())
        self.assertLess(time.monotonic() - flipped, 0.4)
        self.finish(thread, 2)
        self.assertEqual(thread.errors, [])
        self.assertIn(f"voxtype: waiting (state={state})", self.log_text())
        self.assertIn("voxtype: idle after", self.log_text())

    @LINUX_PROCS
    def test_recording_holds_then_plays_when_idle(self):
        self._assert_holds_then_plays("recording")

    @LINUX_PROCS
    def test_transcribing_holds_then_plays_when_idle(self):
        self._assert_holds_then_plays("transcribing")

    def test_stopped_empty_and_garbage_are_not_busy(self):
        for value in ("stopped", "", "garbage"):
            with self.subTest(value=repr(value)):
                self.write_state(value)
                self.assertFalse(VOICE_HOOK.voxtype_busy())
                started = time.perf_counter()
                self.assertEqual(VOICE_HOOK.wait_for_voxtype_idle(), 0.0)
                self.assertLess(time.perf_counter() - started, 0.05)
        self.assertNotIn("voxtype: waiting", self.log_text())

    @LINUX_PROCS
    def test_cap_speaks_anyway(self):
        self.write_state("recording")
        os.environ["FAKE_PLAY_S"] = "0.4"
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.8):
            started = time.monotonic()
            thread = self.start_play()
            seen = None
            while time.monotonic() - started < 1.2:
                if self.starts():
                    seen = time.monotonic() - started
                    break
                time.sleep(0.01)
            self.assertIsNotNone(seen, self.log_text())
            self.assertGreaterEqual(seen, 0.8)
            self.assertLessEqual(seen, 1.2)
            self.finish(thread, 2)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(self.ends()), 1)
        text = self.log_text()
        self.assertIn("voxtype: cap reached after", text)
        self.assertIn("speaking anyway", text)
        self.assertNotIn("will replay once", text)

    @LINUX_PROCS
    def test_dead_pid_is_not_busy(self):
        pid = self.dead_pid()
        self.write_state("recording")
        self.write_pid(pid)
        self.assertFalse(VOICE_HOOK.voxtype_busy())
        os.environ["FAKE_PLAY_S"] = "0.1"
        started = time.monotonic()
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 0.3), self.log_text())
        self.assertLess(time.monotonic() - started, 0.3)
        self.finish(thread, 2)
        self.assertEqual(thread.errors, [])
        self.assertNotIn("voxtype: waiting", self.log_text())
        self.assertEqual(len(self.starts()), 1)

    @LINUX_PROCS
    def test_dictation_during_playback_replays_once(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1))
        started = time.monotonic()
        time.sleep(0.5)
        self.write_state("recording")
        recorded = time.monotonic()
        first = self.player_pids()[0]
        gone = None
        while time.monotonic() - recorded < 0.5:
            if not self.pid_running(first):
                gone = time.monotonic() - recorded
                break
            time.sleep(0.01)
        self.assertIsNotNone(gone, "first player still running")
        self.assertLess(gone, 0.5)
        remaining = 1.2 - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        self.write_state("idle")
        self.finish(thread, 5)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(len(self.ends()), 1)
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay once",
            self.log_text(),
        )

    @LINUX_PROCS
    def test_dictation_during_replay_drops_line(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1))
        started = time.monotonic()
        time.sleep(0.5)
        self.write_state("recording")
        remaining = 1.2 - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        self.write_state("idle")
        self.assertTrue(self.wait_starts(2, 2), self.log_text())
        time.sleep(0.3)
        self.write_state("recording")
        cut = time.monotonic()
        self.finish(thread, 2)
        self.assertLess(time.monotonic() - cut, 1.5)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 2)
        text = self.log_text()
        self.assertIn(
            "voxtype: dictation interrupted the replay, line dropped", text,
        )
        self.assertFalse(self.pid_running(self.player_pids()[1]))

    @LINUX_PROCS
    def test_external_kill_does_not_replay(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "5"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1))
        os.kill(self.player_pids()[0], signal.SIGTERM)
        self.finish(thread, 2)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1)
        self.assertNotIn("will replay once", self.log_text())

    def test_lock_heartbeat_while_waiting(self):
        self.write_state("recording")
        old = time.time() - 3600
        os.utime(self.lock_path, (old, old))
        before = os.path.getmtime(self.lock_path)
        real_utime = os.utime
        seen = []

        def wrapped(path, times=None):
            seen.append(os.fspath(path))
            return real_utime(path, times)

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_HEARTBEAT_S", 0.5), \
                mock.patch.object(VOICE_HOOK.os, "utime", side_effect=wrapped):
            waited = VOICE_HOOK.wait_for_voxtype_idle(1.5)
        after = os.path.getmtime(self.lock_path)
        self.assertGreater(after, before)
        self.assertGreaterEqual(waited, 1.5)
        self.assertLess(waited, 2.5)
        self.assertTrue(seen)
        self.assertTrue(all(path == self.lock_path for path in seen))

    def test_macos_does_not_read_state(self):
        self.write_state("recording")
        with mock.patch.object(VOICE_HOOK, "IS_MACOS", True), self.forbid_voxtype_io():
            self.assertFalse(VOICE_HOOK.voxtype_present())
            self.assertFalse(VOICE_HOOK.voxtype_busy())
            self.assertEqual(VOICE_HOOK.wait_for_voxtype_idle(), 0.0)
            with mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
                VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], "afplay")
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(self.starts(), [])

    def test_kill_switch_uses_today_path(self):
        self.write_state("recording")
        with mock.patch.object(VOICE_HOOK, "_VOXTYPE_WAIT_ENABLED", False), \
                self.forbid_voxtype_io():
            self.assertFalse(VOICE_HOOK.voxtype_present())
            self.assertFalse(VOICE_HOOK.voxtype_busy())
            self.assertEqual(VOICE_HOOK.wait_for_voxtype_idle(), 0.0)
            with mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
                VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        run.assert_called_once_with(
            PAPLAY_ARGV, input=self.pcm, capture_output=True, timeout=30,
        )
        self.assertEqual(self.starts(), [])

    def _fake_requests(self):
        chunks = [b"\x01\x02", b"", b"\x03\x04"]
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.iter_content.side_effect = lambda chunk_size=4096: iter(chunks)
        requests_mod = mock.Mock()
        requests_mod.post.return_value = response
        return requests_mod

    def test_elevenlabs_linux_branch_respects_voxtype(self):
        requests_mod = self._fake_requests()
        self.write_state("idle")
        with mock.patch.dict(sys.modules, {"requests": requests_mod}), \
                mock.patch.object(VOICE_HOOK, "_play_pcm_voxtype_gated") as gated, \
                mock.patch.object(VOICE_HOOK.subprocess, "Popen") as popen:
            VOICE_HOOK.speak_elevenlabs_streaming("hi", "voice", "model", "key")
        gated.assert_called_once_with(b"\x01\x02\x03\x04", 24000, 1)
        popen.assert_not_called()

        os.remove(self.state_path)
        requests_mod = self._fake_requests()
        proc = mock.Mock()
        with mock.patch.dict(sys.modules, {"requests": requests_mod}), \
                mock.patch.object(VOICE_HOOK, "_play_pcm_voxtype_gated") as gated, \
                mock.patch.object(VOICE_HOOK.subprocess, "Popen", return_value=proc) as popen:
            VOICE_HOOK.speak_elevenlabs_streaming("hi", "voice", "model", "key")
        gated.assert_not_called()
        popen.assert_called_once_with(PAPLAY_ARGV, stdin=subprocess.PIPE)
        written = [call.args[0] for call in proc.stdin.write.call_args_list]
        self.assertEqual(written, [b"\x01\x02", b"\x03\x04"])
        proc.stdin.close.assert_called_once()
        proc.wait.assert_called_once_with(timeout=180)

    @LINUX_PROCS
    def test_sigterm_terminates_gated_player(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "30"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 2))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and VOICE_HOOK._current_player is None:
            time.sleep(0.01)
        self.assertIsNotNone(VOICE_HOOK._current_player)
        pid = self.player_pids()[0]
        self.assertTrue(self.pid_running(pid))
        with mock.patch.object(VOICE_HOOK.os, "_exit") as exit_, \
                mock.patch.object(VOICE_HOOK, "release_lock") as release:
            VOICE_HOOK.handle_sigterm(15, None)
        exit_.assert_called_once_with(0)
        release.assert_called_once_with(None)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and self.pid_running(pid):
            time.sleep(0.01)
        self.assertFalse(self.pid_running(pid))
        self.finish(thread, 2)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1)

    @LINUX_PROCS
    def test_arming_race_stops_player_spawned_into_dictation(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        real_popen = subprocess.Popen
        flipped = []

        def popen_then_record(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            if not flipped:
                self.write_state("recording")
                flipped.append(time.monotonic())
            return proc

        with mock.patch.object(
            VOICE_HOOK.subprocess, "Popen", side_effect=popen_then_record,
        ):
            thread = self.start_play()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not flipped:
                time.sleep(0.01)
            self.assertTrue(flipped, self.log_text())
            flip = flipped[0]
            self.assertTrue(self.wait_starts(1, 1), self.log_text())
            first = self.player_pids()[0]
            gone = None
            while time.monotonic() - flip < 0.5:
                if not self.pid_running(first):
                    gone = time.monotonic() - flip
                    break
                time.sleep(0.01)
            self.assertIsNotNone(gone, "first player still running")
            self.assertLess(gone, 0.5)
            remaining = 0.8 - (time.monotonic() - flip)
            if remaining > 0:
                time.sleep(remaining)
            self.write_state("idle")
            self.finish(thread, 6)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(len(self.ends()), 1)
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay once",
            self.log_text(),
        )

    def _play_with_player_already_exited(self, code):
        """Gated playback whose player has exited by the time stop runs."""
        self.write_state("idle")
        fake = _ExitedOnStopPoll(code)
        results = []
        real_stop = VOICE_HOOK._stop_player

        def spy(proc):
            proc._in_stop = True
            result = real_stop(proc)
            results.append(result)
            return result

        def popen(*_args, **_kwargs):
            self.write_state("recording")
            return fake

        with mock.patch.object(VOICE_HOOK.subprocess, "Popen", side_effect=popen) as popen_mock, \
                mock.patch.object(VOICE_HOOK, "_stop_player", side_effect=spy):
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        self.assertEqual(popen_mock.call_count, 1)
        self.assertEqual(results, [False])
        self.assertFalse(fake.terminate_called)
        self.assertFalse(fake.kill_called)
        self.assertNotIn("will replay once", self.log_text())
        self.assertNotIn("line dropped", self.log_text())

    def test_natural_exit_while_busy_does_not_replay(self):
        self._play_with_player_already_exited(0)

    def test_external_kill_while_busy_does_not_replay(self):
        proc = mock.Mock()
        proc.poll.return_value = -15
        proc.wait.return_value = -15
        self.assertFalse(VOICE_HOOK._stop_player(proc))
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        self._play_with_player_already_exited(-15)

    @LINUX_PROCS
    def test_sigterm_during_popen_terminates_registered_player(self):
        if not hasattr(signal, "pthread_sigmask"):
            self.skipTest("signal.pthread_sigmask is required")
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "5"
        real_popen = subprocess.Popen
        spawned = {}

        class _HookExit(Exception):
            pass

        def popen_then_signal(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            spawned["proc"] = proc
            os.kill(os.getpid(), signal.SIGTERM)
            return proc

        previous = signal.getsignal(signal.SIGTERM)

        def safe_restore():
            signal.signal(signal.SIGTERM, lambda signum, frame: None)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
            signal.signal(signal.SIGTERM, previous)

        self.addCleanup(safe_restore)
        signal.signal(signal.SIGTERM, VOICE_HOOK.handle_sigterm)
        with mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=_HookExit), \
                mock.patch.object(VOICE_HOOK, "release_lock", return_value=None), \
                mock.patch.object(
                    VOICE_HOOK.subprocess, "Popen", side_effect=popen_then_signal,
                ):
            with self.assertRaises(_HookExit):
                VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        proc = spawned["proc"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.01)
        self.assertIsNotNone(proc.poll(), "player still alive")
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def test_huge_pid_is_ignored_and_state_stays_busy(self):
        self.write_state("recording")
        Path(self.pid_path).write_bytes(b"9999999999999999999999")
        self.assertFalse(VOICE_HOOK._voxtype_pid_is_dead(self.state_path))
        self.assertEqual(VOICE_HOOK._voxtype_busy_state(), "recording")
        self.assertTrue(VOICE_HOOK.voxtype_busy())

    def test_kill_switch_parser_and_main_wiring(self):
        parse = VOICE_HOOK._voxtype_wait_enabled
        for value in ("false", "0", "off", "no", 0, False):
            with self.subTest(disabled=repr(value)):
                self.assertFalse(parse(value))
        for value in (True, "true"):
            with self.subTest(enabled=repr(value)):
                self.assertTrue(parse(value))
        self.assertTrue(parse({}.get("voxtype_wait", True)))
        self.assertFalse(parse({"voxtype_wait": "false"}.get("voxtype_wait", True)))
        self.assertTrue(parse({"voxtype_wait": "true"}.get("voxtype_wait", True)))
        self.assertFalse(parse({"voxtype_wait": " off "}.get("voxtype_wait", True)))

        tree = ast.parse(HOOK_PATH.read_text())
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        assigns = [
            stmt for stmt in main.body
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_VOXTYPE_WAIT_ENABLED"
                for target in stmt.targets
            )
        ]
        self.assertEqual(len(assigns), 1)
        call = assigns[0].value
        self.assertIsInstance(call, ast.Call)
        self.assertIsInstance(call.func, ast.Name)
        self.assertEqual(call.func.id, "_voxtype_wait_enabled")


if __name__ == "__main__":
    unittest.main()
