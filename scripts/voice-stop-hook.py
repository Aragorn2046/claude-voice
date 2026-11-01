#!/usr/bin/env python3
"""Claude Code Stop hook — multi-engine TTS with a routed Dutch speech lane.

Engines: edge (default/free), elevenlabs (premium/streaming), kokoro (local/free)
Features:
  - Lockfile prevents dual-session double-playback
  - Speech is pinned to English fleetwide by default (Aragorn directive
    2026-08-03). Since 2026-09-28, Model Routing can select OmniVoice for Dutch;
    an unavailable lane falls back to the pin's translation path.
  - Engine switchable via config or /tts command
  - Remote audio: auto-discovers receiver (SSH, MOSH, any remote access)
  - SIGTERM-safe: cleans up lockfile and exits silently when killed

Receives JSON on stdin with last_assistant_message. Extracts the <voice>...</voice>
block, sanitizes it for speech, and plays it.
"""

import asyncio
import errno
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

# Clamp Dutch synthesis time while reserving room for the resolver and English
# translation/Pocket synthesis fallback. Delivery is not bounded by this budget.
# Keep this policy inline: deployments may copy only the hook file, and an
# optional policy-module failure must never disable all speech. Keep these
# helpers aligned with scripts/shelby_speech_policy.py for the CLI.
OVERALL_DUTCH_BUDGET_S = 60.0
DUTCH_RESOLVER_TIMEOUT_S = 3.0
ENGLISH_PIN_TRANSLATION_TIMEOUT_S = 12.0
DEFAULT_POCKET_TIMEOUT_S = 27
MAX_DUTCH_TIMEOUT_S = 15.0
DEFAULT_DUTCH_TIMEOUT_S = MAX_DUTCH_TIMEOUT_S
MIN_DUTCH_TIMEOUT_S = 1.0
MIN_DUTCH_LANE_BUDGET_S = 3.0
SHELBY_DUTCH_PERSONAS = frozenset({
    "jarvis",
    "jarvis-app-dynamic",
    "jarvis-app-focus",
    "jarvis-app-lively",
    "jarvis-studio-v2",
    "eva",
    "codex",
})


def normalize_dutch_persona(voice):
    """Normalize a Dutch-lane persona, rejecting non-ASCII name characters."""
    normalized = str(voice or "").strip().casefold()
    return normalized if re.fullmatch(r"[a-z0-9-]+", normalized) else ""


def pocket_synthesis_timeout_s():
    """Return the effective Pocket timeout reserved for English fallback."""
    try:
        return max(1, int(os.environ.get(
            "SHELBY_POCKET_TIMEOUT", str(DEFAULT_POCKET_TIMEOUT_S)
        )))
    except (TypeError, ValueError):
        return DEFAULT_POCKET_TIMEOUT_S


def remaining_dutch_lane_budget_s(elapsed_s=0.0, reserve_fallback=True):
    """Budget left after resolver and optional English fallback reserves."""
    try:
        elapsed = max(0.0, float(elapsed_s))
    except (TypeError, ValueError):
        elapsed = 0.0
    fallback_reserve = (
        ENGLISH_PIN_TRANSLATION_TIMEOUT_S + pocket_synthesis_timeout_s()
        if reserve_fallback else 0.0
    )
    return (
        OVERALL_DUTCH_BUDGET_S - elapsed - DUTCH_RESOLVER_TIMEOUT_S
        - fallback_reserve
    )


def is_content_voice(voice, configured_content_voice="aragorn"):
    """Return whether a normalized voice name selects a content clone."""
    normalized_voice = str(voice or "").strip().casefold()
    configured_prefix = str(configured_content_voice or "").strip().casefold()
    return bool(
        normalized_voice
        and (
            normalized_voice.startswith("aragorn")
            or (configured_prefix and normalized_voice.startswith(configured_prefix))
        )
    )


def bounded_dutch_timeout(config, elapsed_s=0.0, reserve_fallback=True):
    """Clamp the lane synthesis timeout while retaining the fallback reserve."""
    remaining = remaining_dutch_lane_budget_s(elapsed_s, reserve_fallback)
    if remaining < MIN_DUTCH_LANE_BUDGET_S:
        return None
    if not isinstance(config, dict):
        return min(DEFAULT_DUTCH_TIMEOUT_S, remaining)
    try:
        timeout = float(config.get("timeout_s", DEFAULT_DUTCH_TIMEOUT_S))
    except (TypeError, ValueError):
        return min(DEFAULT_DUTCH_TIMEOUT_S, remaining)
    if not math.isfinite(timeout):
        return min(DEFAULT_DUTCH_TIMEOUT_S, remaining)
    configured_clamp = max(MIN_DUTCH_TIMEOUT_S, min(timeout, MAX_DUTCH_TIMEOUT_S))
    return min(configured_clamp, remaining)

IS_MACOS = platform.system() == "Darwin"

# Ensure WSLg PulseAudio is available (WSL only)
if not IS_MACOS and not os.environ.get("PULSE_SERVER") and os.path.exists("/mnt/wslg/PulseServer"):
    os.environ["PULSE_SERVER"] = "unix:/mnt/wslg/PulseServer"

# Paths
CONFIG_PATH = os.path.expanduser("~/projects/claude-voice/scripts/config.json")
LOCKFILE_PATH = "/tmp/sonia-tts.lock"
LOG_PATH = "/tmp/claude-tts.log"
VOXTYPE_POLL_S = 0.2
VOXTYPE_MAX_WAIT_S = 180.0
VOXTYPE_BUSY_STATES = frozenset({"recording", "transcribing"})
VOXTYPE_HEARTBEAT_S = 5.0
# Non-VoxType steal is main's: SIGKILL, then one immediate LOCK_NB.
# VoxType steal SIGTERMs the holder first (so it can stop paplay), waits
# LOCK_STEAL_TERM_WAIT_S, then SIGKILLs a holder that still verifies and
# stops its paplay children. The post-kill LOCK_NB poll is capped at
# LOCK_STEAL_RETRY_S. Never a blocking flock. A recorded pid is signalled
# only when it verifies as this user's voice-stop-hook; anything else is
# a dead holder.
LOCK_STEAL_RETRY_S = 1.0
LOCK_STEAL_POLL_S = 0.05
LOCK_STEAL_TERM_WAIT_S = 0.5
LOCK_STEAL_PLAYER_WAIT_S = 0.3
_VOXTYPE_WAIT_ENABLED = True
# One cap for this hook run: the pre-lock wait and every first gate wait.
# None means the run never armed a budget (remote_audio, no pre-lock wait,
# or a direct call). Reset at the start of main().
_voxtype_wait_budget = None
# Same-session arrival registry. Stamp is monotonic_ns from the start of
# main(); the entry path is removed on the way out. Both stay None when
# ordering is off, so a host without VoxType does not touch the registry.
_arrival_stamp_ns = None
_arrival_entry_path = None
_ARRIVAL_ENTRY_RE = re.compile(r"^(\d{20})-(\d+)$")
_NEWER_ARRIVAL_LOG = "voxtype: newer line from this session pending, dropping this one"
_ARRIVAL_UNSAFE_LOG = "voxtype: arrival registry unsafe, ordering off"
# A line held before this hook owns the flock is invisible to voice-shutup
# when another session holds the lock. The transcript watch drops it only
# for this session's own prompt. HELD_SIGTERM_GRACE_S is how long a SIGTERM
# during that phase waits before we treat it as another session's prompt.
HELD_SIGTERM_GRACE_S = 2.0
_PROMPT_DROP_LOG = "voxtype: prompt submitted in this session, held line dropped"
_HELD_KEPT_LOG = "voxtype: held line kept, prompt was in another session"
_TRANSCRIPT_READ_CAP = 1024 * 1024
_HARNESS_PREFIXES = (
    "<task-notification",
    "<command-",
    "<local-command",
    "[SYSTEM NOTIFICATION",
    "This session is being continued from a previous conversation",
    "Caveat:",
)
# Held phase: True from a usable transcript baseline until flock is won.
# A lost watch clears it. Outside this phase SIGTERM exits as before.
_held_phase = False
_deferred_sigterm_at = None
_tw_usable = False
_tw_own_prompt = False
_tw_path = None
_tw_dev = None
_tw_ino = None
_tw_offset = 0
_tw_partial = b""

DUTCH_ENGINE_ENDPOINTS = {
    "omnivoice:tts": "http://100.77.19.108:8934",
}
OMNIVOICE_RESPONSE_CHUNK_SIZE = 16 * 1024
MAX_OMNIVOICE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OMNIVOICE_RESPONSE_HEADER_BYTES = 64 * 1024

try:
    from omnivoice_http import post_json as _raw_omnivoice_post_json
except ImportError:
    # The hook is sometimes deployed as a single copied file. A missing
    # optional transport must leave English speech and the rest of the hook up.
    _raw_omnivoice_post_json = None


def _omnivoice_post_json(base_url: str, payload: bytes, timeout: float) -> bytes:
    if _raw_omnivoice_post_json is None:
        raise RuntimeError("OmniVoice HTTP helper is unavailable")
    return _raw_omnivoice_post_json(
        base_url, payload, timeout, MAX_OMNIVOICE_RESPONSE_BYTES,
        OMNIVOICE_RESPONSE_CHUNK_SIZE, MAX_OMNIVOICE_RESPONSE_HEADER_BYTES,
    )


def _model_route_resolver_candidates():
    return (
        os.path.expanduser("~/scripts/shelby-model-route.py"),
        "/Users/aragorn/scripts/shelby-model-route.py",
        "/home/arago/scripts/shelby-model-route.py",
    )


def _find_model_route_resolver():
    return next((path for path in _model_route_resolver_candidates()
                 if os.path.isfile(path)), None)


