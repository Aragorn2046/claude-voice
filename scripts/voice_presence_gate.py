"""CASE presence gate: hold a voice line when the room is empty.

``case-presence`` (a daemon outside this repo) publishes
``$XDG_RUNTIME_DIR/case-presence/state.json``. This module is fail-open:
anything unexpected while deciding returns False so the line is still spoken.
A failure while recording a held line drops that line and still does not speak.

A fresh publisher is honored as written. ``updated_at`` may go stale while
the daemon blocks on an effect or the machine is suspended; a state at most
``_STALE_ALIVE_MAX_S`` old still holds when that pid is alive and its command
line is ``case-presence``. Older state, a dead pid, or any other command
does not hold.

``state.json`` is opened relative to a directory fd (``O_DIRECTORY |
O_NOFOLLOW``) with ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``. It must be a
regular file owned by the current uid. At most 64 KiB is read. A FIFO
cannot block this open; it fails the regular-file check and does not hold.
A symlink or any other failure does not hold.
"""

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
_FRESH_S = 15.0
_FUTURE_SKEW_S = 5.0
_STALE_ALIVE_MAX_S = 180.0
_CMDLINE_READ_MAX = 4096
_STATE_READ_MAX = 65536
_CASE_PRESENCE_BASENAME = b"case-presence"
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
    JSON object from a live publisher with ``hold_voice`` exactly true.
    The publisher is trusted when its timestamp is fresh, or stale by at
    most ``_STALE_ALIVE_MAX_S`` while the pid's command line is
    ``case-presence``. ``cmdline_reader`` overrides the ``/proc`` read.

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
    """True when state says hold and the publisher is still credible."""
    state = _read_state(runtime_dir)
    if not isinstance(state, dict):
        return False
    updated_at = state.get("updated_at")
    pid = state.get("pid")
    if _updated_at_fresh(updated_at, now):
        if not _process_alive(pid):
            return False
        return state.get("hold_voice") is True
    if not _updated_at_within_stale_alive(updated_at, now):
        return False
    if not _process_alive(pid):
        return False
    if not _pid_is_case_presence(pid, cmdline_reader):
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


def _updated_at_fresh(updated_at: object, now: float) -> bool:
    """True when updated_at is at most 15s old and at most 5s in the future."""
    age = _updated_at_age(updated_at, now)
    if age is None:
        return False
    return -_FUTURE_SKEW_S <= age <= _FRESH_S


def _updated_at_within_stale_alive(updated_at: object, now: float) -> bool:
    """True when updated_at is past the fresh window and within the stale cap."""
    age = _updated_at_age(updated_at, now)
    if age is None:
        return False
    return _FRESH_S < age <= _STALE_ALIVE_MAX_S


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
    """True when a cmdline argument's basename is ``case-presence``.

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


def _cmdline_names_case_presence(raw: bytes) -> bool:
    """True when a complete NUL-separated arg's basename is case-presence.

    A buffer that fills the read cap and does not end in NUL was cut off
    mid-argument; that partial tail is ignored.
    """
    if len(raw) == _CMDLINE_READ_MAX and not raw.endswith(b"\0"):
        raw = raw.rpartition(b"\0")[0]
    for arg in raw.split(b"\0"):
        if arg and os.path.basename(arg) == _CASE_PRESENCE_BASENAME:
            return True
    return False


def _held_limit(cfg: dict) -> int:
    raw = cfg.get("case_presence_held_max", _HELD_DEFAULT)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return _HELD_DEFAULT
    if _HELD_MIN <= raw <= _HELD_MAX:
        return raw
    return _HELD_DEFAULT


def _append_held(runtime_dir: str, text: str, now: float, cfg: dict) -> None:
    """Append one JSON line and keep the newest N, under an exclusive lock.

    Refuse a symlinked queue file (O_NOFOLLOW) and a case-presence directory
    that is itself a symlink or owned by another uid. Callers drop the line
    when this raises; they still do not speak it.
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
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=dir_fd,
        )
        fcntl.flock(file_fd, fcntl.LOCK_EX)
        payload = _queue_payload(file_fd, text, now, _held_limit(cfg))
        os.ftruncate(file_fd, 0)
        os.lseek(file_fd, 0, os.SEEK_SET)
        _write_all(file_fd, payload)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(dir_fd)


def _queue_payload(fd: int, text: str, now: float, limit: int) -> bytes:
    existing = _read_all(fd)
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


def _read_all(fd: int) -> bytes:
    chunks = []
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write on held-voice queue")
        view = view[written:]
