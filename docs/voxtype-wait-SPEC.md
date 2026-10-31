# VoxType speech wait — SPEC

Status: architect spec 2026-10-31 (CASE). Code is written by the code worker from this spec.

## 1. Goal (Aragorn's words)

"The voice-feedback from Shelby/TTS should wait for me to finish when I'm recording with VoxType."

## 2. The bar

Like a good human assistant in the room: if Aragorn is talking (dictating), Shelby holds the line until he is done, then says it in full. If he starts dictating while Shelby talks, Shelby stops at once (so the mic does not capture Shelby), then repeats the line from the start once he is done. On machines without VoxType nothing changes at all, not even by a millisecond that anyone could measure.

## 3. Facts (probed 2026-10-31 on CASE)

- VoxType 1.1.1, `/usr/bin/voxtype`, user service `voxtype.service`, config `~/.config/voxtype/config.toml` with `state_file = "auto"`.
- State file: `$XDG_RUNTIME_DIR/voxtype/state` = `/run/user/1000/voxtype/state`. Content is a bare word, no newline. Values the daemon writes (config comment and binary strings): `idle`, `recording`, `transcribing`. UI themes also know `stopped`, which the status UI shows when no daemon runs (the daemon removes the state file on clean exit).
- PID file next to it: `/run/user/1000/voxtype/pid`, bare digits, no newline (today `135648` = `/usr/bin/voxtype daemon`).
- `voxtype status` prints `idle`; `voxtype status --format json` gives `"alt":"idle","class":"idle"`.
- Live wiring on CASE: Claude Code Stop hook -> `~/scripts/voice-hook.sh` -> background `~/claude-voice-venv/.venv/bin/python3 ~/projects/claude-voice/scripts/voice-stop-hook.py`. **The checkout `~/projects/claude-voice` IS the live install on CASE**: every edit there is live on the next Stop hook.
- UserPromptSubmit -> `~/scripts/voice-shutup.sh`: `pkill -x paplay`, `pkill` of `voice-stop-hook.py` (SIGTERM), `rm -f /tmp/sonia-tts.lock`.
- CASE config: `tts_engine=pocket`, `pocket_tts_url=http://127.0.0.1:8935`, `remote_audio=false`.
- Python 3.13 in the venv. Test runner: `unittest` (no pytest). Baseline today: `python3 -m unittest discover -s tests -p 'test_*.py'` -> 94 tests OK in 2.7 s.

### Local playback paths in `scripts/voice-stop-hook.py` (all of them)

| Path | Where it plays on Linux |
|---|---|
| `play_raw_pcm()` | `subprocess.run(["paplay","--raw",...], input=pcm, timeout=30)` |
| `play_audio_file()` | decodes with soundfile -> `play_raw_pcm()` |
| `speak_edge()` local | `play_raw_pcm()` |
| `speak_pocket()` local | `play_audio_file()` |
| Dutch lane `play_dutch_locally_once()` | `play_audio_file()` |
| `speak_kokoro()` | `play_raw_pcm()` |
| `speak_elevenlabs_streaming()` remote-failed fallback | `play_raw_pcm()` |
| `speak_elevenlabs_streaming()` local Linux branch | **its own** `subprocess.Popen(["paplay",...])`, streams chunks to stdin |

macOS (Day) uses `afplay` in the same functions. So there are exactly two Linux choke points: `play_raw_pcm()` and the ElevenLabs streaming `Popen`. `send_audio_remote()` (remote receivers, Dusk presence relay) is not local playback.

## 4. Design

Everything stays inline in `scripts/voice-stop-hook.py` (the file header says deployments may copy only the hook file; a separate module would break them).

### 4.1 Constants and switches

- `VOXTYPE_POLL_S = 0.2` (inside the required 150-250 ms).
- `VOXTYPE_MAX_WAIT_S = 180.0` per wait.
- `VOXTYPE_BUSY_STATES = frozenset({"recording", "transcribing"})`. Every other value (`idle`, `stopped`, empty, unknown, unreadable) = not busy. Unknown future states never block speech.
- State path: env `SHELBY_VOXTYPE_STATE_FILE` if set (tests and the live simulation only; never set in any wrapper or config), else `os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "voxtype", "state")`. PID file = sibling `pid` in the same directory.
- Kill switch: config key `"voxtype_wait"` (default `true`). `false` -> the hook behaves exactly as today. `config.json` is per host and gitignored, so this is the instant rollback without a code change.
- macOS (`IS_MACOS`): the whole feature is skipped; no file access at all.

### 4.2 Helpers