def resolve_dutch_speech_engine():
    """Return the live Dutch speech catalog ID, or None when resolution fails."""
    def failed(reason):
        log(f"Dutch speech Model Routing resolution failed: {reason}")
        return None

    resolver = _find_model_route_resolver()
    if not resolver:
        return failed("resolver not found")
    try:
        result = subprocess.run(
            [sys.executable, resolver, "--consumer", "shelby-audio",
             "--modality", "speech-nl", "--target", "catalog",
             "--field", "catalog_id"],
            capture_output=True, text=True, timeout=DUTCH_RESOLVER_TIMEOUT_S, shell=False,
        )
    except subprocess.TimeoutExpired:
        return failed("resolver timed out after 3 seconds")
    except OSError as exc:
        return failed(f"resolver could not run: {exc}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:160]
        suffix = f": {detail}" if detail else ""
        return failed(f"resolver exited {result.returncode}{suffix}")

    catalog_id = (result.stdout or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", catalog_id):
        return failed("resolver returned an unparseable catalog ID")
    return catalog_id

# Global lock handle for SIGTERM cleanup
_lock_fd = None
_current_player = None

# Defaults (overridden by config.json)
DEFAULTS = {
    "tts_engine": "edge",
    "tts_speed": "+30%",
    "tts_voice_edge_en": "en-GB-SoniaNeural",
    "tts_voice_edge_nl": "nl-NL-FennaNeural",
    "tts_voice_elevenlabs_en": "rTWLXOmnw0ckuMBnjFoZ",  # Day Voice
    "tts_voice_elevenlabs_nl": "rTWLXOmnw0ckuMBnjFoZ",  # Day Voice (multilingual)
    "elevenlabs_model": "eleven_turbo_v2_5",
    "elevenlabs_api_key_env": "ELEVENLABS_API_KEY",
    "elevenlabs_api_key": "",
    "tts_voice_kokoro_en": "af_heart",
    "tts_voice_kokoro_nl": "af_heart",
    "voxtype_wait": True,
}

EDGE_SOURCE_PERSONAS = {
    "day": {"en": "en-GB-RyanNeural", "nl": "nl-NL-MaartenNeural"},
    "dawn": {"en": "en-US-AriaNeural", "nl": "nl-NL-FennaNeural"},
    "dusk": {"en": "en-GB-SoniaNeural", "nl": "nl-BE-DenaNeural"},
}

KOKORO_SOURCE_PERSONAS = {
    "day": "am_michael",
    "dawn": "af_heart",
    "dusk": "bf_emma",
}


def log(msg: str):
    """Write to log file instead of stderr (prevents bleeding into Claude Code output)."""
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def handle_sigterm(signum, frame):
    """Clean up lockfile and exit silently on SIGTERM (from timeout or voice-shutup).

    During the held phase the line does not own the lock, so a SIGTERM may
    belong to another session. Remember the time and return. The poll decides
    whether this session prompted. Outside the held phase, exit as before.
    """
    global _lock_fd, _deferred_sigterm_at, _held_phase
    if _held_phase:
        _deferred_sigterm_at = time.monotonic()
        return
    _held_phase = False
    try:
        _clear_arrival_entry()
    except Exception:
        pass
    player = _current_player
    if player is not None:
        try:
            if player.poll() is None:
                player.terminate()
        except Exception:
            pass
    release_lock(_lock_fd)
    os._exit(0)


def load_config() -> dict:
    """Load config, merging with defaults."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


_SESSION_ID = ""


def _stamp_lockfile(lock_fd):
    lock_fd.seek(0)
    lock_fd.truncate()
    lock_fd.write(f"{os.getpid()}\n{_SESSION_ID}\n")
    lock_fd.flush()


class _LockRefused(Exception):
    """The lock path is a symlink, not a regular file, or owned by someone else."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _OwnPromptDrop(Exception):
    """This session submitted a prompt while our line was held. Do not speak."""


def _open_lockfile():
    """Open the speech lock for stamping. Never follow a symlink.

    O_NONBLOCK so a FIFO at the lock path cannot block before fstat rejects it.
    """
    try:
        fd = os.open(
            LOCKFILE_PATH,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise _LockRefused("symlink") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _LockRefused("not a regular file")
        if st.st_uid != os.getuid():
            raise _LockRefused("owner mismatch")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return os.fdopen(fd, "r+")


def _read_lock_record():
    """Return (lines, mtime) for a lock file we own. Never follow a symlink.

    O_NONBLOCK so a FIFO (O_RDONLY would wait for a writer) is refused by fstat
    instead of hanging the hook.
    """
    try:
        fd = os.open(LOCKFILE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise _LockRefused("symlink") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _LockRefused("not a regular file")
        if st.st_uid != os.getuid():
            raise _LockRefused("owner mismatch")
        chunks = []
        total = 0
        while total < 65536:
            blob = os.read(fd, 4096)
            if not blob:
                break
            chunks.append(blob)
            total += len(blob)
        text = b"".join(chunks).decode("utf-8", "replace")
        return text.splitlines(), st.st_mtime
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _parse_holder_pid(lines):
    """A pid worth considering, or None when the record must not be signalled.

    Non-integers, pid <= 1, and this process are not a holder.
    """
    if not lines:
        return None
    try:
        pid = int(str(lines[0]).strip())
    except (TypeError, ValueError):
        return None
    if pid <= 1 or pid == os.getpid():
        return None
    return pid


def _status_real_uid(pid):
    """Real uid from /proc/<pid>/status, or None on any read or parse error."""
    try:
        with open(
            f"/proc/{int(pid)}/status", "r", encoding="utf-8", errors="replace",
        ) as handle:
            for line in handle:
                if line.startswith("Uid:"):
                    parts = line.split()
                    if len(parts) < 2:
                        return None
                    return int(parts[1])
    except (OSError, ValueError, IndexError, TypeError):
        return None
    return None


def _argv0_basename(cmdline):
    if not cmdline:
        return b""
    argv0 = cmdline.split(b"\x00", 1)[0]
    slash = argv0.rfind(b"/")
    if slash >= 0:
        return argv0[slash + 1:]
    return argv0


def _is_our_hook(pid) -> bool:
    """True when pid is this user's voice-stop-hook. Any failure is False."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        if IS_MACOS:
            return _is_our_hook_macos(pid)
        return _is_our_hook_linux(pid)
    except Exception:
        return False


def _is_our_hook_linux(pid) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            cmdline = handle.read()
    except OSError:
        return False
    if b"voice-stop-hook" not in cmdline:
        return False
    uid = _status_real_uid(pid)
    return uid is not None and uid == os.getuid()


def _is_our_hook_macos(pid) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-o", "uid=,command=", "-p", str(pid)],
            capture_output=True,
            timeout=1,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    if result.returncode != 0 or not result.stdout:
        return False
    try:
        text = result.stdout.decode("utf-8", "replace")
    except Exception:
        return False
    line = ""
    for raw in text.splitlines():
        if raw.strip():
            line = raw.strip()
            break
    if not line:
        return False
    parts = line.split(None, 1)
    if len(parts) != 2:
        return False
    try:
        uid = int(parts[0])
    except ValueError:
        return False
    return uid == os.getuid() and "voice-stop-hook" in parts[1]


def _is_signallable_paplay(pid) -> bool:
    """True when cmdline argv[0] basename is paplay and the real uid is ours."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as handle:
            cmdline = handle.read()
    except (OSError, ValueError, TypeError):
        return False
    if _argv0_basename(cmdline) != b"paplay":
        return False
    uid = _status_real_uid(pid)
    return uid is not None and uid == os.getuid()


def acquire_lock(queue_wait_s=0.0, graceful_steal=False, yield_to_newer=None):
    """Acquire the speech lock, or return None if another instance keeps it.

    queue_wait_s=0 is one decision: a yield returns None with no further
    polling. queue_wait_s>0 polls every VOXTYPE_POLL_S and decides again,
    including the steal rules, until the lock is taken or the budget passes.
    Then log Yielding lock and return None.

    graceful_steal=False is main's steal, used when VoxType is not present.
    A verified live holder is SIGKILLed and LOCK_NB is retried once,
    immediately. No SIGTERM and no poll. cmdline and uid are read for that
    one pid; there is no /proc directory listing.

    graceful_steal=True is the VoxType steal. A verified live holder gets
    SIGTERM, then LOCK_NB is polled for up to 0.5 s. If it still verifies,
    the hook SIGKILLs it and stops its paplay children. The fd is returned
    only after that stop, so the new line cannot overlap the old player.
    If the lock is still free, LOCK_NB is polled for up to 1 s. A dead
    holder, or a live pid that fails the holder check, is not signalled;
    that path only polls. A symlink, a non-regular lock file, or another
    user's lock is refused at once: one "lock refused:" line, no write,
    no signal, and no further polling. Every fd that is not kept is closed.
    Never a blocking flock.

    yield_to_newer is used only on the graceful path. None keeps today's
    behaviour; the non-VoxType path passes None. When it is a zero-arg
    callable, the hook calls it before each take and again before stealing
    a same-session holder. The steal poll calls it before each of its takes
    and again after a take wins. A true result logs the newer-line line once,
    releases a won fd, and returns None without the follow-up poll. The
    signalled holder's player is still stopped before that return.
    """

    logged_newer = False

    def take():
        """Non-blocking acquire. Return the fd, or None. Close any fd not kept.

        The held phase ends the moment flock succeeds, before the pid is
        stamped, so a steal SIGTERM is no longer deferred.
        """
        global _lock_fd, _held_phase
        lock_fd = None
        try:
            lock_fd = _open_lockfile()
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _held_phase = False
            _stamp_lockfile(lock_fd)
        except _LockRefused:
            if lock_fd is not None:
                try:
                    lock_fd.close()
                except OSError:
                    pass
            raise
        except (IOError, OSError):
            if lock_fd is not None:
                try:
                    lock_fd.close()
                except OSError:
                    pass
            return None
        _lock_fd = lock_fd
        return lock_fd

    def _parse_proc_stat(text):
        # comm is the text between the first "(" and the last ")".
        # Field 3 is state, field 4 is ppid.
        start = text.find("(")
        end = text.rfind(")")
        if start < 0 or end <= start:
            raise ValueError("stat")
        rest = text[end + 1:].split()
        if len(rest) < 2:
            raise ValueError("stat")
        return text[start + 1:end], rest[0], int(rest[1])

    def _read_proc_stat(pid):
        with open(f"/proc/{pid}/stat") as handle:
            return _parse_proc_stat(handle.read())

    def _player_children(pid) -> list[int]:
        """Direct paplay children of this user. Ignore every read error.

        ppid matches, comm is paplay, cmdline argv[0] basename is exactly
        paplay, and the real uid is ours.
        """
        found = []
        try:
            names = os.listdir("/proc")
        except OSError:
            return found
        for name in names:
            if not name.isdigit():
                continue
            try:
                comm, _state, ppid = _read_proc_stat(name)
            except (OSError, ValueError, IndexError):
                continue
            if (
                ppid == pid
                and comm == "paplay"
                and _is_signallable_paplay(name)
            ):
                found.append(int(name))
        return found

    def _alive(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _gone_or_zombie(pid):
        try:
            _comm, state, _ppid = _read_proc_stat(pid)
        except (OSError, ValueError, IndexError):
            return True
        return state == "Z"

    class _NewerArrival(Exception):
        """Newer same-session line. Caller returns None and skips the next poll."""

    def _poll_take(limit_s):
        """Retry LOCK_NB until limit_s passes. Return the fd, or None.

        Check for a newer arrival before each take and again after a win.
        A newer line releases a won fd and raises _NewerArrival so the caller
        does not start the follow-up poll. No transcript read here: this
        window is at most 1.5 s and must finish stopping the old player.
        """
        deadline = time.monotonic() + limit_s
        while True:
            if _stop_for_newer():
                raise _NewerArrival()
            lock_fd = take()
            if lock_fd is not None:
                if _stop_for_newer():
                    release_lock(lock_fd)
                    raise _NewerArrival()
                return lock_fd
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(LOCK_STEAL_POLL_S, remaining))

    def _stop_players(old_pid, children):
        """SIGKILL a holder that survived SIGTERM, then stop its paplay.

        Returns only after those children are gone, zombie, or the 0.3 s
        wait is over. The caller must not start the new line before that:
        SIGKILL does not run the holder's player cleanup.

        The SIGKILL is sent only when the holder still verifies. A pid that
        was reused during the 0.5 s wait is left alone. A child is signalled
        only when it still looks like our paplay.
        """
        ordered = []
        seen = set()
        for child in children:
            if child not in seen:
                seen.add(child)
                ordered.append(child)
        if _alive(old_pid) and _is_our_hook(old_pid):
            extra = _player_children(old_pid)
            # Re-verify immediately before the signal. The scan above can
            # take long enough for the pid to be reused.
            if _is_our_hook(old_pid):
                for child in extra:
                    if child not in seen:
                        seen.add(child)
                        ordered.append(child)
                try:
                    os.kill(old_pid, 9)
                except OSError:
                    pass
        targets = []
        for child in ordered:
            # Re-read immediately before the signal so a reused pid whose
            # comm is no longer paplay is left alone. argv[0] and uid are
            # checked again for the same reason.
            try:
                comm, _state, _ppid = _read_proc_stat(child)
            except (OSError, ValueError, IndexError):
                continue
            if comm != "paplay" or not _is_signallable_paplay(child):
                continue
            try:
                os.kill(child, signal.SIGTERM)
            except OSError:
                continue
            targets.append(child)
        deadline = time.monotonic() + LOCK_STEAL_PLAYER_WAIT_S
        pending = targets
        while pending:
            pending = [pid for pid in pending if not _gone_or_zombie(pid)]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(LOCK_STEAL_POLL_S, remaining))

    def _stop_for_newer():
        """True when a newer same-session line should keep this one from speaking.

        Graceful path only. A registry error is not a reason to drop the line.
        The newer-line log is written once per acquire.
        """
        nonlocal logged_newer
        if not graceful_steal or yield_to_newer is None:
            return False
        try:
            newer = yield_to_newer()
        except OSError:
            return False
        if newer:
            if not logged_newer:
                log(_NEWER_ARRIVAL_LOG)
                logged_newer = True
            return True
        return False

    try:
        budget = float(queue_wait_s)
    except (TypeError, ValueError):
        budget = 0.0
    if budget < 0:
        budget = 0.0
    deadline = time.monotonic() + budget

    try:
        while True:
            if _stop_for_newer():
                return None
            # Own-prompt check is here, not inside _poll_take. The in-lock
            # gate (heartbeat) does not call this either.
            _note_held_tick()
            lock_fd = take()
            if lock_fd is not None:
                return lock_fd

            # Another instance holds the lock — decide whether to steal it.
            # Etiquette: only steal if (a) holder PID is gone, (b) holder is THIS
            # session_id (so we replace our own stale lock), or (c) lockfile is
            # stale (mtime > 180s — likely a wedged hook). Otherwise yield
            # so a parallel session doesn't truncate our sibling's playback.
            # A live pid that fails _is_our_hook is a dead holder: not signalled.
            old_pid, old_sid = None, ""
            lock_age = 9999
            try:
                lines, mtime = _read_lock_record()
            except OSError:
                lines = None
            else:
                lock_age = time.time() - mtime
                old_pid = _parse_holder_pid(lines)
                old_sid = lines[1].strip() if len(lines) > 1 else ""
            holder_alive = False
            if old_pid:
                try:
                    os.kill(old_pid, 0)
                except (OSError, OverflowError):
                    holder_alive = False
                else:
                    holder_alive = _is_our_hook(old_pid)
            same_session = bool(old_sid) and old_sid == _SESSION_ID
            may_steal = (not holder_alive) or same_session or lock_age > 180
            if not may_steal:
                now = time.monotonic()
                if budget > 0 and now < deadline:
                    time.sleep(min(VOXTYPE_POLL_S, deadline - now))
                    _note_held_tick()
                    continue
                log(f"Yielding lock: holder pid={old_pid} sid={old_sid[:8]} age={lock_age:.1f}s")
                return None

            if graceful_steal and holder_alive and old_pid:
                if same_session and _stop_for_newer():
                    return None
                children = _player_children(old_pid)
                try:
                    os.kill(old_pid, signal.SIGTERM)
                except OSError:
                    pass
                # A newer arrival during the poll still stops the player we
                # already signalled, then returns. No follow-up poll.
                players_stopped = False
                try:
                    lock_fd = _poll_take(LOCK_STEAL_TERM_WAIT_S)
                    _stop_players(old_pid, children)
                    players_stopped = True
                    if lock_fd is None:
                        lock_fd = _poll_take(LOCK_STEAL_RETRY_S)
                except _NewerArrival:
                    if not players_stopped:
                        _stop_players(old_pid, children)
                    return None
                if lock_fd is not None:
                    return lock_fd
            elif graceful_steal:
                # Dead holder, or a pid that failed verification. Nothing to signal.
                try:
                    lock_fd = _poll_take(LOCK_STEAL_RETRY_S)
                except _NewerArrival:
                    return None
                if lock_fd is not None:
                    return lock_fd
            else:
                # Main's steal: SIGKILL when the holder still verifies, then one
                # immediate retry. No sleep, no /proc directory listing, no SIGTERM.
                if holder_alive and old_pid:
                    try:
                        os.kill(old_pid, 9)
                    except OSError:
                        pass
                lock_fd = take()
                if lock_fd is not None:
                    return lock_fd

            if budget > 0 and time.monotonic() < deadline:
                time.sleep(min(VOXTYPE_POLL_S, max(0.0, deadline - time.monotonic())))
                _note_held_tick()
                continue
            return None
    except _LockRefused as exc:
        log(f"lock refused: {exc.reason}")
        return None


def release_lock(lock_fd):
    """Release lockfile."""
    global _lock_fd
    if lock_fd:
        try:
            lock_fd.seek(0)
            lock_fd.truncate()
            lock_fd.flush()
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except (IOError, OSError):
            pass
    _lock_fd = None


def _reset_held_watch():
    """Watch off, held phase off. One hook run starts clean."""
    global _held_phase, _deferred_sigterm_at
    global _tw_usable, _tw_own_prompt, _tw_path, _tw_dev, _tw_ino
    global _tw_offset, _tw_partial
    _held_phase = False
    _deferred_sigterm_at = None
    _tw_usable = False
    _tw_own_prompt = False
    _tw_path = None
    _tw_dev = None
    _tw_ino = None
    _tw_offset = 0
    _tw_partial = b""


def _end_held_phase():
    """Leave the held phase. A later SIGTERM exits as it does today."""
    global _held_phase
    _held_phase = False


def _lose_transcript_watch():
    """The transcript is no longer the file we baselined. Held phase ends."""
    global _tw_usable
    _tw_usable = False
    _end_held_phase()


def _transcript_open_flags():
    """Read-only, non-blocking, no symlink follow, close on exec."""
    return os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


def _arm_transcript_watch(path):
    """Baseline transcript_path. Any failure leaves the watch off.

    Only a regular file owned by this user is watched. The read offset is
    the size now, so lines already in the file are not prompts.
    """
    global _tw_usable, _tw_own_prompt, _tw_path, _tw_dev, _tw_ino, _tw_offset
    global _tw_partial, _held_phase
    _reset_held_watch()
    if not isinstance(path, str):
        return
    try:
        fd = os.open(path, _transcript_open_flags())
    except (OSError, AttributeError):
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return
        _tw_path = path
        _tw_dev = st.st_dev
        _tw_ino = st.st_ino
        _tw_offset = st.st_size
        _tw_partial = b""
        _tw_own_prompt = False
        _tw_usable = True
        _held_phase = True
    except OSError:
        _reset_held_watch()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _genuine_user_prompt_line(raw):
    """True when one transcript line is a genuine user prompt.

    type user, isMeta and isSidechain not set, origin.kind human when that
    key exists, and message text that is non-empty after stripping leading
    whitespace and does not start with a harness prefix. A tool_result part
    is not a prompt. Anything that does not parse is not a prompt.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(obj, dict) or obj.get("type") != "user":
        return False
    if obj.get("isMeta") or obj.get("isSidechain"):
        return False
    origin = obj.get("origin")
    if isinstance(origin, dict) and "kind" in origin and origin.get("kind") != "human":
        return False
    message = obj.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        chunks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_result":
                return False
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        body = "".join(chunks)
    else:
        return False
    stripped = body.lstrip()
    if not stripped:
        return False
    for prefix in _HARNESS_PREFIXES:
        if stripped.startswith(prefix):
            return False
    return True


def _own_prompt_since_baseline():
    """True when a new complete line is this session's own prompt.

    Reopens the baselined file. A changed device or inode, a file that is
    no longer a regular file of ours, a shrink below the offset, or any
    OSError loses the watch and returns False. At most 1 MiB is read per
    call. Only newline-terminated lines are parsed. The partial tail is
    kept and the offset moves only past complete lines. Never blocks.
    Once an own prompt has been seen, later calls stay True.
    """
    global _tw_offset, _tw_partial, _tw_own_prompt
    if _tw_own_prompt:
        return True
    if not _tw_usable or not isinstance(_tw_path, str):
        return False
    try:
        fd = os.open(_tw_path, _transcript_open_flags())
    except (OSError, AttributeError):
        _lose_transcript_watch()
        return False
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_dev != _tw_dev
            or st.st_ino != _tw_ino
            or st.st_size < _tw_offset
        ):
            _lose_transcript_watch()
            return False
        read_at = _tw_offset + len(_tw_partial)
        if st.st_size < read_at:
            _lose_transcript_watch()
            return False
        blob = b""
        available = st.st_size - read_at
        if available > 0:
            os.lseek(fd, read_at, os.SEEK_SET)
            blob = os.read(fd, min(_TRANSCRIPT_READ_CAP, available))
    except OSError:
        _lose_transcript_watch()
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    data = _tw_partial + blob
    if not data:
        return False
    nl = data.rfind(b"\n")
    if nl < 0:
        # No complete line yet. Keep the tail. Do not move the offset.
        _tw_partial = data
        return False
    complete = data[:nl + 1]
    _tw_partial = data[nl + 1:]
    _tw_offset += len(complete)
    found = False
    for raw in complete.split(b"\n"):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if raw and _genuine_user_prompt_line(raw):
            found = True
    if found:
        _tw_own_prompt = True
    return found


def _note_held_tick():
    """One poll while the line is held and does not own the lock.

    An own prompt drops the line. A lost watch with a pending SIGTERM exits
    the way handle_sigterm does today. A pending SIGTERM that outlives the
    grace is logged once, then the wait continues. No-op when not held, so
    the in-lock gate never reaches the transcript.
    """
    global _deferred_sigterm_at, _held_phase
    if not _held_phase:
        return
    if _own_prompt_since_baseline():
        _held_phase = False
        _deferred_sigterm_at = None
        log(_PROMPT_DROP_LOG)
        raise _OwnPromptDrop()
    if not _tw_usable or not _held_phase:
        pending = _deferred_sigterm_at
        _held_phase = False
        if pending is not None:
            handle_sigterm(signal.SIGTERM, None)
        return
    pending = _deferred_sigterm_at
    if pending is None:
        return
    if time.monotonic() - pending >= HELD_SIGTERM_GRACE_S:
        log(_HELD_KEPT_LOG)
        _deferred_sigterm_at = None


def _honor_deferred_sigterm(lock):
    """After this hook owns the lock, finish a deferred SIGTERM's grace.

    True means speak. False means an own prompt arrived: the lock is
    released and the line is dropped. A lost watch with the SIGTERM still
    pending exits as handle_sigterm does today. No pending mark: no
    transcript read.
    """
    global _deferred_sigterm_at
    pending = _deferred_sigterm_at
    if pending is None:
        return True
    deadline = pending + HELD_SIGTERM_GRACE_S
    while True:
        if _own_prompt_since_baseline():
            log(_PROMPT_DROP_LOG)
            _deferred_sigterm_at = None
            release_lock(lock)
            return False
        if not _tw_usable:
            handle_sigterm(signal.SIGTERM, None)
        now = time.monotonic()
        if now >= deadline:
            _deferred_sigterm_at = None
            return True
        time.sleep(min(0.1, max(0.0, deadline - now)))


# Speakable-summary shape:
#   - Always whole sentences, never mid-cut.
#   - Target 1-2 sentences (quick headsup), absolute max 4.
#   - Soft char target ~280 = ~4 average sentences. If chosen sentences would
#     exceed it, drop sentences from the front until they fit.
#   - If even the LAST single sentence is itself too long to be a clean headsup,
#     scan earlier sentences for a shorter conclusion-shaped line that fits;
#     last resort is a generic "Update ready." rather than truncating mid-word.
# Voice output is a HEADS-UP, not a readout (Codex handoff 2026-06-13, relay #29):
# a deterministic cap so even an over-long <voice> block is reduced to one short
# highlight. ~260 chars / 2 sentences max, status sentences preferred.
SUMMARY_TARGET_CHARS = 250
SUMMARY_MAX_SENTENCES = 2
SUMMARY_HARD_CHARS = 260            # absolute ceiling; never spoken past this
SUMMARY_SOFT_LONG_SENTENCE = 260   # a single sentence longer than this isn't headsup-shaped

# Sentences carrying status/priority signal are preferred over filler when
# choosing what to speak (done / verified / pushed / open ends / needs-you).
_PRIORITY_RE = re.compile(
    r"\b(done|completed?|finished|fixed|wired|installed|verified|tested|pushed|"
    r"committed|shipped|deployed|merged|works|working|ready|blocked|failing|"
    r"failed|error|open end|remaining|next|action needed|needs? you|your call|"
    r"confirm|approve|waiting)\b",
    re.IGNORECASE,
)


def _hard_cap(text: str, limit: int = SUMMARY_HARD_CHARS) -> str:
    """Force text to <= limit chars, cutting at a sentence (then word) boundary
    so the spoken output never trails off mid-word."""
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rsplit('.', 1)[0].strip()
    if len(cut) < 80:
        cut = head.rsplit(' ', 1)[0].strip()
    return cut.rstrip(',:;') + '.'

# Regexes for content that must NEVER reach the speaker (defense against the
# P1 leak findings in 2026-05-19 codex review: unterminated fences, raw
# secrets, bearer-shaped tokens, internal hostnames).
_SECRET_PATTERNS = (
    re.compile(r'\b(?:sk|pk|rk|xoxb|xoxp|xoxa|ghp|gho|ghs|ghr|github_pat|glpat|key|tok|tkn)[_-][A-Za-z0-9_\-]{12,}\b', re.IGNORECASE),
    re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b'),  # JWT
    re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),  # email
    re.compile(r'\b(?:[A-Za-z0-9-]+\.)+(?:internal|local|lan|tail[a-z0-9]+\.ts\.net|onion|consul)\b', re.IGNORECASE),
    re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),  # IPv4
    re.compile(r'\bBearer\s+[A-Za-z0-9._\-]+', re.IGNORECASE),
    re.compile(r'\b[a-f0-9]{32,}\b'),  # long hex (hashes, hex secrets)
)


def extract_voice_block(text: str) -> str:
    """Extract content from <voice>...</voice> tags.

    No <voice> block means SILENCE. Returns "" and main() speaks nothing.

    History: the 2026-05-19 build derived a summary from the visible prose
    instead, so that TTS "never goes silent" when an agent forgot its block.
    That is the wrong trade. Aragorn 2026-08-08: the speaker was reading out
    raw JSON fragments and chunks of the Dutch article draft he was working
    on — '"claims":"claim":"BYD nam in januari 2003 een belang van 77%…' —
    roughly every 25 seconds, because a tool-heavy session produces many turns
    and every one of them was mined for something speakable. His instruction:
    "it should only be the final voice lines".

    The prose fallback could not be made safe by filtering. Any response is
    mostly working material, so whatever it selected was arbitrary by
    construction — it was a guess at a summary the agent had not written.
    Silence is the honest signal that no summary exists, and the missing-block
    sentinel below fixes the cause on the next turn rather than papering over
    it with invented speech every turn.
    """
    # Case-insensitive, attribute-tolerant match for <voice ...>...</voice>.
    match = re.search(r'<voice\b[^>]*>(.*?)</voice\s*>', text, re.DOTALL | re.IGNORECASE)
    if match:
        return _shape_speakable(_redact_secrets(match.group(1).strip()))

    return ""


def extract_voice_lang(text: str):
    """Parse an explicit language declaration from the <voice> tag, e.g.
    <voice lang="nl">. Returns 'nl' or 'en' when declared, None otherwise.

    The agent declaring its own language beats the marker-word heuristic in
    detect_language(), which misfires on short blocks (<3 words => always
    'en') and on Dutch text dense with technical English terms (<15% marker
    ratio => 'en' voice reading Dutch = garbled). S749, 2026-06-10.
    """
    match = re.search(r'<voice\b[^>]*\blang\s*=\s*["\']?([a-zA-Z]{2})', text, re.IGNORECASE)
    if match:
        lang = match.group(1).lower()
        if lang in ('nl', 'en'):
            return lang
    return None


def _strip_unbalanced_block(t: str, open_re: str, close_re: str) -> str:
    """Strip matched open/close pairs first, then drop any tail from a leftover
    open marker to end-of-text. Prevents unterminated ```/``` or `<tool_result>`
    blocks from leaking their contents into the speakable summary.
    """
    t = re.sub(f'{open_re}.*?{close_re}', ' ', t, flags=re.DOTALL)
    t = re.sub(f'{open_re}.*\\Z', ' ', t, flags=re.DOTALL)
    return t


def _split_sentences(t: str) -> list:
    """Split on sentence boundaries, preserve terminal punctuation."""
    parts = re.split(r'(?<=[.!?])\s+', t)
    return [p.strip() for p in parts if p.strip()]


def _shape_speakable(text: str) -> str:
    """Take an already-cleaned string and return a whole-sentence summary that:
       - Ends on a real sentence boundary (no mid-word cuts).
       - Has at most SUMMARY_MAX_SENTENCES sentences.
       - Fits roughly within SUMMARY_TARGET_CHARS for cost control.
       - Falls back to "Update ready." rather than emit a mid-sentence fragment.
    """
    t = re.sub(r'\s+', ' ', text).strip()
    if not t:
        return "Done."

    sentences = _split_sentences(t)
    sentences = [s for s in sentences if len(s) >= 4]
    if not sentences:
        return "Done."

    # If text has no terminal punctuation, the whole thing is one "sentence".
    # Hard-cap it rather than drop the message.
    if len(sentences) == 1 and not sentences[0].endswith(('.', '!', '?')):
        return _hard_cap(sentences[0] if sentences[0].endswith(('.', '!', '?'))
                         else sentences[0] + ".")

    # Already heads-up-shaped (<= max sentences, within the hard cap)? Speak it
    # whole — don't trim a perfectly short 2-sentence block down to one sentence
    # just because the other lacks a status keyword.
    whole = " ".join(sentences)
    if len(sentences) <= SUMMARY_MAX_SENTENCES and len(whole) <= SUMMARY_HARD_CHARS:
        return _hard_cap(whole)

    # Longer block: prefer sentences carrying status/priority signal; if none, all.
    prioritized = [s for s in sentences if _PRIORITY_RE.search(s)]
    pool = prioritized if prioritized else sentences

    # Build from the FRONT (lede-first), in document order. A heads-up leads with
    # the headline ("Done. Main result: X. Open end: Y"), so the first status
    # sentence is the one to speak — NOT the trailing meta/sign-off line.
    picked = []
    char_count = 0
    for s in pool:
        if len(picked) >= SUMMARY_MAX_SENTENCES:
            break
        # Skip individual sentences too long to be a headsup (hard-capped below
        # if they're all we have).
        if len(s) > SUMMARY_SOFT_LONG_SENTENCE:
            if picked:
                break
            continue
        prospective = char_count + len(s) + (1 if picked else 0)
        if picked and prospective > SUMMARY_TARGET_CHARS:
            break
        picked.append(s)
        char_count = prospective
        # Prefer terse: stop after 1 sentence if it's already a substantial headsup.
        if len(picked) == 1 and len(s) >= 120:
            break

    if not picked:
        # Everything exceeded the headsup length — hard-cap the first (or first
        # priority) sentence at a clean boundary rather than dropping the message.
        return _hard_cap(pool[0])

    return _hard_cap(" ".join(picked))


def _redact_secrets(text: str) -> str:
    """Blank out secret-shaped tokens before anything is spoken.

    Until 2026-08-08 this ran only on the auto-derived prose summary, which
    means the text that actually gets spoken almost every turn — the agent's
    own <voice> block — was never scanned. Now it guards the speaking path.
    """
    for pat in _SECRET_PATTERNS:
        text = pat.sub(' ', text)
    return text


def sanitize_for_speech(text: str) -> str:
    """Strip formatting artifacts that sound weird when spoken."""
    text = text.replace('\\n', ' ')
    text = text.replace('\\t', ' ')
    text = text.replace('\\', '')
    text = re.sub(r'[`*_#\[\](){}|~>]', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[~/][a-zA-Z0-9_./-]+', '', text)
    text = re.sub(r'/[a-z_-]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def detect_language(text: str) -> str:
    """Simple heuristic to detect Dutch vs English.
    Returns 'nl' for Dutch, 'en' for everything else."""
    dutch_markers = [
        'dat', 'het', 'een', 'van', 'zijn', 'voor', 'niet', 'maar', 'ook',
        'dit', 'wat', 'aan', 'nog', 'wel', 'naar', 'hier', 'alle', 'waar',
        'moet', 'heel', 'geen', 'klaar', 'gedaan', 'goed', 'alles', 'even',
        'staat', 'wordt', 'wil', 'kan', 'heb', 'bij', 'mij', 'jij', 'zij',
        'zit', 'daar', 'dus', 'nou', 'laten', 'kijk', 'beetje', 'eigenlijk',
    ]
    words = text.lower().split()
    if len(words) < 3:
        return 'en'
    dutch_count = sum(1 for w in words if w.rstrip('.,!?:;') in dutch_markers)
    ratio = dutch_count / len(words)
    return 'nl' if ratio > 0.15 else 'en'


# ---- ENGLISH SPEECH GATE (Aragorn directive 2026-08-03, enforced 2026-08-08)
# The English pin in speak() forces the English *persona*. That alone never
# stopped the garble it was written to stop: an English voice fed Dutch WORDS
# is exactly what Aragorn hears as gibberish. The pin only decided which voice
# read the Dutch out loud.
#
# Observed live 2026-08-08 during the Dutch BYD / Forge Workshop session:
#   [11:25:36] lang 'nl' ignored — voice is English-only (pinned)
#   [11:34:12] lang 'nl' ignored — voice is English-only (pinned)
# Both turns then spoke Dutch text through the English persona.
#
# This gate pins the spoken TEXT, which is the layer the directive is actually
# about. Written Dutch in the response body stays untouched — only what is
# SPOKEN is forced to English.

ENGLISH_BEACON = "Response ready."

# Dutch words that are NOT also English words. Deliberately excludes the
# false-friend set (die, over, even, was, met, van, want, door, tot, in, is,
# of, we, hoe) — those would fire this gate on ordinary English status lines.
_DUTCH_ONLY = frozenset("""
    de het een en dat niet voor maar ook dit wat aan nog wel naar hier alle
    waar moet moeten heel geen alles staat wordt worden wil kan kunnen heb
    hebben bij mij jij zij hij zit daar dus nou laten kijk beetje eigenlijk
    ik je ze zo nu ons onze als dan deze gaat gaan iets ja nee uit zonder
    weer altijd nooit alleen samen tegen tussen onder boven achter naast
    volgens omdat terwijl hoewel zodat waardoor welke wanneer waarom zijn
    klaar gedaan goed gereed bestand bestanden wijziging wijzigingen versie
    regel regels fout fouten vraag antwoord volgende stap stappen artikel
    tekst opgeslagen aangepast toegevoegd verwijderd mislukt gelukt bezig
    afgerond gecontroleerd bijgewerkt geschreven gemaakt verwerkt draait
    werkt uitgevoerd voltooid hersteld ingesteld gestart gestopt toegepast
    beoordeeld nagekeken doorgevoerd samengevat opgezet ontbreekt nodig
    beschikbaar overzicht resultaat resultaten sessie definitief voorstel
    keuze beslissing akkoord wachten openstaand
""".split())

_ENGLISH_ONLY = frozenset("""
    the and are were for with that this these those its from have has had
    been will would should could not but you your they can done saved ready
    fixed updated added removed failed next question answer file files
    change changes version running working complete completed started
    stopped applied reviewed checked wrote written made created processed
    needs needed available overview result results decision choice waiting
    pending error errors line lines step steps article text everything
    nothing again already still both about after before
""".split())


def _lex_scores(text: str):
    """Word list plus Dutch-only and English-only hit counts."""
    words = [w.strip('.,!?:;"\'()[]—-').lower() for w in text.split()]
    words = [w for w in words if w]
    nl = sum(1 for w in words if w in _DUTCH_ONLY)
    en = sum(1 for w in words if w in _ENGLISH_ONLY)
    return words, nl, en


def is_dutch_speech(text: str, lang_hint: str = None) -> bool:
    """True when the text about to be SPOKEN is Dutch.

    Deliberately more sensitive than detect_language(). That one picks a TTS
    engine, where a wrong call costs one mispronounced word; this one is a
    safety gate, where a wrong call costs a whole garbled sentence. A
    self-declared lang="nl" is taken at face value — the model knows what it
    just wrote, and that is the signal that actually fired on 2026-08-08.
    """
    if lang_hint == 'nl':
        return True
    words, nl, en = _lex_scores(text)
    if not words:
        return False
    # Short blocks ("Klaar. Bestand opgeslagen.") never reach a ratio, so
    # judge them on a clean sweep instead.
    if len(words) <= 4:
        return nl >= 1 and en == 0
    return nl >= 2 and nl > en


def _resolve_python3() -> str:
    """A system python3 for the translation subprocess. The hook itself runs
    under the claude-voice venv, which is not guaranteed to carry whatever
    tier2-llm.py imports."""
    for cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3",
                 "/usr/bin/python3"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return sys.executable


def _translate_to_english(text: str, timeout: int = 12):
    """Best-effort Dutch -> English for the spoken line only.

    Routed through tier2-routed.py (consumer tier2-fast, the Model Routing card "Fast text (hooks)":
    Gemini 3.8 Flash @ low thinking first, Aragorn 2026-09-28), whose tier2-llm.py self-loads ~/.secrets/*.env
    — so it works from a hook that inherits no environment at all. Returns None on any failure; callers
    MUST have a non-speaking fallback.
    """
    script = ""
    for cand in ("42/Config/scripts/tier2-routed.py",
                 "vault/Config/scripts/tier2-routed.py",
                 "scripts/tier2-routed.py"):
        path = os.path.join(os.path.expanduser("~"), cand)
        if os.path.isfile(path):
            script = path
            break
    if not script:
        log("english-gate: tier2-routed.py not found — cannot translate")
        return None

    prompt = ("Translate the following short status line into natural spoken "
              "English. Output ONLY the translation — no preamble, no quotes, "
              "no notes.\n\n" + text)
    try:
        proc = subprocess.run(
            [_resolve_python3(), script, "tier2-fast",
             "--prompt", prompt, "--max-tokens", "300"],
            capture_output=True, text=True, timeout=timeout,
            # Leave room for the card's second rung inside the caller's budget.
            env=dict(os.environ, TIER2_ROUTED_RUNG_TIMEOUT=str(max(1, (timeout - 2) // 2))),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"english-gate: translation call failed ({e})")
        return None

    if proc.returncode != 0:
        log(f"english-gate: translator rc={proc.returncode} "
            f"{(proc.stderr or '').strip()[:160]}")
        return None

    out = (proc.stdout or "").strip().strip('"').strip()
    # tier2-llm writes telemetry to stderr, but drop a stray provider banner
    # defensively — it must never be spoken.
    out = re.sub(r'^\[[a-z0-9\-]+/[^\]]+\]\s*', '', out, flags=re.IGNORECASE)
    return out or None


def _record_voice_defect(kind: str, **fields):
    """Leave a breadcrumb so the NEXT turn of this session can correct the
    model at the source. Enforcement on its own — translating Dutch, or going
    silent on a missing block — repairs the symptom every turn and never the
    cause. `kind` selects which correction the reminder emits."""
    try:
        d = os.path.join(os.path.expanduser("~"), ".shelby")
        os.makedirs(d, exist_ok=True)
        payload = {"ts": time.time(), "session_id": _SESSION_ID, "kind": kind}
        payload.update(fields)
        tmp = os.path.join(d, ".voice-lang-drift.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, os.path.join(d, "voice-lang-drift.json"))
    except Exception as e:
        log(f"voice sentinel not recorded ({kind}): {e}")


def _record_lang_drift(text: str, lang_hint):
    _record_voice_defect("dutch", lang_hint=lang_hint, sample=text[:160])


def _record_missing_voice_block():
    _record_voice_defect("missing")


def enforce_english_speech(text: str, lang_hint: str = None) -> str:
    """Guarantee the spoken line is English. Never returns Dutch.

    Order: pass through if already English -> machine-translate -> fall back
    to a short English beacon. The beacon loses the summary, which is bad;
    speaking Dutch through an English persona is worse, and is the exact
    failure this gate exists to remove.
    """
    if not is_dutch_speech(text, lang_hint):
        return text

    _record_lang_drift(text, lang_hint)
    log(f"english-gate: Dutch spoken text caught (lang_hint={lang_hint!r}) "
        f"— translating: {text[:80]!r}")

    translated = _translate_to_english(text)
    if translated and not is_dutch_speech(translated):
        log(f"english-gate: spoke translation instead: {translated[:80]!r}")
        return translated

    if translated:
        log("english-gate: translation came back Dutch — beacon instead")
    else:
        log("english-gate: no translation available — beacon instead")
    return ENGLISH_BEACON


def _enforce_dutch_lane_english_fallback(text: str, lang_hint: str = "nl") -> str:
    """Translate a declared-Dutch lane miss without recording false drift."""
    if not is_dutch_speech(text, lang_hint):
        return text

    log(f"english-gate: Dutch spoken text caught (lang_hint={lang_hint!r}) "
        f"— translating: {text[:80]!r}")
    translated = _translate_to_english(text)
    if translated and not is_dutch_speech(translated):
        log(f"english-gate: spoke translation instead: {translated[:80]!r}")
        return translated

    if translated:
        log("english-gate: translation came back Dutch — beacon instead")
    else:
        log("english-gate: no translation available — beacon instead")
    return ENGLISH_BEACON


def play_audio_file(filepath: str):
    """Play an audio file using the platform-appropriate player."""
    if IS_MACOS:
        subprocess.run(["afplay", filepath], capture_output=True, timeout=30)
    else:
        # Convert to raw PCM and use paplay (WSL/Linux)
        import soundfile as sf
        import numpy as np
        data, srate = sf.read(filepath)
        pcm = (data * 32767).astype(np.int16).tobytes()
        channels = 1 if data.ndim == 1 else data.shape[1]
        play_raw_pcm(pcm, srate, channels)


def _voxtype_state_path() -> str:
    """State file path. SHELBY_VOXTYPE_STATE_FILE overrides the runtime default."""
    override = os.environ.get("SHELBY_VOXTYPE_STATE_FILE")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(runtime, "voxtype", "state")


def _read_small_regular_file(path: str, limit: int = 64) -> bytes | None:
    """At most `limit` bytes of a regular file, or None. Never blocks.

    O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC: a FIFO, symlink, directory, or other
    non-regular path returns None (ELOOP, ENXIO, EAGAIN, or not S_ISREG)
    instead of waiting for a writer.
    """
    flags = (
        os.O_RDONLY
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        return os.read(fd, limit)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _voxtype_state_text() -> str:
    """At most 64 bytes of the state file, stripped and lowercased. Unreadable = empty."""
    raw = _read_small_regular_file(_voxtype_state_path())
    if raw is None:
        return ""
    return raw.decode("utf-8", "replace").strip().lower()


def _voxtype_pid_is_dead(state_path: str) -> bool:
    """True only when the sibling pid file proves the daemon is gone.

    A missing, unreadable, or non-numeric pid file is ignored (not proof of death).
    PermissionError from os.kill means the process exists.
    """
    pid_path = os.path.join(os.path.dirname(state_path), "pid")
    raw = _read_small_regular_file(pid_path)
    if raw is None:
        return False
    try:
        pid = int(raw.decode("utf-8", "replace").strip())
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except (OverflowError, ValueError):
        # A pid that does not fit the OS pid type is unusable, not proof the
        # daemon died. Ignore the pid file and trust the state.
        return False
    except OSError:
        return False
    return False


_VOXTYPE_WAIT_DISABLED = frozenset({"false", "0", "no", "off"})


def _voxtype_wait_enabled(value) -> bool:
    """True unless config explicitly switches the VoxType wait off.

    Disabled for False, 0, and the strings false/0/no/off (case and surrounding
    space ignored). A missing value and everything else stay enabled.
    """
    if value is False or value == 0:
        return False
    if isinstance(value, str) and value.strip().lower() in _VOXTYPE_WAIT_DISABLED:
        return False
    return True


def voxtype_present() -> bool:
    """True when a VoxType state file exists and the gate is active."""
    if IS_MACOS or not _VOXTYPE_WAIT_ENABLED:
        return False
    return os.path.exists(_voxtype_state_path())


def _voxtype_busy_state() -> str | None:
    """Busy state from one read, or None when VoxType is not busy.

    macOS and the kill switch return None without touching the filesystem.
    """
    if IS_MACOS or not _VOXTYPE_WAIT_ENABLED:
        return None
    state_path = _voxtype_state_path()
    state = _voxtype_state_text()
    if state not in VOXTYPE_BUSY_STATES:
        return None
    if _voxtype_pid_is_dead(state_path):
        return None
    return state


def voxtype_busy() -> bool:
    """True only for a live daemon in recording or transcribing."""
    return _voxtype_busy_state() is not None


def _wait_for_voxtype_idle_status(max_wait_s=None, heartbeat=True) -> tuple[float, bool]:
    """Return (seconds waited, cap_fired).

    cap_fired is True only when the cap elapsed while VoxType was still busy.
    Playback arming uses that flag instead of a fresh read after the player
    starts. Already-idle returns (0.0, False) after the single busy read.
    max_wait_s <= 0 while busy logs the cap line once and returns (0.0, True)
    without sleeping.
    heartbeat=False skips the lockfile mtime refresh. The pre-lock wait uses
    that: it does not own the lock, and a refresh would hide a wedged holder
    from the stale-lock rule. When heartbeat is true the refresh is
    os.utime on the held lock fd, and it is skipped when that fd is absent,
    so the touch cannot follow a symlink.
    """
    if IS_MACOS or not _VOXTYPE_WAIT_ENABLED:
        return 0.0, False
    if max_wait_s is None:
        max_wait_s = VOXTYPE_MAX_WAIT_S
    state = _voxtype_busy_state()
    if state is None:
        return 0.0, False
    # An exhausted budget must not sleep and must not start a second wait.
    # The caller that already logged the cap does not call this again.
    if max_wait_s <= 0:
        log(f"voxtype: cap reached after {max_wait_s:g}s, speaking anyway")
        return 0.0, True
    heartbeat_s = VOXTYPE_HEARTBEAT_S
    poll_s = VOXTYPE_POLL_S
    log(f"voxtype: waiting (state={state})")
    started = time.monotonic()
    last_beat = started
    while True:
        time.sleep(poll_s)
        # heartbeat=True is the in-lock gate. It must not touch the transcript.
        # The pre-lock wait (heartbeat=False) checks once per poll tick.
        if not heartbeat:
            _note_held_tick()
        now = time.monotonic()
        if heartbeat and now - last_beat >= heartbeat_s:
            lock_fd = _lock_fd
            if lock_fd is not None:
                try:
                    os.utime(lock_fd.fileno())
                except (OSError, ValueError):
                    pass
            last_beat = now
        elapsed = now - started
        if _voxtype_busy_state() is None:
            log(f"voxtype: idle after {elapsed:.1f}s")
            return elapsed, False
        if elapsed >= max_wait_s:
            log(f"voxtype: cap reached after {max_wait_s:g}s, speaking anyway")
            return elapsed, True


def wait_for_voxtype_idle(max_wait_s=None, heartbeat=True) -> float:
    """Block until VoxType is idle. Return seconds waited (0.0 when already idle).

    max_wait_s defaults to the module VOXTYPE_MAX_WAIT_S at call time.
    heartbeat defaults to True: while waiting with the lock held, the lockfile
    mtime is refreshed via os.utime on that fd so a sibling hook does not
    treat the wait as a wedged process. No fd means no refresh. Pass
    heartbeat=False when the caller does not hold the lock.
    """
    elapsed, _cap_fired = _wait_for_voxtype_idle_status(
        max_wait_s, heartbeat=heartbeat,
    )
    return elapsed


def _feed_pcm(proc, pcm_data: bytes):
    """Write PCM to paplay's stdin from a side thread so the caller can poll."""
    stdin = proc.stdin
    try:
        stdin.write(pcm_data)
    except (BrokenPipeError, OSError):
        pass
    try:
        stdin.close()
    except (BrokenPipeError, OSError):
        pass


def _stop_player(proc) -> bool:
    """Stop proc. True only if terminate() was sent while it was still running.

    An already-exited player, including one reaped by an external pkill, is
    not our interruption. A player we did signal is waited on for at most 1s
    and killed if it is still alive.
    """
    signalled = False
    try:
        if proc.poll() is None:
            proc.terminate()
            signalled = True
    except OSError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    return signalled


def _kill_player(proc):
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _play_one_paplay(argv, pcm_data: bytes, interrupt_armed: bool,
                     timeout: float = 30.0) -> bool:
    """One paplay attempt. True only if we stopped a player that was running.

    interrupt_armed comes from why the wait ended, not from a read after
    Popen. False when the cap fired: play through that busy stretch, and arm
    only after a later non-busy read. True when the wait ended idle: arm from
    the first poll. A player that has already exited, including voice-shutup's
    pkill, is not an interruption and is not replayed.
    """
    global _current_player
    # Block SIGTERM across spawn and registration. Otherwise a shutup signal
    # in that window would exit without a player to terminate.
    previous_mask = None
    if hasattr(signal, "pthread_sigmask"):
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        popen_kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if previous_mask is not None:
            def _restore_child_mask(mask=previous_mask):
                # The child inherits the blocked mask. Restore the parent's
                # previous mask so paplay still dies on SIGTERM.
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)

            popen_kwargs["preexec_fn"] = _restore_child_mask
        proc = subprocess.Popen(argv, **popen_kwargs)
        _current_player = proc
    finally:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    interrupted_by_us = False
    try:
        feeder = threading.Thread(target=_feed_pcm, args=(proc, pcm_data), daemon=True)
        feeder.start()
        poll_s = VOXTYPE_POLL_S
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_player(proc)
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                proc.wait(timeout=min(poll_s, remaining))
            except subprocess.TimeoutExpired:
                # Finished on its own before we looked at dictation: not ours.
                if proc.poll() is not None:
                    break
                busy = voxtype_busy()
                if not busy:
                    interrupt_armed = True
                elif interrupt_armed:
                    interrupted_by_us = _stop_player(proc)
                    break
            else:
                break
        return interrupted_by_us
    finally:
        if _current_player is proc:
            _current_player = None


def _play_pcm_voxtype_gated(pcm_data: bytes, srate: int, channels: int,
                            timeout: float = 30.0):
    """Wait for VoxType, play, and replay the same PCM at most once.

    The first wait uses the hook-run budget when main() armed one. The
    replay wait keeps its own full cap.
    """
    global _current_player
    try:
        _elapsed, cap_fired = _first_voxtype_gate_wait()
        argv = ["paplay", "--raw", f"--rate={srate}", f"--channels={channels}", "--format=s16le"]
        interrupt_armed = not cap_fired
        for attempt in (1, 2):
            interrupted = _play_one_paplay(
                argv, pcm_data, interrupt_armed, timeout=timeout,
            )
            if not interrupted:
                return
            if attempt == 1:
                log("voxtype: dictation started during playback, stopped; will replay once")
                _elapsed, cap_fired = _wait_for_voxtype_idle_status()
                interrupt_armed = not cap_fired
                continue
            log("voxtype: dictation interrupted the replay, line dropped")
            return
    finally:
        _current_player = None


def play_raw_pcm(pcm_data: bytes, srate: int, channels: int):
    """Play raw PCM data via paplay (WSL/Linux only)."""
    if IS_MACOS:
        # Write to temp wav and play with afplay
        import struct
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            # Write WAV header + data
            bits_per_sample = 16
            byte_rate = srate * channels * bits_per_sample // 8
            block_align = channels * bits_per_sample // 8
            data_size = len(pcm_data)
            tmp.write(b'RIFF')
            tmp.write(struct.pack('<I', 36 + data_size))
            tmp.write(b'WAVE')
            tmp.write(b'fmt ')
            tmp.write(struct.pack('<IHHIIHH', 16, 1, channels, srate, byte_rate, block_align, bits_per_sample))
            tmp.write(b'data')
            tmp.write(struct.pack('<I', data_size))
            tmp.write(pcm_data)
            tmp.close()
            subprocess.run(["afplay", tmp.name], capture_output=True, timeout=30)
        finally:
            os.unlink(tmp.name)
        return
    if not voxtype_present():
        subprocess.run(
            ["paplay", "--raw", f"--rate={srate}", f"--channels={channels}", "--format=s16le"],
            input=pcm_data, capture_output=True, timeout=30
        )
    else:
        _play_pcm_voxtype_gated(pcm_data, srate, channels)


async def speak_edge(text: str, voice: str, speed: str, remote_target: str = None,
                     play_local: bool = True, remote_requires_off_lan: bool = False,
                     remote_fallback_target: str = None):
    """Edge TTS — free, cloud-based.

    play_local: if False, do NOT fall through to local playback. Used when the
    machine's default output is a virtual device with no working forwarding
    chain — playing would generate silent audio.
    """
    import edge_tts

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        communicate = edge_tts.Communicate(text, voice, rate=speed)
        await communicate.save(tmp_path)

        if remote_target:
            import soundfile as sf
            import numpy as np
            data, srate = sf.read(tmp_path)
            pcm = (data * 32767).astype(np.int16).tobytes()
            channels = 1 if data.ndim == 1 else data.shape[1]
            wav_data = make_wav(pcm, srate, channels)
            if send_audio_remote(
                wav_data, remote_target, require_off_lan=remote_requires_off_lan,
                fallback_target=remote_fallback_target,
            ):
                return
            if not play_local:
                log("Remote send failed and play_local=False — not falling back to local")
                return

        if not play_local:
            return

        if IS_MACOS:
            play_audio_file(tmp_path)
        else:
            import soundfile as sf
            import numpy as np
            data, srate = sf.read(tmp_path)
            pcm = (data * 32767).astype(np.int16).tobytes()
            channels = 1 if data.ndim == 1 else data.shape[1]
            play_raw_pcm(pcm, srate, channels)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# Silence padding (ms) prepended/appended to synthesized PCM. Covers audio-device
# warmup (start clip — the dropped first word, e.g. "Done") and buffer drain on
# stream end (tail clip — the cut-off last word). Applies to both the remote-WAV
# path and local afplay/paplay. Tunable via config keys tts_pad_lead_ms / tts_pad_tail_ms.
PAD_LEAD_MS = 300
PAD_TAIL_MS = 500


def speak_elevenlabs_streaming(text: str, voice_id: str, model: str, api_key: str,
                              speed: float = 1.0, remote_target: str = None,
                              play_local: bool = True, lead_ms: int = PAD_LEAD_MS,
                              tail_ms: int = PAD_TAIL_MS, primer_amp: int = 0,
                              remote_requires_off_lan: bool = False,
                              remote_fallback_target: str = None):
    """ElevenLabs with streaming via raw HTTP — supports speed parameter.

    play_local: if False, do NOT fall through to local playback.
    Caller should already have called find_audible_path() and gated the
    ElevenLabs API call entirely if neither remote nor local were audible.
    """
    import requests

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "text": text,
        "model_id": model,
    }
    if speed != 1.0:
        body["speed"] = speed

    resp = requests.post(url, json=body, headers=headers, stream=True, timeout=(10, 90))
    resp.raise_for_status()

    if remote_target:
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        pcm_data = pad_pcm(pcm_data, 24000, 1, lead_ms, tail_ms, primer_amp)
        wav_data = make_wav(pcm_data, 24000, 1)
        if send_audio_remote(
            wav_data, remote_target, require_off_lan=remote_requires_off_lan,
            fallback_target=remote_fallback_target,
        ):
            return
        if not play_local:
            log("Remote send failed and play_local=False — not falling back to local")
            return
        # play_local=True: fall through to local playback below
        play_raw_pcm(pcm_data, 24000, 1)
        return

    if not play_local:
        return

    if IS_MACOS:
        # Collect all PCM data, write to temp WAV, play with afplay
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        pcm_data = pad_pcm(pcm_data, 24000, 1, lead_ms, tail_ms, primer_amp)
        play_raw_pcm(pcm_data, 24000, 1)
    elif voxtype_present():
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        # 180s = ~3 min of speech. Was 30s, which clipped any reply over
        # ~470 chars mid-sentence and then spoke the line again via edge.
        # voice-shutup.sh still kills paplay by name on the next prompt.
        _play_pcm_voxtype_gated(pcm_data, 24000, 1, timeout=180)
    else:
        # Stream directly to paplay (WSL/Linux)
        proc = subprocess.Popen(
            ["paplay", "--raw", "--rate=24000", "--channels=1", "--format=s16le"],
            stdin=subprocess.PIPE
        )
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    proc.stdin.write(chunk)
            proc.stdin.close()
            # 180s = ~3 min of speech. Was 30s, which clipped any reply
            # over ~470 chars mid-sentence. voice-shutup.sh still kills the
            # process by name on the next user prompt, so this only governs
            # the natural end-of-playback wait.
            proc.wait(timeout=180)
        except Exception:
            proc.kill()


def speak_kokoro(text: str, voice: str, speed: float = 1.0):
    """Kokoro TTS — local, free, good quality."""
    try:
        from kokoro import KPipeline
        import numpy as np
    except ImportError:
        log("Kokoro not installed, falling back to Edge TTS")
        return False

    try:
        lang = 'a'  # American English default
        if detect_language(text) == 'nl':
            lang = 'a'  # Kokoro doesn't have native Dutch yet, use English

        pipeline = KPipeline(lang_code=lang)
        samples_list = []

        for _, _, audio in pipeline(text, voice=voice, speed=speed):
            if audio is not None:
                samples_list.append(audio.numpy() if hasattr(audio, 'numpy') else audio)

        if not samples_list:
            return False

        import numpy as np
        audio_data = np.concatenate(samples_list)
        pcm = (audio_data * 32767).astype(np.int16).tobytes()
        play_raw_pcm(pcm, 24000, 1)
        return True
    except Exception as e:
        log(f"Kokoro error: {e}")
        return False


REMOTE_AUDIO_PORT = 12345

def _wav_tempo(wav_data: bytes, speed) -> bytes:
    """Speed up a WAV without changing pitch (ffmpeg atempo). pocket-tts has no
    speed parameter, so `pocket_speed` in config.json is applied here. Any
    failure returns the original audio: a slower voice beats a silent one."""
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        log(f"pocket_speed {speed!r} is not a number; playing at 1.0x")
        return wav_data
    if not speed or abs(speed - 1.0) < 0.01 or not 0.5 <= speed <= 2.0:
        return wav_data
    ffmpeg = next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
                   if os.path.exists(p)), "ffmpeg")
    out = None
    try:
        # Output to a file, not a pipe: piped WAV carries no final length header.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            out = f.name
        r = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "wav", "-i", "pipe:0", "-filter:a", f"atempo={speed:.3f}", out],
                           input=wav_data, capture_output=True, timeout=10)
        data = open(out, "rb").read() if r.returncode == 0 else b""
        if len(data) > 44:
            return data
        log(f"pocket speed {speed}: ffmpeg rc={r.returncode}; playing at 1.0x")
    except Exception as e:
        log(f"pocket speed {speed}: {e}; playing at 1.0x")
    finally:
        if out:
            try:
                os.unlink(out)
            except OSError:
                pass
    return wav_data


def _pad_wav_tail(wav_data: bytes, tail_ms: int = 1000) -> bytes:
    """Append PCM silence so Windows playback drains after the final phoneme."""
    import io
    import wave

    if tail_ms <= 0:
        return wav_data
    try:
        source = io.BytesIO(wav_data)
        with wave.open(source, "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(params.nframes)
        silent_frames = int(params.framerate * tail_ms / 1000)
        silence = b"\x00" * silent_frames * params.nchannels * params.sampwidth
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setparams(params)
            writer.writeframes(frames + silence)
        return output.getvalue()
    except Exception as e:
        log(f"Could not pad Pocket WAV tail: {e}")
        return wav_data


def _pocket_fetch(text: str, voice: str, base_url: str, timeout: int) -> bytes | None:
    """One synth request against a single endpoint.

    Returns WAV bytes, or None if THIS endpoint is unusable (unreachable, or a
    payload too small to be real audio). None means "try somewhere else" — it
    is deliberately distinct from a playback failure, so a dead endpoint can
    fall through to a peer instead of dropping straight to a generic voice.
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            f"{base_url}/tts",
            data=json.dumps({"text": text, "voice": voice}).encode(),
            headers={"Content-Type": "application/json"})
        wav_data = urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"pocket-tts request to {base_url} failed: {e}")
        return None
    if not wav_data or len(wav_data) < 1000:
        log(f"pocket-tts {base_url} returned suspiciously small payload "
            f"({len(wav_data)} bytes)")
        return None
    return wav_data


def _is_riff_wav(wav_data: bytes) -> bool:
    """Return whether a payload has the minimum RIFF/WAVE signature."""
    return (
        isinstance(wav_data, (bytes, bytearray))
        and len(wav_data) >= 12
        and wav_data[:4] == b"RIFF"
        and wav_data[8:12] == b"WAVE"
    )


def _omnivoice_fetch(text: str, voice: str, base_url: str,
                     timeout: float) -> bytes | None:
    """Fetch bounded WAV audio from the Dutch lane without changing Pocket."""
    try:
        payload = json.dumps({"text": text, "voice": voice}).encode()
        wav_data = _omnivoice_post_json(base_url, payload, timeout)
    except Exception as exc:
        log(f"OmniVoice request to {base_url} failed: {exc}")
        return None

    if len(wav_data) < 1000:
        log(f"OmniVoice {base_url} returned suspiciously small payload "
            f"({len(wav_data)} bytes)")
        return None
    if not _is_riff_wav(wav_data):
        log(f"OmniVoice {base_url} returned a non-RIFF WAV payload")
        return None
    return wav_data


def speak_pocket(text: str, voice: str, remote_target: str = None, play_local: bool = True,
                 base_url: str = "http://127.0.0.1:8933",
                 fallback_local: bool | None = None, tail_ms: int = 1000,
                 remote_requires_off_lan: bool = False,
                 remote_fallback_target: str = None,
                 fallback_base_url: str = None, speed=1.0) -> bool:
    """pocket-tts synthesis over HTTP. Returns True on success.

    English-only engine — callers must route 'nl' elsewhere. WAV comes back
    whole (no streaming), so latency ≈ generation time (~1-2s for a voice block).

    Two endpoints, in order: `base_url` then `fallback_base_url`. Dawn/Dusk run
    a local CPU server AND can reach Day's over Tailscale, so whichever is
    primary for that machine, the other one covers its outage — the JARVIS
    persona survives instead of degrading to a stock Edge voice.
    """
    # Cloned voices can take longer than eight seconds on a cold model.
    # Keep this aligned with the Codex adapter so neither runtime silently
    # falls back to a generic Edge voice for the same machine identity.
    timeout = int(os.environ.get("SHELBY_POCKET_TIMEOUT", "27"))
    wav_data = _pocket_fetch(text, voice, base_url, timeout)
    if wav_data is None and fallback_base_url and fallback_base_url != base_url:
        log(f"pocket-tts primary {base_url} unusable — trying {fallback_base_url}")
        wav_data = _pocket_fetch(text, voice, fallback_base_url, timeout)
    if wav_data is None:
        return False
    wav_data = _pad_wav_tail(_wav_tempo(wav_data, speed), tail_ms=tail_ms)
    if fallback_local is None:
        fallback_local = play_local

    ok = False
    remote_ok = False
    if remote_target:
        remote_ok = send_audio_remote(
            wav_data, remote_target, require_off_lan=remote_requires_off_lan,
            fallback_target=remote_fallback_target,
        )
        ok = remote_ok or ok
        if remote_ok and not play_local:
            return True

    should_play_local = play_local or (bool(remote_target) and not remote_ok and fallback_local)
    if should_play_local:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_data)
                tmp_path = f.name
            play_audio_file(tmp_path)
            ok = True
        except Exception as e:
            log(f"pocket-tts local playback failed: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    return ok


# Audible-path cache — avoids re-probing receivers on every turn.
# Bust with: rm /tmp/voice-audible-cache.json
AUDIBLE_CACHE_PATH = "/tmp/voice-audible-cache.json"
AUDIBLE_CACHE_TTL = 30  # seconds


def _probe_url(url: str, timeout: float = 0.5) -> bool:
    """Quick HTTP GET probe. Returns True on 2xx, False otherwise."""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


ROUTE_LEGACY = "legacy"
ROUTE_ORIGIN = "origin"
ROUTE_DUSK = "dusk"


def _source_machine(cfg: dict) -> str:
    """Return a stable fleet identity; config is authoritative over hostname."""
    import socket

    configured = str(cfg.get("source_machine") or cfg.get("machine") or "").strip().lower()
    if configured:
        return configured
    return socket.gethostname().split(".", 1)[0].strip().lower()


def _fetch_health_payload(url: str, timeout: float, attempts: int) -> dict | None:
    """Fetch one versioned receiver payload with bounded transient retries."""
    import urllib.request

    payload = None
    for _attempt in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not 200 <= resp.status < 300:
                    return None
                body = resp.read(65536)
            payload = json.loads(body)
            break
        except Exception:
            continue

    return payload if isinstance(payload, dict) else None


def _receiver_health(url: str, expected_receiver: str, timeout: float = 1.0,
                     attempts: int = 2, home_network_names: tuple = ("AragornHQ",),
                     max_location_age_seconds: float = 30.0) -> dict | None:
    """Validate a Dusk receiver's identity, readiness, and tri-state location."""
    payload = _fetch_health_payload(url, timeout, attempts)
    if not payload or payload.get("schema_version") != "shelby.voice-receiver.health.v1":
        return None
    receiver_id = str(payload.get("receiver_id") or "").split(".", 1)[0].strip().lower()
    if receiver_id != expected_receiver.strip().lower():
        return None
    if payload.get("audio_ready") is not True or payload.get("location_ready") is not True:
        return None
    if not isinstance(payload.get("home_lan"), bool):
        return None
    profiles = payload.get("network_profiles")
    if not isinstance(profiles, list) or not profiles or not all(
        isinstance(name, str) and name.strip() for name in profiles
    ):
        return None
    physical_profiles = payload.get("physical_network_profiles")
    if not isinstance(physical_profiles, list) or not all(
        isinstance(name, str) and name.strip() for name in physical_profiles
    ):
        return None
    if any(name not in profiles for name in physical_profiles):
        return None
    age = payload.get("location_age_seconds")
    if not isinstance(age, (int, float)) or not 0 <= age <= max_location_age_seconds:
        return None
    home_names = {str(name).strip().casefold() for name in home_network_names if str(name).strip()}
    computed_home = any(name.strip().casefold() in home_names for name in physical_profiles)
    if payload["home_lan"] != computed_home:
        return None
    state = payload.get("location_state")
    if state not in {"home", "away"}:
        return None
    if state == "home" and not payload["home_lan"]:
        return None
    if state == "away" and (payload["home_lan"] or not physical_profiles):
        return None
    return payload


def _origin_receiver_health(url: str, expected_receiver: str,
                            timeout: float = 0.75, attempts: int = 2):
    """Return valid JSON, 'legacy', False-invalid, or None-unavailable."""
    import urllib.request

    for _attempt in range(max(1, attempts)):
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    return False
                body = response.read(65536)
                headers = getattr(response, "headers", None)
                content_type = str(headers.get("Content-Type", "")) if headers else ""
        except Exception:
            continue
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            text = body.decode("utf-8", errors="replace").strip()
            if text == "OK" and (not content_type or content_type.lower().startswith("text/plain")):
                return "legacy"
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != "shelby.voice-receiver.health.v1":
            return False
        receiver_id = str(payload.get("receiver_id") or "").split(".", 1)[0].strip().lower()
        if receiver_id != expected_receiver.strip().lower():
            return False
        if payload.get("audio_ready") is not True:
            return False
        return payload
    return None


def _is_loopback_url(url: str) -> bool:
    import urllib.parse

    try:
        host = (urllib.parse.urlparse(url).hostname or "").casefold()
    except ValueError:
        return False
    return host in {"127.0.0.1", "::1", "localhost"}


def resolve_dusk_presence_route(cfg: dict) -> tuple[str, str | None]:
    """Return an authoritative legacy/origin/Dusk route decision.

    When enabled, ambiguous or unavailable Dusk state fails safely to the
    originating machine. Only a versioned, identity-checked Dusk receiver can
    redirect Day or Dawn off AragornHQ.
    """
    policy = cfg.get("dusk_presence_routing")
    if not isinstance(policy, dict) or not policy.get("enabled", False):
        return ROUTE_LEGACY, None

    source = _source_machine(cfg)
    origin_target = str(policy.get("origin_target") or "").strip() or None
    if source == "dusk":
        log("Source is Dusk — keeping Dusk voice on Dusk")
        return ROUTE_ORIGIN, origin_target
    if source not in {"day", "dawn"}:
        log(f"Unknown source machine {source!r} — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target

    off_lan_target = str(policy.get("off_lan_target") or policy.get("away_target") or "").strip()
    if not off_lan_target:
        log("Dusk off-LAN target is unset — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target
    health_url = str(
        policy.get("off_lan_health_url")
        or policy.get("away_health_url")
        or off_lan_target.replace("/tts", "/health")
    )
    timeout = float(policy.get("probe_timeout_seconds", 1.0))
    attempts = int(policy.get("probe_attempts", 2))
    home_network_names = tuple(policy.get("home_network_names", ["AragornHQ"]))
    max_location_age = float(policy.get("max_location_age_seconds", 30.0))
    health = _receiver_health(
        health_url, expected_receiver="dusk", timeout=timeout, attempts=attempts,
        home_network_names=home_network_names,
        max_location_age_seconds=max_location_age,
    )
    if health is None:
        log("Dusk location/readiness is unknown — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target
    if health["home_lan"]:
        log("Dusk receiver reports AragornHQ — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target

    log(f"Dusk receiver reports off AragornHQ LAN — routing audio to {off_lan_target}")
    return ROUTE_DUSK, off_lan_target


def resolve_dusk_presence_target(cfg: dict) -> str | None:
    """Compatibility wrapper returning only a confirmed Dusk remote target."""
    route, target = resolve_dusk_presence_route(cfg)
    return target if route == ROUTE_DUSK else None


def is_local_output_real() -> bool:
    """True if the machine's default audio output is a real speaker, False if a
    virtual device (BlackHole, monitor, null sink, loopback) that needs an
    external forwarding chain to be audible.

    Conservative default: True on any detection error — better to attempt
    playback than to silently suppress when we're unsure.
    """
    if IS_MACOS:
        try:
            result = subprocess.run(
                ["system_profiler", "SPAudioDataType"],
                capture_output=True, text=True, timeout=3
            )
            current_device = None
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.endswith(":") and line.startswith("        ") and not line.startswith("          "):
                    current_device = stripped.rstrip(":")
                if "Default Output Device: Yes" in line and current_device:
                    return not any(v in current_device for v in
                                   ("BlackHole", "Virtual", "Loopback", "Aggregate", "Multi-Output"))
        except Exception:
            return True
        return True
    else:
        try:
            result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2)
            for line in result.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip().lower()
                    return not any(v in sink for v in ("null", "monitor", "dummy"))
        except Exception:
            return True
        return True


def find_audible_path(cfg: dict) -> tuple:
    """Determine whether TTS will actually be heard, and where.

    Returns (remote_url, play_local):
      - remote_url: str | None — confirmed-healthy receiver URL, else None
      - play_local: bool — True iff local playback would be audible
                    (real speaker, OR virtual output with healthy forwarding)

    If both falsy → suppress TTS entirely (saves ElevenLabs tokens).

    Cached for `audible_path_cache_ttl_seconds` (default 30s).
    """
    now = time.time()
    cache_ttl = cfg.get("audible_path_cache_ttl_seconds", AUDIBLE_CACHE_TTL)
    policy = cfg.get("dusk_presence_routing")
    policy_enabled = isinstance(policy, dict) and policy.get("enabled", False)

    if not policy_enabled:
        try:
            with open(AUDIBLE_CACHE_PATH) as f:
                cache = json.load(f)
            if now - cache.get("ts", 0) < cache_ttl:
                return cache.get("remote_url"), bool(cache.get("play_local", False))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    remote_url = None
    local_ips = get_local_ips()

    if cfg.get("remote_audio", False):
        route, policy_target = resolve_dusk_presence_route(cfg)
        if route == ROUTE_DUSK:
            remote_url = policy_target
        elif route == ROUTE_ORIGIN:
            if policy_target:
                health_url = policy_target.replace("/tts", "/health")
                source = _source_machine(cfg)
                origin_health = _origin_receiver_health(
                    health_url,
                    expected_receiver=source,
                    timeout=float(policy.get("origin_probe_timeout_seconds", 0.75)),
                    attempts=int(policy.get("origin_probe_attempts", 2)),
                )
                legacy_origin_ok = (
                    origin_health == "legacy"
                    and _is_loopback_url(policy_target)
                    and policy.get("allow_legacy_loopback_origin_health", True)
                )
                if isinstance(origin_health, dict) or legacy_origin_ok:
                    remote_url = policy_target
                    log(f"Using same-machine receiver {policy_target}")
                else:
                    log(f"Same-machine receiver {policy_target} unavailable — trying direct local playback")
        else:
            cfg_target = (cfg.get("remote_audio_target") or "").strip()
            if cfg_target:
                health_url = cfg_target.replace("/tts", "/health")
                if _probe_url(health_url, timeout=0.5):
                    remote_url = cfg_target
                    log(f"Explicit remote_audio_target {cfg_target} healthy")
                else:
                    log(f"Explicit remote_audio_target {cfg_target} DOWN — falling through to receivers list")

        if route == ROUTE_LEGACY and not remote_url:
            for recv in cfg.get("remote_audio_receivers", []):
                ip = (recv.get("ip") or "").strip()
                if not ip or ip in local_ips:
                    continue
                if recv.get("protocol", "http") != "http":
                    continue
                port = recv.get("port", REMOTE_AUDIO_PORT)
                if _probe_url(f"http://{ip}:{port}/health", timeout=0.5):
                    remote_url = f"http://{ip}:{port}/tts"
                    log(f"Auto-discovered receiver: {recv.get('name', ip)} ({remote_url})")
                    break

    play_local = False
    if is_local_output_real():
        play_local = True
    else:
        fwd = cfg.get("local_audio_forward")
        if fwd:
            ip = (fwd.get("ip") or "").strip()
            port = fwd.get("port", REMOTE_AUDIO_PORT)
            health_url = fwd.get("health_url") or f"http://{ip}:{port}/health"
            if _probe_url(health_url, timeout=0.5):
                fwd_tts_url = f"http://{ip}:{port}/tts"
                if remote_url and remote_url == fwd_tts_url:
                    play_local = False
                    log(f"Remote target == forward target ({fwd_tts_url}) — dropping local to avoid double playback")
                else:
                    play_local = True
                    log(f"Virtual local output, forwarding to {fwd.get('name', ip)} healthy — local audible")
            else:
                log(f"Virtual local output, forwarding target {fwd.get('name', ip)} DOWN — local muted")
        else:
            log("Virtual local output with no local_audio_forward configured — local muted")

    if not policy_enabled:
        try:
            with open(AUDIBLE_CACHE_PATH, "w") as f:
                json.dump({"ts": now, "remote_url": remote_url, "play_local": play_local}, f)
        except Exception:
            pass

    return remote_url, play_local


def get_local_ips() -> set:
    """Get this machine's IP addresses to avoid sending audio to ourselves."""
    import socket
    ips = {"127.0.0.1", "::1"}
    try:
        # Get all addresses for this hostname
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    # Also try connecting to a remote address to find our Tailscale IP
    for probe in ["100.77.19.108", "100.99.87.61", "100.96.47.104"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def get_remote_audio_target(cfg: dict) -> str | None:
    """If remote_audio is enabled, find the right receiver automatically.

    Works with SSH, MOSH, Zellij, tmux, or any remote access method.

    Strategy:
    1. REMOTE_AUDIO_TARGET env var (hard override)
    2. If SSH_CLIENT set, prefer receiver matching SSH origin IP
    3. Auto-discover: probe all receivers, skip self, first healthy wins
    """
    if not cfg.get("remote_audio", False):
        return None

    port = cfg.get("remote_audio_port", REMOTE_AUDIO_PORT)

    # 1. Env var override (for edge cases or debugging)
    env_target = os.environ.get("REMOTE_AUDIO_TARGET", "")
    if env_target:
        return env_target if env_target.startswith("http") else f"http://{env_target}:{port}/tts"

    # 2. Explicit config target (set by /tts dawn|dusk|local)
    cfg_target = cfg.get("remote_audio_target", "")
    if cfg_target:
        log(f"Using explicit remote_audio_target from config: {cfg_target}")
        return cfg_target

    # 2. Build receiver list from config
    receivers = cfg.get("remote_audio_receivers", [
        {"name": "Dawn", "ip": "100.77.19.108", "port": 12345},
        {"name": "Dusk", "ip": "100.99.87.61", "port": 12345},
    ])

    # 3. Filter out ourselves — never send audio to the machine generating it
    local_ips = get_local_ips()
    receivers = [r for r in receivers if r.get("ip", "") not in local_ips]

    if not receivers:
        log("No remote receivers after filtering out local IPs")
        return None

    # 4. If SSH_CLIENT is set, sort to prefer the receiver matching the SSH origin
    ssh_client = os.environ.get("SSH_CLIENT", "")
    ssh_origin_ip = ssh_client.split()[0] if ssh_client else ""
    if ssh_origin_ip:
        receivers = sorted(receivers, key=lambda r: r.get("ip", "") != ssh_origin_ip)

    # 5. Probe receivers — first healthy one wins
    import urllib.request
    for recv in receivers:
        ip = recv.get("ip", "")
        rport = recv.get("port", port)
        url = f"http://{ip}:{rport}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    target = f"http://{ip}:{rport}/tts"
                    log(f"Auto-discovered receiver: {recv.get('name', ip)} ({target})")
                    return target
        except Exception:
            continue

    # 6. Last resort: if SSH_CLIENT is set, try that IP directly
    if ssh_origin_ip and ssh_origin_ip not in local_ips:
        log(f"No healthy receiver found, trying SSH_CLIENT IP {ssh_origin_ip}")
        return f"http://{ssh_origin_ip}:{port}/tts"

    log("No remote receiver found")
    return None


def _poll_delivery_status(requests_module, status_url: str,
                          delays=(0.0, 0.15, 0.35, 0.7)) -> bool | None:
    """Return True for accepted playback, False for terminal failure, else None."""
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            response = requests_module.get(status_url, timeout=(1, 2))
            if response.status_code == 404:
                continue
            response.raise_for_status()
            status = str(response.json().get("status") or "")
        except Exception:
            continue
        if status in {"playing", "played"}:
            return True
        if status in {"failed", "rejected", "cancelled"}:
            return False
        # accepted/reserved/missing are non-terminal: playback may still start.
    return None


def _cancel_reserved_delivery(requests_module, cancel_url: str) -> bool | None:
    """Cancel a reservation. False means it was already accepted; None unknown."""
    try:
        response = requests_module.delete(cancel_url, timeout=(1, 2))
        payload = response.json() if response.content else {}
        status = str(payload.get("status") or "")
        if response.status_code == 200 and status == "cancelled":
            return True
        if response.status_code == 409 or status in {"accepted", "playing", "played"}:
            return False
    except Exception:
        return None
    return None


def _is_provably_preconnect_error(error, requests_module) -> bool:
    """True only when no HTTP request bytes could have reached the receiver."""
    import errno
    import socket

    if isinstance(error, requests_module.exceptions.ConnectTimeout):
        return True
    safe_errnos = {errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (ConnectionRefusedError, socket.gaierror)):
            return True
        if isinstance(current, OSError) and current.errno in safe_errnos:
            return True
        if type(current).__name__ in {"NewConnectionError", "NameResolutionError"}:
            return True
        for nested in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(nested, BaseException):
                pending.append(nested)
        for nested in getattr(current, "args", ()):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def send_audio_remote(wav_data: bytes, target_url: str,
                      require_off_lan: bool = False,
                      fallback_target: str = None) -> bool:
    """Deliver WAV exactly once using reserve, upload, reconcile, and cancel."""
    import uuid
    import requests

    def definitive_failure(reason: str) -> bool:
        log(reason)
        if fallback_target and fallback_target != target_url:
            log(f"Failing over definitively rejected audio to origin {fallback_target}")
            return send_audio_remote(wav_data, fallback_target)
        return False

    delivery_id = uuid.uuid4().hex
    headers = {
        "Content-Type": "audio/wav",
        "X-Shelby-Delivery-ID": delivery_id,
    }
    if require_off_lan:
        headers["X-Shelby-Require-Off-Lan"] = "1"
    receiver_base = target_url.rsplit("/", 1)[0]
    status_url = receiver_base + f"/delivery/{delivery_id}"
    reserve_url = status_url + "/reserve"

    # Reserving never starts playback. If it fails ambiguously, origin fallback
    # is safe because no audio bytes have been transmitted yet.
    legacy_loopback = False
    try:
        reserve = requests.post(
            reserve_url, data=b"", headers=headers, timeout=(1, 2)
        )
        if reserve.status_code in {404, 405}:
            legacy_loopback = _is_loopback_url(target_url) and not require_off_lan
            if not legacy_loopback:
                return definitive_failure(
                    f"Receiver at {target_url} lacks exact-once protocol — using origin"
                )
        elif not 200 <= reserve.status_code < 300:
            return definitive_failure(
                f"Receiver reservation rejected ({reserve.status_code}) — using origin"
            )
    except Exception as e:
        return definitive_failure(
            f"Receiver reservation failed ({target_url}): {e} — using origin"
        )

    if legacy_loopback:
        try:
            response = requests.post(
                target_url, data=wav_data, headers=headers, timeout=(2, 10)
            )
            response.raise_for_status()
            log(f"Sent {len(wav_data)} bytes to legacy loopback receiver")
            return True
        except Exception as e:
            return definitive_failure(
                f"Legacy loopback delivery failed ({e}) — using direct local playback"
            )

    try:
        response = requests.post(
            target_url, data=wav_data, headers=headers, timeout=(2, 10)
        )
        if not 200 <= response.status_code < 300:
            # The receiver contract guarantees that 4xx/5xx responses happen
            # before acceptance. Cancel the unused reservation best-effort.
            _cancel_reserved_delivery(requests, status_url)
            return definitive_failure(
                f"Remote delivery rejected ({response.status_code}) — using origin"
            )
        result = _poll_delivery_status(requests, status_url, delays=(0.0, 0.05, 0.1))
        if result is False:
            return definitive_failure(
                f"Delivery {delivery_id[:8]} failed before playback — using origin"
            )
        log(f"Sent {len(wav_data)} bytes to {target_url} delivery={delivery_id[:8]}")
        return True
    except requests.exceptions.ConnectTimeout as e:
        _cancel_reserved_delivery(requests, status_url)
        return definitive_failure(
            f"Remote upload never connected ({e}) — using origin"
        )
    except requests.exceptions.ConnectionError as e:
        if _is_provably_preconnect_error(e, requests):
            _cancel_reserved_delivery(requests, status_url)
            return definitive_failure(
                f"Remote upload failed before connection ({e}) — using origin"
            )
        log(f"Remote delivery outcome ambiguous ({e}) — reconciling")
    except Exception as e:
        log(f"Remote delivery outcome ambiguous ({e}) — reconciling")

    reconciled = _poll_delivery_status(requests, status_url)
    if reconciled is not None:
        log(f"Delivery {delivery_id[:8]} reconciliation={reconciled}")
        return reconciled if reconciled else definitive_failure(
            f"Delivery {delivery_id[:8]} failed terminally — using origin"
        )

    cancelled = _cancel_reserved_delivery(requests, status_url)
    if cancelled is True:
        return definitive_failure(
            f"Delivery {delivery_id[:8]} cancelled before acceptance — using origin"
        )
    if cancelled is False:
        log(f"Delivery {delivery_id[:8]} was already accepted — suppressing duplicate local play")
        return True

    # If neither status nor cancellation can be observed, an accepted upload
    # is indistinguishable from a network outage. Favor exact-once behavior.
    log(f"Delivery {delivery_id[:8]} remains ambiguous — suppressing duplicate local play")
    return True


def make_wav(pcm_data: bytes, srate: int = 24000, channels: int = 1) -> bytes:
    """Convert raw PCM s16le data to WAV format in memory."""
    import struct
    bits_per_sample = 16
    byte_rate = srate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm_data)
    header = b'RIFF'
    header += struct.pack('<I', 36 + data_size)
    header += b'WAVE'
    header += b'fmt '
    header += struct.pack('<IHHIIHH', 16, 1, channels, srate, byte_rate, block_align, bits_per_sample)
    header += b'data'
    header += struct.pack('<I', data_size)
    return header + pcm_data


def _make_primer(lead_ms: int, amp: int, srate: int = 24000, channels: int = 1) -> bytes:
    """Build a fade-in low-amplitude noise lead. The Dawn receiver chain
    (BlackHole -> VBAN -> Voicemeeter) has a noise gate / VAD that strips
    leading SILENCE and then eats ~1.2s of real speech while the device opens
    (proven 2026-06-13: 1500ms vs 3000ms silence lead both lost numbers 1-6).
    A non-silent primer trips the gate open BEFORE the words start; the linear
    fade-in from zero avoids an abrupt 'static' onset. amp<=0 disables it."""
    import array, random
    n = srate * channels * max(0, lead_ms) // 1000
    if n <= 0 or amp <= 0:
        return b'\x00' * (srate * channels * 2 // 1000 * max(0, lead_ms))
    rnd = random.Random(0x5E1B)  # deterministic seed; reproducible primer
    # The gate needs immediate full-level signal from t=0 to open in time — a
    # full-duration fade-in keeps it quiet too long and the gate stays shut
    # (proven 2026-06-13: amp-1100 fade lost numbers 1-6). So hold full
    # amplitude across the body, with only short anti-click ramps at the edges.
    edge = min(max(1, n // 8), srate // 100)  # ~10ms attack/release, capped
    out = array.array('h', bytes(2 * n))
    for i in range(n):
        if i < edge:
            gain = (i + 1) / edge
        elif i >= n - edge:
            gain = (n - i) / edge
        else:
            gain = 1.0
        out[i] = int(rnd.randint(-amp, amp) * gain)
    return out.tobytes()


def pad_pcm(pcm_data: bytes, srate: int = 24000, channels: int = 1,
            lead_ms: int = PAD_LEAD_MS, tail_ms: int = PAD_TAIL_MS,
            primer_amp: int = 0) -> bytes:
    """Prepend a lead (faded-noise primer if primer_amp>0, else silence) and
    append trailing silence, so the receiver's gate/drain doesn't clip speech."""
    bytes_per_ms = srate * channels * 2 // 1000
    if primer_amp > 0 and lead_ms > 0:
        lead = _make_primer(lead_ms, primer_amp, srate, channels)
    else:
        lead = b'\x00' * (bytes_per_ms * max(0, lead_ms))
    tail = b'\x00' * (bytes_per_ms * max(0, tail_ms))
    return lead + pcm_data + tail


def _resolve_elevenlabs_key(cfg: dict) -> str:
    """Resolve the ElevenLabs API key. Prefer config, then the inherited env,
    then ~/.secrets/elevenlabs.env. The secrets-file fallback makes the voice
    consistent across sessions whose launching shell never sourced the env
    (which otherwise silently degraded to the Edge fallback voice)."""
    env_name = cfg.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY")
    key = cfg.get("elevenlabs_api_key") or os.environ.get(env_name, "")
    if key:
        return key
    secrets_path = os.path.expanduser("~/.secrets/elevenlabs.env")
    try:
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == env_name:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        os.environ.setdefault(env_name, v)
                        log(f"Loaded {env_name} from {secrets_path} (env was empty)")
                        return v
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Could not read {secrets_path}: {e}")
    return ""


ELEVENLABS_LOG = os.path.expanduser("~/claude-voice-venv/elevenlabs-usage.log")
ELEVENLABS_QUOTA_CACHE = "/tmp/elevenlabs-quota-cache.json"
ELEVENLABS_QUOTA_TTL = 600  # 10 minutes


def _get_cached_quota(api_key: str) -> dict:
    """Get ElevenLabs quota, using a time-throttled cache to avoid per-call API hits.
    Only calls /v1/user/subscription if cache is stale (>10 min) or missing."""
    now = time.time()

    # Try reading cache
    try:
        with open(ELEVENLABS_QUOTA_CACHE) as f:
            cache = json.load(f)
        if now - cache.get("ts", 0) < ELEVENLABS_QUOTA_TTL:
            return cache
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Cache stale or missing — fetch from API
    try:
        import requests
        resp = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                            headers={"xi-api-key": api_key}, timeout=5)
        if resp.status_code == 401:
            # API key lacks user_read permission — cache this so we don't retry constantly
            log("ElevenLabs quota check: 401 (key lacks user_read permission)")
            used, limit, pct, reset_date = "no_perm", "no_perm", "n/a", "n/a"
        else:
            data = resp.json()
            used = data.get("character_count", "?")
            limit = data.get("character_limit", "?")
            pct = f"{used/limit*100:.1f}%" if isinstance(used, int) and isinstance(limit, int) else "?"
            reset = data.get("next_character_count_reset_unix", 0)
            from datetime import datetime
            reset_date = datetime.fromtimestamp(reset).strftime("%Y-%m-%d") if reset else "?"
    except Exception:
        used, limit, pct, reset_date = "?", "?", "?", "?"

    result = {"ts": now, "used": used, "limit": limit, "pct": pct, "reset_date": reset_date}

    # Write cache (best-effort)
    try:
        with open(ELEVENLABS_QUOTA_CACHE, "w") as f:
            json.dump(result, f)
    except Exception:
        pass

    return result


def log_elevenlabs_usage(chars_this_call: int, api_key: str):
    """Log character usage with throttled quota check (every 10 min, not every call)."""
    quota = _get_cached_quota(api_key)
    used = quota.get("used", "?")
    limit = quota.get("limit", "?")
    pct = quota.get("pct", "?")
    reset_date = quota.get("reset_date", "?")

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | +{chars_this_call} chars | {used}/{limit} ({pct}) | resets {reset_date}\n"
    try:
        with open(ELEVENLABS_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass
    log(f"ElevenLabs: {used}/{limit} ({pct}), resets {reset_date}")


def _resolve_pocket_voice(cfg: dict) -> str:
    """Resolve the Shelby persona consistently for Pocket-compatible lanes."""
    configured_voice = cfg.get("tts_voice_pocket_en", "jarvis")
    expected_voices = {"day": "jarvis", "dawn": "jarvis", "dusk": "jarvis"}
    source = _source_machine(cfg)
    override_voice = os.environ.get("SHELBY_TTS_POCKET_VOICE")
    voice = expected_voices.get(source) or override_voice or configured_voice
    content_voice = cfg.get("tts_voice_pocket_content", "aragorn")
    if is_content_voice(voice, content_voice):
        # Content voice prefixes are forbidden on every path, including this lane.
        # Aragorn's own cloned voice is for CONTENT generation only (his
        # directive 2026-09-10); Shelby never speaks as him. The whole
        # normalized aragorn* family and configured content prefix are refused.
        log(f"Refused Pocket persona {voice!r} (content voice) for Shelby speech; using jarvis")
        voice = "jarvis"
    if override_voice and source in expected_voices and override_voice != voice:
        log(f"Ignored Pocket override {override_voice!r}; {source} persona is authoritative")
    if voice != configured_voice:
        log(f"Corrected Pocket persona {configured_voice!r} -> {voice!r} for source machine")
    return voice


def _is_shelby_dutch_persona(voice, cfg: dict) -> bool:
    return normalize_dutch_persona(voice) in SHELBY_DUTCH_PERSONAS


def _resolve_dutch_lane_voice(cfg: dict) -> str:
    """Apply content-clone refusal and the Dutch lane's Shelby-only allowlist."""
    voice = _resolve_pocket_voice(cfg)
    normalized = normalize_dutch_persona(voice)
    if normalized not in SHELBY_DUTCH_PERSONAS:
        log(f"Refused non-Shelby Dutch Pocket persona {voice!r}; using jarvis")
        return "jarvis"
    return normalized


def _precheck_dutch_audio(wav_data: bytes, local: bool = False) -> None:
    """Decode lane audio before delivery; check local playback prerequisites if needed."""
    import io
    import soundfile as sf

    data, _srate = sf.read(io.BytesIO(wav_data), frames=-1)
    if data.size == 0:
        raise ValueError("Dutch WAV decoded with no audio frames")

    if not local:
        return

    import shutil

    player = "afplay" if IS_MACOS else "paplay"
    if not shutil.which(player):
        raise FileNotFoundError(f"Dutch local audio player is unavailable: {player}")
    if not IS_MACOS:
        import numpy as np

        (data * 32767).astype(np.int16).tobytes()


def speak(text: str, cfg: dict, lang_hint: str = None):
    """Route to the configured TTS engine with language detection.

    lang_hint: language declared in the <voice lang="..."> tag. An enabled,
    Model-Routing-selected Dutch lane can honor Dutch; all other paths retain
    the English pin.

    Audibility pre-check: if neither a healthy remote receiver NOR an audible
    local output exists, suppress the entire TTS call. This is the load-bearing
    cost-saver — it prevents ElevenLabs character spend on audio that wouldn't
    be heard anywhere (default output is BlackHole with no forwarding alive,
    all remote receivers down, etc.).
    """
    engine = cfg.get("tts_engine", "edge")

    # ---- ENGLISH PIN (Aragorn directive, 2026-08-03) -----------------------
    # The 2026-08-03 English pin was conditionally lifted on 2026-09-28: when
    # config enables Dutch speech and Model Routing selects OmniVoice, Dutch is
    # spoken directly. Every other path retains the pin's translation fallback.
    # Written Dutch in the response body is untouched; only spoken text routes.
    requested = lang_hint if lang_hint in ('nl', 'en') else detect_language(text)
    lang = "en"
    dutch_speech_config = cfg.get("dutch_speech")
    dutch_speech_config = (dutch_speech_config
                           if isinstance(dutch_speech_config, dict) else {})
    try:
        dutch_lane_requested = (
            dutch_speech_config.get("enabled") is True
            and is_dutch_speech(text, lang_hint)
        )
    except Exception as exc:
        dutch_lane_requested = False
        log(f"Dutch speech eligibility check failed; using the English pin: {exc}")
    dutch_turn_started = time.monotonic() if dutch_lane_requested else None
    if requested != "en" and not dutch_lane_requested:
        log(f"lang '{requested}' ignored — voice is English-only (pinned); "
            f"speaking with the English persona")
    # ------------------------------------------------------------------------

    speed = cfg.get("tts_speed", "+30%")

    remote_target, play_local = find_audible_path(cfg)
    if not remote_target and not play_local:
        log(f"No audible path — suppressing TTS for {len(text)} chars "
            f"(engine={engine}, lang={lang}) — saved API call")
        return

    if dutch_lane_requested:
        declared_dutch = lang_hint == "nl"
        if declared_dutch:
            log('voice-lang: declared Dutch is allowed by the enabled Dutch lane')
        else:
            log('voice-lang: Dutch speech without lang="nl" declaration')

        lane_started = time.time()
        lane_timeout_s = bounded_dutch_timeout(
            dutch_speech_config,
            elapsed_s=time.monotonic() - dutch_turn_started,
        )
        if lane_timeout_s is None:
            catalog_id = None
            endpoint = None
            log("Dutch speech lane skipped: synthesis budget is below the minimum after fallback reserve")
        else:
            try:
                catalog_id = resolve_dutch_speech_engine()
            except Exception as exc:
                catalog_id = None
                log(f"Dutch speech Model Routing resolution failed: {exc}")
            endpoint = DUTCH_ENGINE_ENDPOINTS.get(catalog_id)
        if not endpoint:
            if lane_timeout_s is not None:
                if catalog_id:
                    log(f"Dutch speech lane off: Model Routing selected {catalog_id!r}; "
                        "no Dutch endpoint is mapped")
                else:
                    log("Dutch speech lane off: Model Routing returned no catalog ID")
        else:
            policy = cfg.get("dusk_presence_routing") if isinstance(
                cfg.get("dusk_presence_routing"), dict
            ) else {}
            off_lan_target = str(
                policy.get("off_lan_target") or policy.get("away_target") or ""
            ).strip()
            remote_requires_off_lan = bool(
                remote_target and off_lan_target and remote_target == off_lan_target
                and _source_machine(cfg) in {"day", "dawn"}
            )
            remote_fallback_target = (
                str(policy.get("origin_target") or "").strip() or None
                if remote_requires_off_lan else None
            )
            dutch_play_local = play_local
            if remote_target and play_local and not cfg.get("pocket_play_both", False):
                dutch_play_local = False
            local_fallback = play_local

            lane_timeout_s = bounded_dutch_timeout(
                dutch_speech_config,
                elapsed_s=time.monotonic() - dutch_turn_started,
            )
            lane_audio = None
            if lane_timeout_s is None:
                log("Dutch speech lane skipped: synthesis budget is below the minimum after fallback reserve")
            else:
                try:
                    voice = _resolve_dutch_lane_voice(cfg)
                    if not voice:
                        log("OmniVoice Dutch persona refused; continuing with the English pin fallback")
                        lane_audio = None
                    else:
                        lane_audio = _omnivoice_fetch(
                            text, voice, endpoint, lane_timeout_s
                        )
                        if lane_audio:
                            if not _is_riff_wav(lane_audio):
                                raise ValueError("OmniVoice returned empty or non-RIFF WAV audio")
                            lane_audio = _wav_tempo(
                                lane_audio, cfg.get("pocket_speed", 1.0)
                            )
                            if not _is_riff_wav(lane_audio):
                                raise ValueError("OmniVoice tempo transform returned invalid WAV audio")
                            lane_audio = _pad_wav_tail(
                                lane_audio,
                                tail_ms=int(cfg.get("pocket_tail_ms", 1000)),
                            )
                            if not _is_riff_wav(lane_audio):
                                raise ValueError("OmniVoice tail transform returned invalid WAV audio")
                        else:
                            lane_audio = None
                except Exception as exc:
                    lane_audio = None
                    log(f"OmniVoice Dutch synthesis failed ({exc})")

                if lane_audio:
                    try:
                        _precheck_dutch_audio(lane_audio)
                    except Exception as exc:
                        lane_audio = None
                        log(f"OmniVoice Dutch WAV pre-check failed before delivery ({exc})")

                if lane_audio:
                    if not declared_dutch:
                        _record_lang_drift(text, lang_hint)
                    delivery_attempted = False
                    PRECHECK_FAILED, PLAYED, UNCERTAIN = object(), object(), object()

                    def play_dutch_locally_once():
                        nonlocal delivery_attempted
                        tmp_path = None
                        try:
                            try:
                                _precheck_dutch_audio(lane_audio, local=True)
                            except Exception:
                                return PRECHECK_FAILED

                            try:
                                with tempfile.NamedTemporaryFile(
                                    suffix=".wav", delete=False
                                ) as audio_file:
                                    tmp_path = audio_file.name
                                    audio_file.write(lane_audio)
                            except Exception:
                                return PRECHECK_FAILED

                            delivery_attempted = True
                            try:
                                # Like the English path, this shared helper does not treat non-zero player exits as failure.
                                # Pre-checks and staging passed; an exception may follow
                                # started playback, so do not risk an English replay.
                                play_audio_file(tmp_path)
                            except Exception:
                                # The shared helper cannot report whether playback started; classify this as uncertain.
                                return UNCERTAIN
                            return PLAYED
                        finally:
                            if tmp_path:
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass

                    try:
                        if remote_target:
                            delivery_attempted = True
                            remote_ok = send_audio_remote(
                                lane_audio, remote_target,
                                require_off_lan=remote_requires_off_lan,
                                fallback_target=remote_fallback_target,
                            )
                            if not remote_ok:
                                # Match the English path's False contract: replay locally
                                # when allowed, using this same Dutch WAV. A successful
                                # replay returns before English fallback, avoiding double speech.
                                delivery_attempted = False
                                if local_fallback:
                                    local_status = play_dutch_locally_once()
                                    if local_status is PRECHECK_FAILED:
                                        log("TTS (omnivoice/nl): Dutch local pre-check failed; continuing with the English pin fallback")
                                        lane_audio = None
                                        dutch_play_local = False
                                    elif local_status is UNCERTAIN:
                                        log("TTS (omnivoice/nl): Dutch local fallback not confirmed; staying silent")
                                        return
                                    elif local_status is PLAYED:
                                        log("TTS (omnivoice/nl/local-fallback): remote delivery unconfirmed; played the same Dutch audio locally")
                                        return
                                if lane_audio:
                                    log("TTS (omnivoice/nl): remote delivery failed before playback; continuing with the English pin fallback")
                                    delivery_attempted = False
                                    lane_audio = None

                        if dutch_play_local:
                            local_status = play_dutch_locally_once()
                            if local_status is PRECHECK_FAILED:
                                log("TTS (omnivoice/nl): Dutch local pre-check failed; continuing with the English pin fallback")
                                dutch_play_local = False
                                if not delivery_attempted:
                                    lane_audio = None
                            elif local_status is UNCERTAIN:
                                log("TTS (omnivoice/nl): delivery not confirmed; no English fallback (avoid double speech)")
                                return

                        if not delivery_attempted:
                            lane_audio = None
                        else:
                            if remote_target and dutch_play_local:
                                log(f"Audible: remote={remote_target} + local")
                            elif remote_target:
                                log(f"Audible: remote={remote_target} (local muted)")
                            else:
                                log("Audible: local only (no remote receiver)")
                            mode = ("remote+local" if remote_target and dutch_play_local
                                    else ("remote" if remote_target else "local"))
                            log(f"TTS (omnivoice/nl/{mode}): "
                                f"{time.time()-lane_started:.2f}s, {len(text)} chars, $0")
                            return
                    except Exception as exc:
                        if delivery_attempted:
                            log("TTS (omnivoice/nl): delivery not confirmed; no English fallback (avoid double speech)")
                            return
                        log(f"OmniVoice Dutch local preparation failed before delivery ({exc})")
                        lane_audio = None

            if lane_timeout_s is None or not lane_audio:
                log("OmniVoice Dutch lane failed before delivery — continuing with the English pin fallback")


        if requested != "en":
            log(f"lang '{requested}' ignored — voice is English-only (pinned); "
                f"speaking with the English persona")

    # No Dutch reaches an English engine. The audibility check stays ahead of
    # translation, and the configured-off path keeps the original two-argument
    # call and behavior.
    if dutch_lane_requested and lang_hint == "nl":
        text = _enforce_dutch_lane_english_fallback(text, lang_hint)
    else:
        text = enforce_english_speech(text, lang_hint)

    if remote_target and play_local:
        log(f"Audible: remote={remote_target} + local")
    elif remote_target:
        log(f"Audible: remote={remote_target} (local muted)")
    else:
        log(f"Audible: local only (no remote receiver)")

    t0 = time.time()
    policy = cfg.get("dusk_presence_routing") if isinstance(
        cfg.get("dusk_presence_routing"), dict
    ) else {}
    off_lan_target = str(
        policy.get("off_lan_target") or policy.get("away_target") or ""
    ).strip()
    remote_requires_off_lan = bool(
        remote_target and off_lan_target and remote_target == off_lan_target
        and _source_machine(cfg) in {"day", "dawn"}
    )
    remote_fallback_target = (
        str(policy.get("origin_target") or "").strip() or None
        if remote_requires_off_lan else None
    )

    if engine == "pocket":
        # pocket-tts is English-only — Dutch routes to the next engine in line.
        if lang != "en":
            engine = cfg.get("engine_fallback", {}).get("pocket", "edge")
            log(f"pocket-tts has no '{lang}' support — falling back to {engine}")
        else:
            # Single-playback default (2026-07-06): remote receiver + local output
            # both audible at the desk = double voice. Remote (the desk receiver)
            # wins; set pocket_play_both=true to restore dual output.
            local_fallback = play_local
            pocket_play_local = play_local
            if remote_target and play_local and not cfg.get("pocket_play_both", False):
                pocket_play_local = False
            voice = _resolve_pocket_voice(cfg)
            base_url = cfg.get("pocket_tts_url", "http://127.0.0.1:8933")
            if speak_pocket(text, voice, remote_target=remote_target,
                            play_local=pocket_play_local, base_url=base_url,
                            fallback_local=local_fallback,
                            tail_ms=int(cfg.get("pocket_tail_ms", 1000)),
                            remote_requires_off_lan=remote_requires_off_lan,
                            remote_fallback_target=remote_fallback_target,
                            speed=cfg.get("pocket_speed", 1.0)):
                mode = ("remote+local" if remote_target and pocket_play_local
                        else ("remote" if remote_target else "local"))
                log(f"TTS (pocket/{lang}/{mode}): {time.time()-t0:.2f}s, {len(text)} chars, $0")
                return
            engine = cfg.get("engine_fallback", {}).get("pocket", "edge")
            log(f"pocket-tts failed — falling back to {engine}")

    if engine == "elevenlabs":
        api_key = _resolve_elevenlabs_key(cfg)
        if not api_key:
            log("No ElevenLabs API key, falling back to Edge")
            engine = "edge"
        else:
            # Quota gate (S749): the May-2026 cap-out showed calls still hitting
            # ElevenLabs at 200% of quota — there was no pre-call check, forcing a
            # manual config flip to edge. Degrade automatically at >=95% instead;
            # the 10-min quota cache keeps this nearly free. Recovers by itself
            # after the monthly reset.
            quota = _get_cached_quota(api_key)
            _used, _limit = quota.get("used"), quota.get("limit")
            if isinstance(_used, int) and isinstance(_limit, int) and _used >= 0.95 * _limit:
                log(f"ElevenLabs quota {_used}/{_limit} ({_used/_limit*100:.0f}%) — degrading to edge until reset")
                engine = "edge"
        if engine == "elevenlabs" and api_key:
            source = _source_machine(cfg)
            voice_key = f"tts_voice_elevenlabs_{source}_{lang}"
            voice_id = os.environ.get("SHELBY_TTS_ELEVENLABS_VOICE_ID") or cfg.get(voice_key)
            if not voice_id:
                log(f"No source-specific ElevenLabs persona for {source}/{lang} — falling back to Edge")
                engine = "edge"
        if engine == "elevenlabs" and api_key:
            model = cfg.get("elevenlabs_model", "eleven_turbo_v2_5")
            el_speed = cfg.get("elevenlabs_speed", 1.0)
            lead_ms = int(cfg.get("tts_pad_lead_ms", PAD_LEAD_MS))
            tail_ms = int(cfg.get("tts_pad_tail_ms", PAD_TAIL_MS))
            primer_amp = int(cfg.get("tts_lead_primer_amp", 0))
            try:
                speak_elevenlabs_streaming(text, voice_id, model, api_key, speed=el_speed,
                                           remote_target=remote_target, play_local=play_local,
                                           lead_ms=lead_ms, tail_ms=tail_ms, primer_amp=primer_amp,
                                           remote_requires_off_lan=remote_requires_off_lan,
                                           remote_fallback_target=remote_fallback_target)
            except Exception as e:
                # Codex review 2026-07-06: an unhandled raise here used to kill the
                # hook with no audio at all — degrade to edge instead.
                log(f"ElevenLabs failed ({e}) — falling back to edge")
                engine = "edge"
            else:
                chars_used = len(text)
                log_elevenlabs_usage(chars_used, api_key)
                mode = ("remote+local" if remote_target and play_local
                        else ("remote" if remote_target else "local"))
                log(f"TTS (elevenlabs/{lang}/{mode}): {time.time()-t0:.2f}s, {chars_used} chars")
                return

    if engine == "kokoro":
        source = _source_machine(cfg)
        voice_key = f"tts_voice_kokoro_{source}_{lang}"
        voice = cfg.get(
            voice_key,
            KOKORO_SOURCE_PERSONAS.get(source, cfg.get("tts_voice_kokoro_en", "af_heart")),
        )
        # Kokoro is local-only; if there's no audible local path but remote exists,
        # we still need an engine that can send to remote — fall through to edge.
        if play_local and speak_kokoro(text, voice):
            log(f"TTS (kokoro/{lang}): {time.time()-t0:.2f}s")
            return
        log("Kokoro failed or not local-audible, falling back to Edge")
        engine = "edge"

    if engine == "edge":
        source = _source_machine(cfg)
        voice_key = f"tts_voice_edge_{source}_{lang}"
        voice = cfg.get(
            voice_key,
            EDGE_SOURCE_PERSONAS.get(source, {}).get(
                lang, cfg.get(f"tts_voice_edge_{lang}", "en-GB-SoniaNeural")
            ),
        )
        edge_coro = speak_edge(
            text, voice, speed, remote_target=remote_target, play_local=play_local,
            remote_requires_off_lan=remote_requires_off_lan,
            remote_fallback_target=remote_fallback_target,
        )
        asyncio.run(edge_coro)
        mode = ("remote+local" if remote_target and play_local
                else ("remote" if remote_target else "local"))
        log(f"TTS (edge/{lang}/{mode}): {time.time()-t0:.2f}s")


def _reset_voxtype_wait_budget():
    """Drop this run's cap and stamp so a later main() starts clean."""
    global _voxtype_wait_budget, _arrival_stamp_ns
    _voxtype_wait_budget = None
    _arrival_stamp_ns = None


def _arm_voxtype_wait_budget(started_at, cap_s):
    """Deadline for the pre-lock wait and every first gate wait in this run."""
    global _voxtype_wait_budget
    _voxtype_wait_budget = {
        "deadline": started_at + cap_s,
        "cap_fired": False,
    }


def _mark_voxtype_cap_fired():
    budget = _voxtype_wait_budget
    if budget is not None:
        budget["cap_fired"] = True


def _first_voxtype_gate_wait():
    """First wait before attempt 1.

    No budget waits the full cap, as before. A cap that already fired does
    not wait and does not log a second cap line. The replay wait does not
    use this.
    """
    budget = _voxtype_wait_budget
    if budget is None:
        return _wait_for_voxtype_idle_status()
    if budget["cap_fired"]:
        return 0.0, True
    remaining = budget["deadline"] - time.monotonic()
    if remaining < 0.0:
        remaining = 0.0
    elapsed, cap_fired = _wait_for_voxtype_idle_status(max_wait_s=remaining)
    if cap_fired:
        budget["cap_fired"] = True
    return elapsed, cap_fired


def _voice_arrival_dir():
    """Registry directory, read at call time. None turns ordering off.

    SHELBY_VOICE_ARRIVAL_DIR wins. Otherwise $XDG_RUNTIME_DIR/voice-stop-hook-arrivals.
    No override and no XDG_RUNTIME_DIR: off. Never a world-writable fallback.
    """
    override = os.environ.get("SHELBY_VOICE_ARRIVAL_DIR")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return None
    return os.path.join(runtime, "voice-stop-hook-arrivals")


def _session_arrival_name(session_id: str) -> str:
    """First 16 hex chars of sha256(session_id). Never the raw session id."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]


def _dir_is_private(path) -> bool:
    """True when path is a real directory we own, with no group or other write.

    lstat, so a symlink is not a directory. Raises OSError when path is missing.
    """
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    if st.st_uid != os.getuid():
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return True


def _ensure_private_dir(path) -> bool:
    """Create path mode 0700 and require _dir_is_private. False on any failure."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        return _dir_is_private(path)
    except OSError:
        return False


def _register_session_arrival(stamp_ns: int):
    """Create this hook's arrival file. None means ordering is off.

    Called only when the VoxType steal is on, after the line is speakable
    and before the pre-lock wait. An empty session id, a missing runtime
    dir, or an unsafe directory leaves ordering off. The unsafe directory
    logs one line.
    """
    global _arrival_stamp_ns, _arrival_entry_path
    if not _SESSION_ID:
        return None
    root = _voice_arrival_dir()
    if not root:
        return None
    sub = os.path.join(root, _session_arrival_name(_SESSION_ID))
    if not (_ensure_private_dir(root) and _ensure_private_dir(sub)):
        log(_ARRIVAL_UNSAFE_LOG)
        return None
    stamp = int(stamp_ns)
    path = os.path.join(sub, f"{stamp:020d}-{os.getpid()}")
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return None
    _arrival_stamp_ns = stamp
    _arrival_entry_path = path
    try:
        os.close(fd)
    except OSError:
        pass
    return path


def _clear_arrival_entry(path=None):
    """Unlink our arrival file. Best effort. Ignores errors."""
    global _arrival_entry_path
    target = _arrival_entry_path if path is None else path
    if path is None or path == _arrival_entry_path:
        _arrival_entry_path = None
    if not target:
        return
    try:
        os.unlink(target)
    except OSError:
        pass


def _arrival_pid_alive(pid) -> bool:
    """True when os.kill(pid, 0) says the process exists.

    ProcessLookupError and OverflowError are dead. PermissionError is alive.
    Any other OSError propagates so the caller can refuse to block speech.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    return True


def _scan_newer_arrival() -> bool:
    session_id = _SESSION_ID
    my_stamp = _arrival_stamp_ns
    if not session_id or my_stamp is None:
        return False
    root = _voice_arrival_dir()
    if not root:
        return False
    sub = os.path.join(root, _session_arrival_name(session_id))
    if not _dir_is_private(sub):
        return False
    my_pid = os.getpid()
    newer = False
    for name in os.listdir(sub):
        match = _ARRIVAL_ENTRY_RE.match(name)
        if match is None:
            continue
        stamp = int(match.group(1))
        pid = int(match.group(2))
        if pid <= 1 or pid == my_pid:
            continue
        if not _arrival_pid_alive(pid):
            try:
                os.unlink(os.path.join(sub, name))
            except OSError:
                pass
            continue
        if not _is_our_hook(pid):
            continue
        if (stamp, pid) > (my_stamp, my_pid):
            newer = True
    return newer


def _newer_same_session_arrival() -> bool:
    """True when a newer live voice-stop-hook from this session has arrived.

    Names that are not ``<20 digits>-<pid>`` are ignored. A newer entry counts
    only when its pid is > 1, is not this process, is alive, and _is_our_hook
    matches. Dead pids are unlinked. A live pid that fails the hook check is
    left in place. Any OSError returns False.
    """
    try:
        return _scan_newer_arrival()
    except OSError:
        return False


def main():
    # Stamp before stdin so two Stop hooks from one session order by arrival,
    # not by how long sanitising took. The clock read is the only extra work
    # on hosts that never register an entry.
    arrival_stamp = time.monotonic_ns()
    _reset_voxtype_wait_budget()
    _reset_held_watch()
    # Install SIGTERM handler — exit silently when killed by timeout or voice-shutup
    signal.signal(signal.SIGTERM, handle_sigterm)

    # Read JSON input from stdin
    try:
        raw = sys.stdin.read()
    except Exception:
        return

    if not raw:
        return

    # Parse JSON
    response = ""
    data = None
    try:
        data = json.loads(raw)
        response = data.get("last_assistant_message", "")
        # Capture session_id so voice-shutup can scope kills to the same session.
        global _SESSION_ID
        _SESSION_ID = str(data.get("session_id", "") or "")
    except (json.JSONDecodeError, TypeError):
        response = raw

    if not response:
        return

    # Skip TTS for automated sessions (defense-in-depth — shell wrapper also guards)
    session_type = os.environ.get("CLAUDE_SESSION_TYPE", "main")
    if session_type in ("cron", "spinoff", "headless"):
        log(f"Skipped TTS: session_type={session_type}")
        return

    # Load config first: a missing <voice> block is only a defect worth
    # reporting when voice is actually switched on.
    cfg = load_config()
    global _VOXTYPE_WAIT_ENABLED
    _VOXTYPE_WAIT_ENABLED = _voxtype_wait_enabled(cfg.get("voxtype_wait", True))

    # Honor tts_enabled config flag
    if not cfg.get("tts_enabled", True):
        log("TTS disabled via config")
        return

    # Extract voice block. No block => silence, and a breadcrumb so the next
    # turn gets told. Never invent something to say from the response prose.
    voice_text = extract_voice_block(response)
    if not voice_text:
        _record_missing_voice_block()
        log("no <voice> block — silent (prose fallback removed 2026-08-08)")
        return

    # Sanitize
    clean = sanitize_for_speech(voice_text)
    if not clean:
        return

    # Wait out dictation before taking the lock, so a sibling line can queue
    # instead of being dropped for the whole wait. remote_audio hosts keep
    # today's order: lock first, then the in-lock gate.
    # Presence is read once. A missing state file keeps main's steal, so
    # Day, Dawn, and Dusk do not grow a poll or a SIGTERM on this path.
    # The arrival registry is the same gate: off unless the VoxType steal
    # is on, including remote_audio hosts. It is not touched otherwise.
    arrival_entry = None
    try:
        voxtype_on = voxtype_present()
        graceful_steal = (not IS_MACOS) and _VOXTYPE_WAIT_ENABLED and voxtype_on
        if graceful_steal:
            # Baseline before the arrival entry. A missing or unusable
            # transcript leaves the watch off and changes nothing else.
            transcript_path = data.get("transcript_path") if isinstance(data, dict) else None
            _arm_transcript_watch(transcript_path)
            arrival_entry = _register_session_arrival(arrival_stamp)

        held_for_voxtype = False
        if graceful_steal and not cfg.get("remote_audio", False):
            # One budget for this run. The cap is read here, at the start of
            # the pre-lock wait. The first gate wait spends whatever remains.
            cap_s = VOXTYPE_MAX_WAIT_S
            started_at = time.monotonic()
            _arm_voxtype_wait_budget(started_at, cap_s)
            elapsed, cap_fired = _wait_for_voxtype_idle_status(
                max_wait_s=cap_s, heartbeat=False,
            )
            if cap_fired:
                _mark_voxtype_cap_fired()
            held_for_voxtype = elapsed > 0

        if arrival_entry is not None and _newer_same_session_arrival():
            log(_NEWER_ARRIVAL_LOG)
            return

        # A held line queues for a live sibling. Every other path decides once.
        # Read the cap at call time so a patched VOXTYPE_MAX_WAIT_S is honored.
        queue_wait_s = VOXTYPE_MAX_WAIT_S if held_for_voxtype else 0.0
        yield_to_newer = (
            _newer_same_session_arrival if arrival_entry is not None else None
        )
        lock = acquire_lock(
            queue_wait_s=queue_wait_s,
            graceful_steal=graceful_steal,
            yield_to_newer=yield_to_newer,
        )
        if lock is None:
            log("Could not acquire lock, skipping")
            return
        # Won the flock while a deferred SIGTERM is still inside its grace:
        # keep reading the transcript until the grace ends, then speak.
        if not _honor_deferred_sigterm(lock):
            return

        try:
            speak(clean, cfg, lang_hint=extract_voice_lang(response))
        except Exception as e:
            log(f"TTS error: {e}")
        finally:
            release_lock(lock)
    except _OwnPromptDrop:
        return
    finally:
        _end_held_phase()
        _clear_arrival_entry(arrival_entry)


if __name__ == "__main__":
    main()
