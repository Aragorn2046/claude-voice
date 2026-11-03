"""CASE presence gate: hold a voice line when the room is empty.

``case-presence`` (a daemon outside this repo) publishes
``$XDG_RUNTIME_DIR/case-presence/state.json``. This module is fail-open:
anything unexpected while deciding returns False so the line is still spoken.
A failure while recording a held line drops that line and still does not speak.

Publisher checks run before any timestamp check. The recorded pid must be
alive, its command line must be the case-presence daemon, and an integer
``pid_start`` must match that process. When those pass, ``hold_voice`` is
honoured at any ``updated_at``, past or future, so clock skew cannot drop
an authenticated hold. A missing ``pid_start`` is legacy state: any age
still holds, but a stamp more than 5s in the future does not. A dead pid,
or a live pid that is not that daemon, fails open (speak). There is no
fresh-window exception that skips the command-line check.

The daemon invocation is argv[0] basename ``case-presence``, or a ``python*``
interpreter (leading flags such as ``-u`` skipped) whose script basename is
``case-presence``. A ``-c`` or ``-m`` option is not a script: separate
(``-c CODE``, ``-m mod``), attached (``-cCODE``, ``-mmod``), or clustered
with plain flags (``-uc``, ``-Im``). The first non-flag argument after the
script is absent or ``daemon``, the only long-running subcommand in the
case-presence CLI. An editor, ``cat``, or ``status``/``on``/``off``/``toggle``
does not match.

When the state object includes an integer ``pid_start`` (``/proc/<pid>/stat``
field 22, starttime in clock ticks), it must equal the live process. A
missing ``pid_start`` stays backward compatible. A mismatch, a non-integer,
or a stat line that cannot be parsed fails open.

``state.json`` is opened relative to a directory fd (``O_DIRECTORY |
O_NOFOLLOW``) with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``. It must be a
regular file owned by the current uid. At most 64 KiB is read. A FIFO
cannot block this open; it fails the regular-file check and does not hold.
A symlink or any other failure does not hold.
"""

import errno
import fcntl
import json
import math
import os
import stat
import time
from collections.abc import Callable


_PRESENCE_DIR = "case-presence"
_STATE_NAME = "state.json"
_HELD_NAME = "held-voice.jsonl"
_FUTURE_SKEW_S = 5.0
_CMDLINE_READ_MAX = 4096
_STAT_READ_MAX = 4096
_STATE_READ_MAX = 65536
_HELD_READ_MAX = 256 * 1024
_HELD_LOCK_WAIT_S = 0.5
_CASE_PRESENCE_BASENAME = b"case-presence"
# case-presence usage: daemon [--dry-run] | status [--json] | on | off | toggle.
# `daemon` is the only subcommand that keeps running. There is no `run`.
_DAEMON_SUBCOMMANDS = frozenset({b"daemon"})
# Short options that take a value and still leave room for a script path.
# ``-W`` and ``-X`` consume the rest of the cluster, or the next argv when
# nothing is attached. ``c`` and ``m`` are not in this set: they select code
# or a module, so the process is not running a script file.
_PY_OPTIONS_WITH_VALUE = frozenset({b"W", b"X"})
_PY_NO_SCRIPT = frozenset({b"c", b"m"})
# _interpreter_flag_width: this argv is -c or -m, so there is no script.
_PY_NOT_A_SCRIPT = 0
_TEXT_MAX = 300
_HELD_DEFAULT = 20
_HELD_MIN = 1
_HELD_MAX = 200


def should_hold(
    cfg: dict,
    text: str,
    *,
    runtime_dir: str | None = None,
    now: float | None = None,
    cmdline_reader: Callable[[int], bytes] | None = None,
    queue: bool = True,
) -> bool:
    """Return True when this line must be queued instead of spoken.

    Hold only when the gate is exactly enabled and the presence state is a
    JSON object with ``hold_voice`` exactly true, from a live process whose
    command line is the case-presence daemon. Publisher identity is checked
    before the timestamp. An integer ``pid_start`` that matches
    ``/proc/<pid>/stat`` field 22 honours ``hold_voice`` at any
    ``updated_at``, including clock skew. A missing ``pid_start`` is legacy
    state: any age holds, except a stamp more than 5s in the future. A dead
    pid or any other command fails open (speak). ``cmdline_reader`` overrides
    the ``/proc`` cmdline read.

    Each call reads the state again. ``queue`` defaults to True and appends
    the line when holding; ``queue=False`` decides without writing. Two
    calls do not share a decision or suppress each other's queue write.
    """
    try:
        if cfg.get("case_presence_gate") is not True:
            return False
        base = _runtime_dir(runtime_dir)
        moment = _coerce_now(now)
        if not _state_says_hold(base, moment, cmdline_reader=cmdline_reader):
            return False
    except Exception:
        return False

    if not queue:
        return True
    try:
        _append_held(base, text, moment, cfg)
    except Exception:
        return True
    return True