- `voxtype_present() -> bool`: `os.path.exists(state_path)`; False on macOS or when switched off.
- `voxtype_busy() -> bool`: read at most 64 bytes, `strip().lower()`, busy only if in `VOXTYPE_BUSY_STATES`. Any `OSError` -> False. Stale guard: if the pid file exists, parses as an int, and `os.kill(pid, 0)` raises `ProcessLookupError`, return False (daemon died mid-recording). `PermissionError` = alive.
- `wait_for_voxtype_idle(max_wait_s=VOXTYPE_MAX_WAIT_S) -> float`: if not busy, return 0.0 after that single read (no sleep, no log line). Else log `voxtype: waiting (state=<value>)`, then loop `time.sleep(VOXTYPE_POLL_S)` until not busy or `max_wait_s` passed. Every 5 s while waiting, refresh the lockfile mtime (`os.utime(LOCKFILE_PATH)`, ignore `FileNotFoundError`) so a sibling hook does not treat a legitimate wait as a wedged hook (the steal rule fires at lockfile age > 180 s). Log the result: `voxtype: idle after N.Ns` or `voxtype: cap reached after 180s, speaking anyway`. Return seconds waited.

### 4.3 Gated local player (Linux only)

`play_raw_pcm()` Linux branch becomes:

1. If `not voxtype_present()`: run the **current code unchanged** (`subprocess.run([...paplay...], input=pcm, capture_output=True, timeout=30)`). Same args, same timeout, same exceptions. This is the Day/Dawn/Dusk path and the VoxType-stopped path.
2. Else call `_play_pcm_voxtype_gated(pcm, srate, channels)`:
   - `wait_for_voxtype_idle()`.
   - Attempt loop, at most 2 attempts (the original + one replay):
     - `Popen(paplay ..., stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL)`; register it in a module global `_current_player` (for SIGTERM cleanup); feed the PCM from a daemon thread (write all, then close stdin; ignore `BrokenPipeError`), so the main thread can poll.
     - Every `VOXTYPE_POLL_S`: if the player exited -> done. If `voxtype_busy()` -> set `interrupted_by_us = True`, `terminate()`, wait <= 1 s, `kill()` if needed. Overall per-attempt timeout 30 s (same as today): on timeout kill the player and raise `subprocess.TimeoutExpired` like today.
     - A player that exits on its own with a non-zero code (for example `voice-shutup.sh` ran `pkill -x paplay`) is **not** an interruption: no replay, return normally.
     - If `interrupted_by_us` on attempt 1: log `voxtype: dictation started during playback, stopped; will replay once`, `wait_for_voxtype_idle()` again (its own 180 s cap), then attempt 2 from the start of the same PCM.
     - If `interrupted_by_us` on attempt 2: stop, log `voxtype: dictation interrupted the replay, line dropped`, return normally. Never a third attempt.
   - Clear `_current_player` in `finally`.
3. Return contract unchanged: returns `None`; raises only where today's code would raise. A dropped line returns normally, so the Dutch lane reports PLAYED and does not trigger an English fallback (no double speech).

### 4.4 ElevenLabs streaming branch (Linux)

- `voxtype_present()` False -> current streaming `Popen` code unchanged.
- True -> read the whole stream into memory (as the macOS branch already does), then call `_play_pcm_voxtype_gated(pcm, 24000, 1)`. No padding change. CASE uses pocket; this is a fallback path.

### 4.5 SIGTERM and lock

