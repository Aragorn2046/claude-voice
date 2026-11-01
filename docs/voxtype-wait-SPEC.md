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
- `VOXTYPE_MAX_WAIT_S = 180.0`. One budget covers the pre-lock wait and the first gate wait of that hook run. The replay wait after an interruption keeps its own full cap.
- `VOXTYPE_BUSY_STATES = frozenset({"recording", "transcribing"})`. Every other value (`idle`, `stopped`, empty, unknown, unreadable) = not busy. Unknown future states never block speech.
- State path: env `SHELBY_VOXTYPE_STATE_FILE` if set (tests and the live simulation only; never set in any wrapper or config), else `os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "voxtype", "state")`. PID file = sibling `pid` in the same directory.
- Kill switch: config key `"voxtype_wait"` (default `true`). `false` -> the hook behaves exactly as today. `config.json` is per host and gitignored, so this is the instant rollback without a code change.
- macOS (`IS_MACOS`): the whole feature is skipped; no file access at all.

### 4.2 Helpers

- `voxtype_present() -> bool`: `os.path.exists(state_path)`; False on macOS or when switched off.
- `voxtype_busy() -> bool`: read at most 64 bytes, `strip().lower()`, busy only if in `VOXTYPE_BUSY_STATES`. Any `OSError` -> False. Stale guard: if the pid file exists, parses as an int, and `os.kill(pid, 0)` raises `ProcessLookupError`, return False (daemon died mid-recording). `PermissionError` = alive.
- `wait_for_voxtype_idle(max_wait_s=VOXTYPE_MAX_WAIT_S, heartbeat=True) -> float`: if not busy, return 0.0 after that single read (no sleep, no log line). Else log `voxtype: waiting (state=<value>)`, then loop `time.sleep(VOXTYPE_POLL_S)` until not busy or `max_wait_s` passed. When `heartbeat` is true (the default, used while the lock is held), every 5 s refresh the lockfile mtime with `os.utime` on the held lock fd (no refresh when that fd is absent; ignore `FileNotFoundError` and `OSError`) so a sibling hook does not treat a legitimate wait as a wedged hook (the steal rule fires at lockfile age > 180 s). The pre-lock wait passes `heartbeat=False` and does not touch the lockfile. Log the result: `voxtype: idle after N.Ns` or `voxtype: cap reached after 180s, speaking anyway`. Return seconds waited. A `max_wait_s` of 0 or less, while VoxType is still busy, logs that cap line once and returns without sleeping.

### 4.3 Gated local player (Linux only)

`play_raw_pcm()` Linux branch becomes:

1. If `not voxtype_present()`: run the **current code unchanged** (`subprocess.run([...paplay...], input=pcm, capture_output=True, timeout=30)`). Same args, same timeout, same exceptions. This is the Day/Dawn/Dusk path and the VoxType-stopped path.
2. Else call `_play_pcm_voxtype_gated(pcm, srate, channels)`:
   - First wait: if this hook run armed a budget (the pre-lock wait did), spend only what is left of that one cap. If the cap already fired, do not wait again and do not log a second `cap reached` line; playback stays unarmed for interrupt, same as any other cap. If the budget was never set, wait the full cap as before.
   - Attempt loop, at most 2 attempts (the original + one replay):
     - `Popen(paplay ..., stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL)`; register it in a module global `_current_player` (for SIGTERM cleanup); feed the PCM from a daemon thread (write all, then close stdin; ignore `BrokenPipeError`), so the main thread can poll.
     - Every `VOXTYPE_POLL_S`: if the player exited -> done. If `voxtype_busy()` -> set `interrupted_by_us = True`, `terminate()`, wait <= 1 s, `kill()` if needed. Overall per-attempt timeout 30 s (same as today): on timeout kill the player and raise `subprocess.TimeoutExpired` like today.
     - A player that exits on its own with a non-zero code (for example `voice-shutup.sh` ran `pkill -x paplay`) is **not** an interruption: no replay, return normally.
     - If `interrupted_by_us` on attempt 1: log `voxtype: dictation started during playback, stopped; will replay once`, `wait_for_voxtype_idle()` again (its own fresh 180 s cap, not the leftover pre-lock budget), then attempt 2 from the start of the same PCM.
     - If `interrupted_by_us` on attempt 2: stop, log `voxtype: dictation interrupted the replay, line dropped`, return normally. Never a third attempt.
   - Clear `_current_player` in `finally`.
3. Return contract unchanged: returns `None`; raises only where today's code would raise. A dropped line returns normally, so the Dutch lane reports PLAYED and does not trigger an English fallback (no double speech).

### 4.4 ElevenLabs streaming branch (Linux)

- `voxtype_present()` False -> current streaming `Popen` code unchanged.
- True -> read the whole stream into memory (as the macOS branch already does), then call `_play_pcm_voxtype_gated(pcm, 24000, 1)`. No padding change. CASE uses pocket; this is a fallback path.