def _runtime_dir(runtime_dir: str | None) -> str:
    if runtime_dir is not None:
        return runtime_dir
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return configured
    return f"/run/user/{os.getuid()}"


def _coerce_now(now: float | None) -> float:
    if now is None:
        moment = time.time()
    elif isinstance(now, bool) or not isinstance(now, (int, float)):
        raise TypeError("now must be a real number")
    else:
        moment = float(now)
    if not math.isfinite(moment):
        raise ValueError("now must be finite")
    return moment


def _state_says_hold(
    runtime_dir: str,
    now: float,
    *,
    cmdline_reader: Callable[[int], bytes] | None = None,
) -> bool:
    """True when a live case-presence daemon says hold.

    ``_read_state`` already required a regular file owned by this uid, opened
    ``O_NOFOLLOW|O_NONBLOCK`` under the directory fd. Publisher checks run
    before any timestamp check: the pid is alive, its command line is the
    case-presence daemon, and an integer ``pid_start`` matches that process
    when the key is present. A match honours ``hold_voice`` at any
    ``updated_at``. Legacy state with no ``pid_start`` still rejects a
    timestamp more than 5s in the future, and a non-numeric stamp.
    """
    state = _read_state(runtime_dir)
    if not isinstance(state, dict):
        return False
    pid = state.get("pid")
    if not _process_alive(pid):
        return False
    if not _pid_is_case_presence(pid, cmdline_reader):
        return False
    if not _pid_start_ok(pid, state):
        return False
    # Matching integer pid_start authenticates the publisher. Clock skew,
    # suspend, or a stamp in the future must not drop that hold.
    if "pid_start" in state:
        return state.get("hold_voice") is True
    age = _updated_at_age(state.get("updated_at"), now)
    if age is None or age < -_FUTURE_SKEW_S:
        return False
    return state.get("hold_voice") is True