- `handle_sigterm()` additionally terminates `_current_player` if it is set and still running, then releases the lock and `os._exit(0)` as today. (Today an orphaned player could keep talking after the hook dies.)
- The lock is held across the wait (a sibling session's line yields while one line waits, as it does today when two lines overlap; known limitation, stated here). Max hold = 180 s wait + play + 180 s wait + play; heartbeats keep it from being stolen as stale.
- `voice-shutup.sh` is not changed. When Aragorn submits a prompt, shutup kills the waiting or playing hook as today: the pending line is dropped, which is the existing behaviour.

### 4.6 What does not change

`send_audio_remote()` and every remote/Dusk relay path, `find_audible_path()`, engine selection, Dutch lane routing, sanitising, the lockfile steal rule, `voice-hook.sh`, `voice-shutup.sh`, `tts.py`, `voice_server.py`, `ack-play.sh`, macOS behaviour.

## 5. Files

- `scripts/voice-stop-hook.py` (only code file changed).
- `tests/test_voxtype_wait.py` (new, `unittest`, imports the hook with `importlib` like `tests/test_dusk_presence_routing.py`).
- `docs/voxtype-wait-SPEC.md` (this file).

This spec was written untracked into the live checkout (`docs/` is new); copy it into the worktree and commit it on the feature branch with the code.

Development happens in a git worktree, not in the live checkout: `git -C ~/projects/claude-voice worktree add ~/projects/claude-voice-voxtype -b feat/voxtype-wait` (write scope = that worktree). The live checkout changes only when the reviewed branch lands on `main`. Never `git reset --hard`, checkout or stash in `~/projects/claude-voice` (it is the deployed copy).

## 6. Tests (`tests/test_voxtype_wait.py`; no real audio)

Fixtures: a temp dir with `state` and `pid` files, `SHELBY_VOXTYPE_STATE_FILE` pointing at it, `LOCKFILE_PATH` patched to a temp file, `VOXTYPE_MAX_WAIT_S` patched small where needed. A fake `paplay` executable placed first on `PATH` in a temp dir: it appends `start <pid>` to a log, reads stdin to EOF, sleeps `FAKE_PLAY_S` (default 1.0), appends `end`. Real `paplay` must never run in tests.

1. No state file: `voxtype_busy()` False; `wait_for_voxtype_idle()` returns 0.0 in < 5 ms; `play_raw_pcm` calls `subprocess.run` with exactly today's args and `timeout=30` (mocked).
2. `idle`: player starts within 0.3 s.
3. `recording`: no player for 0.6 s; flip to `idle`; player starts within 0.4 s of the flip.
4. `transcribing`: same as 3.
5. `stopped`, empty, `garbage`: not busy.
6. Cap: `recording` forever, cap patched to 0.8 s: player starts after 0.8-1.2 s and the log line says cap reached.
7. Stale: `recording` + pid file with a dead pid -> not busy, plays at once.
8. Interruption: `idle`, `FAKE_PLAY_S=2`; at 0.5 s write `recording`: first player gone within 0.5 s; at 1.2 s write `idle`: a second player runs to `end`; exactly 2 `start` lines.
9. Interrupted replay: as 8, but write `recording` again 0.3 s into the replay: replay stopped, exactly 2 `start` lines, function returns normally.
10. External kill (shutup simulation): kill the fake player's pid from the test while state stays `idle`: no replay (1 `start` line).
11. Lock heartbeat: during a 6 s wait the lockfile mtime advances at least once.
12. macOS (`IS_MACOS` patched True): no read of the state path at all (open/exists mocked and asserted not called for that path).
13. Kill switch `voxtype_wait: false` with state `recording`: plays at once, today's path.
14. ElevenLabs Linux branch: VoxType present -> buffered then gated (mock `requests` stream); absent -> the existing streaming `Popen` path (mocked) is used.
15. SIGTERM during gated playback: calling `handle_sigterm` (with `os._exit` patched) terminates the fake player.

All existing tests still pass: `python3 -m unittest discover -s tests -p 'test_*.py'` (>= 94 + new). `bash tests/test-voice-shutup.sh ~/scripts/voice-shutup.sh` still passes.

## 7. Acceptance checks (CASE, run from the worktree unless stated)

Runner: `PY=~/claude-voice-venv/.venv/bin/python3`, `WT=~/projects/claude-voice-voxtype`, `S=<scratch dir>`.

- **B1 tests**: `cd $WT && $PY -m unittest discover -s tests -p 'test_*.py'` -> OK, count >= 109.
- **B2 zero cost without VoxType**: `SHELBY_VOXTYPE_STATE_FILE=$S/absent/state $PY -c` (import the hook via importlib, time 1000 calls of `wait_for_voxtype_idle()`) -> mean < 0.05 ms per call.
- **B3 live, idle**: real state file reads `idle` (`cat /run/user/1000/voxtype/state`). `echo '{"last_assistant_message":"<voice lang=\"en\">VoxType gate check one.</voice>","session_id":"voxtest"}' | $PY $WT/scripts/voice-stop-hook.py` -> line is heard; `/tmp/claude-tts.log` has no `voxtype: waiting` line for this run.
- **B4 live, simulated busy**: `printf recording > $S/state; printf %s $(cat /run/user/1000/voxtype/pid) > $S/pid` then the same command with `SHELBY_VOXTYPE_STATE_FILE=$S/state` in the background. After 3 s: `pgrep -x paplay` empty and the log shows `voxtype: waiting (state=recording)`. `printf idle > $S/state`: within 0.5 s `pgrep -x paplay` finds a player and the line is heard.
- **B5 live, simulated interruption**: same setup with state `idle` and a longer line; 1 s after `paplay` appears write `recording`: `paplay` disappears within 0.5 s; write `idle`: the line replays from the start once.
- **B6 untouched real state**: `stat -c %Y /run/user/1000/voxtype/state` unchanged by B1-B5 (the tests never write it). No real `voxtype record` command is run (it would type text into the focused window).
- **B7 no regressions in wiring**: after landing on `main`, a normal Claude Code turn on CASE speaks its `<voice>` line as before; `voice-shutup` on prompt submit still stops speech (`tests/test-voice-shutup.sh` passes).

## 8. Failure modes

| Situation | Behaviour |
|---|---|
| VoxType not installed / daemon stopped (state file absent) | Exactly today's code path. |
| Daemon crashed while `recording` | PID check -> not busy; otherwise the 180 s cap. |
| Dictation longer than 180 s | Speaks after 180 s anyway (as asked). |
| Aragorn submits a prompt while a line waits | `voice-shutup.sh` kills the hook; line dropped (today's behaviour). |
| State file unreadable / garbage | Not busy; speak. |
| Second dictation during the replay | Stop and drop; never a third attempt. |
| Another session's line arrives during a wait | It yields, as today for overlapping lines. |

## 9. Rollback

1. Instant, per host, no code change: set `"voxtype_wait": false` in that host's `scripts/config.json`.
2. Code: `git revert <merge commit>` on `main` and push; on CASE run `git -C ~/projects/claude-voice pull --ff-only` (never `reset --hard` in the deployed checkout). Other hosts pick it up on their normal pull; for them the change was a no-op anyway.
