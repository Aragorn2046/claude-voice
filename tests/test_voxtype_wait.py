"""VoxType speech-wait gate. No real audio: paplay is a temp stand-in."""

import ast
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
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

# Holds the speech lock in another process. flock is per open file description,
# so a thread in this process cannot stand in for a sibling hook.
LOCK_HOLDER = r"""
import fcntl
import os
import sys
import time

path, sid, ready_path, release_path, pid_text = sys.argv[1:6]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    if not pid_text:
        pid_text = str(os.getpid())
    payload = (pid_text + "\n" + sid + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)
    ready_fd = os.open(ready_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    os.close(ready_fd)
    if release_path == "-":
        time.sleep(30)
    else:
        while not os.path.exists(release_path):
            time.sleep(0.02)
finally:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
"""

# Long fake paplay for the steal-overlap tests. The on-PATH launcher is a
# bash wrapper named paplay that execs this body with argv[0] paplay, and
# the body sets comm to paplay, so a /proc scan sees both.
# "end" is written only when the sleep finishes. SIGTERM (the default
# disposition) kills it first. SIG_DFL is set explicitly because a holder
# that ignores SIGTERM would otherwise pass that disposition across exec.
LONG_PAPLAY = r"""
import os
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_DFL)
try:
    fd = os.open("/proc/self/comm", os.O_WRONLY)
    try:
        os.write(fd, b"paplay")
    finally:
        os.close(fd)
except OSError:
    pass

log_path = os.environ["FAKE_PAPLAY_LOG"]


def append(line):
    fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


append(f"start {os.getpid()} {time.time():.6f}")
deadline = time.monotonic() + 5.0
while time.monotonic() < deadline:
    try:
        sys.stdin.buffer.read(1)
    except Exception:
        pass
    time.sleep(0.05)
append("end")
"""

# Child hook: same-session lock, SIGTERM -> handle_sigterm, gated paplay.
GATED_HOLDER = r"""
import importlib.util
import os
import signal
import sys

hook_path, lock_path, log_path, sid, ready_path = sys.argv[1:6]
spec = importlib.util.spec_from_file_location(
    "voice_stop_hook_holder_child", hook_path,
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
hook.LOG_PATH = log_path
hook.LOCKFILE_PATH = lock_path
hook._SESSION_ID = sid
signal.signal(signal.SIGTERM, hook.handle_sigterm)
lock = hook.acquire_lock()
if lock is None:
    sys.stderr.write("holder could not take the lock\n")
    sys.exit(1)
fd = os.open(ready_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
os.close(fd)
hook._play_pcm_voxtype_gated(b"\x00" * 4800, 24000, 1)
"""

# Ungated holder: ignores SIGTERM, stamps the lock, plays via subprocess.run.
IGNORING_HOLDER = r"""
import fcntl
import os
import signal
import subprocess
import sys

path, sid, ready_path = sys.argv[1:4]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    payload = (str(os.getpid()) + "\n" + sid + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)
    ready = os.open(ready_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    os.close(ready)
    subprocess.run(
        ["paplay", "--raw", "--rate=24000", "--channels=1", "--format=s16le"],
        input=b"\x00" * 4800,
        capture_output=True,
        timeout=30,
    )
finally:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
"""

# comm is paplay, but argv[0] is the Python interpreter. _stop_players must
# not signal it.
COMM_PAPLAY_DECOY = r"""
import os
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
try:
    fd = os.open("/proc/self/comm", os.O_WRONLY)
    try:
        os.write(fd, b"paplay")
    finally:
        os.close(fd)
except OSError:
    pass

ready_path = sys.argv[1]
ready = os.open(ready_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
os.write(ready, str(os.getpid()).encode())
os.close(ready)
while True:
    time.sleep(0.2)
"""

# Same-session holder whose cmdline contains voice-stop-hook. It ignores
# SIGTERM, holds the flock, and parents the decoy above.
DECOY_PARENT = r"""
import fcntl
import os
import signal
import subprocess
import sys
import time

path, sid, ready_path, decoy_script, decoy_ready = sys.argv[1:6]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
decoy = subprocess.Popen([sys.executable, decoy_script, decoy_ready])
deadline = time.monotonic() + 5
while not os.path.exists(decoy_ready):
    if decoy.poll() is not None or time.monotonic() > deadline:
        sys.stderr.write("decoy did not become ready\n")
        sys.exit(1)
    time.sleep(0.01)
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    payload = (str(os.getpid()) + "\n" + sid + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)
    ready = os.open(ready_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    os.close(ready)
    while True:
        time.sleep(0.05)
finally:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
"""

# Live peer whose cmdline contains voice-stop-hook. Used as an arrival pid.
PEER_SLEEP = r"""
import os
import sys
import time

ready = sys.argv[1]
fd = os.open(ready, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
os.close(fd)
time.sleep(30)
"""

