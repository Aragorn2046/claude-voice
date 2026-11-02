"""VoxType speech-wait gate. No real audio: paplay is a temp stand-in."""

import ast
import contextlib
import errno
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
import socket
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
import signal
import sys
import time

log_path = os.environ["FAKE_PAPLAY_LOG"]
play_s = float(os.environ.get("FAKE_PLAY_S", "1.0"))
exit_code = int(os.environ.get("FAKE_PAPLAY_EXIT", "0"))
term_exit0 = os.environ.get("FAKE_PAPLAY_TERM_EXIT0") == "1"


def append(line):
    fd = os.open(log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def on_term(_signum, _frame):
    append("term")
    os._exit(0)


if term_exit0:
    signal.signal(signal.SIGTERM, on_term)

append(f"start {os.getpid()}")
if exit_code:
    sys.exit(exit_code)
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

# Same body as LONG_PAPLAY, but SIGTERM logs termrecv and does not exit.
# The process keeps running until SIGKILL.
SLOW_TERM_PAPLAY = r"""
import os
import signal
import sys
import time

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


def on_term(_signum, _frame):
    append(f"termrecv {os.getpid()} {time.time():.6f}")


signal.signal(signal.SIGTERM, on_term)
append(f"start {os.getpid()} {time.time():.6f}")
deadline = time.monotonic() + 60.0
while time.monotonic() < deadline:
    try:
        sys.stdin.buffer.read(1)
    except Exception:
        pass
    try:
        time.sleep(0.05)
    except Exception:
        pass
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

# Holds the lock, parents a paplay, and exits on SIGTERM without killing it.
# os._exit skips the finally, so the kernel closes the lock fd and the child
# paplay keeps running.
TERM_EXIT_PAPLAY_HOLDER = r"""
import fcntl
import os
import signal
import subprocess
import sys
import time

path, sid, ready_path = sys.argv[1:4]


def on_term(_signum, _frame):
    os._exit(0)


signal.signal(signal.SIGTERM, on_term)
player = subprocess.Popen(
    ["paplay", "--raw", "--rate=24000", "--channels=1", "--format=s16le"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
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
    while player.poll() is None:
        time.sleep(0.05)
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

# start/end carry timestamps so queue tests can prove the players do not overlap.
TIMED_PAPLAY = r"""
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


append(f"start {os.getpid()} {time.time():.6f}")
sys.stdin.buffer.read()
time.sleep(play_s)
append(f"end {os.getpid()} {time.time():.6f}")
"""

# Child hook. speak() records the line, then plays PCM through the gated player.
QUEUE_SPEAKER = r"""
import importlib.util
import io
import json
import os
import sys
import time

hook_path, line, session_id, lock_path, log_path = sys.argv[1:6]
spec = importlib.util.spec_from_file_location(
    "voice_stop_hook_queue_child", hook_path,
)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
hook.LOCKFILE_PATH = lock_path
hook.LOG_PATH = log_path

# Optional: record every SIGTERM the hook receives, then run the real handler.
# Queue mode must not be sent one. An empty log is the proof.
sig_log = os.environ.get("QUEUE_SIG_LOG")
if sig_log:
    _real_sigterm = hook.handle_sigterm

    def _wrapped_sigterm(signum, frame, _real=_real_sigterm, _path=sig_log):
        fd = os.open(_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, (str(int(signum)) + "\n").encode())
        finally:
            os.close(fd)
        return _real(signum, frame)

    hook.handle_sigterm = _wrapped_sigterm


def speak(clean, cfg, lang_hint=None):
    path = os.environ["QUEUE_SPOKEN_LOG"]
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, (clean + "\n").encode())
    finally:
        os.close(fd)
    hook.play_raw_pcm(b"\x00" * 4800, 24000, 1)


hook.speak = speak


def load_config():
    # Optional hold after the early ticket exists and before config returns.
    # QUEUE_HOLD_CONFIG is the release path. QUEUE_HELD_FLAG is touched first.
    hold = os.environ.get("QUEUE_HOLD_CONFIG") or ""
    if hold:
        flag = os.environ.get("QUEUE_HELD_FLAG") or ""
        if flag:
            fd = os.open(flag, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
            os.close(fd)
        deadline = time.monotonic() + 15
        while not os.path.exists(hold) and time.monotonic() < deadline:
            time.sleep(0.01)
    return {
        "tts_enabled": True,
        "remote_audio": False,
        "voxtype_wait": True,
    }


hook.load_config = load_config
payload = json.dumps({
    "last_assistant_message": '<voice lang="en">' + line + "</voice>",
    "session_id": session_id,
})
sys.stdin = io.StringIO(payload)
hook.main()
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


class _ImmediateExit:
    """Player that has already exited by the time the hook waits."""

    def __init__(self, code):
        self.code = code
        self.returncode = None
        self.stdin = _NullStdin()
        self.terminate_called = False
        self.kill_called = False

    def poll(self):
        self.returncode = self.code
        return self.code

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True


class VoxTypeWaitTests(unittest.TestCase):
    def setUp(self):
        # pid -> pidfd, opened only while that pid was still our fake paplay.
        self._player_pidfds = {}
        self._player_fd_lock = threading.Lock()
        self.addCleanup(self._close_player_pidfds)
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
        self.queue_dir = os.path.join(root, "queue")
        self.bin_dir = os.path.join(root, "bin")
        os.makedirs(self.bin_dir)
        self.paplay_path = os.path.join(self.bin_dir, "paplay")
        self._install_paplay(FAKE_PAPLAY)
        Path(self.lock_path).write_text("")
        self.pcm = PCM
        self.threads = []

        env = {
            "SHELBY_VOXTYPE_STATE_FILE": self.state_path,
            "SHELBY_VOICE_QUEUE_DIR": self.queue_dir,
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
        VOICE_HOOK._ticket_stamp_ns = None
        VOICE_HOOK._ticket_path = None
        VOICE_HOOK._queue_mode = False
        VOICE_HOOK._queue_sigterm_logged = False
        VOICE_HOOK._wait_spent_s = 0.0
        VOICE_HOOK._last_heartbeat_at = None

    def tearDown(self):
        try:
            self.write_state("idle")
        except OSError:
            pass
        for pid in self.player_pids():
            # Historical log pids are often already reaped. Signal only the
            # pidfd taken while the process was still our live paplay.
            self._signal_owned_player(pid, signal.SIGKILL, reap=True)
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
        pids = [int(line.split()[1]) for line in self.starts()]
        for pid in pids:
            self._remember_player(pid)
        return pids

    def _close_player_pidfds(self):
        with self._player_fd_lock:
            fds = list(self._player_pidfds.values())
            self._player_pidfds.clear()
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _player_identity(self, pid):
        """(starttime, state, ppid, comm, argv0) or OSError if pid is gone."""
        text = Path(f"/proc/{pid}/stat").read_text()
        start = text.find("(")
        end = text.rfind(")")
        if start < 0 or end <= start:
            raise OSError(f"unreadable stat for {pid}")
        rest = text[end + 1:].split()
        if len(rest) < 20:
            raise OSError(f"short stat for {pid}")
        comm = Path(f"/proc/{pid}/comm").read_text().split("\n", 1)[0].strip()
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00", 1)[0]
        except OSError:
            raw = b""
        return (int(rest[19]), rest[0], int(rest[1]), comm, os.path.basename(raw))

    def _ancestor_is_this_test(self, pid):
        """True when pid is this process or a descendant of it."""
        me = os.getpid()
        cur = int(pid)
        seen = set()
        for _ in range(8):
            if cur == me:
                return True
            if cur <= 1 or cur in seen:
                return False
            seen.add(cur)
            try:
                _comm, _state, parent = self._proc_stat(cur)
            except (OSError, ValueError, IndexError):
                return False
            cur = parent
        return False

    def _identity_is_owned_paplay(self, pid, ident):
        """Live fake paplay this test started. Not a zombie, not a reused pid.

        The default fake is ``exec -a paplay``, so comm stays the interpreter
        and argv0 is paplay. The long fakes set comm to paplay. A holder this
        test spawned may be the parent; that child is still ours.
        """
        _starttime, state, _ppid, comm, argv0 = ident
        if state == "Z":
            return False
        if comm != "paplay" and argv0 != b"paplay":
            return False
        return self._ancestor_is_this_test(pid)

    def _remember_player(self, pid):
        """Open a pidfd while pid is still the fake paplay we started.

        The fd keeps referring to that process after the pid is reused.
        A pid that fails the check is not recorded and is never signalled.
        """
        pid = int(pid)
        if not hasattr(os, "pidfd_open"):
            return False
        with self._player_fd_lock:
            if pid in self._player_pidfds:
                return True
        try:
            before = self._player_identity(pid)
        except (OSError, ValueError):
            return False
        if not self._identity_is_owned_paplay(pid, before):
            return False
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return False
        try:
            after = self._player_identity(pid)
        except (OSError, ValueError):
            os.close(fd)
            return False
        # starttime changes when the pid is reused. Drop the fd if it no
        # longer names the process we just checked.
        if before[0] != after[0] or not self._identity_is_owned_paplay(pid, after):
            os.close(fd)
            return False
        with self._player_fd_lock:
            existing = self._player_pidfds.get(pid)
            if existing is not None:
                os.close(fd)
                return True
            self._player_pidfds[pid] = fd
        return True

    def _reap_player(self, pid):
        """Reap a direct child we just signalled. A grandchild is the holder's."""
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                got, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError:
                return
            if got:
                return
            time.sleep(0.01)

    def _signal_owned_player(self, pid, sig, reap=False):
        """Signal pid only through a pidfd opened while it was our paplay.

        Returns False without signalling when the pid was never owned.
        reap is for cleanup SIGKILL. The hook must reap an outside SIGTERM
        itself, or Popen treats ECHILD as exit 0 and the status is lost.
        """
        pid = int(pid)
        if not hasattr(signal, "pidfd_send_signal"):
            return False
        self.player_pids()
        if not self._remember_player(pid):
            with self._player_fd_lock:
                owned = pid in self._player_pidfds
            if not owned:
                return False
        with self._player_fd_lock:
            fd = self._player_pidfds.get(pid)
        if fd is None:
            return False
        try:
            signal.pidfd_send_signal(fd, sig)
        except OSError:
            return False
        if reap:
            self._reap_player(pid)
        return True

    def _start_logged(self, pid):
        token = str(int(pid))
        for line in self.starts():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == token:
                return True
        return False

    def _wait_for_start(self, pid, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._start_logged(pid):
                self._remember_player(pid)
                return True
            time.sleep(0.01)
        return False

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
                # Adopt the pidfd while the fake is still alive.
                self.player_pids()
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
                self._signal_owned_player(pid, signal.SIGKILL, reap=True)
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
                or self._is_queue_path(text)
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

    def _is_queue_path(self, text):
        if text == self.queue_dir or text.startswith(self.queue_dir + os.sep):
            return True
        if "voice-stop-hook-queue" in text or "voice-stop-hook-arrivals" in text:
            return True
        runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
        if runtime:
            default = os.path.join(runtime, "voice-stop-hook-queue")
            if text == default or text.startswith(default + os.sep):
                return True
        return False

    @contextlib.contextmanager
    def forbid_arrival_io(self):
        """Fail if the queue registry or the real runtime dir is touched.

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
            if self._is_queue_path(text):
                raise AssertionError(f"queue path accessed: {text}")

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
        env["SHELBY_VOICE_QUEUE_DIR"] = self.queue_dir
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
        env["SHELBY_VOICE_QUEUE_DIR"] = self.queue_dir
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
        self._remember_player(old_pid)
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
        self._signal_owned_player(pid, signal.SIGKILL, reap=True)

    def _age_lock(self, extra_s=30.0):
        """Push the lock mtime far enough past LOCK_STALE_S to count as wedged."""
        old = time.time() - float(VOICE_HOOK.LOCK_STALE_S) - extra_s
        os.utime(self.lock_path, (old, old))

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

    @LINUX_PROCS
    def test_streaming_holds_then_plays_when_idle(self):
        self._assert_holds_then_plays("streaming")

    @LINUX_PROCS
    def test_paused_holds_then_plays_when_idle(self):
        self._assert_holds_then_plays("paused")

    def test_stopped_empty_and_garbage_are_not_busy(self):
        """idle, stopped, empty, and malformed bytes are not busy.

        The word garbage is a well-formed state, so it is busy. A space,
        a control byte, non-ASCII, or a word longer than 32 is not.
        """
        not_busy = (
            "idle",
            "stopped",
            "",
            "bad state\x00",
            "café",
            "a" * 40,
        )
        for value in not_busy:
            with self.subTest(value=repr(value)):
                self.write_state(value)
                self.assertFalse(VOICE_HOOK.voxtype_busy())
                self.assertIsNone(VOICE_HOOK._voxtype_busy_state())
                with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.4):
                    started = time.perf_counter()
                    waited = VOICE_HOOK.wait_for_voxtype_idle()
                    elapsed = time.perf_counter() - started
                self.assertEqual(waited, 0.0)
                self.assertLess(elapsed, 0.05)
        self.write_state("garbage")
        self.assertTrue(VOICE_HOOK.voxtype_busy())
        self.assertEqual(VOICE_HOOK._voxtype_busy_state(), "garbage")
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
            "voxtype: cap reached after 0.6s total wait while still busy, line dropped",
            text,
        )
        self.assertNotIn("will replay", text)

    def test_cap_while_streaming_drops_line(self):
        """A gate that stays streaming until the cap never starts a player."""
        self.write_state("streaming")
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
            "voxtype: cap reached after 0.6s total wait while still busy, line dropped",
            text,
        )
        self.assertIn("voxtype: waiting (state=streaming)", text)
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
    def test_busy_word_with_dead_daemon_pid_is_not_busy(self):
        """A busy word is not busy once the pid file proves the daemon is dead."""
        pid = self.dead_pid()
        for word in ("streaming", "paused"):
            with self.subTest(word=word):
                self.write_state(word)
                if os.path.exists(self.pid_path):
                    os.remove(self.pid_path)
                self.assertEqual(VOICE_HOOK._voxtype_busy_state(), word)
                self.write_pid(pid)
                self.assertFalse(VOICE_HOOK.voxtype_busy())
                self.assertIsNone(VOICE_HOOK._voxtype_busy_state())
                with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.4):
                    self.assertEqual(
                        VOICE_HOOK._wait_for_voxtype_idle_status(),
                        (0.0, False),
                    )
        self.assertNotIn("voxtype: waiting", self.log_text())

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
            "voxtype: dictation started during playback, stopped; will replay (1)",
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
            "voxtype: dictation started during playback, stopped; will replay (1)",
            text,
        )
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (2)",
            text,
        )
        self.assertNotIn("line dropped", text)

    @LINUX_PROCS
    def test_repeated_dictation_replays_past_the_old_limit(self):
        """Dictation can interrupt more than the old cap of 5. The line still ends."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        interruptions = 6
        thread = self.start_play()
        for n in range(1, interruptions + 1):
            self.assertTrue(self.wait_starts(n, 2), self.log_text())
            time.sleep(0.12)
            self.write_state("recording")
            pid = self.player_pids()[n - 1]
            self._wait_until(
                lambda pid=pid: not self.pid_running(pid),
                1.0,
                self.log_text(),
            )
            if n == interruptions:
                os.environ["FAKE_PLAY_S"] = "0.2"
            time.sleep(0.08)
            self.write_state("idle")
        self.assertTrue(self.wait_starts(interruptions + 1, 2), self.log_text())
        self.finish(thread, 3)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), interruptions + 1)
        self.assertEqual(len(self.ends()), 1)
        text = self.log_text()
        for k in range(1, interruptions + 1):
            self.assertIn(
                "voxtype: dictation started during playback, stopped; "
                f"will replay ({k})",
                text,
            )
        self.assertNotIn("interrupted the line", text)
        self.assertNotIn("line dropped", text)

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
            "voxtype: dictation started during playback, stopped; will replay (1)",
            text,
        )
        self.assertEqual(text.count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 0.6s total wait while still busy, line dropped",
            text,
        )

    @LINUX_PROCS
    def test_outside_kill_while_idle_ends_the_line(self):
        """A SIGTERM this test sends to its own paplay ends the line. No replay."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "30"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1), self.log_text())
        first = self.player_pids()[0]
        killed = time.monotonic()
        self.assertTrue(
            self._signal_owned_player(first, signal.SIGTERM),
            f"did not signal owned paplay {first}",
        )
        self.finish(thread, 2)
        self.assertLess(time.monotonic() - killed, 1.0)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1, self._play_lines())
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertIn(
            "voxtype: player stopped from outside (signal 15), line ends",
            text,
        )
        self.assertEqual(text.count("line ends"), 1)
        self.assertNotIn("will replay", text)
        self.assertNotIn("giving up", text)
        self.assertNotIn("dictation started", text)

    def _assert_outside_kill_while_busy(self, exit0):
        """Outside SIGTERM while state is recording. No busy-helper patch.

        The poll stays long so the hook is blocked in wait() when the signal
        lands. That is the outside-stop path: the hook does not send its own
        dictation SIGTERM. The state file stays recording through classification.
        """
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "30"
        if exit0:
            os.environ["FAKE_PAPLAY_TERM_EXIT0"] = "1"
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_POLL_S", 5.0):
            thread = self.start_play()
            self.assertTrue(self.wait_starts(1, 1), self.log_text())
            pid = self.player_pids()[0]
            self.write_state("recording")
            self.assertTrue(VOICE_HOOK.voxtype_busy())
            self.assertEqual(VOICE_HOOK._voxtype_busy_state(), "recording")
            killed = time.monotonic()
            self.assertTrue(
                self._signal_owned_player(pid, signal.SIGTERM),
                f"did not signal owned paplay {pid}",
            )
            # Still busy while the hook observes the exit and returns.
            self.assertTrue(VOICE_HOOK.voxtype_busy())
            self.finish(thread, 2)
            elapsed = time.monotonic() - killed
        self.assertLess(elapsed, 1.0, self.log_text())
        self.assertEqual(thread.errors, [])
        self.assertTrue(VOICE_HOOK.voxtype_busy())
        self.assertEqual(VOICE_HOOK._voxtype_busy_state(), "recording")
        self.assertEqual(len(self.starts()), 1, self._play_lines())
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertNotIn("will replay", text)
        self.assertNotIn("dictation started", text)
        self.assertNotIn("voxtype: waiting", text)
        self.assertNotIn("line dropped", text)
        self.assertNotIn("giving up", text)
        return text

    @LINUX_PROCS
    def test_outside_kill_while_busy_ends_the_line(self):
        """Outside SIGTERM while recording, player dies by signal. No replay."""
        text = self._assert_outside_kill_while_busy(exit0=False)
        self.assertIn(
            "voxtype: player stopped from outside (signal 15), line ends",
            text,
        )
        self.assertEqual(text.count("line ends"), 1)

    @LINUX_PROCS
    def test_outside_kill_while_busy_exit0_ends_the_line(self):
        """Outside SIGTERM while recording, paplay exits 0. The line ends.

        Real paplay exits 0 on SIGTERM. The hook cannot tell that from a
        natural end, so it logs nothing extra and does not replay. It must
        still return without waiting for VoxType to go idle.
        """
        text = self._assert_outside_kill_while_busy(exit0=True)
        self.assertIn("term", self._play_lines())
        self.assertNotIn("player stopped from outside", text)
        self.assertNotIn("line ends", text)

    @LINUX_PROCS
    def test_outside_kill_ends_without_replay(self):
        """One outside kill ends the line. There is no replay."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "30"
        stop = threading.Event()
        killed = []

        def killer():
            seen = set()
            while not stop.is_set():
                for pid in self.player_pids():
                    if pid in seen:
                        continue
                    seen.add(pid)
                    self._signal_owned_player(pid, signal.SIGTERM)
                    killed.append(pid)
                time.sleep(0.01)

        killer_thread = threading.Thread(target=killer, daemon=True)
        killer_thread.start()
        self.addCleanup(stop.set)
        self.addCleanup(killer_thread.join, 2)
        started = time.monotonic()
        result = VOICE_HOOK._play_pcm_voxtype_gated(
            self.pcm, 24000, 1, timeout=3,
        )
        elapsed = time.monotonic() - started
        stop.set()
        killer_thread.join(2)
        self.assertIsNone(result)
        self.assertLess(elapsed, 2, self.log_text())
        text = self.log_text()
        self.assertEqual(len(self.starts()), 1, self._play_lines())
        self.assertEqual(self.ends(), [])
        self.assertIn(
            "voxtype: player stopped from outside (signal 15), line ends",
            text,
        )
        self.assertEqual(text.count("line ends"), 1)
        self.assertNotIn("will replay", text)
        self.assertNotIn("giving up", text)
        self.assertNotIn("dictation started", text)
        self.assertEqual(len(killed), 1)

    @LINUX_PROCS
    def test_outside_sigterm_exit0_ends_without_replay(self):
        """Outside SIGTERM while idle, paplay exits 0. The line ends, no replay.

        The hook cannot tell this exit 0 from a natural end. It logs nothing
        extra (no ``line ends`` line). A negative status is the path that logs
        ``player stopped from outside``.
        """
        self.write_state("idle")
        os.environ["FAKE_PAPLAY_TERM_EXIT0"] = "1"
        os.environ["FAKE_PLAY_S"] = "30"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1), self.log_text())
        pid = self.player_pids()[0]
        killed = time.monotonic()
        self.assertTrue(
            self._signal_owned_player(pid, signal.SIGTERM),
            f"did not signal owned paplay {pid}",
        )
        self.finish(thread, 2)
        self.assertLess(time.monotonic() - killed, 1.0, self._play_lines())
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 1, self._play_lines())
        self.assertIn("term", self._play_lines())
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertNotIn("will replay", text)
        self.assertNotIn("dictation started", text)
        self.assertNotIn("player stopped from outside", text)
        self.assertNotIn("line ends", text)

    @LINUX_PROCS
    def test_dictation_replays_when_paplay_exits_0_on_our_sigterm(self):
        """The hook's own stop is still dictation when paplay exits 0."""
        self.write_state("idle")
        os.environ["FAKE_PAPLAY_TERM_EXIT0"] = "1"
        os.environ["FAKE_PLAY_S"] = "2"
        thread = self.start_play()
        self.assertTrue(self.wait_starts(1, 1), self.log_text())
        time.sleep(0.25)
        self.write_state("recording")
        first = self.player_pids()[0]
        self._wait_until(
            lambda: not self.pid_running(first), 1.0, "first player still running",
        )
        os.environ["FAKE_PLAY_S"] = "0.2"
        time.sleep(0.2)
        self.write_state("idle")
        self.finish(thread, 5)
        self.assertEqual(thread.errors, [])
        self.assertEqual(len(self.starts()), 2, self._play_lines())
        self.assertIn("term", self._play_lines())
        self.assertEqual(len(self.ends()), 1)
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (1)",
            self.log_text(),
        )
        self.assertNotIn("line ends", self.log_text())

    @LINUX_PROCS
    def test_paplay_error_exit_does_not_replay(self):
        """A positive exit code is a paplay error, not an outside kill."""
        self.write_state("idle")
        os.environ["FAKE_PAPLAY_EXIT"] = "1"
        os.environ["FAKE_PLAY_S"] = "5"
        started = time.monotonic()
        result = VOICE_HOOK._play_pcm_voxtype_gated(
            self.pcm, 24000, 1, timeout=3,
        )
        elapsed = time.monotonic() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 2)
        self.assertEqual(len(self.starts()), 1, self._play_lines())
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertNotIn("will replay", text)
        self.assertNotIn("outside signal", text)
        self.assertNotIn("line ends", text)
        self.assertNotIn("line dropped", text)

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
        """Outside queue mode, SIGTERM stops the player and the line ends."""
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "30"
        # os._exit is mocked, so this process stays up. The player's death
        # is an outside stop: the line ends, and it is not replayed.
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
        text = self.log_text()
        self.assertIn(
            "voxtype: player stopped from outside (signal 15), line ends",
            text,
        )
        self.assertNotIn("will replay", text)

    @LINUX_PROCS
    def test_arming_race_stops_player_spawned_into_dictation(self):
        self.write_state("idle")
        os.environ["FAKE_PLAY_S"] = "2"
        real_popen = subprocess.Popen
        flipped = []

        def popen_then_record(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            if not flipped:
                # State stays idle until this child's start line is in the log.
                # The hook may SIGTERM as soon as we return and the state is
                # recording; the marker has to exist before that flip.
                self._wait_for_start(proc.pid, 1.0)
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
            "voxtype: dictation started during playback, stopped; will replay (1)",
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

    def test_external_kill_while_busy_ends_the_line(self):
        """A player already dead with a signal is not our stop. The line ends."""
        proc = mock.Mock()
        proc.poll.return_value = -15
        proc.wait.return_value = -15
        self.assertFalse(VOICE_HOOK._stop_player(proc))
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

        self.write_state("idle")
        dead = _ExitedOnStopPoll(-signal.SIGTERM)
        results = []
        real_stop = VOICE_HOOK._stop_player

        def spy(player):
            player._in_stop = True
            result = real_stop(player)
            results.append(result)
            return result

        def popen(*_args, **_kwargs):
            self.write_state("recording")
            return dead

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 2.0), \
                mock.patch.object(
                    VOICE_HOOK.subprocess, "Popen", side_effect=popen,
                ) as popen_mock, \
                mock.patch.object(VOICE_HOOK, "_stop_player", side_effect=spy):
            result = VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
        self.assertIsNone(result)
        self.assertEqual(popen_mock.call_count, 1)
        self.assertEqual(results, [False])
        self.assertFalse(dead.terminate_called)
        self.assertFalse(dead.kill_called)
        text = self.log_text()
        self.assertIn(
            "voxtype: player stopped from outside (signal 15), line ends",
            text,
        )
        self.assertEqual(text.count("line ends"), 1)
        self.assertNotIn("will replay", text)
        self.assertNotIn("voxtype: waiting", text)
        self.assertNotIn("dictation started", text)
        self.assertNotIn("line dropped", text)
        self.assertNotIn("giving up", text)

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
        # The assignment sits inside the exit try, so walk the whole function.
        assigns = [
            node for node in ast.walk(main)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_VOXTYPE_WAIT_ENABLED"
                for target in node.targets
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

        def spy_utime(path, times=None, **kwargs):
            utimed.append(path if isinstance(path, int) else os.fspath(path))
            return real_utime(path, times)

        acquire_calls = []
        real_acquire = VOICE_HOOK.acquire_lock

        def spy_acquire(queue_wait_s=0.0, graceful_steal=False):
            acquire_calls.append(queue_wait_s)
            return real_acquire(
                queue_wait_s=queue_wait_s,
                graceful_steal=graceful_steal,
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
        self.assertFalse(
            any(os.fspath(item) == self.lock_path for item in snapshot["utimed"]),
            snapshot["utimed"],
        )
        self.assertEqual(os.stat(self.lock_path).st_mtime_ns, before)
        self.assertEqual(Path(self.lock_path).read_text(), expected)
        self.assertNotIn(self.lock_path, utimed)
        self.assertEqual(
            wait_calls, [((), {"max_wait_s": 1.5, "heartbeat": True})],
        )
        self.assertEqual(acquire_calls, [0.0])
        self.assertEqual(spoken, [])
        self.assertNotIn("Yielding lock", self.log_text())
        self.assertIn("queue: cap reached after 1.5s, line dropped", self.log_text())
        self.assertNotIn(self.lock_path, [os.fspath(item) for item in utimed])
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

        def spy_acquire(queue_wait_s=0.0, graceful_steal=False):
            acquire_calls.append((queue_wait_s, graceful_steal))
            return real_acquire(
                queue_wait_s=queue_wait_s,
                graceful_steal=graceful_steal,
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
        self.assertEqual(acquire_calls, [(0.0, True)])
        self.assertNotIn("Yielding lock", self.log_text())
        self.assertIn("voxtype: waiting (state=recording)", self.log_text())
        self.assertIn("voxtype: idle after", self.log_text())
        holder.wait(timeout=2)

    @LINUX_PROCS
    def test_idle_line_waits_behind_a_fresh_holder(self):
        """A fresh verified holder is not stolen. The shared budget drops the line."""
        self.write_state("idle")
        holder, _release = self.spawn_holder("sibling-session")
        seen, spy = self._guarded_kills([holder.pid])
        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.6), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            spoken = self._run_main("incoming-session", "Already idle line.")
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.55, self.log_text())
        self.assertLess(elapsed, 1.3, self.log_text())
        self.assertEqual(spoken, [])
        self.assertNotIn("Yielding lock", self.log_text())
        self.assertNotIn("voxtype: waiting", self.log_text())
        self.assertIn("queue: waiting for the line ahead to finish", self.log_text())
        self.assertIn("queue: cap reached after 0.6s, line dropped", self.log_text())
        self._assert_no_fatal(seen, holder.pid)
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
        """A wedged same-session holder is stopped, then the lock is retried."""
        sid = "same-session"
        VOICE_HOOK._SESSION_ID = sid
        holder, _release = self.spawn_holder(sid, release_path="-")
        self._age_lock()
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
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 1.0):
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
        self._age_lock()
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
        self._age_lock()
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

                def spy_acquire(queue_wait_s=0.0, graceful_steal=False):
                    calls.append((queue_wait_s, graceful_steal))
                    return real_acquire(
                        queue_wait_s=queue_wait_s,
                        graceful_steal=graceful_steal,
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
                self.assertEqual(calls, [(0.0, expect)])

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
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 1.0), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
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
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 1.0), \
                mock.patch.object(VOICE_HOOK, "_status_real_uid", side_effect=other_uid), \
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
                # The graceful path draws on the one shared budget. The old
                # dead-holder poll was a fresh LOCK_STEAL_RETRY_S each call.
                VOICE_HOOK._reset_wait_budget()
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
                with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 1.0), \
                        mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
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

    @unittest.skipIf(os.geteuid() == 0, "root can open a mode 000 file")
    def test_unreadable_lock_is_refused_without_sleep(self):
        """Mode 000 is permission denied. No sleep, no write, no signal."""
        sentinel = "do-not-touch\n"
        Path(self.lock_path).write_text(sentinel)
        os.chmod(self.lock_path, 0o000)
        self.addCleanup(os.chmod, self.lock_path, 0o600)
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                self._clear_log()
                seen, spy = self._guarded_kills([])
                with mock.patch.object(VOICE_HOOK.time, "sleep") as sleep, \
                        mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    started = time.monotonic()
                    lock = VOICE_HOOK.acquire_lock(graceful_steal=graceful)
                    elapsed = time.monotonic() - started
                sleep.assert_not_called()
                self.assertIsNone(lock)
                self.assertLess(elapsed, 1.0, self.log_text())
                self.assertEqual(seen, [])
                self.assertEqual(self.log_text().count("lock refused:"), 1)
                self.assertIn("lock refused: permission denied", self.log_text())
        os.chmod(self.lock_path, 0o600)
        self.assertEqual(Path(self.lock_path).read_text(), sentinel)

    def test_eperm_on_lock_open_is_refused_without_sleep(self):
        """EPERM from os.open on the lock path is the same refusal."""
        real_open = VOICE_HOOK.os.open

        def spy_open(path, flags, *args, **kwargs):
            if os.fspath(path) == VOICE_HOOK.LOCKFILE_PATH:
                raise PermissionError(errno.EPERM, "Operation not permitted", path)
            return real_open(path, flags, *args, **kwargs)

        sentinel = Path(self.lock_path).read_text()
        for graceful in (False, True):
            with self.subTest(graceful=graceful):
                self._clear_log()
                seen, spy = self._guarded_kills([])
                with mock.patch.object(VOICE_HOOK.time, "sleep") as sleep, \
                        mock.patch.object(VOICE_HOOK.os, "open", side_effect=spy_open), \
                        mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    started = time.monotonic()
                    lock = VOICE_HOOK.acquire_lock(graceful_steal=graceful)
                    elapsed = time.monotonic() - started
                sleep.assert_not_called()
                self.assertIsNone(lock)
                self.assertLess(elapsed, 1.0, self.log_text())
                self.assertEqual(seen, [])
                self.assertEqual(Path(self.lock_path).read_text(), sentinel)
                self.assertEqual(self.log_text().count("lock refused:"), 1)
                self.assertIn("lock refused: permission denied", self.log_text())

    @unittest.skipIf(os.geteuid() == 0, "root can open a mode 000 file")
    def test_main_drops_the_line_when_the_lock_is_unreadable(self):
        """Queue mode with a mode 000 lock speaks nothing and leaves no ticket."""
        self.write_state("idle")
        Path(self.lock_path).write_text("do-not-touch\n")
        os.chmod(self.lock_path, 0o000)
        self.addCleanup(os.chmod, self.lock_path, 0o600)
        played = []

        def on_speak(clean, _cfg, _lang=None):
            played.append(clean)
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.8):
            started = time.monotonic()
            spoken = self._run_main("sess-lock", "Cannot lock.", on_speak=on_speak)
            elapsed = time.monotonic() - started
        os.chmod(self.lock_path, 0o600)
        self.assertLess(elapsed, 1.0, self.log_text())
        self.assertEqual(spoken, [])
        self.assertEqual(played, [])
        self.assertEqual(self.starts(), [])
        self.assertEqual(Path(self.lock_path).read_text(), "do-not-touch\n")
        self.assertEqual(self.log_text().count("lock refused:"), 1)
        self.assertIn("lock refused: permission denied", self.log_text())
        self.assertIn("Could not acquire lock, skipping", self.log_text())
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])
        self.assertNotIn("voxtype: waiting", self.log_text())

    def test_main_drops_the_line_when_the_lock_is_a_socket(self):
        """Queue mode with a socket lock speaks nothing and leaves no ticket."""
        self.write_state("idle")
        os.remove(self.lock_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(self.lock_path)
        played = []

        def on_speak(clean, _cfg, _lang=None):
            played.append(clean)
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.8):
            started = time.monotonic()
            spoken = self._run_main("sess-sock", "Cannot lock.", on_speak=on_speak)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, self.log_text())
        self.assertEqual(spoken, [])
        self.assertEqual(played, [])
        self.assertEqual(self.starts(), [])
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.lock_path).st_mode))
        self.assertEqual(self.log_text().count("lock refused:"), 1)
        self.assertIn("lock refused: not a regular file", self.log_text())
        self.assertIn("Could not acquire lock, skipping", self.log_text())
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])
        self.assertNotIn("voxtype: waiting", self.log_text())
        self.assertNotIn("queue: waiting", self.log_text())

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
        self._remember_player(decoy_pid)
        self.addCleanup(self._kill_pid, decoy_pid)
        comm, _state, ppid = self._proc_stat(decoy_pid)
        self.assertEqual(comm, "paplay")
        self.assertEqual(ppid, holder.pid)
        argv0 = Path(f"/proc/{decoy_pid}/cmdline").read_bytes().split(b"\x00", 1)[0]
        self.assertNotEqual(os.path.basename(argv0), b"paplay")
        VOICE_HOOK._SESSION_ID = sid
        self._age_lock()
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
        self._age_lock()
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
        self._age_lock()
        calls = {"n": 0}
        real_uid = VOICE_HOOK._status_real_uid

        def flipping(pid):
            if int(pid) != holder.pid:
                return real_uid(pid)
            calls["n"] += 1
            # The queue take verifies once, then again immediately before
            # SIGTERM. Those two must pass. The SIGKILL check inside
            # _stop_players is the one that must fail.
            if calls["n"] <= 2:
                return real_uid(pid)
            return os.getuid() + 1

        seen, spy = self._guarded_kills([holder.pid])
        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.0), \
                mock.patch.object(VOICE_HOOK, "_status_real_uid", side_effect=flipping), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertIn((holder.pid, signal.SIGTERM), seen)
        self.assertNotIn((holder.pid, 9), seen)
        self.assertIsNone(holder.poll())
        self.assertGreaterEqual(calls["n"], 3)
        self.assertGreaterEqual(elapsed, 1.3)
        self.assertLess(elapsed, 2.5)

    def _fatal_signals(self, seen, pid):
        fatal = (signal.SIGTERM, signal.SIGKILL)
        return [item for item in seen if item[0] == pid and item[1] in fatal]

    def _spawn_sleep(self):
        child = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_proc, child)
        return child

    @LINUX_PROCS
    def test_stale_sigterm_rechecks_holder_and_sends_nothing(self):
        """A holder that fails the second check is not signalled at all."""
        child = self._spawn_sleep()
        sid = "incoming-session"
        holder, _release = self.spawn_holder(
            sid, pid_text=str(child.pid), release_path="-",
        )
        self._age_lock()
        calls = {"n": 0}

        def flipping(_pid):
            calls["n"] += 1
            return calls["n"] == 1

        seen = []
        real_kill = os.kill

        def spy(pid, sig):
            seen.append((int(pid), int(sig)))
            return real_kill(pid, sig)

        VOICE_HOOK._SESSION_ID = sid
        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.5), \
                mock.patch.object(VOICE_HOOK, "_is_our_hook", side_effect=flipping), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(self._fatal_signals(seen, child.pid), [])
        self.assertEqual(self._fatal_signals(seen, holder.pid), [])
        self.assertIsNone(child.poll())
        self.assertIsNone(holder.poll())
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 1.5)

    @LINUX_PROCS
    def test_nongraceful_sigkill_rechecks_holder_and_sends_nothing(self):
        """The non-queue SIGKILL also waits for a second verification."""
        child = self._spawn_sleep()
        sid = "same-session"
        holder, _release = self.spawn_holder(
            sid, pid_text=str(child.pid), release_path="-",
        )
        calls = {"n": 0}

        def flipping(_pid):
            calls["n"] += 1
            return calls["n"] == 1

        seen = []
        real_kill = os.kill

        def spy(pid, sig):
            seen.append((int(pid), int(sig)))
            return real_kill(pid, sig)

        VOICE_HOOK._SESSION_ID = sid
        started = time.monotonic()
        with mock.patch.object(VOICE_HOOK, "_is_our_hook", side_effect=flipping), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            lock = VOICE_HOOK.acquire_lock()
        elapsed = time.monotonic() - started
        self.assertIsNone(lock)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertEqual(self._fatal_signals(seen, child.pid), [])
        self.assertEqual(self._fatal_signals(seen, holder.pid), [])
        self.assertIsNone(child.poll())
        self.assertIsNone(holder.poll())
        self.assertLess(elapsed, 0.5)

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

    def _read_text(self, path):
        try:
            return Path(path).read_text()
        except FileNotFoundError:
            return ""

    def _queue_names(self):
        if os.path.islink(self.queue_dir) or not os.path.isdir(self.queue_dir):
            return []
        return sorted(os.listdir(self.queue_dir))

    def _wipe_queue(self):
        if os.path.islink(self.queue_dir):
            os.unlink(self.queue_dir)
        elif os.path.isdir(self.queue_dir):
            shutil.rmtree(self.queue_dir)

    def _write_ticket(self, name):
        os.makedirs(self.queue_dir, 0o700, exist_ok=True)
        path = os.path.join(self.queue_dir, name)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return path

    def _spawn_ticket_peer(self, label):
        """Child whose cmdline contains voice-stop-hook. Returns the process."""
        ready = os.path.join(self.root, f"{label}-ready")
        peer, err_path = self._spawn_script(
            f"fake-voice-stop-hook-{label}.py", PEER_SLEEP, [ready],
        )
        self._wait_until(
            lambda: os.path.exists(ready) or peer.poll() is not None,
            3.0,
            f"{label} did not start " + self._holder_debug(err_path),
        )
        self.assertIsNone(peer.poll(), self._holder_debug(err_path))
        self.assertIn(
            b"voice-stop-hook",
            Path(f"/proc/{peer.pid}/cmdline").read_bytes(),
        )
        return peer

    def _timed_play_events(self):
        events = []
        for line in self._play_lines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] in ("start", "end"):
                events.append((parts[0], int(parts[1]), float(parts[2])))
        return events

    def _spawn_queue_speaker(self, line, session_id, spoken_path, sig_path=None):
        """Child hook. Temp lock, log, state, and queue dir come from the env."""
        log_path = os.path.join(self.root, f"speaker-{time.monotonic_ns()}.log")
        os.environ["QUEUE_SPOKEN_LOG"] = spoken_path
        os.environ["CLAUDE_SESSION_TYPE"] = "main"
        if sig_path:
            os.environ["QUEUE_SIG_LOG"] = sig_path
        else:
            os.environ.pop("QUEUE_SIG_LOG", None)
        proc, err_path = self._spawn_script(
            "fake-voice-stop-hook-queue.py",
            QUEUE_SPEAKER,
            [str(HOOK_PATH), line, session_id, self.lock_path, log_path],
        )
        return proc, err_path, log_path

    def _run_main_driven(self, session_id, text, drive, extra=None, cfg=None,
                         on_speak=None):
        """Run main() on this thread. `drive` runs beside it on a helper."""
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

    def _assert_two_hooks_play_in_order(self, first_session, second_session):
        """Both lines play, older first, no overlap, and the holder is not signalled."""
        self._install_paplay(TIMED_PAPLAY)
        os.environ["FAKE_PLAY_S"] = "0.6"
        self.write_state("idle")
        spoken_path = os.path.join(self.root, "queue-spoken.log")
        sig_path = os.path.join(self.root, "queue-sig.log")
        first_line = "First line plays."
        second_line = "Second line plays."
        first, first_err, first_log = self._spawn_queue_speaker(
            first_line, first_session, spoken_path, sig_path=sig_path,
        )
        self._wait_until(
            lambda: self._timed_play_events() or first.poll() is not None,
            5.0,
            "first hook did not start " + self._holder_debug(first_err),
        )
        self.assertIsNone(first.poll(), self._holder_debug(first_err))
        second, second_err, second_log = self._spawn_queue_speaker(
            second_line, second_session, spoken_path,
        )
        self.assertEqual(
            first.wait(timeout=8), 0,
            self._holder_debug(first_err) + self._read_text(first_log),
        )
        self.assertEqual(
            second.wait(timeout=8), 0,
            self._holder_debug(second_err) + self._read_text(second_log),
        )
        events = self._timed_play_events()
        starts = [item for item in events if item[0] == "start"]
        ends = [item for item in events if item[0] == "end"]
        self.assertEqual(len(starts), 2, events)
        self.assertEqual(len(ends), 2, events)
        by_pid = {}
        for kind, pid, ts in events:
            by_pid.setdefault(pid, {})[kind] = ts
        ordered = sorted(starts, key=lambda item: item[2])
        older_pid, newer_pid = ordered[0][1], ordered[1][1]
        self.assertIn("end", by_pid[older_pid], events)
        self.assertLess(
            by_pid[older_pid]["end"], by_pid[newer_pid]["start"], events,
        )
        spoken = [
            line for line in self._read_text(spoken_path).splitlines() if line.strip()
        ]
        self.assertEqual(spoken, [first_line, second_line])
        self.assertEqual(self._read_text(sig_path), "", self._read_text(first_log))
        self.assertIn(
            "queue: waiting for the line ahead to finish",
            self._read_text(second_log),
        )
        self.assertNotIn("line dropped", self._read_text(first_log))
        self.assertNotIn("line dropped", self._read_text(second_log))
        self.assertEqual(self._queue_names(), [])

    @LINUX_PROCS
    def test_queue_order_across_two_sessions(self):
        """Session s2 waits. Both play. s1 ends before s2 starts. s1 is not signalled."""
        self._assert_two_hooks_play_in_order("session-one", "session-two")

    @LINUX_PROCS
    def test_queue_order_within_one_session(self):
        """Same session: both lines play, older first. A fresh holder is not stolen."""
        self._assert_two_hooks_play_in_order("session-same", "session-same")

    @LINUX_PROCS
    def test_stale_holder_stopped_before_the_new_line(self):
        """A wedged verified holder is stopped, and its player is gone first."""
        self._install_long_paplay()
        self.write_state("idle")
        sid = "same-session"
        ready = os.path.join(self.root, "stale-ready")
        holder, holder_err = self._spawn_script(
            "fake-voice-stop-hook-ignoring.py",
            IGNORING_HOLDER,
            [self.lock_path, sid, ready],
        )
        old_pid = self._await_playing_holder(holder, ready, holder_err)
        self._age_lock()
        self._install_paplay(TIMED_PAPLAY)
        os.environ["FAKE_PLAY_S"] = "0.25"
        spoken = os.path.join(self.root, "stale-spoken.log")
        speaker, speaker_err, speaker_log = self._spawn_queue_speaker(
            "After the wedge.", sid, spoken,
        )
        died_at = None
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if died_at is None and self._gone_or_zombie(old_pid):
                died_at = time.time()
            if speaker.poll() is not None and died_at is not None:
                break
            time.sleep(0.01)
        detail = self._holder_debug(speaker_err) + self._read_text(speaker_log)
        self.assertIsNotNone(died_at, detail)
        self.assertEqual(speaker.wait(timeout=2), 0, detail)
        self.assertEqual(holder.wait(timeout=2), -signal.SIGKILL)
        self.assertIn("After the wedge.", self._read_text(spoken))
        events = self._timed_play_events()
        new_starts = [
            item for item in events if item[0] == "start" and item[1] != old_pid
        ]
        self.assertTrue(new_starts, self._play_lines())
        new_start = min(item[2] for item in new_starts)
        self.assertGreaterEqual(new_start + 0.05, died_at, self._play_lines())
        self.assertFalse(any(line == "end" for line in self._play_lines()))
        self.assertTrue(any(line.startswith("end ") for line in self._play_lines()))

    @LINUX_PROCS
    def test_slow_term_paplay_is_killed_before_the_new_line(self):
        """A paplay that ignores SIGTERM is SIGKILLed before the new line starts."""
        self._install_paplay(SLOW_TERM_PAPLAY)
        self.write_state("idle")
        sid = "same-session"
        ready = os.path.join(self.root, "slow-term-ready")
        holder, holder_err = self._spawn_script(
            "fake-voice-stop-hook-ignoring.py",
            IGNORING_HOLDER,
            [self.lock_path, sid, ready],
        )
        old_pid = self._await_playing_holder(holder, ready, holder_err)
        self._age_lock()
        self._install_paplay(TIMED_PAPLAY)
        os.environ["FAKE_PLAY_S"] = "0.25"
        spoken = os.path.join(self.root, "slow-term-spoken.log")
        speaker, speaker_err, speaker_log = self._spawn_queue_speaker(
            "After the slow term.", sid, spoken,
        )
        died_at = None
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if died_at is None and self._gone_or_zombie(old_pid):
                died_at = time.time()
            if speaker.poll() is not None and died_at is not None:
                break
            time.sleep(0.01)
        detail = self._holder_debug(speaker_err) + self._read_text(speaker_log)
        self.assertIsNotNone(died_at, detail)
        self.assertEqual(speaker.wait(timeout=2), 0, detail)
        self.assertEqual(holder.wait(timeout=2), -signal.SIGKILL)
        self.assertIn("After the slow term.", self._read_text(spoken))
        events = self._timed_play_events()
        new_starts = [
            item for item in events if item[0] == "start" and item[1] != old_pid
        ]
        self.assertEqual(len(new_starts), 1, self._play_lines())
        new_start = new_starts[0][2]
        self.assertGreaterEqual(new_start + 0.05, died_at, self._play_lines())
        new_pid = new_starts[0][1]
        new_ends = [
            item for item in events if item[0] == "end" and item[1] == new_pid
        ]
        self.assertEqual(len(new_ends), 1, self._play_lines())
        self.assertLess(new_start, new_ends[0][2])
        old_ends = [
            item for item in events if item[0] == "end" and item[1] == old_pid
        ]
        self.assertEqual(old_ends, [])
        terms = [
            line for line in self._play_lines() if line.startswith("termrecv ")
        ]
        self.assertEqual(len(terms), 1, self._play_lines())
        _kind, term_pid, term_at = terms[0].split()
        self.assertEqual(int(term_pid), old_pid)
        self.assertLess(float(term_at), new_start)
        self.assertTrue(self._gone_or_zombie(old_pid))
        self.assertNotIn("stale player would not stop", self._read_text(speaker_log))

    @LINUX_PROCS
    def test_survivor_paplay_drops_the_line_and_frees_the_lock(self):
        """A paplay that survives SIGKILL drops the line and releases the lock."""
        self._install_paplay(SLOW_TERM_PAPLAY)
        sid = "same-session"
        ready = os.path.join(self.root, "survivor-ready")
        holder, holder_err = self._spawn_script(
            "fake-voice-stop-hook-survivor.py",
            TERM_EXIT_PAPLAY_HOLDER,
            [self.lock_path, sid, ready],
        )
        old_pid = self._await_playing_holder(holder, ready, holder_err)
        self.addCleanup(self._kill_pid, old_pid)
        self._age_lock()
        VOICE_HOOK._SESSION_ID = sid
        seen, spy = self._guarded_kills([old_pid])

        def never_gone(_pid):
            return False

        self._clear_log()
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 2.0), \
                mock.patch.object(VOICE_HOOK, "_gone_or_zombie", side_effect=never_gone), \
                mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
            started = time.monotonic()
            lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
            elapsed = time.monotonic() - started
        self.assertIsNone(lock, self._holder_debug(holder_err))
        self.assertIsNone(VOICE_HOOK._lock_fd)
        self.assertGreaterEqual(elapsed, 0.7, self.log_text())
        self.assertLess(elapsed, 3.0, self.log_text())
        self.assertEqual(
            self.log_text().count(
                "lock: stale player would not stop, line dropped",
            ),
            1,
            self.log_text(),
        )
        self.assertNotIn("queue:", self.log_text())
        self.assertNotIn("lock refused:", self.log_text())
        fatal = self._fatal_signals(seen, old_pid)
        self.assertIn((old_pid, signal.SIGTERM), fatal)
        self.assertIn((old_pid, signal.SIGKILL), fatal)
        allowed = {old_pid, holder.pid}
        unexpected = [
            item for item in seen
            if item[1] in (signal.SIGTERM, signal.SIGKILL, 9)
            and item[0] not in allowed
        ]
        self.assertEqual(unexpected, [])
        self.assertTrue(self.pid_running(old_pid))
        probe = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fail(f"lock still held after the drop: {exc}")
        finally:
            try:
                fcntl.flock(probe, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(probe)
        self.assertEqual(holder.wait(timeout=2), 0, self._holder_debug(holder_err))

    @LINUX_PROCS
    def test_dead_holder_is_taken_without_a_signal(self):
        """A dead recorded pid is not signalled. The free lock is taken at once.

        A live pid that fails _is_our_hook is covered by
        test_unverified_cmdline_is_never_signalled.
        """
        dead = self.dead_pid()
        Path(self.lock_path).write_text(f"{dead}\nother-session\n")
        seen, spy = self._guarded_kills([dead])
        VOICE_HOOK._SESSION_ID = "incoming"
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                started = time.monotonic()
                lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
                elapsed = time.monotonic() - started
            self.assertIsNotNone(lock)
            self.assertLess(elapsed, 0.4)
            self._assert_no_fatal(seen, dead)
            self.assertNotIn(dead, [pid for pid, _sig in seen])
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    @LINUX_PROCS
    def test_held_line_survives_prompt_sigterm(self):
        """Enter's SIGTERM does not drop a line held while VoxType is recording."""
        self.write_state("recording")
        os.environ["FAKE_PLAY_S"] = "0.2"
        spoken = os.path.join(self.root, "held-spoken.log")
        proc, err_path, log_path = self._spawn_queue_speaker(
            "Held through enter.", "sess-held", spoken,
        )
        self._wait_until(
            lambda: (
                "voxtype: waiting" in self._read_text(log_path)
                or proc.poll() is not None
            ),
            4.0,
            "held wait did not start " + self._holder_debug(err_path),
        )
        self.assertIsNone(proc.poll(), self._holder_debug(err_path))
        proc.send_signal(signal.SIGTERM)
        self._wait_until(
            lambda: (
                "queue mode: SIGTERM ignored, line will finish"
                in self._read_text(log_path)
            ),
            2.0,
            self._read_text(log_path),
        )
        self.assertIsNone(proc.poll(), self._read_text(log_path))
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        self.assertEqual(
            self._read_text(log_path).count(
                "queue mode: SIGTERM ignored, line will finish",
            ),
            1,
        )
        self.assertEqual(self.starts(), [])
        self.write_state("idle")
        self.assertEqual(
            proc.wait(timeout=5), 0, self._holder_debug(err_path),
        )
        self.assertIn("Held through enter.", self._read_text(spoken))
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(len(self.ends()), 1)
        self.assertNotIn("line dropped", self._read_text(log_path))

    def test_shared_cap_drops_the_replay_after_a_prelock_wait(self):
        """A pre-lock wait and the later replay wait share one budget."""
        self.write_state("recording")
        os.environ["FAKE_PLAY_S"] = "2"
        cap = 1.2

        def on_speak(_clean, _cfg, _lang=None):
            VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            time.sleep(0.7)
            self.write_state("idle")
            self._wait_until(
                lambda: self.starts() or self._main_done.is_set(),
                2.0,
                self.log_text(),
            )
            self.write_state("recording")

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", cap):
            started = time.monotonic()
            spoken = self._run_main_driven(
                "budget-session", "Budget line.", drive, on_speak=on_speak,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(spoken, ["Budget line."])
        self.assertEqual(len(self.starts()), 1, self.log_text())
        self.assertEqual(self.ends(), [])
        text = self.log_text()
        self.assertIn(
            "voxtype: dictation started during playback, stopped; will replay (1)",
            text,
        )
        self.assertEqual(text.count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 1.2s total wait while still busy, "
            "line dropped",
            text,
        )
        self.assertGreaterEqual(elapsed, 1.05, text)
        self.assertLess(elapsed, 1.8, text)

    @LINUX_PROCS
    def test_shared_cap_across_prelock_and_queue(self):
        """Time queued behind a holder draws on the same budget as the pre-lock wait."""
        self.write_state("recording")
        holder, _release = self.spawn_holder("other-session", release_path="-")
        cap = 1.2

        def drive():
            self._wait_until(
                lambda: "voxtype: waiting" in self.log_text(),
                2.0,
                self.log_text(),
            )
            time.sleep(0.7)
            self.write_state("idle")

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", cap):
            started = time.monotonic()
            spoken = self._run_main_driven(
                "incoming-session", "Queued on the same budget.", drive,
            )
            elapsed = time.monotonic() - started
        self.assertEqual(spoken, [])
        self.assertEqual(self.starts(), [])
        text = self.log_text()
        self.assertIn("queue: cap reached after 1.2s, line dropped", text)
        self.assertNotIn("voxtype: cap reached after", text)
        self.assertNotIn("Yielding lock", text)
        self.assertGreaterEqual(elapsed, 1.05, text)
        self.assertLess(elapsed, 1.65, text)
        self.assertIsNone(holder.poll())
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])

    def test_cap_while_recording_never_plays(self):
        """The shared budget runs out while VoxType is still busy. Nothing plays."""
        self.write_state("recording")
        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.45):
            started = time.monotonic()
            spoken = self._run_main("cap-session", "Must not be spoken.")
            elapsed = time.monotonic() - started
        self.assertEqual(spoken, [])
        self.assertEqual(self.starts(), [])
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 1.2, self.log_text())
        text = self.log_text()
        self.assertEqual(text.count("voxtype: cap reached after"), 1)
        self.assertIn(
            "voxtype: cap reached after 0.45s total wait while still busy, "
            "line dropped",
            text,
        )
        self.assertIsNone(VOICE_HOOK._lock_fd)
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])
        self.assertNotIn(str(os.getpid()), Path(self.lock_path).read_text())

    def test_direct_gate_cap_drops_without_a_player(self):
        """A direct gated call draws on the shared cap, then drops."""
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
            "voxtype: cap reached after 0.4s total wait while still busy, line dropped",
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
            "voxtype: cap reached after 0s total wait while still busy, line dropped",
            self.log_text(),
        )
        self.assertEqual(self.log_text().count("voxtype: cap reached after"), 1)

    def test_absent_state_does_not_touch_queue_registry(self):
        """No state file: main's path, no queue directory, SIGTERM still exits."""
        self.assertFalse(os.path.exists(self.state_path))
        with self.forbid_arrival_io():
            spoken = self._run_main("day-session", "No voxtype here.")
            with mock.patch.object(VOICE_HOOK.subprocess, "run") as run:
                VOICE_HOOK.play_raw_pcm(self.pcm, 24000, 1)
            with mock.patch.object(VOICE_HOOK.os, "_exit") as exit_:
                VOICE_HOOK.handle_sigterm(signal.SIGTERM, None)
        self.assertEqual(spoken, ["No voxtype here."])
        run.assert_called_once()
        exit_.assert_called_once_with(0)
        self.assertFalse(os.path.exists(self.queue_dir))
        self.assertNotIn("voxtype:", self.log_text())
        self.assertNotIn("queue:", self.log_text())

    def test_ticket_removed_when_the_line_is_spoken(self):
        self.write_state("idle")
        seen = {}

        def on_speak(_clean, _cfg, _lang=None):
            seen["names"] = self._queue_names()
            seen["path"] = VOICE_HOOK._ticket_path

        spoken = self._run_main("sess-ticket", "Short line.", on_speak=on_speak)
        self.assertEqual(spoken, ["Short line."])
        self.assertTrue(seen["names"])
        self.assertTrue(str(seen["path"]).endswith(f"-{os.getpid()}"))
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])

    def test_ticket_is_registered_before_stdin_is_parsed(self):
        """The ticket exists before json.loads and before config is loaded."""
        self.write_state("idle")
        seen = {}
        real_loads = VOICE_HOOK.json.loads

        def spy(raw):
            seen["path"] = VOICE_HOOK._ticket_path
            seen["names"] = (
                os.listdir(self.queue_dir) if os.path.isdir(self.queue_dir) else []
            )
            return real_loads(raw)

        with mock.patch.object(VOICE_HOOK.json, "loads", side_effect=spy):
            spoken = self._run_main("sess-early", "Hello there.")
        self.assertEqual(spoken, ["Hello there."])
        self.assertIsNotNone(seen["path"])
        name = os.path.basename(seen["path"])
        self.assertRegex(name, r"^\d{20}-%d$" % os.getpid())
        self.assertIn(name, seen["names"])
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])

    def test_early_returns_leave_no_ticket(self):
        """Every early return drops the ticket that the state file registered."""
        self.write_state("idle")

        def run(payload, cfg=None, session_type="main"):
            if cfg is None:
                cfg = self._cfg()
            previous = signal.getsignal(signal.SIGTERM)
            try:
                with mock.patch.dict(
                    os.environ, {"CLAUDE_SESSION_TYPE": session_type},
                ), mock.patch.object(
                    VOICE_HOOK, "load_config", return_value=cfg,
                ), mock.patch.object(
                    VOICE_HOOK, "speak",
                    side_effect=AssertionError("spoke"),
                ), mock.patch.object(
                    VOICE_HOOK, "_record_missing_voice_block",
                ), mock.patch.object(
                    VOICE_HOOK.sys, "stdin", io.StringIO(payload),
                ):
                    VOICE_HOOK.main()
            finally:
                signal.signal(signal.SIGTERM, previous)

        voice = json.dumps({
            "last_assistant_message": '<voice lang="en">Hello there.</voice>',
            "session_id": "sess-early-return",
        })
        prose = json.dumps({
            "last_assistant_message": "plain prose with no voice block",
            "session_id": "sess-early-return",
        })

        run(prose)
        self.assertTrue(os.path.isdir(self.queue_dir))
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])

        self._wipe_queue()
        run(voice, cfg=self._cfg(tts_enabled=False))
        self.assertTrue(os.path.isdir(self.queue_dir))
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])
        self.assertIn("TTS disabled via config", self.log_text())

        self._wipe_queue()
        self._clear_log()
        run(voice, session_type="headless")
        self.assertTrue(os.path.isdir(self.queue_dir))
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])
        self.assertIn("Skipped TTS: session_type=headless", self.log_text())

    def test_voxtype_wait_false_clears_ticket_and_does_not_wait(self):
        """A false switch drops the early ticket before the lock. No voxtype wait."""
        self.write_state("recording")
        seen = {}
        real_acquire = VOICE_HOOK.acquire_lock

        def spy_acquire(queue_wait_s=0.0, graceful_steal=False):
            seen["path"] = VOICE_HOOK._ticket_path
            seen["names"] = self._queue_names()
            seen["queue_mode"] = VOICE_HOOK._queue_mode
            seen["graceful"] = graceful_steal
            return real_acquire(
                queue_wait_s=queue_wait_s, graceful_steal=graceful_steal,
            )

        with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.4), \
                mock.patch.object(VOICE_HOOK, "acquire_lock", side_effect=spy_acquire):
            started = time.monotonic()
            spoken = self._run_main(
                "sess-switch",
                "Still spoken.",
                cfg=self._cfg(voxtype_wait=False),
            )
            elapsed = time.monotonic() - started
        self.assertEqual(spoken, ["Still spoken."])
        self.assertLess(elapsed, 0.3, self.log_text())
        self.assertIsNone(seen["path"])
        self.assertEqual(seen["names"], [])
        self.assertFalse(seen["queue_mode"])
        self.assertFalse(seen["graceful"])
        self.assertTrue(os.path.isdir(self.queue_dir))
        self.assertIsNone(VOICE_HOOK._ticket_path)
        self.assertEqual(self._queue_names(), [])
        self.assertNotIn("voxtype:", self.log_text())

    @LINUX_PROCS
    def test_slow_older_hook_is_ahead_before_config(self):
        """A newer hook waits while an older hook is still before config."""
        self._install_paplay(TIMED_PAPLAY)
        os.environ["FAKE_PLAY_S"] = "0.4"
        self.write_state("idle")
        spoken_path = os.path.join(self.root, "slow-spoken.log")
        hold = os.path.join(self.root, "hold-release")
        flag = os.path.join(self.root, "held-flag")
        os.environ["QUEUE_HOLD_CONFIG"] = hold
        os.environ["QUEUE_HELD_FLAG"] = flag
        first_line = "Older line."
        second_line = "Newer line."
        first, first_err, first_log = self._spawn_queue_speaker(
            first_line, "session-old", spoken_path,
        )
        self._wait_until(
            lambda: os.path.exists(flag) or first.poll() is not None,
            4.0,
            "older hook did not reach config " + self._holder_debug(first_err),
        )
        self.assertIsNone(first.poll(), self._holder_debug(first_err))
        held_names = self._queue_names()
        self.assertEqual(len(held_names), 1, held_names)
        self.assertTrue(held_names[0].endswith(f"-{first.pid}"), held_names)

        os.environ.pop("QUEUE_HOLD_CONFIG", None)
        os.environ.pop("QUEUE_HELD_FLAG", None)
        second, second_err, second_log = self._spawn_queue_speaker(
            second_line, "session-new", spoken_path,
        )
        try:
            self._wait_until(
                lambda: (
                    "queue: waiting for the line ahead to finish"
                    in self._read_text(second_log)
                    or second.poll() is not None
                ),
                4.0,
                "newer hook did not queue " + self._holder_debug(second_err),
            )
            self.assertIsNone(second.poll(), self._read_text(second_log))
            self.assertNotIn(second_line, self._read_text(spoken_path))
            self.assertNotIn(first_line, self._read_text(spoken_path))
            self.assertEqual(self.starts(), [])
            self.assertNotIn(str(second.pid), Path(self.lock_path).read_text())
            self.assertFalse(self._lock_is_held())
            names = self._queue_names()
            self.assertTrue(
                any(name.endswith(f"-{first.pid}") for name in names), names,
            )
            self.assertTrue(
                any(name.endswith(f"-{second.pid}") for name in names), names,
            )
        finally:
            Path(hold).write_text("go")
        self.assertEqual(
            first.wait(timeout=8), 0,
            self._holder_debug(first_err) + self._read_text(first_log),
        )
        self.assertEqual(
            second.wait(timeout=8), 0,
            self._holder_debug(second_err) + self._read_text(second_log),
        )
        spoken = [
            line for line in self._read_text(spoken_path).splitlines() if line.strip()
        ]
        self.assertEqual(spoken, [first_line, second_line])
        events = self._timed_play_events()
        starts = [item for item in events if item[0] == "start"]
        ends = [item for item in events if item[0] == "end"]
        self.assertEqual(len(starts), 2, events)
        self.assertEqual(len(ends), 2, events)
        by_pid = {}
        for kind, pid, ts in events:
            by_pid.setdefault(pid, {})[kind] = ts
        ordered = sorted(starts, key=lambda item: item[2])
        older_pid, newer_pid = ordered[0][1], ordered[1][1]
        self.assertLess(
            by_pid[older_pid]["end"], by_pid[newer_pid]["start"], events,
        )
        self.assertIn(
            "queue: waiting for the line ahead to finish",
            self._read_text(second_log),
        )
        self.assertEqual(self._queue_names(), [])

    @LINUX_PROCS
    def test_queue_registry_unsafe_orders_by_lock_only(self):
        """An unsafe directory logs once and does not order. No runtime dir stays off."""
        unsafe = "queue: registry unsafe, ordering by lock only"
        stamp = 1_000_000_000_000_000_000

        self._clear_log()
        real = os.path.join(self.root, "real-queue")
        os.makedirs(real, 0o700)
        link = os.path.join(self.root, "queue-link")
        os.symlink(real, link)
        with mock.patch.dict(os.environ, {"SHELBY_VOICE_QUEUE_DIR": link}):
            result = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)
        self.assertEqual(os.listdir(real), [])

        self._wipe_queue()
        self._clear_log()
        os.makedirs(self.queue_dir, 0o700)
        os.chmod(self.queue_dir, 0o770)
        result = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)
        self.assertEqual(os.listdir(self.queue_dir), [])

        self._wipe_queue()
        self._clear_log()
        os.makedirs(self.queue_dir, 0o700)
        real_lstat = os.lstat

        def foreign_owner(path, *args, **kwargs):
            st = real_lstat(path)
            return mock.Mock(
                st_mode=st.st_mode,
                st_uid=st.st_uid + 1,
                st_mtime=st.st_mtime,
            )

        with mock.patch.object(VOICE_HOOK.os, "lstat", side_effect=foreign_owner):
            result = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)

        self._clear_log()
        calls = []
        real_makedirs = os.makedirs

        def spy_makedirs(path, *args, **kwargs):
            calls.append(os.fspath(path))
            return real_makedirs(path, *args, **kwargs)

        with mock.patch.dict(os.environ):
            os.environ.pop("SHELBY_VOICE_QUEUE_DIR", None)
            os.environ.pop("XDG_RUNTIME_DIR", None)
            self.assertIsNone(VOICE_HOOK._voice_queue_dir())
            with mock.patch.object(VOICE_HOOK.os, "makedirs", side_effect=spy_makedirs):
                result = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNone(result)
        self.assertEqual(calls, [])
        self.assertNotIn(unsafe, self.log_text())

        self._wipe_queue()
        self._clear_log()
        VOICE_HOOK._ticket_path = None
        VOICE_HOOK._ticket_stamp_ns = None
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
        os.makedirs(self.queue_dir, 0o700)
        os.chmod(self.queue_dir, 0o770)
        planted = self._write_ticket(f"{stamp:020d}-{peer.pid}")
        result = VOICE_HOOK._register_ticket(stamp + 5)
        self.assertIsNone(result)
        self.assertEqual(self.log_text().count(unsafe), 1)
        self.assertTrue(os.path.exists(planted))
        lock = None
        try:
            with mock.patch.object(VOICE_HOOK, "VOXTYPE_MAX_WAIT_S", 0.8):
                VOICE_HOOK._reset_wait_budget()
                started = time.monotonic()
                lock = VOICE_HOOK.acquire_lock(graceful_steal=True)
                elapsed = time.monotonic() - started
            self.assertIsNotNone(lock)
            self.assertLess(elapsed, 0.35, self.log_text())
            self.assertIsNone(peer.poll())
            self.assertTrue(os.path.exists(planted))
        finally:
            if lock is not None:
                VOICE_HOOK.release_lock(lock)

    @LINUX_PROCS
    def test_queue_tickets_unlink_dead_and_skip_unverified_or_stale(self):
        """Dead tickets go. A live unverified pid and a stale ticket stay, unsignalled."""
        stamp = 5_000_000_000_000_000_000
        path = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNotNone(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.islink(self.queue_dir))
        self.assertEqual(os.lstat(self.queue_dir).st_mode & 0o022, 0)
        self.assertEqual(os.path.basename(path), f"{stamp:020d}-{os.getpid()}")

        dead = self.dead_pid()
        dead_path = self._write_ticket(f"{stamp - 1:020d}-{dead}")
        self.assertFalse(VOICE_HOOK._older_ticket_ahead())
        self.assertFalse(os.path.exists(dead_path))

        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(self._stop_proc, sleeper)
        foreign = self._write_ticket(f"{stamp - 2:020d}-{sleeper.pid}")
        self.assertNotIn(
            b"voice-stop-hook",
            Path(f"/proc/{sleeper.pid}/cmdline").read_bytes(),
        )
        self.assertFalse(VOICE_HOOK._older_ticket_ahead())
        self.assertTrue(os.path.exists(foreign))
        self.assertIsNone(sleeper.poll())

        ready = os.path.join(self.root, "ticket-peer-ready")
        peer, err_path = self._spawn_script(
            "fake-voice-stop-hook-peer.py", PEER_SLEEP, [ready],
        )
        self._wait_until(
            lambda: os.path.exists(ready) or peer.poll() is not None,
            3.0,
            "peer did not start " + self._holder_debug(err_path),
        )
        self.assertIn(
            b"voice-stop-hook",
            Path(f"/proc/{peer.pid}/cmdline").read_bytes(),
        )
        stale = self._write_ticket(f"{stamp - 3:020d}-{peer.pid}")
        old = time.time() - float(VOICE_HOOK.LOCK_STALE_S) - 5
        os.utime(stale, (old, old))
        self.assertFalse(VOICE_HOOK._older_ticket_ahead())
        self.assertTrue(os.path.exists(stale))
        self.assertIsNone(peer.poll())

        fresh = self._write_ticket(f"{stamp - 4:020d}-{peer.pid}")
        self.assertTrue(VOICE_HOOK._older_ticket_ahead())
        self.assertTrue(os.path.exists(fresh))
        self.assertIsNone(peer.poll())

        junk = os.path.join(self.queue_dir, "not-a-ticket")
        Path(junk).write_text("x")
        link_name = f"{stamp - 5:020d}-{peer.pid}"
        link = os.path.join(self.queue_dir, link_name)
        os.symlink(fresh, link)
        self.assertTrue(VOICE_HOOK._older_ticket_ahead())
        self.assertTrue(os.path.exists(junk))
        self.assertTrue(os.path.islink(link))
        os.unlink(fresh)
        self.assertFalse(VOICE_HOOK._older_ticket_ahead())
        self.assertTrue(os.path.islink(link))
        self.assertIsNone(peer.poll())

    @LINUX_PROCS
    def test_vanished_older_ticket_does_not_end_the_scan(self):
        """One missing ticket must not hide another older live ticket."""
        stamp = 6_000_000_000_000_000_000
        path = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNotNone(path)
        peer_a = self._spawn_ticket_peer("peer-a")
        peer_b = self._spawn_ticket_peer("peer-b")
        path_a = self._write_ticket(f"{stamp - 20:020d}-{peer_a.pid}")
        path_b = self._write_ticket(f"{stamp - 10:020d}-{peer_b.pid}")
        self.assertTrue(VOICE_HOOK._older_ticket_ahead())
        real_lstat = os.lstat

        def lstat_vanish_one(target, *args, **kwargs):
            if os.fspath(target) == path_a:
                raise FileNotFoundError(path_a)
            return real_lstat(target, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK.os, "lstat", side_effect=lstat_vanish_one):
            self.assertTrue(VOICE_HOOK._older_ticket_ahead())
        self.assertIsNone(peer_a.poll())
        self.assertIsNone(peer_b.poll())
        self.assertTrue(os.path.exists(path_a))
        self.assertTrue(os.path.exists(path_b))

    @LINUX_PROCS
    def test_only_vanished_older_ticket_is_not_ahead(self):
        """The only older ticket vanishing is not a reason to wait."""
        stamp = 6_100_000_000_000_000_000
        path = VOICE_HOOK._register_ticket(stamp)
        self.assertIsNotNone(path)
        peer = self._spawn_ticket_peer("only-peer")
        older = self._write_ticket(f"{stamp - 10:020d}-{peer.pid}")
        self.assertTrue(VOICE_HOOK._older_ticket_ahead())
        real_lstat = os.lstat

        def lstat_vanish(target, *args, **kwargs):
            if os.fspath(target) == older:
                raise FileNotFoundError(older)
            return real_lstat(target, *args, **kwargs)

        with mock.patch.object(VOICE_HOOK.os, "lstat", side_effect=lstat_vanish):
            self.assertFalse(VOICE_HOOK._older_ticket_ahead())
        self.assertIsNone(peer.poll())
        self.assertTrue(os.path.exists(older))

    def test_heartbeat_advances_lock_and_ticket_while_waiting_and_playing(self):
        """In-lock wait and gated playback both refresh the lock fd and the ticket."""
        self.write_state("recording")
        ticket = VOICE_HOOK._register_ticket(time.monotonic_ns())
        self.assertIsNotNone(ticket)
        lock = VOICE_HOOK.acquire_lock()
        self.assertIsNotNone(lock)
        try:
            old = time.time() - 50
            os.utime(self.lock_path, (old, old))
            os.utime(ticket, (old, old))
            before_lock = os.stat(self.lock_path).st_mtime_ns
            before_ticket = os.lstat(ticket).st_mtime_ns
            with mock.patch.object(VOICE_HOOK, "VOXTYPE_HEARTBEAT_S", 0.15):
                waited = VOICE_HOOK.wait_for_voxtype_idle(0.7)
                self.assertGreaterEqual(waited, 0.7)
                self.assertLess(waited, 1.4)
                self.assertGreater(os.stat(self.lock_path).st_mtime_ns, before_lock)
                self.assertGreater(os.lstat(ticket).st_mtime_ns, before_ticket)

                self.write_state("idle")
                os.environ["FAKE_PLAY_S"] = "1.0"
                os.utime(self.lock_path, (old, old))
                os.utime(ticket, (old, old))
                before_lock = os.stat(self.lock_path).st_mtime_ns
                before_ticket = os.lstat(ticket).st_mtime_ns
                VOICE_HOOK._play_pcm_voxtype_gated(self.pcm, 24000, 1, timeout=5)
            self.assertGreater(os.stat(self.lock_path).st_mtime_ns, before_lock)
            self.assertGreater(os.lstat(ticket).st_mtime_ns, before_ticket)
            self.assertEqual(len(self.ends()), 1)
        finally:
            if VOICE_HOOK._lock_fd is not None:
                VOICE_HOOK.release_lock(lock)
            VOICE_HOOK._clear_ticket()

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

    def test_socket_lock_is_refused_without_sleep(self):
        """A Unix socket at the lock path is refused at once, with no sleep."""
        os.remove(self.lock_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.bind(self.lock_path)
        cases = (
            {"graceful_steal": True},
            {"queue_wait_s": 0, "graceful_steal": False},
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                self._clear_log()
                seen, spy = self._guarded_kills([])
                with mock.patch.object(VOICE_HOOK.time, "sleep") as sleep, \
                        mock.patch.object(VOICE_HOOK.os, "kill", side_effect=spy):
                    started = time.monotonic()
                    lock = VOICE_HOOK.acquire_lock(**kwargs)
                    elapsed = time.monotonic() - started
                sleep.assert_not_called()
                self.assertIsNone(lock)
                self.assertLess(elapsed, 1.0, self.log_text())
                self.assertEqual(seen, [])
                self.assertTrue(stat.S_ISSOCK(os.lstat(self.lock_path).st_mode))
                self.assertEqual(self.log_text().count("lock refused:"), 1)
                self.assertIn("lock refused: not a regular file", self.log_text())
                self.assertNotIn("queue:", self.log_text())

if __name__ == "__main__":
    unittest.main()
