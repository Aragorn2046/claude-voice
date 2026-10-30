"""CASE presence gate: hold a voice line when the room is empty.

``case-presence`` (a daemon outside this repo) publishes
``$XDG_RUNTIME_DIR/case-presence/state.json``. This module is fail-open:
anything unexpected while deciding returns False so the line is still spoken.
A failure while recording a held line drops that line and still does not speak.
"""

import fcntl
import json
import math
import os
import stat
import time


_PRESENCE_DIR = "case-presence"
_STATE_NAME = "state.json"
_HELD_NAME = "held-voice.jsonl"
_FRESH_S = 15.0
_FUTURE_SKEW_S = 5.0
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
) -> bool:
    """Return True when this line must be queued instead of spoken.

    Hold only when the gate is exactly enabled and the presence state is a
    fresh JSON object from a live publisher with ``hold_voice`` exactly true.
    """
    try:
        if cfg.get("case_presence_gate") is not True:
            return False
        base = _runtime_dir(runtime_dir)
        moment = _coerce_now(now)
        if not _state_says_hold(base, moment):
            return False
    except Exception:
        return False

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


def _state_says_hold(runtime_dir: str, now: float) -> bool:
    path = os.path.join(runtime_dir, _PRESENCE_DIR, _STATE_NAME)
    with open(path, encoding="utf-8") as handle:
        state = json.load(handle)
    if not isinstance(state, dict):
        return False
    if not _updated_at_fresh(state.get("updated_at"), now):
        return False
    if not _process_alive(state.get("pid")):
        return False
    return state.get("hold_voice") is True


def _updated_at_fresh(updated_at: object, now: float) -> bool:
    """True when updated_at is at most 15s old and at most 5s in the future."""
    if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
        return False
    stamp = float(updated_at)
    if not math.isfinite(stamp):
        return False
    age = now - stamp
    return -_FUTURE_SKEW_S <= age <= _FRESH_S


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
