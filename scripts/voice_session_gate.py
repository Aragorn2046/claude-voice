#!/usr/bin/env python3
"""Decide whether a Claude Code session may speak on this host.

``speak_sessions`` is ``all`` (default) or ``co_only``. ``co_only`` means
only CO-Shelby. Identity comes only from the Claude session registry: the
entry ``name`` must fullmatch the CO name, ``nameSource`` must be ``user``,
``entrypoint`` must be ``cli``, and ``kind`` must be ``interactive``. Only
entries whose process is alive count. Every live match must be a CO entry;
a mix denies with ``registry_conflict``. The check uses env hard-denies,
session-id validation, and that registry. It never reads argv, process
command lines, prompts, or transcripts. Any unexpected error denies.

Any registry ``.json`` entry that is not a readable regular-file JSON
object (oversized, symlink, FIFO, invalid UTF-8 or JSON, not an object)
denies with ``registry_too_large`` or ``registry_unreadable``, because
identity cannot be proven; a file that vanished during the scan is
ignored. A ``<pid>.json`` file whose stem is an ASCII pid greater than 1
is ignored when that process is dead and the record is too large or
unreadable: a dead producer cannot describe a live session. A non-digit
name, a stem of 0 or 1, or a live stem pid still denies. ``detail`` names
the first offending path for that reason (``~`` for home). A Claude Code
registry schema change makes every session silent (fail silent, visible
as ``voice-gate deny
reason=not_registered|registry_not_co|registry_conflict|registry_too_large|registry_unreadable``
in ``/tmp/claude-tts.log``).
"""

import glob
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass

POLICY_ALL = "all"
POLICY_CO_ONLY = "co_only"

_MAX_REGISTRY_BYTES = 65536
_MAX_ENV_FILE_BYTES = 65536
_DETAIL_LIMIT = 160
_ENV_LOG_LIMIT = 32
_SESSION_ID_RE = re.compile(r"[0-9A-Fa-f-]{8,64}")
_CO_NAME_RE = re.compile("(?:[A-Za-z0-9]+ \u00b7 )?co-shelby-[a-z]+")
_LOG_SAFE_RE = re.compile(r"[^A-Za-z0-9._/~:@+-]")
_DENIED_SESSION_TYPES = frozenset({"cron", "spinoff", "headless"})

USAGE = (
    "usage: python3 scripts/voice_session_gate.py --explain "
    "--session-id SID [--env KEY=VAL ...] [--env-file PATH] [--home DIR]\n"
)


