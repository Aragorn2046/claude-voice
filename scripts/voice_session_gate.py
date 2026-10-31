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
ignored. A Claude Code registry schema change makes every session silent
(fail silent, visible as ``voice-gate deny
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
_SESSION_ID_RE = re.compile(r"[0-9A-Fa-f-]{8,64}")
_CO_NAME_RE = re.compile("(?:[A-Za-z0-9]+ \u00b7 )?co-shelby-[a-z]+")
_DENIED_SESSION_TYPES = frozenset({"cron", "spinoff", "headless"})

USAGE = (
    "usage: python3 scripts/voice_session_gate.py --explain "
    "--session-id SID [--env KEY=VAL ...] [--home DIR]\n"
)


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

    def log_line(self, host: str, session_id: str) -> str:
        verdict = "allow" if self.allow else "deny"
        text = session_id if isinstance(session_id, str) else ""
        sid = text[:8] or "-"
        return f"voice-gate {verdict} reason={self.reason} host={host} sid={sid}"


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
    matches, bad_reason = _registry_matches(env, home, session_id)
    # An unreadable or oversized entry makes identity unknowable.
    # Deny before liveness or CO checks so a live sibling cannot allow it.
    if bad_reason:
        return Decision(False, bad_reason)
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


def _registry_matches(env, home, session_id):
    """Return ``(matches, bad_reason)`` in directory then name order.

    ``matches`` holds ``(filename, entry)`` for every parsed object whose
    ``sessionId`` equals ``session_id``. ``bad_reason`` is
    ``registry_too_large`` if any ``.json`` entry was too large, else
    ``registry_unreadable`` if any entry was unreadable, else None. Names
    that do not end in ``.json`` are never opened. A file that vanished
    between listdir and open is ignored.
    """
    matches = []
    saw_too_large = False
    saw_unreadable = False
    for directory in _registry_dirs(env, home):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            entry, status = _read_registry_record(path, session_id)
            if status == "too_large":
                saw_too_large = True
            elif status == "unreadable":
                saw_unreadable = True
            elif entry is not None:
                matches.append((name, entry))
    if saw_too_large:
        return matches, "registry_too_large"
    if saw_unreadable:
        return matches, "registry_unreadable"
    return matches, None


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
    extra = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--explain":
            explain = True
            index += 1
            continue
        if arg not in ("--session-id", "--home", "--env"):
            return None
        if index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        index += 2
        if arg == "--session-id":
            session_id = value
        elif arg == "--home":
            home = value
        else:
            key, sep, _env_value = value.partition("=")
            if not sep or not key:
                return None
            extra.append(value)
    if not explain or session_id is None:
        return None
    return session_id, home, extra


def _explain_env(extra) -> dict:
    """Build the read-only env for ``--explain``.

    Only ``HOME`` and ``PATH`` are copied from the process. Each ``--env``
    is added after that, so ``--env`` can replace them.
    """
    clean = {}
    for key in ("HOME", "PATH"):
        value = os.environ.get(key)
        if value is not None:
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
    session_id, home, extra = parsed
    try:
        env = _explain_env(extra)
    except ValueError:
        sys.stderr.write(USAGE)
        return 2
    decision = decide(session_id, env=env, home=home)
    print(json.dumps({
        "allow": decision.allow,
        "reason": decision.reason,
        "session_id": session_id,
    }), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