# Two of these wait on one recording -> idle flip. speak() appends the line
# and then holds, so the arrival entry stays until the other hook has yielded.
# The script name contains voice-stop-hook so _is_our_hook matches the child.
ARRIVAL_CHILD = r"""
import importlib.util
import io
import json
import os
import sys
import time

hook_path, line, session_id, lock_path, log_path = sys.argv[1:6]
spec = importlib.util.spec_from_file_location(
    "voice_stop_hook_arrival_child", hook_path,
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
hook.LOCKFILE_PATH = lock_path
hook.LOG_PATH = log_path


def speak(clean, cfg, lang_hint=None):
    path = os.environ["ARRIVAL_SPOKEN_LOG"]
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (clean + "\n").encode())
    finally:
        os.close(fd)
    release = os.environ.get("ARRIVAL_SPEAK_RELEASE") or ""
    if not release:
        return
    deadline = time.monotonic() + 5
    while not os.path.exists(release) and time.monotonic() < deadline:
        time.sleep(0.01)


hook.speak = speak
hook.load_config = lambda: {
    "tts_enabled": True,
    "remote_audio": False,
    "voxtype_wait": True,
}
payload = json.dumps({
    "last_assistant_message": '<voice lang="en">' + line + "</voice>",
    "session_id": session_id,
})
sys.stdin = io.StringIO(payload)
hook.main()
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
        self.arrival_dir = os.path.join(root, "arrivals")
        self.bin_dir = os.path.join(root, "bin")
        os.makedirs(self.bin_dir)
        self.paplay_path = os.path.join(self.bin_dir, "paplay")
        self._install_paplay(FAKE_PAPLAY)
        Path(self.lock_path).write_text("")
        self.pcm = PCM
        self.threads = []

        env = {
            "SHELBY_VOXTYPE_STATE_FILE": self.state_path,
            "SHELBY_VOICE_ARRIVAL_DIR": self.arrival_dir,
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
        if VOICE_HOOK._lock_fd is not None:
            VOICE_HOOK.release_lock(VOICE_HOOK._lock_fd)
        VOICE_HOOK._SESSION_ID = ""
        VOICE_HOOK._arrival_stamp_ns = None
        VOICE_HOOK._reset_held_watch()
        VOICE_HOOK._arrival_entry_path = None

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

    def run_bounded(self, func, timeout=3.0):
        """Run func on a daemon thread. Fail if it is still alive after timeout."""
        outcome = {}

        def target():
            try:
                outcome["value"] = func()
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout)
        self.assertFalse(
            thread.is_alive(),
            f"call still blocked after {timeout:.0f}s",
        )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

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
        real_mkdir = os.mkdir
        real_makedirs = os.makedirs
        real_lstat = os.lstat
        real_listdir = os.listdir
        real_unlink = os.unlink

        def forbidden(path):
            text = os.fspath(path)
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            if (
                text in (self.state_path, self.pid_path)
                or text.endswith("/voxtype/state")
                or text.endswith("/voxtype/pid")
                or ("/run/user/" in text and "voxtype" in text)
                or self._is_arrival_path(text)
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

        def guarded_mkdir(path, *args, **kwargs):
            forbidden(path)
            return real_mkdir(path, *args, **kwargs)

        def guarded_makedirs(path, *args, **kwargs):
            forbidden(path)
            return real_makedirs(path, *args, **kwargs)

        def guarded_lstat(path, *args, **kwargs):
            forbidden(path)
            return real_lstat(path, *args, **kwargs)

        def guarded_listdir(path, *args, **kwargs):
            forbidden(path)
            return real_listdir(path, *args, **kwargs)

        def guarded_unlink(path, *args, **kwargs):
            forbidden(path)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK.os.path, "exists", guarded_exists), \
                mock.patch("builtins.open", guarded_open), \
                mock.patch.object(VOICE_HOOK.os, "open", guarded_os_open), \
                mock.patch.object(VOICE_HOOK.os, "mkdir", guarded_mkdir), \
                mock.patch.object(VOICE_HOOK.os, "makedirs", guarded_makedirs), \
                mock.patch.object(VOICE_HOOK.os, "lstat", guarded_lstat), \
                mock.patch.object(VOICE_HOOK.os, "listdir", guarded_listdir), \
                mock.patch.object(VOICE_HOOK.os, "unlink", guarded_unlink):
            yield

    def _is_arrival_path(self, text):
        if text == self.arrival_dir or text.startswith(self.arrival_dir + os.sep):
            return True
        if "voice-stop-hook-arrivals" in text:
            return True
        runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
        if runtime:
            default = os.path.join(runtime, "voice-stop-hook-arrivals")
            if text == default or text.startswith(default + os.sep):
                return True
        return False

    @contextlib.contextmanager
    def forbid_arrival_io(self):
        """Fail if the arrival registry or the real runtime dir is touched.

        The VoxType state path stays readable: an absent state file is how
        the hook learns the feature is off.
        """
        real_exists = os.path.exists
        real_open = open
        real_os_open = os.open
        real_mkdir = os.mkdir
        real_makedirs = os.makedirs
        real_lstat = os.lstat
        real_listdir = os.listdir
        real_unlink = os.unlink

        def forbidden(path):
            text = os.fspath(path)
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            if self._is_arrival_path(text):
                raise AssertionError(f"arrival path accessed: {text}")

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

        def guarded_mkdir(path, *args, **kwargs):
            forbidden(path)
            return real_mkdir(path, *args, **kwargs)

        def guarded_makedirs(path, *args, **kwargs):
            forbidden(path)
            return real_makedirs(path, *args, **kwargs)

        def guarded_lstat(path, *args, **kwargs):
            forbidden(path)
            return real_lstat(path, *args, **kwargs)

        def guarded_listdir(path, *args, **kwargs):
            forbidden(path)
            return real_listdir(path, *args, **kwargs)

        def guarded_unlink(path, *args, **kwargs):
            forbidden(path)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK.os.path, "exists", guarded_exists), \
                mock.patch("builtins.open", guarded_open), \
                mock.patch.object(VOICE_HOOK.os, "open", guarded_os_open), \
                mock.patch.object(VOICE_HOOK.os, "mkdir", guarded_mkdir), \
                mock.patch.object(VOICE_HOOK.os, "makedirs", guarded_makedirs), \
                mock.patch.object(VOICE_HOOK.os, "lstat", guarded_lstat), \
                mock.patch.object(VOICE_HOOK.os, "listdir", guarded_listdir), \
                mock.patch.object(VOICE_HOOK.os, "unlink", guarded_unlink):
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

    def _stop_proc(self, proc):
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def spawn_holder(self, sid, pid_text="", release_path=None):
        """Child process holding LOCK_EX on the temp lockfile."""
        ready = os.path.join(self.root, f"holder-ready-{time.monotonic_ns()}")
        if release_path is None:
            release_path = os.path.join(
                self.root, f"holder-release-{time.monotonic_ns()}",
            )
        script = os.path.join(self.root, "fake-voice-stop-hook-holder.py")
        if not os.path.exists(script):
            Path(script).write_text("#!" + sys.executable + "\n" + LOCK_HOLDER)
            os.chmod(script, 0o755)
        env = os.environ.copy()
        env["SHELBY_VOXTYPE_STATE_FILE"] = self.state_path
        env["SHELBY_VOICE_ARRIVAL_DIR"] = self.arrival_dir
        proc = subprocess.Popen(
            [sys.executable, script, self.lock_path, sid, ready, release_path, pid_text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.addCleanup(self._stop_proc, proc)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not os.path.exists(ready):
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8", "replace")
                self.fail(f"lock holder exited early ({proc.returncode}): {err}")
            time.sleep(0.01)
        self.assertTrue(os.path.exists(ready), "lock holder did not take the lock")
        return proc, release_path

    def _install_paplay(self, body):
        """argv[0] is paplay. The body is Python, launched via exec -a."""
        impl = os.path.join(self.bin_dir, "paplay-impl.py")
        Path(impl).write_text(body)
        wrapper = (
            "#!/bin/bash\n"
            "exec -a paplay "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(impl)
            + ' "$@"\n'
        )
        Path(self.paplay_path).write_text(wrapper)
        os.chmod(self.paplay_path, 0o755)

    def _install_long_paplay(self):
        self._install_paplay(LONG_PAPLAY)

    def _spawn_script(self, name, body, args):
        path = os.path.join(self.root, name)
        Path(path).write_text("#!" + sys.executable + "\n" + body)
        os.chmod(path, 0o755)
        err_path = path + ".err"
        err_fh = open(err_path, "w")
        self.addCleanup(err_fh.close)
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["SHELBY_VOXTYPE_STATE_FILE"] = self.state_path
        env["SHELBY_VOICE_ARRIVAL_DIR"] = self.arrival_dir
        proc = subprocess.Popen(
            [sys.executable, path, *args],
            stdout=subprocess.DEVNULL,
            stderr=err_fh,
            env=env,
        )
        self.addCleanup(self._stop_proc, proc)
        return proc, err_path

    def _holder_debug(self, err_path):
        try:
            err = Path(err_path).read_text()[-2000:]
        except OSError:
            err = ""
        return f"hook={self.log_text()!r} paplay={self._play_lines()!r} err={err!r}"

    def _wait_until(self, predicate, timeout, message):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(message)

    def _proc_stat(self, pid):
        text = Path(f"/proc/{pid}/stat").read_text()
        start = text.find("(")
        end = text.rfind(")")
        if start < 0 or end <= start:
            raise ValueError("stat")
        rest = text[end + 1:].split()
        return text[start + 1:end], rest[0], int(rest[1])

    def _gone_or_zombie(self, pid):
        try:
            _comm, state, _ppid = self._proc_stat(pid)
        except (OSError, ValueError, IndexError):
            return True
        return state == "Z"

    def _await_playing_holder(self, proc, ready, err_path):
        self._wait_until(
            lambda: os.path.exists(ready) or proc.poll() is not None,
            5.0,
            "holder did not take the lock " + self._holder_debug(err_path),
        )
        self.assertIsNone(proc.poll(), self._holder_debug(err_path))
        self._wait_until(
            lambda: self.starts() or proc.poll() is not None,
            3.0,
            "paplay did not start " + self._holder_debug(err_path),
        )
        self.assertIsNone(proc.poll(), self._holder_debug(err_path))
        old_pid = int(self.starts()[0].split()[1])
        comm, _state, ppid = self._proc_stat(old_pid)
        self.assertEqual(comm, "paplay")
        self.assertEqual(ppid, proc.pid)
        self.assertFalse(self._gone_or_zombie(old_pid))
        return old_pid

    def _assert_no_player_overlap(self, old_pid, observed_dead_at):
        before = len(self.starts())
        proc = subprocess.Popen(
            [self.paplay_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_proc, proc)
        self.assertTrue(self.wait_starts(before + 1, 2.0), self._play_lines())
        self.assertTrue(self._gone_or_zombie(old_pid))
        self.assertFalse(self._gone_or_zombie(proc.pid))
        latest = self.starts()[-1].split()
        self.assertEqual(int(latest[1]), proc.pid)
        self.assertGreaterEqual(float(latest[2]) + 0.05, observed_dead_at)
        self.assertEqual(self.ends(), [])

    def _cfg(self, **extra):
        cfg = {"tts_enabled": True, "remote_audio": False, "voxtype_wait": True}
        cfg.update(extra)
        return cfg

    def _run_main(self, session_id, text, cfg=None, on_speak=None, extra=None):
        """Run main() on a voice line. speak() is stubbed; no audio."""
        spoken = []

        def speak(clean, hook_cfg, lang_hint=None):
            spoken.append(clean)
            if on_speak is not None:
                on_speak(clean, hook_cfg, lang_hint)

        if cfg is None:
            cfg = self._cfg()
        previous = signal.getsignal(signal.SIGTERM)
        payload_obj = {
            "last_assistant_message": f'<voice lang="en">{text}</voice>',
            "session_id": session_id,
        }
        if extra:
            payload_obj.update(extra)
        payload = json.dumps(payload_obj)
        try:
            with mock.patch.dict(os.environ, {"CLAUDE_SESSION_TYPE": "main"}), \
                    mock.patch.object(VOICE_HOOK, "load_config", return_value=cfg), \
                    mock.patch.object(VOICE_HOOK, "speak", side_effect=speak), \
                    mock.patch.object(
                        VOICE_HOOK.sys, "stdin", io.StringIO(payload),
                    ):
                VOICE_HOOK.main()
        finally:
            signal.signal(signal.SIGTERM, previous)
        return spoken

    def _clear_log(self):
        Path(self.log_path).write_text("")

    def _guarded_kills(self, protect):
        """Record os.kill. Do not deliver a fatal signal to a protected pid."""
        real = os.kill
        seen = []
        protected = {int(pid) for pid in protect}
        protected.update((1, os.getpid()))

        def spy(pid, sig):
            seen.append((int(pid), sig))
            if int(pid) in protected and sig not in (0,):
                return None
            return real(pid, sig)

        return seen, spy

    def _kill_pid(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def _lock_is_held(self):
        """True when some other open file description holds the lock."""
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            return False
        finally:
            os.close(fd)

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

    def test_cap_at_gate_drops_line(self):
        """A gate that stays recording until the cap never starts a player."""
        self.write_state("recording")
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.6):
            started = time.monotonic()
            result = VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
            elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.6)
        self.assertLess(elapsed, 1.2)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertEqual(text.count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 0.6s while still busy, line dropped",
            text,
        )
        self.assertNotIn("will replay", text)

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
            "voxtype: dictation started during playback, stopped; will replay (1/5)",
            self.log_text(),
        )

    @LINUX_PROCS
    def test_two_interruptions_then_idle_plays_the_third(self):
        """Two dictations each cause a replay. The third attempt runs to end."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1), self.log_text())
        time.sleep(0.35)
        self.write_state("recording")
        first = self.player_pids()[0]
        self._wait_until(
            lambda: not self.pid_running(first), 1.0, "first player still running",
        )
        time.sleep(0.25)
        self.write_state("idle")
        self.assertTrue(self.wait_starts(2, 2), self.log_text())
        time.sleep(0.35)
        self.write_state("recording")
        second = self.player_pids()[1]
        self._wait_until(
            lambda: not self.pid_running(second), 1.0, self.log_text(),
        )
        time.sleep(0.25)
        self.write_state("idle")
        self.assertTrue(self.wait_starts(3, 2), self.log_text())
        self.finish(thread, 4)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 3)
        self.assertEqual(len(self.ends()), 1)
        text = self.log_text()
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (1/5)",
            text,
        )
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (2/5)",
            text,
        )
        self.assertNotIn("line dropped", text)

    @LINUX_PROCS
    def test_replay_limit_drops_line(self):
        """Interrupting every attempt stops after VOXTYPE_MAX_REPLAYS replays."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_REPLAYS", 2), \
                mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 2.0):
            thread = self.start_play()
            for n in (1, 2, 3):
                self.assertTrue(self.wait_starts(n, 2), self.log_text())
                time.sleep(0.25)
                self.write_state("recording")
                pid = self.player_pids()[n - 1]
                self._wait_until(
                    lambda pid=pid: not self.pid_running(pid),
                    1.0,
                    self.log_text(),
                )
                if n < 3:
                    time.sleep(0.15)
                    self.write_state("idle")
            self.finish(thread, 3)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 3)
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (1/2)",
            text,
        )
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (2/2)",
            text,
        )
        self.assertIn(
            "voxtype: dictation interrupted the line 3 times, line dropped",
            text,
        )
        self.assertEqual(text.count("line dropped"), 1)

    @LINUX_PROCS
    def test_cap_during_replay_wait_drops_line(self):
        """A cap during the replay wait drops the line. No second player."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.6):
            thread = self.start_play()
            self.assertTrue(self.wait_starts(1, 1), self.log_text())
            time.sleep(0.25)
            self.write_state("recording")
            self.finish(thread, 3)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1)
        text = self.log_text()
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (1/5)",
            text,
        )
        self.assertEqual(text.count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 0.6s while still busy, line dropped",
            text,
        )

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
        self.assertNotIn("will replay", self.log_text())

    def test_lock_heartbeat_while_waiting(self):
        self.write_state("recording")
        old = time.time() - 3600
        os.utime(self.lock_path, (old, old))
        before = os.path.getmtime(self.lock_path)
        raw_fd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        lock_file = os.fdopen(raw_fd, "r+")
        VOICE_HOOK._lock_fd = lock_file

        def drop_lock_fd():
            VOICE_HOOK._lock_fd = None
            try:
                lock_file.close()
            except OSError:
                pass

        self.addCleanup(drop_lock_fd)
        real_utime = os.utime
        seen = []

        def wrapped(path, times=None):
            seen.append(path)
            return real_utime(path, times)

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_HEARTBEAT_S", 0.5), \
                mock.patch.object(VOICE_HOOK.os, "utime", side_effect=wrapped):
            waited = VOICE_HOOK.wait_for_voxtype_idle(1.5)
        after = os.path.getmtime(self.lock_path)
        self.assertGreater(after, before)
        self.assertGreaterEqual(waited, 1.5)
        self.assertLess(waited, 2.5)
        self.assertTrue(seen)
        self.assertTrue(all(path == lock_file.fileno() for path in seen))

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
        gated.assert_called_once_with(b"\x01\x02\x03\x04", 24000, 1, timeout=180)
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

    def test_play_raw_pcm_keeps_default_gated_timeout(self):
        self.write_state("idle")
        with mock.patch.object(VOICE_HOOK, "_play_pcm_voxtype_gated") as gated:
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        self.assertEqual(gated.call_args.args, (self.pcm, 24000, 1))
        self.assertEqual(gated.call_args.kwargs.get("timeout", 30.0), 30.0)
        self.assertLessEqual(set(gated.call_args.kwargs), {"timeout"})

        seen = []

        def capture(*args, **kwargs):
            if "timeout" in kwargs:
                seen.append(kwargs["timeout"])
            elif len(args) > 3:
                seen.append(args[3])
            else:
                seen.append(30.0)
            return len(seen) == 1

        with mock.patch.object(VOICE_HOOK, "_play_one_paplay", side_effect=capture):
            VOICE_HOOK._play_pcm_voxtype_gated(self.pcm, 24000, 1, timeout=180)
        self.assertEqual(seen, [180, 180])

    @LINUX_PROCS
    def test_gated_paplay_timeout_follows_argument(self):
        """A short timeout kills a longer fake play; a larger one lets it finish.

        Uses sub-second deadlines so the suite does not wait out the real 30s
        or 180s caps. The raised TimeoutExpired.timeout is the argument.
        """
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "1.0"
        argv = PAPLAY_ARGV

        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            VOICE_HOOK._play_one_paplay(argv, self.pcm, True, timeout=0.3)
        self.assertEqual(caught.exception.timeout, 0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.ends(), [])

        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            VOICE_HOOK._play_pcm_voxtype_gated(self.pcm, 24000, 1, timeout=0.3)
        self.assertEqual(caught.exception.timeout, 0.3)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(len(self.starts()), 2)
        self.assertEqual(self.ends(), [])

        started = time.monotonic()
        VOICE_HOOK._play_pcm_voxtype_gated(self.pcm, 24000, 1, timeout=3.0)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(self.starts()), 3)
        self.assertEqual(len(self.ends()), 1)

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
            "voxtype: dictation started during playback, stopped; will replay (1/5)",
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
        self.assertNotIn("will replay", self.log_text())
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

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo unavailable")
    def test_fifo_state_file_is_not_busy_without_blocking(self):
        os.mkfifo(self.state_path)
        self.assertEqual(self.run_bounded(VOICE_HOOK._voxtype_state_text), "")
        self.assertFalse(self.run_bounded(VOICE_HOOK.voxtype_busy))
        self.assertEqual(
            self.run_bounded(VOICE_HOOK._wait_for_voxtype_idle_status),
            (0.0, False),
        )
        self.assertNotIn("voxtype: waiting", self.log_text())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo unavailable")
    def test_fifo_pid_file_is_ignored_and_state_stays_busy(self):
        self.write_state("recording")
        os.mkfifo(self.pid_path)
        self.assertFalse(
            self.run_bounded(
                lambda: VOICE_HOOK._voxtype_pid_is_dead(self.state_path),
            )
        )
        self.assertTrue(self.run_bounded(VOICE_HOOK.voxtype_busy))
        self.assertEqual(VOICE_HOOK._voxtype_busy_state(), "recording")

    def test_directory_state_path_is_not_busy(self):
        os.mkdir(self.state_path)
        self.assertEqual(self.run_bounded(VOICE_HOOK._voxtype_state_text), "")
        self.assertFalse(self.run_bounded(VOICE_HOOK.voxtype_busy))
        self.assertEqual(
            self.run_bounded(VOICE_HOOK._wait_for_voxtype_idle_status),
            (0.0, False),
        )

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

    def test_heartbeat_false_does_not_refresh_lock_mtime(self):
        self.write_state("recording")
        old = time.time() - 30
        os.utime(self.lock_path, (old, old))
        before = os.stat(self.lock_path).st_mtime_ns
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_HEARTBEAT_S", 0.2):
            waited = VOICE_HOOK.wait_for_voxtype_idle(0.7, heartbeat=False)
        self.assertGreaterEqual(waited, 0.7)
        self.assertLess(waited, 1.5)
        self.assertEqual(os.stat(self.lock_path).st_mtime_ns, before)
        self.assertIn("voxtype: cap reached after", self.log_text())

    @LINUX_PROCS
    def test_prelock_wait_does_not_take_or_touch_the_lock(self):
        """A recording line waits without the lock, and does not refresh it."""
        self.write_state("recording")
        holder, _release = self.spawn_holder("sibling-session")
        before = os.stat(self.lock_path).st_mtime_ns
        expected = Path(self.lock_path).read_text()
        self.assertTrue(expected.startswith(f"{holder.pid}\n"))
        utimed = []
        real_utime = os.utime

        def spy_utime(path, times=None):
            utimed.append(os.fspath(path))
            return real_utime(path, times)

        acquire_calls = []
        real_acquire = VOICE_HOOK.acquire_lock

        def spy_acquire(queue_wait_s=0.0, graceful_steal=False, yield_to_newer=None):
            acquire_calls.append(queue_wait_s)
            return real_acquire(
                queue_wait_s=queue_wait_s,
                graceful_steal=graceful_steal,
                yield_to_newer=yield_to_newer,
            )

        wait_calls = []
        real_wait = VOICE_HOOK._wait_for_voxtype_idle_status

        def spy_wait(*args, **kwargs):
            wait_calls.append((args, kwargs))
            return real_wait(*args, **kwargs)

        snapshot = {}

        def watcher():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and "voxtype: waiting" not in self.log_text():
                time.sleep(0.01)
            time.sleep(0.55)
            snapshot["waiting"] = "voxtype: waiting" in self.log_text()
            snapshot["yielding"] = "Yielding lock" in self.log_text()
            snapshot["acquire_calls"] = len(acquire_calls)
            snapshot["mtime"] = os.stat(self.lock_path).st_mtime_ns
            snapshot["content"] = Path(self.lock_path).read_text()
            snapshot["lock_held"] = self._lock_is_held()
            snapshot["holder_alive"] = holder.poll() is None
            snapshot["utimed"] = list(utimed)
            snapshot["ready"] = True
            # End the wait idle. A cap now drops the line, and this test
            # is about the wait itself, then the queue behind the sibling.
            self.write_state("idle")

        watcher_thread = threading.Thread(target=watcher, daemon=True)
        self.threads.append(watcher_thread)
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 1.5), \
                mock.patch.object(VOICE_HOOK, "VOXTYPE_HEARTBEAT_S", 0.2), \
                mock.patch.object(VOICE_HOOK.os, "utime", side_effect=spy_utime), \
                mock.patch.object(VOICE_HOOK, "acquire_lock", side_effect=spy_acquire), \
                mock.patch.object(
                    VOICE_HOOK, "_wait_for_voxtype_idle_status", side_effect=spy_wait,
                ):
            watcher_thread.start()
            spoken = self._run_main("incoming-session", "Held while you dictate.")
        watcher_thread.join(2)
        self.assertTrue(snapshot.get("ready"), self.log_text())
        self.assertTrue(snapshot["waiting"], self.log_text())
        self.assertFalse(snapshot["yielding"], snapshot)
        self.assertEqual(snapshot["acquire_calls"], 0)
        self.assertEqual(snapshot["mtime"], before)
        self.assertEqual(snapshot["content"], expected)
        self.assertTrue(snapshot["lock_held"])
        self.assertTrue(snapshot["holder_alive"])
        self.assertEqual(snapshot["utimed"], [])
        self.assertEqual(os.stat(self.lock_path).st_mtime_ns, before)
        self.assertEqual(Path(self.lock_path).read_text(), expected)
        self.assertNotIn(self.lock_path, utimed)
        self.assertEqual(
            wait_calls, [((), {"max_wait_s": 1.5, "heartbeat": False})],
        )
        self.assertEqual(acquire_calls, [1.5])
        self.assertEqual(spoken, [])
        self.assertIn("Yielding lock", self.log_text())
        self.assertIsNone(holder.poll())

    @LINUX_PROCS
    def test_held_line_queues_until_sibling_releases(self):
        self.write_state("recording")
        holder, release_path = self.spawn_holder("sibling-session")
        seen_pid = []

        def on_speak(_text, _cfg, _lang=None):
            seen_pid.append(Path(self.lock_path).read_text().splitlines()[0])

        errors = []

        def director():
            try:
                deadline = time.monotonic() + 2
                while (
                    time.monotonic() < deadline
                    and "voxtype: waiting" not in self.log_text()
                ):
                    time.sleep(0.01)
                if "voxtype: waiting" not in self.log_text():
                    errors.append("pre-lock wait did not start")
                    return
                time.sleep(0.35)
                self.write_state("idle")
                time.sleep(0.45)
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                Path(release_path).write_text("go")

        director_thread = threading.Thread(target=director, daemon=True)
        self.threads.append(director_thread)
        acquire_calls = []
        real_acquire = VOICE_HOOK.acquire_lock

        def spy_acquire(queue_wait_s=0.0, graceful_steal=False, yield_to_newer=None):
            acquire_calls.append(queue_wait_s)
            return real_acquire(
                queue_wait_s=queue_wait_s,
                graceful_steal=graceful_steal,
                yield_to_newer=yield_to_newer,
            )

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 3.0), \
                mock.patch.object(VOICE_HOOK, "acquire_lock", side_effect=spy_acquire):
            director_thread.start()
            spoken = self._run_main(
                "incoming-session", "Queued after you.", on_speak=on_speak,
            )
        director_thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(spoken, ["Queued after you."])
        self.assertEqual(seen_pid, [str(os.getpid())])
        self.assertEqual(acquire_calls, [3.0])
        self.assertNotIn("Yielding lock", self.log_text())
        self.assertIn("voxtype: waiting (state=recording)", self.log_text())
        self.assertIn("voxtype: idle after", self.log_text())
        holder.wait(timeout=2)

    @LINUX_PROCS
    def test_idle_line_yields_at_once_to_live_sibling(self):
        self.write_state("idle")
        holder, _release = self.spawn_holder("sibling-session")
        acquire_calls = []
        real_acquire = VOICE_HOOK.acquire_lock

        def spy_acquire(queue_wait_s=0.0, graceful_steal=False, yield_to_newer=None):
            acquire_calls.append(queue_wait_s)
            return real_acquire(
                queue_wait_s=queue_wait_s,
                graceful_steal=graceful_steal,
                yield_to_newer=yield_to_newer,
            )

        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK, "acquire_lock", side_effect=spy_acquire):
            spoken = self._run_main("incoming-session", "Already idle line.")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.18, self.log_text())
        self.assertEqual(spoken, [])
        self.assertEqual(acquire_calls, [0.0])
        self.assertIn("Yielding lock", self.log_text())
        self.assertNotIn("voxtype: waiting", self.log_text())
        self.assertIsNone(holder.poll())

    @LINUX_PROCS
    def test_queue_wait_s_cap_then_yields(self):
        holder, _release = self.spawn_holder("sibling-session")
        VOICE_HOOK._SESSION_ID = "incoming-session"
        started = time.monotonic()
        lock = VOICE_HOOK.acquire_lock(queue_wait_s=0.6)
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertGreaterEqual(elapsed, 0.55)
        self.assertLess(elapsed, 1.05)
        self.assertEqual(self.log_text().count("Yielding lock"), 1)
        self.assertIsNone(holder.poll())

    @LINUX_PROCS
    def test_same_session_steal_retries_until_lock_frees(self):
        """Graceful steal keeps polling while the flock outlives the signal."""
        sid = "same-session"
        VOICE_HOOK._SESSION_ID = sid
        holder, _release = self.spawn_holder(sid, release_path="-")
        real_kill = os.kill
        killer = {}
        seen_signals = []

        def delayed_kill(pid, sig):
            if pid == holder.pid:
                seen_signals.append(sig)
            # Swallow SIGTERM so this holder keeps the flock. The post-kill
            # poll is what has to notice the delayed SIGKILL.
            if pid == holder.pid and sig == signal.SIGTERM:
                return None
            if sig == 9 and pid == holder.pid:
                def later():
                    time.sleep(0.30)
                    real_kill(pid, 9)

                thread = threading.Thread(target=later, daemon=True)
                killer["thread"] = thread
                thread.start()
                return None
            return real_kill(pid, sig)

        started = time.monotonic()
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=delayed_kill):
                lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            elapsed = time.monotonic() - started
            self.assertIsNotNone(lock)
            self.assertGreaterEqual(elapsed, 0.70)
            self.assertLess(elapsed, 1.6)
            self.assertIn(signal.SIGTERM, seen_signals)
            self.assertIn(9, seen_signals)
            stamped = Path(self.lock_path).read_text().splitlines()
            self.assertEqual(stamped[0], str(os.getpid()))
            self.assertEqual(stamped[1], sid)
            self.assertNotIn("Yielding lock", self.log_text())
            killer["thread"].join(2)
            self.assertEqual(holder.wait(timeout=2), -signal.SIGKILL)
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    @LINUX_PROCS
    def test_steal_retry_returns_none_when_lock_stays_held(self):
        """Dead recorded pid, but a different live process still holds the flock."""
        bogus = 1 << 30
        with self.assertRaises(OSError):
            os.kill(bogus, 0)
        holder, _release = self.spawn_holder("other-session", pid_text=str(bogus))
        VOICE_HOOK._SESSION_ID = "incoming-session"
        started = time.monotonic()
        lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertGreaterEqual(elapsed, 0.95)
        self.assertLess(elapsed, 1.5)
        self.assertNotIn("Yielding lock", self.log_text())
        self.assertIsNone(holder.poll())
        self.assertTrue(self._lock_is_held())

    @LINUX_PROCS
    def test_default_steal_is_one_immediate_sigkill(self):
        """Without VoxType, a same-session steal is main's: SIGKILL, one retry."""
        sid = "same-session"
        VOICE_HOOK._SESSION_ID = sid
        holder, _release = self.spawn_holder(sid, release_path="-")
        real_kill = os.kill
        real_open = VOICE_HOOK._open_lockfile
        real_listdir = os.listdir
        kills = []
        opens = []

        def spy_kill(pid, sig):
            kills.append((pid, sig))
            return real_kill(pid, sig)

        def spy_open():
            opens.append(1)
            return real_open()

        def spy_listdir(path):
            if os.fspath(path) == "/proc":
                raise AssertionError("non-graceful steal scanned /proc")
            return real_listdir(path)

        lock = None
        try:
            with mock.patch.object(VOICE_HOOK.time, "sleep") as sleep, \
                    mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy_kill), \
                    mock.patch.object(VOICE_HOOK.os, "listdir", side_effect=spy_listdir), \
                    mock.patch.object(VOICE_HOOK, "_open_lockfile", side_effect=spy_open):
                started = time.monotonic()
                lock = VOICE_HOOK.acquire_lock()
                elapsed = time.monotonic() - started
            sleep.assert_not_called()
            self.assertLess(elapsed, 0.2)
            self.assertEqual(kills, [(holder.pid, 0), (holder.pid, 9)])
            self.assertEqual(len(opens), 2)
            self.assertEqual(holder.wait(timeout=2), -signal.SIGKILL)
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    @LINUX_PROCS
    def test_graceful_steal_stops_registered_player_before_return(self):
        """SIGTERM runs the holder's cleanup. The old paplay is gone first."""
        self._install_long_paplay()
        self.write_state("idle")
        sid = "same-session"
        ready = os.path.join(self.root, "gated-ready")
        holder, err_path = self._spawn_script(
            "fake-voice-stop-hook-gated.py",
            GATED_HOLDER,
            [str(HOOK_PATH), self.lock_path, self.log_path, sid, ready],
        )
        old_pid = self._await_playing_holder(holder, ready, err_path)
        VOICE_HOOK._SESSION_ID = sid
        lock = None
        try:
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            elapsed = time.monotonic() - started
            self.assertIsNotNone(lock, self._holder_debug(err_path))
            self.assertTrue(self._gone_or_zombie(old_pid))
            observed = time.time()
            self.assertLess(elapsed, 2.0)
            self._assert_no_player_overlap(old_pid, observed)
            self.assertIsNotNone(holder.wait(timeout=2))
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    @LINUX_PROCS
    def test_graceful_steal_stops_unregistered_player_that_ignores_sigterm(self):
        """subprocess.run paplay is not in _current_player. Stop it anyway."""
        self._install_long_paplay()
        sid = "same-session"
        ready = os.path.join(self.root, "ignore-ready")
        holder, err_path = self._spawn_script(
            "fake-voice-stop-hook-ignoring.py",
            IGNORING_HOLDER,
            [self.lock_path, sid, ready],
        )
        old_pid = self._await_playing_holder(holder, ready, err_path)
        VOICE_HOOK._SESSION_ID = sid
        lock = None
        try:
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            elapsed = time.monotonic() - started
            self.assertIsNotNone(lock, self._holder_debug(err_path))
            self.assertTrue(self._gone_or_zombie(old_pid))
            observed = time.time()
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLess(elapsed, 2.5)
            self.assertEqual(holder.wait(timeout=2), -signal.SIGKILL)
            self._assert_no_player_overlap(old_pid, observed)
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    def test_main_graceful_steal_follows_voxtype_presence(self):
        cases = (
            ("state present", True, None, True),
            ("state absent", False, None, False),
            ("wait disabled", True, self._cfg(voxtype_wait=False), False),
        )
        for label, present, cfg, expect in cases:
            with self.subTest(label):
                calls = []
                real_acquire = VOICE_HOOK.acquire_lock

                def spy_acquire(
                    queue_wait_s=0.0, graceful_steal=False, yield_to_newer=None,
                ):
                    calls.append((
                        queue_wait_s,
                        graceful_steal,
                        yield_to_newer is not None,
                    ))
                    return real_acquire(
                        queue_wait_s=queue_wait_s,
                        graceful_steal=graceful_steal,
                        yield_to_newer=yield_to_newer,
                    )

                if present:
                    self.write_state("idle")
                else:
                    try:
                        os.remove(self.state_path)
                    except FileNotFoundError:
                        pass
                with mock.patch.object(
                    VOICE_HOOK, "acquire_lock", side_effect=spy_acquire,
                ):
                    spoken = self._run_main("wire-session", "Wiring line.", cfg=cfg)
                self.assertEqual(spoken, ["Wiring line."])
                self.assertEqual(calls, [(0.0, expect, expect)])

    def test_remote_audio_skips_prelock_wait(self):
        self.write_state("recording")
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.4), \
                mock.patch.object(VOICE_HOOK, "wait_for_voxtype_idle") as wait, \
                mock.patch.object(VOICE_HOOK, "_wait_for_voxtype_idle_status") as status:
            started = time.monotonic()
            spoken = self._run_main(
                "remote-session",
                "Remote line.",
                cfg=self._cfg(remote_audio=True),
            )
            elapsed = time.monotonic() - started
        wait.assert_not_called()
        status.assert_not_called()
        self.assertEqual(spoken, ["Remote line."])
        self.assertLess(elapsed, 0.18)
        self.assertNotIn("voxtype:", self.log_text())

    def test_absent_state_main_adds_no_wait(self):
        self.assertFalse(os.path.exists(self.state_path))
        with mock.patch.object(VOICE_HOOK, "wait_for_voxtype_idle") as wait, \
                mock.patch.object(VOICE_HOOK, "_wait_for_voxtype_idle_status") as status:
            started = time.monotonic()
            spoken = self._run_main("day-session", "No voxtype here.")
            elapsed = time.monotonic() - started
        wait.assert_not_called()
        status.assert_not_called()
        self.assertEqual(spoken, ["No voxtype here."])
        self.assertLess(elapsed, 0.12)
        self.assertNotIn("voxtype:", self.log_text())

    def test_sigterm_during_prelock_releases_nothing(self):
        """A hook killed before it takes the lock still exits cleanly."""
        VOICE_HOOK._lock_fd = None
        VOICE_HOOK._current_player = None
        before = Path(self.lock_path).read_bytes()
        with mock.patch.object(VOICE_HOOK.os, "_exit") as exit_:
            VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
        exit_.assert_called_once_with(0)
        self.assertIsNone(VOICE_HOOK._lock_fd)
        self.assertIsNone(VOICE_HOOK._current_player)
        self.assertEqual(Path(self.lock_path).read_bytes(), before)

    _FATAL = {signal.SIGTERM, signal.SIGKILL, 9}

    def _assert_no_fatal(self, seen, pid):
        self.assertFalse(
            any(seen_pid == pid and sig in self._FATAL for seen_pid, sig in seen),
            seen,
        )

    @LINUX_PROCS
    def test_unverified_cmdline_is_never_signalled(self):
        """A live pid whose cmdline is not the hook is a dead holder."""
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(self._stop_proc, sleeper)
        holder, _release = self.spawn_holder(
            "other-session", pid_text=str(sleeper.pid), release_path="-",
        )
        self.assertNotIn(
            b"voice-stop-hook",
            Path(f"/proc/{sleeper.pid}/cmdline").read_bytes(),
        )
        seen, spy = self._guarded_kills([sleeper.pid, holder.pid])
        with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock()
            main_elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertLess(main_elapsed, 0.4)
        self._assert_no_fatal(seen, sleeper.pid)
        self._assert_no_fatal(seen, holder.pid)
        self.assertIsNone(sleeper.poll())
        self.assertIsNone(holder.poll())
        seen.clear()
        with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            graceful_elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertGreaterEqual(graceful_elapsed, 0.9)
        self.assertLess(graceful_elapsed, 1.8)
        self._assert_no_fatal(seen, sleeper.pid)
        self._assert_no_fatal(seen, holder.pid)
        self.assertIsNone(sleeper.poll())
        self.assertIsNone(holder.poll())

    @LINUX_PROCS
    def test_other_uid_holder_is_not_signalled(self):
        holder, _release = self.spawn_holder("other-session", release_path="-")
        self.assertIn(
            b"voice-stop-hook",
            Path(f"/proc/{holder.pid}/cmdline").read_bytes(),
        )
        real_uid = VOICE_HOOK._status_real_uid

        def other_uid(pid):
            if int(pid) == holder.pid:
                return os.getuid() + 1
            return real_uid(pid)

        seen, spy = self._guarded_kills([holder.pid])
        with mock.patch.object(VOICE_HOOK, "_status_real_uid", side_effect=other_uid), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock()
        self.assertIsNone(lock)
        self._assert_no_fatal(seen, holder.pid)
        self.assertIsNone(holder.poll())
        seen.clear()
        with mock.patch.object(VOICE_HOOK, "_status_real_uid", side_effect=other_uid), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 1.8)
        self._assert_no_fatal(seen, holder.pid)
        self.assertIsNone(holder.poll())

    @LINUX_PROCS
    def test_pid_one_and_self_are_not_signalled(self):
        cases = (("init", "1"), ("self", str(os.getpid())))
        for label, pid_text in cases:
            with self.subTest(label):
                holder, _release = self.spawn_holder(
                    "other-session", pid_text=pid_text, release_path="-",
                )
                seen, spy = self._guarded_kills([holder.pid])
                with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    started = time.monotonic()
                    lock = VOICE_HOOK.acquire_lock()
                    main_elapsed = time.monotonic() - started
                self.assertIsNone(lock)
                self.assertLess(main_elapsed, 0.4)
                self.assertNotIn(1, [pid for pid, _sig in seen])
                self.assertNotIn(os.getpid(), [pid for pid, _sig in seen])
                self.assertIsNone(holder.poll())
                seen.clear()
                with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    started = time.monotonic()
                    lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
                    elapsed = time.monotonic() - started
                self.assertIsNone(lock)
                self.assertGreaterEqual(elapsed, 0.9)
                self.assertLess(elapsed, 1.8)
                self.assertNotIn(1, [pid for pid, _sig in seen])
                self.assertNotIn(os.getpid(), [pid for pid, _sig in seen])
                self.assertIsNone(holder.poll())
                self._stop_proc(holder)

    def test_symlink_lock_is_refused(self):
        os.remove(self.lock_path)
        target = os.path.join(self.root, "lock-target")
        Path(target).write_text("untouched-target\n")
        os.symlink(target, self.lock_path)
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                self._clear_log()
                seen, spy = self._guarded_kills([])
                started = time.monotonic()
                with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    lock = VOICE_HOOK.acquire_lock(graceful_steal=graceful)
                elapsed = time.monotonic() - started
                self.assertIsNone(lock)
                self.assertLess(elapsed, 0.3)
                self.assertEqual(seen, [])
                self.assertEqual(Path(target).read_text(), "untouched-target\n")
                self.assertTrue(os.path.islink(self.lock_path))
                self.assertEqual(self.log_text().count("lock refused:"), 1)
                self.assertIn("lock refused: symlink", self.log_text())

    def test_foreign_lock_owner_is_refused(self):
        sentinel = "do-not-touch\n"
        Path(self.lock_path).write_text(sentinel)
        real_fstat = os.fstat

        def fake_fstat(fd):
            st = real_fstat(fd)
            return mock.Mock(
                st_mode=st.st_mode,
                st_uid=st.st_uid + 1,
                st_mtime=st.st_mtime,
            )

        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                self._clear_log()
                seen, spy = self._guarded_kills([])
                started = time.monotonic()
                with mock.patch.object(VOICE_HOOK.os, "fstat", side_effect=fake_fstat), \
                        mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    lock = VOICE_HOOK.acquire_lock(graceful_steal=graceful)
                elapsed = time.monotonic() - started
                self.assertIsNone(lock)
                self.assertLess(elapsed, 0.3)
                self.assertEqual(seen, [])
                self.assertEqual(Path(self.lock_path).read_text(), sentinel)
                self.assertEqual(self.log_text().count("lock refused:"), 1)
                self.assertIn("lock refused: owner mismatch", self.log_text())

    @LINUX_PROCS
    def test_non_paplay_argv0_child_is_not_signalled(self):
        decoy_script = os.path.join(self.root, "decoy-comm-paplay.py")
        Path(decoy_script).write_text(COMM_PAPLAY_DECOY)
        decoy_ready = os.path.join(self.root, "decoy-ready")
        ready = os.path.join(self.root, "decoy-parent-ready")
        sid = "same-session"
        holder, err_path = self._spawn_script(
            "fake-voice-stop-hook-decoy-parent.py",
            DECOY_PARENT,
            [self.lock_path, sid, ready, decoy_script, decoy_ready],
        )
        self._wait_until(
            lambda: os.path.exists(ready) or holder.poll() is not None,
            5.0,
            "decoy parent did not take the lock " + self._holder_debug(err_path),
        )
        self.assertIsNone(holder.poll(), self._holder_debug(err_path))
        decoy_pid = int(Path(decoy_ready).read_text().strip())
        self.addCleanup(self._kill_pid, decoy_pid)
        comm, _state, ppid = self._proc_stat(decoy_pid)
        self.assertEqual(comm, "paplay")
        self.assertEqual(ppid, holder.pid)
        argv0 = Path(f"/proc/{decoy_pid}/cmdline").read_bytes().split(b"\x00", 1)[0]
        self.assertNotEqual(os.path.basename(argv0), b"paplay")
        VOICE_HOOK._SESSION_ID = sid
        seen, spy = self._guarded_kills([decoy_pid])
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            self.assertIsNotNone(lock, self._holder_debug(err_path))
            self._assert_no_fatal(seen, decoy_pid)
            self.assertTrue(self.pid_running(decoy_pid))
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    @LINUX_PROCS
    def test_paplay_child_with_other_uid_is_not_signalled(self):
        self._install_long_paplay()
        sid = "same-session"
        ready = os.path.join(self.root, "uid-ready")
        holder, err_path = self._spawn_script(
            "fake-voice-stop-hook-ignoring-uid.py",
            IGNORING_HOLDER,
            [self.lock_path, sid, ready],
        )
        old_pid = self._await_playing_holder(holder, ready, err_path)
        argv0 = Path(f"/proc/{old_pid}/cmdline").read_bytes().split(b"\x00", 1)[0]
        self.assertEqual(os.path.basename(argv0), b"paplay")
        real_uid = VOICE_HOOK._status_real_uid

        def other_child_uid(pid):
            if int(pid) == old_pid:
                return os.getuid() + 1
            return real_uid(pid)

        VOICE_HOOK._SESSION_ID = sid
        seen, spy = self._guarded_kills([old_pid])
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK, "_status_real_uid", side_effect=other_child_uid), \
                    mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            self.assertIsNotNone(lock, self._holder_debug(err_path))
            self._assert_no_fatal(seen, old_pid)
            self.assertTrue(self.pid_running(old_pid))
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)
            self._kill_pid(old_pid)

    @LINUX_PROCS
    def test_sigkill_skipped_when_holder_stops_verifying(self):
        sid = "same-session"
        VOICE_HOOK._SESSION_ID = sid
        holder, _release = self.spawn_holder(sid, release_path="-")
        calls = {"n": 0}
        real_uid = VOICE_HOOK._status_real_uid

        def flipping(pid):
            if int(pid) != holder.pid:
                return real_uid(pid)
            calls["n"] += 1
            if calls["n"] == 1:
                return real_uid(pid)
            return os.getuid() + 1

        seen, spy = self._guarded_kills([holder.pid])
        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK, "_status_real_uid", side_effect=flipping), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertIn((holder.pid, signal.SIGTERM), seen)
        self.assertNotIn((holder.pid, 9), seen)
        self.assertIsNone(holder.poll())
        self.assertGreaterEqual(elapsed, 1.3)
        self.assertLess(elapsed, 2.5)

    @LINUX_PROCS
    def test_macos_ps_other_uid_is_not_signalled(self):
        holder, _release = self.spawn_holder("same-session", release_path="-")
        VOICE_HOOK._SESSION_ID = "same-session"
        ps_calls = []

        def fake_ps(argv, **kwargs):
            ps_calls.append((list(argv), kwargs))
            result = mock.Mock()
            result.returncode = 0
            result.stdout = (
                f"{os.getuid() + 1} python voice-stop-hook\n".encode()
            )
            return result

        seen, spy = self._guarded_kills([holder.pid])
        with mock.patch.object(VOICE_HOOK, "IS_MACOS", True), \
                mock.patch.object(VOICE_HOOK.subprocess, "run", side_effect=fake_ps), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock()
            elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertLess(elapsed, 0.4)
        self._assert_no_fatal(seen, holder.pid)
        self.assertIsNone(holder.poll())
        self.assertTrue(ps_calls)
        argv, kwargs = ps_calls[0]
        self.assertEqual(argv[:4], ["ps", "-o", "uid=,command=", "-p"])
        self.assertEqual(argv[4], str(holder.pid))
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs.get("timeout"), 1)
        self.assertTrue(kwargs.get("capture_output"))

    def _arrival_files(self):
        found = []
        if not os.path.isdir(self.arrival_dir):
            return found
        for dirpath, _dirs, names in os.walk(self.arrival_dir):
            for name in names:
                found.append(os.path.join(dirpath, name))
        return found

    def _wipe_arrival(self):
        if os.path.isdir(self.arrival_dir):
            shutil.rmtree(self.arrival_dir)

    def _register_at(self, session_id, stamp):
        VOICE_HOOK._SESSION_ID = session_id
        return VOICE_HOOK._register_session_arrival(stamp)

    def _write_named(self, directory, name):
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return path

    @LINUX_PROCS
    def test_arrival_registry_orders_a_live_hook(self):
        """Newer live hook counts. Older, dead, foreign, and junk do not."""
        stamp = 1_000_000_000_000_000
        entry = self._register_at("sess-order", stamp)
        self.assertIsNotNone(entry)
        self.assertEqual(os.path.basename(entry), f"{stamp:020d}-{os.getpid()}")
        self.assertEqual(os.stat(entry).st_mode & 0o777, 0o600)
        sub = os.path.dirname(entry)
        self.assertFalse(os.path.islink(self.arrival_dir))
        self.assertFalse(os.path.islink(sub))
        self.assertEqual(os.lstat(self.arrival_dir).st_uid, os.getuid())
        self.assertEqual(os.lstat(self.arrival_dir).st_mode & 0o022, 0)
        self.assertEqual(os.lstat(sub).st_mode & 0o022, 0)
        self.assertEqual(
            os.path.basename(sub),
            hashlib.sha256(b"sess-order").hexdigest()[:16],
        )

        ready = os.path.join(self.root, "peer-ready")
        peer, err_path = self._spawn_script(
            "fake-voice-stop-hook-peer.py", PEER_SLEEP, [ready],
        )
        self._wait_until(
            lambda: os.path.exists(ready) or peer.poll() is not None,
            3.0,
            "peer did not start " + self._holder_debug(err_path),
        )
        self.assertIsNone(peer.poll())
        self.assertIn(
            b"voice-stop-hook",
            Path(f"/proc/{peer.pid}/cmdline").read_bytes(),
        )

        older = self._write_named(sub, f"{stamp - 1:020d}-{peer.pid}")
        self.assertFalse(VOICE_HOOK._newer_same_session_arrival())
        self.assertTrue(os.path.exists(older))

        newer = self._write_named(sub, f"{stamp + 1:020d}-{peer.pid}")
        self.assertTrue(VOICE_HOOK._newer_same_session_arrival())
        os.unlink(newer)

        dead = self.dead_pid()
        dead_path = self._write_named(sub, f"{stamp + 2:020d}-{dead}")
        self.assertFalse(VOICE_HOOK._newer_same_session_arrival())
        self.assertFalse(os.path.exists(dead_path))

        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(self._stop_proc, sleeper)
        foreign = self._write_named(sub, f"{stamp + 3:020d}-{sleeper.pid}")
        self.assertNotIn(
            b"voice-stop-hook",
            Path(f"/proc/{sleeper.pid}/cmdline").read_bytes(),
        )
        self.assertFalse(VOICE_HOOK._newer_same_session_arrival())
        self.assertTrue(os.path.exists(foreign))

        malformed = os.path.join(sub, "not-an-entry")
        short = os.path.join(sub, f"{stamp + 4}-{peer.pid}")
        Path(malformed).write_text("x")
        Path(short).write_text("x")
        self.assertFalse(VOICE_HOOK._newer_same_session_arrival())
        self.assertTrue(os.path.exists(malformed))
        self.assertTrue(os.path.exists(short))

        other = hashlib.sha256(b"other-session").hexdigest()[:16]
        other_dir = os.path.join(self.arrival_dir, other)
        os.makedirs(other_dir, 0o700)
        other_entry = self._write_named(other_dir, f"{stamp + 9:020d}-{peer.pid}")
        self.assertFalse(VOICE_HOOK._newer_same_session_arrival())
        self.assertTrue(os.path.exists(other_entry))

    def test_arrival_registry_unsafe_or_unconfigured_is_off(self):
        """Unsafe dirs log once and do no ordering. Empty id and no runtime stay off."""
        stamp = 1_000_000_000_000_000
        unsafe = "voxtype: arrival registry unsafe, ordering off"

        self._clear_log()
        real = os.path.join(self.root, "real-arrivals")
        os.makedirs(real, 0o700)
        link = os.path.join(self.root, "arrivals-link")
        os.symlink(real, link)
        with mock.patch.dict(os.environ, {"SHELBY_VOICE_ARRIVAL_DIR": link}):
            result = self._register_at("sess-link", stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)
        self.assertEqual(os.listdir(real), [])

        self._wipe_arrival()
        self._clear_log()
        os.makedirs(self.arrival_dir, 0o700)
        os.chmod(self.arrival_dir, 0o770)
        with mock.patch.dict(os.environ, {"SHELBY_VOICE_ARRIVAL_DIR": self.arrival_dir}):
            result = self._register_at("sess-group", stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)
        self.assertEqual(os.listdir(self.arrival_dir), [])

        self._wipe_arrival()
        self._clear_log()
        os.makedirs(self.arrival_dir, 0o700)
        real_lstat = os.lstat

        def foreign_owner(path):
            st = real_lstat(path)
            return mock.Mock(st_mode=st.st_mode, st_uid=st.st_uid + 1)

        with mock.patch.dict(os.environ, {"SHELBY_VOICE_ARRIVAL_DIR": self.arrival_dir}), \
                mock.patch.object(VOICE_HOOK.os, "lstat", side_effect=foreign_owner):
            result = self._register_at("sess-owner", stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)

        self._wipe_arrival()
        self._clear_log()
        os.makedirs(self.arrival_dir, 0o700)
        digest = hashlib.sha256(b"sess-sub").hexdigest()[:16]
        target = os.path.join(self.root, "sub-target")
        os.makedirs(target, 0o700)
        os.symlink(target, os.path.join(self.arrival_dir, digest))
        with mock.patch.dict(os.environ, {"SHELBY_VOICE_ARRIVAL_DIR": self.arrival_dir}):
            result = self._register_at("sess-sub", stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)
        self.assertEqual(os.listdir(target), [])

        self._clear_log()
        calls = []
        real_makedirs = os.makedirs

        def spy_makedirs(path, *args, **kwargs):
            calls.append(os.fspath(path))
            return real_makedirs(path, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK.os, "makedirs", side_effect=spy_makedirs):
            result = self._register_at("", stamp)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertNotIn(unsafe, self.log_text())

        self._clear_log()
        self._wipe_arrival()
        with mock.patch.dict(os.environ):
            os.environ.pop("SHELBY_VOICE_ARRIVAL_DIR", None)
            os.environ.pop("XDG_RUNTIME_DIR", None)
            self.assertIsNone(VOICE_HOOK._voice_arrival_dir())
            with mock.patch.object(VOICE_HOOK.os, "makedirs", side_effect=spy_makedirs):
                calls.clear()
                result = self._register_at("sess-no-runtime", stamp)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertNotIn(unsafe, self.log_text())
        self.assertFalse(os.path.exists(self.arrival_dir))

    @LINUX_PROCS
    def test_yield_to_newer_skips_take_and_same_session_steal(self):
        """A newer arrival drops the line. None still steals, as on main."""
        self._clear_log()
        drop = "voxtype: newer line from this session pending, dropping this one"
        VOICE_HOOK._SESSION_ID = "same-session"
        seen, spy = self._guarded_kills([])
        with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock(
                graceful_steal=True, yield_to_newer=lambda: True,
            )
        self.assertIsNone(lock)
        self.assertEqual(seen, [])
        self.assertEqual(self.log_text().count(drop), 1)
        self.assertNotIn(str(os.getpid()), Path(self.lock_path).read_text())

        self._clear_log()
        holder, _release = self.spawn_holder("same-session", release_path="-")
        calls = {"n": 0}

        def yield_on_steal():
            calls["n"] += 1
            return calls["n"] >= 2

        seen, spy = self._guarded_kills([holder.pid])
        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock(
                graceful_steal=True, yield_to_newer=yield_on_steal,
            )
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertLess(elapsed, 0.4)
        self.assertGreaterEqual(calls["n"], 2)
        self._assert_no_fatal(seen, holder.pid)
        self.assertIsNone(holder.poll())
        self.assertEqual(self.log_text().count(drop), 1)
        self.assertNotIn("Yielding lock", self.log_text())

        self._clear_log()
        real_kill = os.kill
        delivered = []

        def deliver(pid, sig):
            delivered.append((int(pid), sig))
            return real_kill(pid, sig)

        with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=deliver):
            lock = VOICE_HOOK.acquire_lock(
                graceful_steal=True, yield_to_newer=None,
            )
        self.assertIsNotNone(lock)
        self.assertIn((holder.pid, signal.SIGTERM), delivered)
        self.assertNotIn(drop, self.log_text())
        VOICE_HOOK.release_lock(lock)
        self.assertIsNotNone(holder.wait(timeout=2))

    @LINUX_PROCS
    def test_newer_same_session_line_is_the_one_spoken(self):
        """Five recording -> idle flips. The later arrival speaks. The older yields."""
        session = "same-session-order"
        older_line = "Older line stays quiet."
        newer_line = "Newer line is spoken."
        script = os.path.join(self.root, "fake-voice-stop-hook-arrival.py")
        Path(script).write_text("#!" + sys.executable + "\n" + ARRIVAL_CHILD)
        os.chmod(script, 0o755)
        drop = "voxtype: newer line from this session pending, dropping this one"

        for turn in range(5):
            with self.subTest(turn=turn):
                self._wipe_arrival()
                self.write_state("recording")
                spoken_path = os.path.join(self.root, f"spoken-{turn}.log")
                release_path = os.path.join(self.root, f"speak-release-{turn}")
                try:
                    os.remove(spoken_path)
                except FileNotFoundError:
                    pass
                try:
                    os.remove(release_path)
                except FileNotFoundError:
                    pass
                older_log = os.path.join(self.root, f"older-{turn}.log")
                newer_log = os.path.join(self.root, f"newer-{turn}.log")

                def spawn(line, log_path):
                    err_path = log_path + ".err"
                    err_fh = open(err_path, "w")
                    self.addCleanup(err_fh.close)
                    env = os.environ.copy()
                    env["PYTHONUNBUFFERED"] = "1"
                    env["SHELBY_VOXTYPE_STATE_FILE"] = self.state_path
                    env["SHELBY_VOICE_ARRIVAL_DIR"] = self.arrival_dir
                    env["ARRIVAL_SPOKEN_LOG"] = spoken_path
                    env["ARRIVAL_SPEAK_RELEASE"] = release_path
                    env["CLAUDE_SESSION_TYPE"] = "main"
                    proc = subprocess.Popen(
                        [
                            sys.executable, script, str(HOOK_PATH), line, session,
                            self.lock_path, log_path,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=err_fh,
                        env=env,
                    )
                    self.addCleanup(self._stop_proc, proc)
                    return proc, err_path

                older, older_err = spawn(older_line, older_log)
                self._wait_until(
                    lambda: len(self._arrival_files()) >= 1 or older.poll() is not None,
                    5.0,
                    "older did not register " + self._holder_debug(older_err),
                )
                self.assertIsNone(older.poll(), self._holder_debug(older_err))
                newer, newer_err = spawn(newer_line, newer_log)
                self._wait_until(
                    lambda: len(self._arrival_files()) >= 2 or newer.poll() is not None,
                    5.0,
                    "newer did not register " + self._holder_debug(newer_err),
                )
                self.assertIsNone(newer.poll(), self._holder_debug(newer_err))
                time.sleep(0.35)
                self.write_state("idle")
                self._wait_until(
                    lambda: (
                        older.poll() is not None
                        and self._read_text(spoken_path).strip() == newer_line
                    ),
                    3.0,
                    "older did not yield " + self._holder_debug(older_err)
                    + " newer=" + self._holder_debug(newer_err)
                    + " spoken=" + self._read_text(spoken_path),
                )
                spoken = self._read_text(spoken_path)
                self.assertEqual(spoken.strip(), newer_line, spoken)
                self.assertNotIn(older_line, spoken)
                self.assertIn(drop, self._read_text(older_log))
                self.assertNotIn(drop, self._read_text(newer_log))
                Path(release_path).write_text("go")
                self.assertEqual(newer.wait(timeout=3), 0, self._holder_debug(newer_err))
                self.assertEqual(older.wait(timeout=2), 0, self._holder_debug(older_err))

    def _read_text(self, path):
        try:
            return Path(path).read_text()
        except FileNotFoundError:
            return ""

    def test_prelock_cap_drops_before_lock(self):
        """A pre-lock cap drops the line before the lock and before synthesis."""
        self._clear_log()
        self._wipe_arrival()
        self.write_state("recording")
        path = self._transcript(self._user_json("old prompt") + "\n")
        seen = {}

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            seen["held"] = VOICE_HOOK._held_phase
            seen["lock_fd"] = VOICE_HOOK._lock_fd

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.6):
            started = time.monotonic()
            spoken = self._run_main_driven(
                "cap-session",
                "Must not be spoken.",
                drive,
                extra={"transcript_path": path},
            )
            elapsed = time.monotonic() - started
        self.assertTrue(seen.get("held"), self.log_text())
        self.assertIsNone(seen["lock_fd"])
        self.assertEqual(spoken, [])
        self.assertEqual(self.starts(), [])
        self.assertGreaterEqual(elapsed, 0.55)
        self.assertLess(elapsed, 1.4, self.log_text())
        fd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.fail("lock was held after a capped pre-lock wait")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        text = self.log_text()
        self.assertEqual(text.count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 0.6s while still busy, line dropped",
            text,
        )
        self.assertNotIn("Yielding lock", text)
        self.assertFalse(VOICE_HOOK._held_phase)
        self.assertIsNone(VOICE_HOOK._lock_fd)
        self.assertIsNone(VOICE_HOOK._arrival_entry_path)
        self.assertEqual(self._arrival_files(), [])
        self.assertNotIn(str(os.getpid()), Path(self.lock_path).read_text())

    def test_fresh_cap_per_wait(self):
        """The gate wait gets a full cap after the pre-lock wait used most of one."""
        self.write_state("recording")
        os.environ["FAKE_PLAY_S"] = "0.05"
        cap = 1.0
        real_status = VOICE_HOOK._wait_for_voxtype_idle_status
        seen = []

        def spy_status(max_wait_s=None, heartbeat=True):
            t0 = time.monotonic()
            result = real_status(max_wait_s=max_wait_s, heartbeat=heartbeat)
            seen.append((max_wait_s, heartbeat, time.monotonic() - t0, result[1]))
            return result

        def on_speak(_clean, _cfg, _lang=None):
            self.write_state("recording")
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)

        def drive():
            self._wait_until(
                lambda: self.log_text().count("voxtype: waiting") >= 1,
                2.0,
                self.log_text(),
            )
            time.sleep(0.8)
            self.write_state("idle")
            self._wait_until(
                lambda: self.log_text().count("voxtype: waiting") >= 2,
                2.0,
                self.log_text(),
            )
            time.sleep(0.6)
            self.write_state("idle")

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", cap), \
                mock.patch.object(
                    VOICE_HOOK, "_wait_for_voxtype_idle_status", side_effect=spy_status,
                ):
            started = time.monotonic()
            spoken = self._run_main_driven(
                "budget-session", "Budget line.", drive, on_speak=on_speak,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(spoken, ["Budget line."])
        self.assertEqual(len(seen), 2, seen)
        self.assertEqual(seen[0][0], cap)
        self.assertFalse(seen[0][1])
        self.assertFalse(seen[0][3])
        self.assertGreaterEqual(seen[0][2], 0.75)
        # Longer than the ~0.2s left of a shared cap, and still the full value.
        self.assertEqual(seen[1][0], cap)
        self.assertTrue(seen[1][1])
        self.assertFalse(seen[1][3])
        self.assertGreaterEqual(seen[1][2], 0.55)
        self.assertLess(seen[1][2], cap)
        self.assertGreaterEqual(elapsed, 1.3)
        self.assertLess(elapsed, 2.8, self.log_text())
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(self.ends()), 1)
        self.assertNotIn("cap reached", self.log_text())
        self.assertNotIn("line dropped", self.log_text())

    def test_direct_gate_cap_drops_without_a_player(self):
        """A direct gated call waits its own full cap, then drops."""
        self.write_state("recording")

        def play_once(*_args, **_kwargs):
            raise AssertionError("player started after the cap")

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.4), \
                mock.patch.object(VOICE_HOOK, "_play_one_paplay", side_effect=play_once):
            started = time.monotonic()
            result = VOICE_HOOK._play_pcm_voxtype_gated(self.pcm, 24000, 1)
            elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 0.9)
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.log_text().count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 0.4s while still busy, line dropped",
            self.log_text(),
        )

    def test_zero_max_wait_on_busy_returns_cap_without_sleep(self):
        self.write_state("recording")
        with mock.patch.object(VOICE_HOOK.time, "sleep") as sleep:
            started = time.monotonic()
            result = VOICE_HOOK._wait_for_voxtype_idle_status(max_wait_s=0)
            elapsed = time.monotonic() - started
        sleep.assert_not_called()
        self.assertEqual(result, (0.0, True))
        self.assertLess(elapsed, 0.1)
        self.assertIn(
            "voxtype: cap reached after 0s while still busy, line dropped",
            self.log_text(),
        )
        self.assertEqual(self.log_text().count("voxtype: cap reached after"), 1)

    def test_absent_state_does_not_touch_arrival_registry(self):
        self.assertFalse(os.path.exists(self.state_path))
        with self.forbid_arrival_io():
            spoken = self._run_main("day-session", "No voxtype here.")
            with mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
                VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        self.assertEqual(spoken, ["No voxtype here."])
        run.assert_called_once()
        self.assertFalse(os.path.exists(self.arrival_dir))
        self.assertNotIn("voxtype:", self.log_text())

    def _transcript(self, body):
        path = os.path.join(self.root, "transcript.jsonl")
        data = body if isinstance(body, bytes) else body.encode()
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return path

    def _append_transcript(self, path, text):
        data = text if isinstance(text, bytes) else text.encode()
        fd = os.open(path, os.O_APPEND | os.O_WRONLY)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def _user_json(self, content, **fields):
        obj = {
            "type": "user",
            "message": {"role": "user", "content": content},
        }
        obj.update(fields)
        return json.dumps(obj)

    def _run_main_driven(self, session_id, text, drive, extra=None, cfg=None,
                         on_speak=None):
        """Run main() on this thread. `drive` runs beside it on a helper.

        signal.signal inside main() works only on the unittest thread, so the
        hook cannot be started on the helper. The helper appends transcript
        lines or calls handle_sigterm while main is in the pre-lock wait.
        self._main_done is set when main() returns.
        """
        self._main_done = threading.Event()
        box = {"error": None}

        def helper():
            try:
                drive()
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=helper, daemon=True)
        self.threads.append(thread)
        thread.start()
        spoken = None
        main_error = None
        try:
            spoken = self._run_main(
                session_id, text, cfg=cfg, on_speak=on_speak, extra=extra,
            )
        except BaseException as exc:
            main_error = exc
        finally:
            self._main_done.set()
            thread.join(8)
        if thread.is_alive():
            self.fail("driver still running after main() returned")
        if box["error"] is not None:
            if main_error is not None:
                raise box["error"] from main_error
            raise box["error"]
        if main_error is not None:
            raise main_error
        return spoken

    def _record_exit(self):
        exits = []

        def fake_exit(code):
            exits.append(code)

        return exits, fake_exit

    def test_own_prompt_during_prelock_drops_the_line(self):
        """A user prompt in this session drops a line held in the pre-lock wait."""
        drop = VOICE_HOOK._PROMPT_DROP_LOG
        for label, with_holder in (("other session holds lock", True), ("lock empty", False)):
            with self.subTest(label):
                self._clear_log()
                self._wipe_arrival()
                self.write_state("recording")
                path = self._transcript(self._user_json("old prompt") + "\n")
                holder = None
                release = None
                if with_holder:
                    holder, release = self.spawn_holder("other-session")
                exits, fake_exit = self._record_exit()
                text = "Line that must not be spoken."
                appended = {}

                def drive(path=path, appended=appended):
                    self._wait_until(
                        lambda: "voxtype: waiting" in self.log_text(),
                        2.0,
                        self.log_text(),
                    )
                    self.assertTrue(VOICE_HOOK._held_phase)
                    self.assertNotIn(drop, self.log_text())
                    prompt = self._user_json(
                        "  please stop",
                        origin={"kind": "human"},
                        isMeta=False,
                    ) + "\n"
                    appended["t"] = time.monotonic()
                    self._append_transcript(path, prompt)
                    self._wait_until(
                        lambda: drop in self.log_text(),
                        2.0,
                        self.log_text(),
                    )

                try:
                    with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 3.0), \
                            mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
                        spoken = self._run_main_driven(
                            "sess-b", text, drive, extra={"transcript_path": path},
                        )
                    self.assertIn("t", appended)
                    self.assertLess(
                        time.monotonic() - appended["t"], 1.5, self.log_text(),
                    )
                    self.assertEqual(spoken, [])
                    self.assertEqual(self.starts(), [])
                    self.assertEqual(self.log_text().count(drop), 1)
                    self.assertNotIn("Yielding lock", self.log_text())
                    self.assertNotIn("cap reached", self.log_text())
                    self.assertEqual(exits, [])
                    self.assertFalse(VOICE_HOOK._held_phase)
                    self.assertIsNone(VOICE_HOOK._lock_fd)
                    self.assertIsNone(VOICE_HOOK._arrival_entry_path)
                    self.assertEqual(self._arrival_files(), [])
                    if holder is not None:
                        self.assertIsNone(holder.poll())
                        self.assertTrue(self._lock_is_held())
                    else:
                        self.assertFalse(self._lock_is_held())
                        self.assertNotIn(
                            str(os.getpid()), Path(self.lock_path).read_text(),
                        )
                finally:
                    self.write_state("idle")
                    if release is not None:
                        Path(release).write_text("go")
                    if holder is not None:
                        holder.wait(timeout=2)

    @LINUX_PROCS
    def test_own_prompt_while_queued_drops_the_line(self):
        """Own prompt while queued behind another session drops the line."""
        drop = VOICE_HOOK._PROMPT_DROP_LOG
        self._clear_log()
        self.write_state("recording")
        path = self._transcript(self._user_json("old prompt") + "\n")
        holder, release = self.spawn_holder("other-session")
        queued = threading.Event()
        real_sleep = time.sleep

        def spy_sleep(seconds):
            if (
                "voxtype: idle after" in self.log_text()
                and seconds >= VOICE_HOOK.VOXTYPE_POLL_S * 0.5
            ):
                queued.set()
            return real_sleep(seconds)

        exits, fake_exit = self._record_exit()
        text = "Queued line."
        prompt = json.dumps({
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": "stop now"}],
            },
        }) + "\n"
        appended = {}

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            self.write_state("idle")
            self._wait_until(
                lambda: queued.is_set() or self._main_done.is_set(),
                2.0,
                self.log_text(),
            )
            self.assertTrue(queued.is_set(), self.log_text())
            self.assertNotIn(drop, self.log_text())
            appended["t"] = time.monotonic()
            self._append_transcript(path, prompt)
            self._wait_until(
                lambda: drop in self.log_text(),
                2.0,
                self.log_text(),
            )

        try:
            with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 3.0), \
                    mock.patch.object(VOICE_HOOK.time, "sleep", side_effect=spy_sleep), \
                    mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
                spoken = self._run_main_driven(
                    "sess-b", text, drive, extra={"transcript_path": path},
                )
            self.assertIn("t", appended)
            self.assertLess(time.monotonic() - appended["t"], 1.5, self.log_text())
            self.assertEqual(spoken, [])
            self.assertEqual(self.starts(), [])
            self.assertEqual(self.log_text().count(drop), 1)
            self.assertNotIn("Yielding lock", self.log_text())
            self.assertEqual(exits, [])
            self.assertIsNone(holder.poll())
            self.assertIsNone(VOICE_HOOK._lock_fd)
        finally:
            self.write_state("idle")
            Path(release).write_text("go")
            holder.wait(timeout=2)

    def test_non_user_transcript_entries_do_not_drop(self):
        """Harness, meta, sidechain, peer, and tool_result lines are not a prompt."""
        samples = [
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "hi"},
                {"type": "text", "text": "nope"},
            ]}},
            {"type": "user", "isMeta": True, "message": {"content": "hello there"}},
            {"type": "user", "message": {"content": "<task-notification>done"}},
            {"type": "user", "message": {"content": "<command-name>foo</command-name>"}},
            {"type": "user", "message": {"content": "<local-command>stdout"}},
            {"type": "user", "message": {"content": "[SYSTEM NOTIFICATION] hi"}},
            {"type": "user", "message": {"content": (
                "This session is being continued from a previous conversation. hi"
            )}},
            {"type": "user", "message": {"content": "Caveat: something"}},
            {"type": "user", "message": {"content": "  <task-notification>x"}},
            {"type": "user", "origin": {"kind": "peer"}, "message": {"content": "from a peer"}},
            {"type": "user", "isSidechain": True, "message": {"content": "side chain"}},
            {"type": "assistant", "message": {"content": "not a user"}},
            {"type": "user", "message": {"content": "   "}},
            {"type": "user", "message": {"content": [
                {"type": "text", "text": "<command-"},
                {"type": "text", "text": "x"},
            ]}},
        ]
        for obj in samples:
            raw = (json.dumps(obj) + "\n").encode()
            self.assertFalse(
                VOICE_HOOK._genuine_user_prompt_line(raw[:-1]),
                obj,
            )
        self.assertTrue(VOICE_HOOK._genuine_user_prompt_line(
            self._user_json("hello").encode(),
        ))
        self.assertTrue(VOICE_HOOK._genuine_user_prompt_line(
            self._user_json("hello", origin={"kind": "human"}).encode(),
        ))
        self.assertTrue(VOICE_HOOK._genuine_user_prompt_line(
            self._user_json([{"type": "text", "text": "from parts"}]).encode(),
        ))

        drop = VOICE_HOOK._PROMPT_DROP_LOG
        self.write_state("recording")
        path = self._transcript(self._user_json("old") + "\n")
        text = "This line is spoken."

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            blob = "".join(json.dumps(obj) + "\n" for obj in samples)
            self._append_transcript(path, blob)
            time.sleep(0.6)
            self.assertFalse(self._main_done.is_set(), self.log_text())
            self.assertNotIn(drop, self.log_text())
            self.write_state("idle")

        try:
            with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 4.0):
                spoken = self._run_main_driven(
                    "sess-b", text, drive, extra={"transcript_path": path},
                )
            self.assertEqual(spoken, [text])
            self.assertNotIn(drop, self.log_text())
            self.assertEqual(self.starts(), [])
        finally:
            self.write_state("idle")

    def test_partial_transcript_line_waits_for_newline(self):
        path = self._transcript(self._user_json("old prompt") + "\n")
        VOICE_HOOK._arm_transcript_watch(path)
        self.assertTrue(VOICE_HOOK._held_phase)
        self.assertFalse(VOICE_HOOK._own_prompt_since_baseline())
        partial = (
            json.dumps({"type": "assistant", "message": {"content": "no"}})
            + "\n"
            + self._user_json("hello")
        )
        self._append_transcript(path, partial)
        self.assertFalse(VOICE_HOOK._own_prompt_since_baseline())
        self.assertFalse(VOICE_HOOK._tw_own_prompt)
        self.assertTrue(VOICE_HOOK._held_phase)
        self._append_transcript(path, "\n")
        self.assertTrue(VOICE_HOOK._own_prompt_since_baseline())
        self.assertTrue(VOICE_HOOK._own_prompt_since_baseline())

    def test_sigterm_during_held_phase_keeps_line_after_grace(self):
        """No own prompt: do not exit. After the grace, keep the line, log once."""
        self.assertEqual(VOICE_HOOK.HELD_SIGTERM_GRACE_S, 2.0)
        kept = VOICE_HOOK._HELD_KEPT_LOG
        drop = VOICE_HOOK._PROMPT_DROP_LOG
        self.write_state("recording")
        path = self._transcript(self._user_json("old") + "\n")
        exits, fake_exit = self._record_exit()
        text = "Keep this line."

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            self.assertTrue(VOICE_HOOK._held_phase)
            VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
            self.assertEqual(exits, [])
            self.assertFalse(self._main_done.is_set())
            self._wait_until(
                lambda: kept in self.log_text(),
                2.0,
                self.log_text(),
            )
            time.sleep(0.4)
            self.assertEqual(self.log_text().count(kept), 1)
            self.assertNotIn(drop, self.log_text())
            self.assertEqual(exits, [])
            self.write_state("idle")

        try:
            with mock.patch.object(VOICE_HOOK, "HELD_SIGTERM_GRACE_S", 0.45), \
                    mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 4.0), \
                    mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
                spoken = self._run_main_driven(
                    "sess-b", text, drive, extra={"transcript_path": path},
                )
            self.assertEqual(spoken, [text])
            self.assertEqual(self.log_text().count(kept), 1)
            self.assertNotIn(drop, self.log_text())
            self.assertEqual(exits, [])
        finally:
            self.write_state("idle")

    def test_sigterm_during_held_phase_then_own_prompt_drops(self):
        self.assertEqual(VOICE_HOOK.HELD_SIGTERM_GRACE_S, 2.0)
        drop = VOICE_HOOK._PROMPT_DROP_LOG
        kept = VOICE_HOOK._HELD_KEPT_LOG
        self.write_state("recording")
        path = self._transcript(self._user_json("old") + "\n")
        exits, fake_exit = self._record_exit()

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
            self.assertEqual(exits, [])
            self._append_transcript(path, self._user_json("stop") + "\n")
            self._wait_until(
                lambda: drop in self.log_text(),
                2.0,
                self.log_text(),
            )

        try:
            with mock.patch.object(VOICE_HOOK, "HELD_SIGTERM_GRACE_S", 1.0), \
                    mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 4.0), \
                    mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
                spoken = self._run_main_driven(
                    "sess-b", "Drop me.", drive, extra={"transcript_path": path},
                )
            self.assertEqual(spoken, [])
            self.assertEqual(self.starts(), [])
            self.assertEqual(self.log_text().count(drop), 1)
            self.assertNotIn(kept, self.log_text())
            self.assertEqual(exits, [])
            self.assertFalse(self._lock_is_held())
        finally:
            self.write_state("idle")

    def test_unusable_transcript_sigterm_exits_as_today(self):
        """No path, a symlink, or another uid: SIGTERM exits immediately."""
        self.write_state("recording")
        real = self._transcript(self._user_json("old") + "\n")
        link = os.path.join(self.root, "transcript-link")
        os.symlink(real, link)
        ino = os.stat(real).st_ino
        real_fstat = os.fstat
        seen = {"n": 0}

        def foreign_fstat(fd):
            st = real_fstat(fd)
            if st.st_ino == ino and stat.S_ISREG(st.st_mode):
                seen["n"] += 1
                return mock.Mock(
                    st_mode=st.st_mode,
                    st_uid=st.st_uid + 1,
                    st_dev=st.st_dev,
                    st_ino=st.st_ino,
                    st_size=st.st_size,
                )
            return st

        cases = (
            ("no path", None, None),
            ("symlink", {"transcript_path": link}, None),
            ("other uid", {"transcript_path": real}, foreign_fstat),
        )
        for label, extra, fstat in cases:
            with self.subTest(label):
                self._clear_log()
                self._wipe_arrival()
                self.write_state("recording")
                seen["n"] = 0
                exits, fake_exit = self._record_exit()
                patches = [
                    mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 2.0),
                    mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit),
                ]
                if fstat is not None:
                    patches.append(
                        mock.patch.object(VOICE_HOOK.os, "fstat", side_effect=fstat),
                    )
                def drive(label=label):
                    self._wait_until(
                        lambda: "voxtype: waiting" in self.log_text(),
                        2.0,
                        self.log_text(),
                    )
                    self.assertFalse(VOICE_HOOK._held_phase, label)
                    self.assertFalse(VOICE_HOOK._tw_usable, label)
                    if label == "other uid":
                        self.assertGreaterEqual(seen["n"], 1)
                    VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
                    self.assertEqual(exits, [0])
                    self.write_state("idle")

                try:
                    with contextlib.ExitStack() as stack:
                        for patcher in patches:
                            stack.enter_context(patcher)
                        self._run_main_driven(
                            "sess-b", "Still spoken.", drive, extra=extra,
                        )
                finally:
                    self.write_state("idle")

    def test_lost_watch_with_pending_sigterm_exits(self):
        path = self._transcript(self._user_json("old") + "\n")
        VOICE_HOOK._arm_transcript_watch(path)
        self.assertTrue(VOICE_HOOK._held_phase)
        VOICE_HOOK._deferred_sigterm_at = time.monotonic()
        os.truncate(path, 0)

        def fake_exit(code):
            raise SystemExit(code)

        with mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
            with self.assertRaises(SystemExit) as caught:
                VOICE_HOOK._note_held_tick()
        self.assertEqual(caught.exception.code, 0)
        self.assertFalse(VOICE_HOOK._held_phase)
        self.assertFalse(VOICE_HOOK._tw_usable)

    def test_take_clears_held_phase_before_stamp(self):
        path = self._transcript(b"")
        VOICE_HOOK._arm_transcript_watch(path)
        self.assertTrue(VOICE_HOOK._held_phase)
        seen = {}
        real_stamp = VOICE_HOOK._stamp_lockfile

        def spy_stamp(lock_fd):
            seen["held"] = VOICE_HOOK._held_phase
            lock_fd.seek(0)
            seen["body"] = lock_fd.read()
            lock_fd.seek(0)
            return real_stamp(lock_fd)

        VOICE_HOOK._SESSION_ID = "sess"
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK, "_stamp_lockfile", side_effect=spy_stamp):
                lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            self.assertIsNotNone(lock)
            self.assertIn("held", seen)
            self.assertFalse(seen["held"])
            self.assertNotIn(str(os.getpid()), seen["body"])
            stamped = Path(self.lock_path).read_text().splitlines()
            self.assertEqual(stamped[0], str(os.getpid()))
            self.assertEqual(stamped[1], "sess")
            self.assertFalse(VOICE_HOOK._held_phase)
            with mock.patch.object(VOICE_HOOK.os, "_exit") as exit_:
                VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
            exit_.assert_called_once_with(0)
            self.assertIsNone(VOICE_HOOK._lock_fd)
        finally:
            if lock is not None and VOICE_HOOK._lock_fd is not None:
                VOICE_HOOK.release_lock(lock)

    def test_deferred_sigterm_after_lock_waits_then_speaks_or_drops(self):
        """Winning the lock inside the grace still watches the transcript."""
        self.assertEqual(VOICE_HOOK.HELD_SIGTERM_GRACE_S, 2.0)
        drop = VOICE_HOOK._PROMPT_DROP_LOG
        path = self._transcript(self._user_json("old") + "\n")
        self.write_state("recording")
        spoken_at = []

        def on_speak(_clean, _cfg, _lang=None):
            spoken_at.append(time.monotonic())

        exits, fake_exit = self._record_exit()
        grace = 0.6
        t0 = {}

        def drive_keep():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            t0["t"] = time.monotonic()
            VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
            time.sleep(0.08)
            self.write_state("idle")

        try:
            with mock.patch.object(VOICE_HOOK, "HELD_SIGTERM_GRACE_S", grace), \
                    mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 3.0), \
                    mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
                spoken = self._run_main_driven(
                    "sess-b",
                    "Say it after the grace.",
                    drive_keep,
                    extra={"transcript_path": path},
                    on_speak=on_speak,
                )
            self.assertEqual(spoken, ["Say it after the grace."])
            self.assertEqual(len(spoken_at), 1)
            self.assertGreaterEqual(spoken_at[0] - t0["t"], grace - 0.05)
            self.assertLess(spoken_at[0] - t0["t"], grace + 1.0)
            self.assertNotIn(drop, self.log_text())
            self.assertEqual(exits, [])
        finally:
            self.write_state("idle")

        self._clear_log()
        self.write_state("recording")
        path = self._transcript(self._user_json("old") + "\n")
        exits, fake_exit = self._record_exit()
        grace = 1.2

        def drive_drop():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
            self.write_state("idle")
            self._wait_until(
                lambda: Path(self.lock_path).read_text().startswith(str(os.getpid()))
                or drop in self.log_text()
                or self._main_done.is_set(),
                1.5,
                self.log_text(),
            )
            self._append_transcript(path, self._user_json("stop") + "\n")
            self._wait_until(
                lambda: drop in self.log_text() or self._main_done.is_set(),
                2.0,
                self.log_text(),
            )

        try:
            with mock.patch.object(VOICE_HOOK, "HELD_SIGTERM_GRACE_S", grace), \
                    mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 3.0), \
                    mock.patch.object(VOICE_HOOK.os, "_exit", side_effect=fake_exit):
                spoken = self._run_main_driven(
                    "sess-b", "Do not say this.", drive_drop,
                    extra={"transcript_path": path},
                )
            self.assertEqual(spoken, [])
            self.assertEqual(self.log_text().count(drop), 1)
            self.assertEqual(exits, [])
            self.assertFalse(self._lock_is_held())
            self.assertIsNone(VOICE_HOOK._lock_fd)
        finally:
            self.write_state("idle")

    def test_in_lock_gate_does_not_read_the_transcript(self):
        path = self._transcript(self._user_json("old") + "\n")
        VOICE_HOOK._arm_transcript_watch(path)
        self.assertTrue(VOICE_HOOK._held_phase)
        self.write_state("recording")
        calls = []

        def spy():
            calls.append(1)
            return False

        with mock.patch.object(VOICE_HOOK, "_own_prompt_since_baseline", side_effect=spy):
            VOICE_HOOK._wait_for_voxtype_idle_status(max_wait_s=0.35, heartbeat=True)
            self.assertEqual(calls, [])
            VOICE_HOOK._wait_for_voxtype_idle_status(max_wait_s=0.35, heartbeat=False)
        self.assertGreater(len(calls), 0)

    def test_absent_state_does_not_open_the_transcript(self):
        path = self._transcript(self._user_json("hi") + "\n")
        opened = []
        real_open = os.open

        def spy(file, flags, *args, **kwargs):
            if os.fspath(file) == path:
                opened.append(flags)
            return real_open(file, flags, *args, **kwargs)

        cases = (
            ("absent", False, None, "day-session"),
            ("macos", True, None, "day-session"),
            ("kill switch", True, self._cfg(voxtype_wait=False), "day-session"),
        )
        for label, present, cfg, session in cases:
            with self.subTest(label):
                opened.clear()
                if present:
                    self.write_state("idle")
                else:
                    try:
                        os.remove(self.state_path)
                    except FileNotFoundError:
                        pass
                patches = [mock.patch.object(VOICE_HOOK.os, "open", side_effect=spy)]
                if label == "macos":
                    patches.append(mock.patch.object(VOICE_HOOK, "IS_MACOS", True))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    spoken = self._run_main(
                        session, "No voxtype here.", cfg=cfg,
                        extra={"transcript_path": path},
                    )
                self.assertEqual(spoken, ["No voxtype here."])
                self.assertEqual(opened, [], label)
                self.assertFalse(VOICE_HOOK._held_phase)
                self.assertFalse(VOICE_HOOK._tw_usable)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo unavailable")
    def test_fifo_lock_is_refused_without_blocking(self):
        os.remove(self.lock_path)
        os.mkfifo(self.lock_path, 0o444)
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                self._clear_log()
                started = time.monotonic()
                lock = self.run_bounded(
                    lambda g=graceful: VOICE_HOOK.acquire_lock(graceful_steal=g),
                    timeout=2.0,
                )
                elapsed = time.monotonic() - started
                self.assertIsNone(lock)
                self.assertLess(elapsed, 0.5, self.log_text())
                self.assertIn("lock refused:", self.log_text())

    @LINUX_PROCS
    def test_poll_take_drops_when_newer_arrives_during_steal(self):
        """Newer arrival during the steal poll returns None and keeps no lock."""
        drop = VOICE_HOOK._NEWER_ARRIVAL_LOG
        sid = "same-session"
        VOICE_HOOK._SESSION_ID = sid
        holder, release = self.spawn_holder(sid)
        real_kill = os.kill
        polls = {"n": 0}
        real_sleep = time.sleep

        def spy_kill(pid, sig):
            if int(pid) == holder.pid and sig != 0:
                return None
            return real_kill(pid, sig)

        def spy_sleep(seconds):
            if seconds <= VOICE_HOOK.LOCK_STEAL_POLL_S + 1e-9:
                polls["n"] += 1
            return real_sleep(seconds)

        def yield_to_newer():
            return polls["n"] >= 1

        self._clear_log()
        try:
            started = time.monotonic()
            with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy_kill), \
                    mock.patch.object(VOICE_HOOK.time, "sleep", side_effect=spy_sleep):
                lock = VOICE_HOOK.acquire_lock(
                    graceful_steal=True, yield_to_newer=yield_to_newer,
                )
            elapsed = time.monotonic() - started
            self.assertIsNone(lock)
            self.assertIsNone(VOICE_HOOK._lock_fd)
            self.assertLess(elapsed, 1.0, self.log_text())
            self.assertEqual(self.log_text().count(drop), 1)
            self.assertIsNone(holder.poll())
        finally:
            Path(release).write_text("go")
            holder.wait(timeout=2)
        probe = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    @LINUX_PROCS
    def test_poll_take_releases_fd_when_newer_after_win(self):
        """A take() that wins inside the steal poll is released if a newer line exists."""
        drop = VOICE_HOOK._NEWER_ARRIVAL_LOG
        sid = "same-session"
        VOICE_HOOK._SESSION_ID = sid
        holder, _release = self.spawn_holder(sid, release_path="-")
        stamped = {"n": 0}
        real_stamp = VOICE_HOOK._stamp_lockfile

        def spy_stamp(lock_fd):
            stamped["n"] += 1
            return real_stamp(lock_fd)

        def yield_to_newer():
            return stamped["n"] > 0

        self._clear_log()
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK, "_stamp_lockfile", side_effect=spy_stamp):
                lock = VOICE_HOOK.acquire_lock(
                    graceful_steal=True, yield_to_newer=yield_to_newer,
                )
            self.assertIsNone(lock)
            self.assertIsNone(VOICE_HOOK._lock_fd)
            self.assertGreaterEqual(stamped["n"], 1)
            self.assertEqual(self.log_text().count(drop), 1)
            self.assertIsNotNone(holder.wait(timeout=2))
            probe = os.open(self.lock_path, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)
            if holder.poll() is None:
                holder.kill()


if __name__ == "__main__":
    unittest.main()