def _sanitize(text, limit: int) -> str:
    """Keep ``[A-Za-z0-9._/~:@+-]`` and cap the result at ``limit`` chars."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return _LOG_SAFE_RE.sub("?", text)[:limit]


def _env_log_field(env, key: str) -> str:
    """One sanitized env value for a log line, or ``-`` when absent."""
    try:
        raw = env.get(key)
    except Exception:
        return "-"
    if not isinstance(raw, str) or raw == "":
        return "-"
    cleaned = _sanitize(raw, _ENV_LOG_LIMIT)
    return cleaned or "-"


def speak_policy(cfg: dict) -> str:
    """Return ``all`` or ``co_only``. Missing, null, and ``all`` stay open.

    Every other value, including typos and non-strings, fails closed to
    ``co_only``.
    """
    if not isinstance(cfg, dict):
        return POLICY_CO_ONLY
    value = cfg.get("speak_sessions", POLICY_ALL)
    if value is None or value == POLICY_ALL:
        return POLICY_ALL
    return POLICY_CO_ONLY


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    detail: str = ""

    def log_line(self, host: str, session_id: str, env=None) -> str:
        verdict = "allow" if self.allow else "deny"
        text = session_id if isinstance(session_id, str) else ""
        sid = text[:8] or "-"
        line = f"voice-gate {verdict} reason={self.reason} host={host} sid={sid}"
        if self.detail:
            line += " detail=" + _sanitize(self.detail, _DETAIL_LIMIT)
        if env is not None:
            line += " ep=" + _env_log_field(env, "CLAUDE_CODE_ENTRYPOINT")
            line += " att=" + _env_log_field(env, "CLAUDE_CODE_SESSION_ATTENDED")
        return line


def decide(
    session_id: str,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
    pid_alive=None,
) -> Decision:
    """Return the speech decision for every live registry match. Never raises.

    ``pid_alive``, when set, is ``callable(pid) -> bool`` and replaces the
    ``os.kill(pid, 0)`` liveness check.
    """
    try:
        if env is None:
            env = os.environ
        if home is None:
            home = os.path.expanduser("~")
        return _decide(session_id, env, home, pid_alive)
    except Exception as exc:
        return Decision(False, f"error:{type(exc).__name__}")


def _decide(session_id, env, home, pid_alive) -> Decision:
    reason = _hard_deny_reason(env)
    if reason:
        return Decision(False, reason)
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return Decision(False, "bad_session_id")
    matches, bad_reason, detail = _registry_matches(
        env, home, session_id, pid_alive
    )
    # An unreadable or oversized entry makes identity unknowable.
    # Deny before liveness or CO checks so a live sibling cannot allow it.
    if bad_reason:
        return Decision(False, bad_reason, detail)
    live = []
    for name, entry in matches:
        pid = _entry_pid(entry, name)
        if pid is None:
            continue
        if _process_alive(pid, pid_alive):
            live.append(entry)
    if not live:
        return Decision(False, "not_registered")
    co_flags = [_is_co_entry(entry) for entry in live]
    if all(co_flags):
        return Decision(True, "registry_name")
    if any(co_flags):
        return Decision(False, "registry_conflict")
    return Decision(False, "registry_not_co")


def _hard_deny_reason(env: Mapping[str, str]) -> str | None:
    # Claude Code 2.1.314 sets CLAUDE_CODE_CHILD_SESSION=1 for every hook
    # subprocess, even of a top-level session (probe 2026-10-30), so it cannot
    # tell a CO from its children. Do not deny on CLAUDE_CODE_CHILD_SESSION.
    if env.get("CLAUDE_SESSION_TYPE") in _DENIED_SESSION_TYPES:
        return "session_type"
    if env.get("CLAUDE_CODE_SHELBY_WORKER") == "1":
        return "worker"
    if env.get("SHELBY_DELEGATED") == "1":
        return "delegated"
    if env.get("SHELBY_CODE_WORKER_DEPTH"):
        return "worker_depth"
    entrypoint = env.get("CLAUDE_CODE_ENTRYPOINT")
    if entrypoint and entrypoint != "cli":
        return "entrypoint"
    if env.get("CLAUDE_CODE_SESSION_ATTENDED") == "0":
        return "unattended"
    return None


def _registry_dirs(env: Mapping[str, str], home: str) -> list:
    candidates = []
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        candidates.append(os.path.join(config_dir, "sessions"))
    candidates.append(os.path.join(home, ".claude", "sessions"))
    pattern = os.path.join(glob.escape(home), ".claude-accounts", "*", "sessions")
    candidates.extend(sorted(glob.glob(pattern)))

    seen = set()
    directories = []
    for path in candidates:
        if not os.path.isdir(path):
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        directories.append(path)
    return directories


def _display_registry_path(path: str, home: str) -> str:
    """Replace a ``home`` prefix with ``~`` for a log detail."""
    if not isinstance(home, str) or not home:
        return path
    root = home.rstrip(os.sep) or home
    if path == root:
        return "~"
    prefix = root + os.sep
    if path.startswith(prefix):
        return "~" + path[len(root):]
    return path


def _offender_detail(path: str, status: str, home: str) -> str:
    shown = _display_registry_path(path, home) + ":" + status
    return _sanitize(shown, _DETAIL_LIMIT)


def _ascii_pid_stem(name: str):
    """Pid from a ``<pid>.json`` stem of ASCII digits greater than 1, else None."""
    if not isinstance(name, str) or not name.endswith(".json"):
        return None
    stem = name[:-5]
    if not stem or not stem.isascii() or not stem.isdigit():
        return None
    value = int(stem)
    if value <= 1:
        return None
    return value


def _ignore_dead_producer(name: str, status: str, pid_alive) -> bool:
    """True when a bad ``<pid>.json`` belongs to a process that is not alive.

    Only ``too_large`` and ``unreadable`` qualify. A non-digit stem, a stem
    of 0 or 1, and a live stem pid stay fail-closed. ``gone`` is not handled
    here; the caller already ignores a file that vanished.
    """
    if status not in ("too_large", "unreadable"):
        return False
    pid = _ascii_pid_stem(name)
    if pid is None:
        return False
    return not _process_alive(pid, pid_alive)


def _registry_matches(env, home, session_id, pid_alive):
    """Return ``(matches, bad_reason, detail)`` in directory then name order.

    ``matches`` holds ``(filename, entry)`` for every parsed object whose
    ``sessionId`` equals ``session_id``. ``bad_reason`` is
    ``registry_too_large`` if any ``.json`` entry was too large, else
    ``registry_unreadable`` if any entry was unreadable, else None.
    ``detail`` is ``<path>:<status>`` of the first offending file for that
    reason, with ``home`` shown as ``~``, or ``""``. A too-large or
    unreadable ``<pid>.json`` whose ASCII pid is greater than 1 and whose
    process is dead is ignored. Names that do not end in ``.json`` are
    never opened. A file that vanished between listdir and open is ignored.
    """
    matches = []
    saw_too_large = False
    saw_unreadable = False
    first_too_large = None
    first_unreadable = None
    for directory in _registry_dirs(env, home):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            entry, status = _read_registry_record(path, session_id)
            if _ignore_dead_producer(name, status, pid_alive):
                continue
            if status == "too_large":
                if first_too_large is None:
                    first_too_large = path
                saw_too_large = True
            elif status == "unreadable":
                if first_unreadable is None:
                    first_unreadable = path
                saw_unreadable = True
            elif entry is not None:
                matches.append((name, entry))
    if saw_too_large:
        return (
            matches,
            "registry_too_large",
            _offender_detail(first_too_large, "too_large", home),
        )
    if saw_unreadable:
        return (
            matches,
            "registry_unreadable",
            _offender_detail(first_unreadable, "unreadable", home),
        )
    return matches, None, ""


def _read_capped(fd: int, limit: int) -> bytes:
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_registry_record(path: str, session_id: str):
    """Return ``(entry or None, status)``. Never raises.

    ``status`` is ``ok`` (parsed object; ``entry`` is that object only when
    its ``sessionId`` equals ``session_id``), ``gone`` (``FileNotFoundError``
    on open), ``too_large`` (regular file over 65536 bytes by fstat or by
    the capped read), or ``unreadable`` (not a regular file, symlink refused
    by ``O_NOFOLLOW``, any other ``OSError``, ``UnicodeError``,
    ``ValueError``, ``RecursionError``, or JSON that is not an object).

    Open uses ``O_NOFOLLOW`` | ``O_NONBLOCK`` | ``O_CLOEXEC``, checks the
    fd is a regular file, reads at most 65537 bytes, and decodes strict
    UTF-8. The fd is closed in ``finally``.
    """
    flags = (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = None
    try:
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None, "gone"
        except OSError:
            return None, "unreadable"
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, "unreadable"
            if info.st_size > _MAX_REGISTRY_BYTES:
                return None, "too_large"
            data = _read_capped(fd, _MAX_REGISTRY_BYTES + 1)
            if len(data) > _MAX_REGISTRY_BYTES:
                return None, "too_large"
            text = data.decode("utf-8")
            parsed = json.loads(text)
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None, "unreadable"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if not isinstance(parsed, dict):
        return None, "unreadable"
    if parsed.get("sessionId") != session_id:
        return None, "ok"
    return parsed, "ok"


def _entry_pid(entry: dict, filename: str):
    """Pid to liveness-check, or None when the entry cannot name a process.

    The ``pid`` field wins when it is an int greater than 1. Otherwise a
    filename stem that is all digits and greater than 1 is the pid. 0 and 1
    are never signalled.
    """
    pid = entry.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
        return pid
    stem = filename[:-5] if filename.endswith(".json") else filename
    if stem.isdigit():
        value = int(stem)
        if value > 1:
            return value
    return None


def _process_alive(pid: int, pid_alive) -> bool:
    if pid_alive is not None:
        return bool(pid_alive(pid))
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_co_entry(entry: dict) -> bool:
    name = entry.get("name")
    return (
        isinstance(name, str)
        and _CO_NAME_RE.fullmatch(name) is not None
        and entry.get("nameSource") == "user"
        and entry.get("entrypoint") == "cli"
        and entry.get("kind") == "interactive"
    )


def _parse_cli(argv):
    explain = False
    session_id = None
    home = None
    env_file = None
    extra = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--explain":
            explain = True
            index += 1
            continue
        if arg not in ("--session-id", "--home", "--env", "--env-file"):
            return None
        if index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        index += 2
        if arg == "--session-id":
            session_id = value
        elif arg == "--home":
            home = value
        elif arg == "--env-file":
            env_file = value
        else:
            key, sep, _env_value = value.partition("=")
            if not sep or not key:
                return None
            extra.append(value)
    if not explain or session_id is None:
        return None
    return session_id, home, extra, env_file


def _read_env_file(path: str) -> list:
    """Return ``(key, value)`` rows from a ``--env-file``.

    Blank lines and lines whose first non-whitespace character is ``#`` are
    ignored. A line without ``=`` or with an empty key raises ``ValueError``.
    A file larger than 65536 bytes, or one that cannot be read as a regular
    UTF-8 file, raises ``ValueError``.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    fd = None
    try:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ValueError("env-file") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ENV_FILE_BYTES:
                raise ValueError("env-file")
            chunks = []
            remaining = _MAX_ENV_FILE_BYTES + 1
            while remaining > 0:
                blob = os.read(fd, remaining)
                if not blob:
                    break
                chunks.append(blob)
                remaining -= len(blob)
            data = b"".join(chunks)
        except OSError as exc:
            raise ValueError("env-file") from exc
        if len(data) > _MAX_ENV_FILE_BYTES:
            raise ValueError("env-file")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("env-file") from exc
    items = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError("env-file")
        items.append((key, value))
    return items


def _explain_env(extra, file_items=()) -> dict:
    """Build the read-only env for ``--explain``.

    ``HOME`` and ``PATH`` are copied from the process, then each env-file
    entry, then each ``--env``. Later values replace earlier ones.
    """
    clean = {}
    for key in ("HOME", "PATH"):
        value = os.environ.get(key)
        if value is not None:
            clean[key] = value
    for key, value in file_items:
        clean[key] = value
    for item in extra:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(item)
        clean[key] = value
    return clean


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parsed = _parse_cli(list(argv))
    if parsed is None:
        sys.stderr.write(USAGE)
        return 2
    session_id, home, extra, env_file = parsed
    try:
        file_items = _read_env_file(env_file) if env_file else []
        env = _explain_env(extra, file_items)
    except ValueError:
        sys.stderr.write(USAGE)
        return 2
    decision = decide(session_id, env=env, home=home)
    print(json.dumps({
        "allow": decision.allow,
        "reason": decision.reason,
        "session_id": session_id,
        "detail": decision.detail,
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT"),
        "attended": env.get("CLAUDE_CODE_SESSION_ATTENDED"),
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