def _read_state(runtime_dir: str):
    """Load ``state.json`` without following a symlink.

    Returns the parsed JSON value, or None when the file is not a regular
    file owned by the current uid. The open uses ``O_NONBLOCK`` so a FIFO
    cannot stall the caller. I/O and parse errors propagate so the caller
    can fail open.
    """
    presence = os.path.join(runtime_dir, _PRESENCE_DIR)
    dir_fd = os.open(presence, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file_fd = -1
    rejected = False
    raw = b""
    try:
        file_fd = os.open(
            _STATE_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=dir_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            rejected = True
        else:
            raw = _read_capped(file_fd, _STATE_READ_MAX)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(dir_fd)
    if rejected:
        return None
    return json.loads(raw.decode("utf-8"))


def _read_capped(fd: int, limit: int) -> bytes:
    """Read at most ``limit`` bytes. A short read is not padded."""
    chunks = []
    remaining = limit
    while remaining > 0:
        block = os.read(fd, remaining)
        if not block:
            break
        if len(block) > remaining:
            block = block[:remaining]
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _updated_at_age(updated_at: object, now: float) -> float | None:
    if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
        return None
    stamp = float(updated_at)
    if not math.isfinite(stamp):
        return None
    return now - stamp


def _process_alive(pid: object) -> bool:
    """True when pid is a positive int and that process exists.

    PermissionError means the process is alive but not signalable by us.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_is_case_presence(
    pid: object,
    cmdline_reader: Callable[[int], bytes] | None,
) -> bool:
    """True when the process command line is the case-presence daemon.

    A missing, unreadable, or partial argument is not a match.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    reader = _read_proc_cmdline if cmdline_reader is None else cmdline_reader
    try:
        raw = reader(pid)
    except Exception:
        return False
    if not isinstance(raw, (bytes, bytearray)):
        return False
    return _cmdline_names_case_presence(bytes(raw[:_CMDLINE_READ_MAX]))


def _read_proc_cmdline(pid: int) -> bytes:
    """Read at most 4096 bytes of ``/proc/<pid>/cmdline``."""
    fd = os.open(f"/proc/{pid}/cmdline", os.O_RDONLY | os.O_CLOEXEC)
    try:
        chunks = []
        remaining = _CMDLINE_READ_MAX
        while remaining:
            block = os.read(fd, remaining)
            if not block:
                break
            if len(block) > remaining:
                block = block[:remaining]
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _cmdline_args(raw: bytes) -> list[bytes]:
    """Complete NUL-separated argv. A cut-off tail and empty slots are dropped.

    A buffer that fills the read cap and does not end in NUL was cut off
    mid-argument; that partial tail is ignored. The trailing NUL becomes an
    empty segment and is not an argument.
    """
    if len(raw) == _CMDLINE_READ_MAX and not raw.endswith(b"\0"):
        raw = raw.rpartition(b"\0")[0]
    return [arg for arg in raw.split(b"\0") if arg]


def _cmdline_names_case_presence(raw: bytes) -> bool:
    """True when argv is the case-presence daemon, not a mention of the file.

    argv[0] basename is ``case-presence``, or argv[0] basename starts with
    ``python`` and the first non-flag after leading interpreter flags
    (``-u``, ``-B``, ``-I``, ``-E``, ``-s``, and the same class) has basename
    ``case-presence``. ``-c`` and ``-m`` (separate, attached, or clustered)
    mean the interpreter is not running a script, so they do not match.
    The first non-flag after that script is absent or ``daemon``.
    ``nvim .../case-presence`` and ``cat case-presence`` do not match.
    """
    args = _cmdline_args(raw)
    if not args:
        return False
    argv0 = os.path.basename(args[0])
    if argv0 == _CASE_PRESENCE_BASENAME:
        rest = args[1:]
    elif argv0.startswith(b"python"):
        script_at = _python_script_index(args)
        if script_at is None:
            return False
        if os.path.basename(args[script_at]) != _CASE_PRESENCE_BASENAME:
            return False
        rest = args[script_at + 1:]
    else:
        return False
    return _first_subcommand_is_daemon(rest)


def _python_script_index(args: list[bytes]) -> int | None:
    """Index of the script path after leading interpreter flags.

    ``-c`` and ``-m`` do not run a script file, so they return None. That
    covers a separate option (``-c CODE``, ``-m mod``), an attached value
    (``-cCODE``, ``-mmod``), and the same letters in a short cluster
    (``-uc``, ``-Im``). Plain flags and valued ``-W``/``-X`` are skipped.
    """
    index = 1
    limit = len(args)
    while index < limit:
        width = _interpreter_flag_width(args[index])
        if width is None:
            return index
        if width == _PY_NOT_A_SCRIPT:
            return None
        index += width
    return None


def _interpreter_flag_width(flag: bytes) -> int | None:
    """Argv slots used by a leading CPython flag, or None when it is not one.

    ``-c`` and ``-m`` return ``_PY_NOT_A_SCRIPT`` (0): the interpreter is
    running code or a module, not a script, whether the option is separate,
    attached (``-cCODE``, ``-mmod``), or clustered (``-uc``, ``-Im``).
    ``-W`` and ``-X`` with no attached value consume the next slot. The
    rest of a cluster after ``-W`` or ``-X`` is that option's value, not
    more flags (``-Wc`` is a warning, not ``-c``). Combined plain flags
    (``-uB``) and an attached value (``-Werror``) use one slot.
    """
    if not flag.startswith(b"-") or flag in (b"-", b"--"):
        return None
    if flag.startswith(b"--"):
        return 1
    body = flag[1:]
    cursor = 0
    length = len(body)
    while cursor < length:
        letter = body[cursor:cursor + 1]
        cursor += 1
        if letter in _PY_NO_SCRIPT:
            return _PY_NOT_A_SCRIPT
        if letter in _PY_OPTIONS_WITH_VALUE:
            if cursor < length:
                return 1
            return 2
    return 1


def _first_subcommand_is_daemon(rest: list[bytes]) -> bool:
    """True when the first non-flag argument is absent or ``daemon``."""
    for arg in rest:
        if arg.startswith(b"-") and arg != b"-":
            continue
        return arg in _DAEMON_SUBCOMMANDS
    return True


def _pid_start_ok(pid: object, state: dict) -> bool:
    """True when ``pid_start`` is absent or equals the live starttime.

    A present integer is ``/proc/<pid>/stat`` field 22 (clock ticks since
    boot). Any other present value, a mismatch, or an unreadable stat line
    does not match, so the caller fails open. Bool is not an integer here.
    """
    if "pid_start" not in state:
        return True
    recorded = state.get("pid_start")
    if isinstance(recorded, bool) or not isinstance(recorded, int):
        return False
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    live = _proc_starttime(pid)
    return live is not None and live == recorded


def _proc_starttime(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``, or None when it cannot be parsed."""
    try:
        raw = _read_proc_stat(pid)
    except Exception:
        return None
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        return None
    return _starttime_from_stat(raw)


def _read_proc_stat(pid: int) -> bytes:
    """Read at most ``_STAT_READ_MAX`` bytes of ``/proc/<pid>/stat``."""
    fd = os.open(f"/proc/{pid}/stat", os.O_RDONLY | os.O_CLOEXEC)
    try:
        return _read_capped(fd, _STAT_READ_MAX)
    finally:
        os.close(fd)


def _starttime_from_stat(raw: bytes) -> int | None:
    """Parse starttime from a ``stat`` line.

    ``comm`` sits in parentheses and may contain spaces and parentheses.
    Split after the last ``)``. Field 3 is the first tail token, so field 22
    is tail index 19. A short or non-numeric field fails open (None).
    """
    end = raw.rfind(b")")
    if end < 0:
        return None
    tail = raw[end + 1:].split()
    if len(tail) < 20:
        return None
    token = tail[19]
    if not token.isdigit():
        return None
    return int(token)


def _held_limit(cfg: dict) -> int:
    raw = cfg.get("case_presence_held_max", _HELD_DEFAULT)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return _HELD_DEFAULT
    if _HELD_MIN <= raw <= _HELD_MAX:
        return raw
    return _HELD_DEFAULT


def _append_held(runtime_dir: str, text: str, now: float, cfg: dict) -> None:
    """Append one JSON line and keep the newest N, under an exclusive lock.

    Open ``held-voice.jsonl`` with ``O_RDWR|O_CREAT|O_NOFOLLOW|O_NONBLOCK``.
    It must be a regular file owned by this uid; a FIFO, symlink, or other
    owner's file drops the line. ``LOCK_EX|LOCK_NB`` is retried for up to
    half a second, and contention drops the line. A file larger than 256 KiB
    is replaced by the new line. Callers still do not speak when this drops
    the line or raises.
    """
    presence = os.path.join(runtime_dir, _PRESENCE_DIR)
    dir_fd = os.open(presence, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file_fd = -1
    try:
        info = os.fstat(dir_fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return
        file_fd = os.open(
            _HELD_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=dir_fd,
        )
        held = os.fstat(file_fd)
        if not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid():
            return
        if not _lock_exclusive(file_fd):
            return
        payload = _queue_payload(file_fd, text, now, _held_limit(cfg))
        os.ftruncate(file_fd, 0)
        os.lseek(file_fd, 0, os.SEEK_SET)
        _write_all(file_fd, payload)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(dir_fd)


def _lock_exclusive(fd: int) -> bool:
    """Take ``LOCK_EX|LOCK_NB``, retrying until ``_HELD_LOCK_WAIT_S``.

    False means the line is dropped. A non-contention error propagates.
    """
    deadline = time.monotonic() + _HELD_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _queue_payload(fd: int, text: str, now: float, limit: int) -> bytes:
    info = os.fstat(fd)
    if info.st_size > _HELD_READ_MAX:
        existing = b""
    else:
        existing = _read_capped(fd, _HELD_READ_MAX)
    lines = [line for line in existing.splitlines() if line.strip()]
    spoken = text if isinstance(text, str) else str(text)
    record = json.dumps(
        {"ts": now, "text": spoken[:_TEXT_MAX]},
        ensure_ascii=False,
    ).encode("utf-8")
    if b"\n" in record or b"\r" in record:
        raise ValueError("held record must be a single line")
    lines.append(record)
    if len(lines) > limit:
        lines = lines[-limit:]
    return b"\n".join(lines) + b"\n"


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write on held-voice queue")
        view = view[written:]
