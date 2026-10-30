#!/usr/bin/env python3
"""Decide whether a Claude Code session may speak on this host.

``speak_sessions`` is ``all`` (default) or ``co_only``. The check uses env
hard-denies, an opt-in marker, the Claude session registry, then a tmux
session-name fallback. It never reads argv, process command lines, prompts,
or transcripts. Any unexpected error denies.
"""

import glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass

POLICY_ALL = "all"
POLICY_CO_ONLY = "co_only"

_MAX_REGISTRY_BYTES = 65536
_SESSION_ID_RE = re.compile(r"[0-9A-Fa-f-]{8,64}")
_CO_NAME_RE = re.compile("(?:[A-Za-z0-9]+ \u00b7 )?co-shelby-[a-z]+")
_CO_TMUX_RE = re.compile(r"co-shelby-[a-z]+")
_TMUX_PANE_RE = re.compile(r"%[0-9]+")
_DENIED_SESSION_TYPES = frozenset({"cron", "spinoff", "headless"})
_TMUX_CANDIDATES = (
    "/opt/homebrew/bin/tmux",
    "/usr/local/bin/tmux",
    "/usr/bin/tmux",
)

USAGE = (
    "usage: python3 scripts/voice_session_gate.py --explain "
    "--session-id SID [--tmux-pane %N] [--env KEY=VAL ...] [--home DIR]\n"
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
    tmux_bin: str | None = None,
    tmux_timeout_s: float = 1.0,
) -> Decision:
    """Return the first matching speech decision. Never raises."""
    try:
        if env is None:
            env = os.environ
        if home is None:
            home = os.path.expanduser("~")
        return _decide(session_id, env, home, tmux_bin, tmux_timeout_s)
    except Exception as exc:
        return Decision(False, f"error:{type(exc).__name__}")


def _decide(session_id, env, home, tmux_bin, tmux_timeout_s) -> Decision:
    reason = _hard_deny_reason(env)
    if reason:
        return Decision(False, reason)
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return Decision(False, "bad_session_id")
    if _opted_in(home, session_id):
        return Decision(True, "opt_in")
    entry = _registry_entry(env, home, session_id)
    if entry is not None:
        return _classify_entry(entry)
    return _tmux_decision(env, tmux_bin, tmux_timeout_s)


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


def _opted_in(home: str, session_id: str) -> bool:
    path = os.path.join(home, ".shelby", "voice-allow", session_id)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


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


def _registry_entry(env, home, session_id):
    for directory in _registry_dirs(env, home):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_REGISTRY_BYTES:
                continue
            entry = _read_json_object(path)
            if entry is not None and entry.get("sessionId") == session_id:
                return entry
    return None


def _read_json_object(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    return entry


def _classify_entry(entry: dict) -> Decision:
    name = entry.get("name")
    source = entry.get("nameSource")
    if (
        isinstance(name, str)
        and _CO_NAME_RE.fullmatch(name)
        and source in (None, "user")
    ):
        return Decision(True, "registry_name")
    tmux = entry.get("tmux")
    if source != "user" and isinstance(tmux, str):
        session_name = tmux.split(":", 1)[0]
        if _CO_TMUX_RE.fullmatch(session_name):
            return Decision(True, "registry_tmux")
    return Decision(False, "registry_not_co")


def _resolve_tmux(tmux_bin, env: Mapping[str, str]) -> str | None:
    if tmux_bin:
        return tmux_bin
    found = shutil.which("tmux", path=env.get("PATH"))
    if found:
        return found
    for candidate in _TMUX_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _tmux_decision(env, tmux_bin, tmux_timeout_s) -> Decision:
    pane = env.get("TMUX_PANE") or ""
    if not isinstance(pane, str) or not _TMUX_PANE_RE.fullmatch(pane):
        return Decision(False, "no_marker")
    binary = _resolve_tmux(tmux_bin, env)
    if not binary:
        return Decision(False, "tmux_missing")
    try:
        result = subprocess.run(
            [binary, "display-message", "-p", "-t", pane, "#S"],
            capture_output=True,
            text=True,
            timeout=tmux_timeout_s,
            env=dict(env),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return Decision(False, "tmux_timeout")
    except OSError:
        return Decision(False, "tmux_error")
    if result.returncode != 0:
        return Decision(False, "tmux_error")
    session_name = (result.stdout or "").strip()
    if _CO_TMUX_RE.fullmatch(session_name):
        return Decision(True, "tmux_fallback")
    return Decision(False, "tmux_not_co")


def _parse_cli(argv):
    explain = False
    session_id = None
    tmux_pane = None
    home = None
    extra = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--explain":
            explain = True
            index += 1
            continue
        if arg not in ("--session-id", "--tmux-pane", "--home", "--env"):
            return None
        if index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        index += 2
        if arg == "--session-id":
            session_id = value
        elif arg == "--tmux-pane":
            tmux_pane = value
        elif arg == "--home":
            home = value
        else:
            key, sep, _env_value = value.partition("=")
            if not sep or not key:
                return None
            extra.append(value)
    if not explain or session_id is None:
        return None
    return session_id, tmux_pane, home, extra


def _explain_env(tmux_pane, extra) -> dict:
    """Build the read-only env for ``--explain``.

    Only ``HOME`` and ``PATH`` are copied from the process. ``TMUX_PANE`` and
    each ``--env`` are added after that, so ``--env`` can replace them.
    """
    clean = {}
    for key in ("HOME", "PATH"):
        value = os.environ.get(key)
        if value is not None:
            clean[key] = value
    if tmux_pane is not None:
        clean["TMUX_PANE"] = tmux_pane
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
    session_id, tmux_pane, home, extra = parsed
    try:
        env = _explain_env(tmux_pane, extra)
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