### 4.5 SIGTERM and lock

- `handle_sigterm()` terminates `_current_player` if it is set and still running, then releases the lock and `os._exit(0)` as today. `release_lock(None)` is safe, so a hook killed during the pre-lock wait (no lock and no player) still exits cleanly.
- On Linux, when the gate is on, a VoxType state file exists, and `remote_audio` is not set, `main()` waits for VoxType before `acquire_lock()`. That wait does not hold the lock and does not refresh the lockfile mtime. It records a deadline of its start plus `VOXTYPE_MAX_WAIT_S` (the cap is read at that call) and whether the cap already fired. `main()` resets that budget at the start of every run. The first gate wait in the run, including a fallback engine that plays after another engine failed, uses what remains. If nothing is left and VoxType is busy, it returns the cap at once. Hosts with `remote_audio` keep today's path: take the lock, then the in-lock gate, and that gate waits a full cap because no pre-lock budget was set. A direct call with no budget does the same.
- A line that actually waited (a held line) then calls `acquire_lock(queue_wait_s=VOXTYPE_MAX_WAIT_S)`, reading the cap at call time. While a live sibling holds the lock, it polls every `VOXTYPE_POLL_S` and decides again, including the steal rules, until it takes the lock or the budget passes. It then logs `Yielding lock` and drops the line. A line that did not wait uses `queue_wait_s=0`: one decision, and a yield returns at once.
- The in-lock gate is unchanged. Dictation that starts during synthesis or playback still waits, stops, and replays once, with the lock held and the heartbeat on. A sibling that arrives during that in-lock wait sees VoxType busy, so it waits pre-lock and then queues. The line is not dropped.
- Non-VoxType hosts keep main's steal: when the recorded holder is alive, SIGKILL it and retry `LOCK_NB` once, immediately. No poll and no SIGTERM.
- VoxType hosts: SIGTERM the live holder, poll `LOCK_NB` for up to 0.5 s, then SIGKILL it if it is still alive and stop its paplay children, then poll `LOCK_NB` for up to 1 s. The new line does not start until that stop has finished. A dead holder is not signalled.
- `main()` uses the VoxType steal only when the host is not macOS, the gate is on, and the state file is present. It reads presence once. A missing state file keeps main's steal.
- Same-session ordering, on that same VoxType-steal path (including `remote_audio`): the newest line wins. The older waiter yields; it does not steal the newer line. Dropping one same-session line is by design. Hosts without VoxType, macOS, and `voxtype_wait` disabled do not use this and do no registry IO.
- The registry directory is `SHELBY_VOICE_ARRIVAL_DIR` when that is set, otherwise `$XDG_RUNTIME_DIR/voice-stop-hook-arrivals`. If `XDG_RUNTIME_DIR` is unset and there is no override, ordering stays off. There is no fallback to `/tmp` or any other world-writable directory. The path is read at call time. The hook creates the directory mode `0700`, then `lstat`s it. It must be a real directory, not a symlink, owned by this user, with no group or other write bits. The per-session subdirectory gets the same checks. Its name is the first 16 hex characters of `sha256(session_id)`, never the raw session id. An empty session id turns ordering off. A failed check logs `voxtype: arrival registry unsafe, ordering off` once and keeps today's behaviour.
- The arrival stamp is `time.monotonic_ns()` taken at the start of `main()`, before stdin is read. After the line is known to be speakable, and before the pre-lock wait, the hook creates `<stamp 20 digits>-<pid>` with `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` and mode `0600`. It removes that file on every exit after registration, and best-effort from `handle_sigterm`. A SIGKILLed hook leaves a stale file; the liveness check unlinks a dead pid, so that is harmless.
- A newer entry has a greater `(stamp, pid)`. It counts only when the pid is greater than 1, is not this process, `os.kill(pid, 0)` shows it alive, and `_is_our_hook` matches. Anything else in the directory is skipped. Dead pids are unlinked. A live pid that fails the hook check is left in place. Any `OSError` means there is no newer line, so a registry error never blocks speech.
- The check runs right before `acquire_lock`, and on the graceful path before each take and before a same-session steal (`yield_to_newer`, default `None`). The steal poll checks again before each of its takes and right after a take wins. A newer line there releases the fd and returns at once, after the signalled holder's player has been stopped. It does not start the follow-up poll. A newer arrival logs `voxtype: newer line from this session pending, dropping this one` once and returns without speaking. A check that fires before the steal does not signal the holder. The newer line still steals an older holder. That is main's rule. `yield_to_newer=None` does not change today's steal.
- Success stamps and returns the fd. Failure returns None, or keeps polling while `queue_wait_s` remains. Every fd that is not kept is closed. There is no blocking flock.
- On the graceful path, before the arrival entry, the hook baselines `transcript_path` from the Stop stdin when that value is a string. It opens the path `O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC`, and keeps the watch only when `fstat` shows a regular file owned by this user. It records device, inode, and size. A missing path, a non-string, or any failure leaves the watch off. Behaviour is then today's.
- The held phase starts when that baseline is usable and ends when this hook wins the flock. The flag is cleared inside the successful take, before the lock file is stamped, and on every exit. A lost watch clears it too. While it is on, the hook reads the transcript on each pre-lock poll, on each queue sleep, and before each lock take. It does not read the transcript during the in-lock gate, and it does not read it during the short steal poll after a holder was signalled.
- A new complete line is this session's own prompt when it is a JSON object with `type` `user`, `isMeta` and `isSidechain` not set, and `origin.kind` `human` when that key is present. `message.content` is a string, or a list with no `tool_result` part whose text is the `text` parts joined. After leading whitespace is stripped the text must be non-empty and must not start with `<task-notification`, `<command-`, `<local-command`, `[SYSTEM NOTIFICATION`, `This session is being continued from a previous conversation`, or `Caveat:`. A partial line waits for its newline. The hook reads at most 1 MiB per call and never blocks.
- An own prompt drops the held line. The hook logs `voxtype: prompt submitted in this session, held line dropped`, speaks nothing, keeps no lock, and the arrival entry is cleared. That happens within one poll tick, including when another session holds the lock.
- A SIGTERM during the held phase does not exit. The hook remembers the time. If no own prompt arrives within 2 s, it logs `voxtype: held line kept, prompt was in another session` once and keeps waiting. If the watch is lost while that SIGTERM is still pending, the hook exits as it does today. If the hook wins the lock while those 2 s are still running, it checks the transcript every 0.1 s until the grace ends. An own prompt in that time releases the lock and drops the line with the same log line. Otherwise it speaks. Outside the held phase, SIGTERM exits as today.
- `voice-shutup.sh` is not changed. With no transcript, or an unusable transcript, a prompt still kills the hook the way it does today.

Speech-lock steal is hardened against a stale or forged `/tmp/sonia-tts.lock`. A recorded pid is accepted only when it is an int greater than 1 and not this process. Before any signal to that holder (graceful SIGTERM, the later SIGKILL, and main's SIGKILL), `_is_our_hook` must match: on Linux `/proc/<pid>/cmdline` contains `voice-stop-hook` and the real uid in `/proc/<pid>/status` equals this user; on macOS `ps -o uid=,command= -p <pid>` must show the same. Anything else is a dead holder: no signal, then the usual flock retry (the graceful path polls; main's path tries once). The SIGKILL is verified again immediately before it is sent. The lock is opened with `O_NOFOLLOW | O_NONBLOCK` and kept only when `fstat` shows a regular file owned by this user. A FIFO cannot block the open. A symlink (`ELOOP`), another owner, or a non-regular file is refused with one `lock refused:` line and `acquire_lock` returns None without writing, signalling, or polling. Lock age comes from that fd's `st_mtime`. The in-lock heartbeat calls `os.utime` on the held lock fd and skips the refresh when no fd is held. A paplay child is signalled only when its ppid matches, its comm is still `paplay`, its cmdline argv[0] basename is exactly `paplay`, and its real uid is this user.

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
| Dictation longer than 180 s | Speaks after 180 s anyway (as asked). That one cap covers the pre-lock wait and the first gate wait together. The replay wait has its own cap. |
| Aragorn submits a prompt while a line waits | Own session: the held line is dropped within one poll tick, also when another session holds the lock. Another session's prompt: the held line is kept. No transcript, or an unusable transcript: today's behaviour. |
| State file unreadable / garbage | Not busy; speak. |
| Second dictation during the replay | Stop and drop; never a third attempt. |
| Another session's line arrives during a wait | The waiter holds no lock. If VoxType is busy the new line waits first, then queues for the lock for up to 180 s and is spoken after the holder finishes. A line that finds VoxType already idle still yields at once to a live sibling. |
| Second Stop in the same session | Newest line wins. On a VoxType host the older waiter yields when the arrival registry shows a newer line from that session, and does not steal it. If the older line already holds the lock, the newer hook SIGTERMs it, stops its paplay, and retries the lock for up to 1 s, then speaks. Any other host keeps main's steal: SIGKILL and one immediate retry. Dropping one same-session line is by design. |

## 9. Rollback

1. Instant, per host, no code change: set `"voxtype_wait": false` in that host's `scripts/config.json`.
2. Code: `git revert <merge commit>` on `main` and push; on CASE run `git -C ~/projects/claude-voice pull --ff-only` (never `reset --hard` in the deployed checkout). Other hosts pick it up on their normal pull; for them the change was a no-op anyway.
