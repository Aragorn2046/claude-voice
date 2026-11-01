# VoxType speech wait — SPEC

Status: architect spec 2026-10-31 (CASE), queue mode 2026-11-01. Code is written by the code worker from this spec.

Queue mode replaces the per-session arrival registry, the transcript watch, newest-line-wins, the per-wait fresh caps, and the dictation 5-replay limit. An outside kill of a line that is already playing is replayed from the start, at most 5 times.

## 1. Goal (Aragorn's words)

"The voice-feedback from Shelby/TTS should wait for me to finish when I'm recording with VoxType."

"Whenever the voice is telling me something, the Shelby Jarvis voice, and I press enter or any key, it is interrupted and stops speaking. It also still starts speaking when I'm actually using voxtype to record. I thought we agreed that it would hold messages until I stopped recording when I'm recording."

Chosen behaviour: JARVIS always finishes his line. The reply to the new prompt waits and plays after it. A message is not dropped because a newer line arrived or because Enter was pressed.

## 2. The bar

Like a good human assistant in the room: if Aragorn is talking (dictating), Shelby holds the line until he is done, then says it in full. If he starts dictating while Shelby talks, Shelby stops at once (so the mic does not capture Shelby), then repeats the line from the start once he is done. A line already queued is not thrown away when a new prompt arrives: it finishes, then the next line plays, older first, across sessions and within one session. On machines without VoxType nothing changes at all, not even by a millisecond that anyone could measure.

## 3. Facts (probed 2026-10-31 on CASE)

- VoxType 1.1.1, `/usr/bin/voxtype`, user service `voxtype.service`, config `~/.config/voxtype/config.toml` with `state_file = "auto"`.
- State file: `$XDG_RUNTIME_DIR/voxtype/state` = `/run/user/1000/voxtype/state`. Content is a bare word, no newline. Values the daemon writes (config comment and binary strings): `idle`, `recording`, `transcribing`. UI themes also know `stopped`, which the status UI shows when no daemon runs (the daemon removes the state file on clean exit).
- PID file next to it: `/run/user/1000/voxtype/pid`, bare digits, no newline (today `135648` = `/usr/bin/voxtype daemon`).
- `voxtype status` prints `idle`; `voxtype status --format json` gives `"alt":"idle","class":"idle"`.
- Live wiring on CASE: Claude Code Stop hook -> `~/scripts/voice-hook.sh` -> background `~/claude-voice-venv/.venv/bin/python3 ~/projects/claude-voice/scripts/voice-stop-hook.py`. **The checkout `~/projects/claude-voice` IS the live install on CASE**: every edit there is live on the next Stop hook.
- UserPromptSubmit -> `~/scripts/voice-shutup.sh`: `pkill -x paplay`, `pkill` of `voice-stop-hook.py` (SIGTERM), `rm -f /tmp/sonia-tts.lock`. That script is outside this repo and is not changed here.
- CASE config: `tts_engine=pocket`, `pocket_tts_url=http://127.0.0.1:8935`, `remote_audio=false`.
- Python 3.13 in the venv. Test runner: `unittest` (no pytest).

### Local playback paths in `scripts/voice-stop-hook.py` (all of them)

| Path | Where it plays on Linux |
|---|---|
| `play_raw_pcm()` | gated paplay when VoxType is present; otherwise `subprocess.run(["paplay","--raw",...], input=pcm, timeout=30)` |
| `play_audio_file()` | decodes with soundfile -> `play_raw_pcm()` |
| `speak_edge()` local | `play_raw_pcm()` |
| `speak_pocket()` local | `play_audio_file()` |
| Dutch lane `play_dutch_locally_once()` | `play_audio_file()` |
| `speak_kokoro()` | `play_raw_pcm()` |
| `speak_elevenlabs_streaming()` remote-failed fallback | `play_raw_pcm()` |
| `speak_elevenlabs_streaming()` local Linux branch | buffered, then the same gated player when VoxType is present; otherwise its own streaming `Popen` |

macOS (Day) uses `afplay` in the same functions. So there are exactly two Linux choke points: `play_raw_pcm()` and the ElevenLabs streaming `Popen`. `send_audio_remote()` (remote receivers, Dusk presence relay) is not local playback.

## 4. Design

Everything stays inline in `scripts/voice-stop-hook.py` (the file header says deployments may copy only the hook file; a separate module would break them).

### 4.1 Constants and switches

- `VOXTYPE_POLL_S = 0.2` (inside the required 150-250 ms).
- `VOXTYPE_MAX_WAIT_S = 300.0`. One shared wait budget per hook run, counted once across the pre-lock VoxType wait, the queue wait, the in-lock gate, and every replay wait. Time spent synthesising or actually playing does not count. The cap is read at call time. An explicit `max_wait_s` further limits that call (the minimum of the argument and the budget still left). `max_wait_s <= 0` while busy logs the cap line once and returns without sleeping.
- `VOXTYPE_MAX_OUTSIDE_REPLAYS = 5`. Cap on replays caused by an outside signal (a player death this hook did not request). Dictation replays do not count toward it. Read at call time. When the next outside kill would pass the cap, the hook logs the give-up line and returns without raising.
- `VOXTYPE_BUSY_STATES = frozenset({"recording", "transcribing"})`. Every other value (`idle`, `stopped`, empty, unknown, unreadable) = not busy. Unknown future states never block speech.
- `VOXTYPE_HEARTBEAT_S = 5.0`. One helper, used by every poll loop in queue mode (pre-lock wait, queue wait, in-lock gate, and the gated playback poll in `_play_one_paplay`). At most once per interval it refreshes the mtime of the held lock fd (`os.utime` on the fd; skipped when no fd is held) and of this hook's ticket (`os.utime(path, follow_symlinks=False)`, OSError ignored).
- `LOCK_STALE_S = 180.0`. A verified live holder older than this is wedged. Read at call time. The heartbeat keeps a hook that is waiting or playing from looking stale.
- State path: env `SHELBY_VOXTYPE_STATE_FILE` if set (tests and the live simulation only; never set in any wrapper or config), else `os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "voxtype", "state")`. PID file = sibling `pid` in the same directory.
- Kill switch: config key `"voxtype_wait"` (default `true`). `false` -> queue mode off -> the hook behaves exactly as main's non-VoxType path. `config.json` is per host and gitignored, so this is the instant rollback without a code change.
- macOS (`IS_MACOS`): the whole feature is skipped; no state-file access and no queue IO.

There is no dictation replay-count limit and no transcript watch. An outside kill of the player is replayed at most `VOXTYPE_MAX_OUTSIDE_REPLAYS` times (5).

### 4.2 Queue mode

Queue mode is exactly the old graceful-steal condition: `not IS_MACOS and _VOXTYPE_WAIT_ENABLED and voxtype_present()`. Presence is read once. That includes `remote_audio` hosts.

When queue mode is off (macOS, `voxtype_wait: false`, or no VoxType state file: Day, Dawn, Dusk) the hook keeps main's path: `acquire_lock(queue_wait_s=0, graceful_steal=False)` SIGKILLs a verified same-session, dead, or stale holder and retries `LOCK_NB` once, immediately; it yields to a live other-session holder with a fresh lock. SIGTERM exits as before. There is no queue or registry IO, no extra poll, and no extra sleep.

### 4.3 Helpers

- `voxtype_present() -> bool`: `os.path.exists(state_path)`; False on macOS or when switched off.
- `voxtype_busy() -> bool`: read at most 64 bytes, `strip().lower()`, busy only if in `VOXTYPE_BUSY_STATES`. Any `OSError` -> False. Stale guard: if the pid file exists, parses as an int, and `os.kill(pid, 0)` raises `ProcessLookupError`, return False (daemon died mid-recording). `PermissionError` = alive.
- `wait_for_voxtype_idle(max_wait_s=None, heartbeat=True) -> float`: draws on the shared budget. If not busy, return 0.0 after that single read (no sleep, no log line). Else log `voxtype: waiting (state=<value>)`, then loop `time.sleep(VOXTYPE_POLL_S)` until not busy or the allowance passes. Each sleep is charged to the shared budget. When `heartbeat` is true (the default), the shared helper refreshes the held lock fd, if there is one, and this hook's ticket. No fd means the lock file is not touched, so a pre-lock wait cannot hide a wedged holder. Log `voxtype: idle after N.Ns` or `voxtype: cap reached after <N>s total wait while still busy, line dropped`. `<N>` is the shared budget. Return seconds waited. A fired cap drops the line. It is never played over the recording.

### 4.4 FIFO ticket queue

One global queue replaces the per-session arrival registry. There is no session id in the ticket name or body, and there is no "newest line wins" rule. Same-session lines both play, older first.

- Directory: env `SHELBY_VOICE_QUEUE_DIR` if set (tests only), else `$XDG_RUNTIME_DIR/voice-stop-hook-queue`. No override and no `XDG_RUNTIME_DIR`: registry off (lock-only ordering). Never a `/tmp` or world-writable fallback. Read at call time.
- Create mode `0700`. `lstat` must show a real directory (not a symlink) owned by this uid with no group or other write bits. Otherwise log once `queue: registry unsafe, ordering by lock only` and run with the registry off.
- The stamp is `time.monotonic_ns()` taken at the start of `main()`, before stdin is read. After the line is known speakable, and before any wait, the hook creates `<stamp 20 digits>-<pid>` with `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` and mode `0600`.
- The ticket is removed on every exit after registration (normal, drop, exception, and best-effort on the non-queue SIGTERM path).
- "Older ticket ahead" is an entry matching `^\d{20}-\d+$` with `(stamp, pid) < (my_stamp, my_pid)` whose pid is > 1, not this process, alive (`os.kill(pid, 0)`; `PermissionError` = alive), `_is_our_hook(pid)` true, and whose ticket mtime is not older than `LOCK_STALE_S`. Dead pids are unlinked. A live pid that fails `_is_our_hook` is left in place. A stale-by-mtime live entry is skipped (not unlinked, not signalled). Any `OSError` during the scan means there is no older ticket. A registry error must not block speech; the flock still prevents overlap.

A line takes its turn only when no older ticket is ahead and `flock(LOCK_EX|LOCK_NB)` succeeds. While either fails, it polls every `VOXTYPE_POLL_S`. Each sleep counts against the shared budget. The first time it queues it logs `queue: waiting for the line ahead to finish`. When the budget runs out while still queued: log `queue: cap reached after <N>s, line dropped` and speak nothing. The lock is not kept.

While the flock is held by someone else, the hook reads the lock record as before:

- Holder pid alive, verified as our hook, and lock age <= `LOCK_STALE_S`: never signal it, whatever its session (same session included). Keep waiting.
- Holder alive, verified, and lock age > `LOCK_STALE_S` (wedged): today's graceful stop. After the paplay children scan, SIGTERM is sent only if `_is_our_hook` still matches immediately before the signal. If that second check fails, send no signal and do not call `_stop_players`. Otherwise poll take up to `LOCK_STEAL_TERM_WAIT_S`, then SIGKILL only if it still verifies, and stop its verified paplay children via `_stop_players`, then poll take up to `LOCK_STEAL_RETRY_S`. The new line does not start before that stop has finished. `_stop_players` SIGKILLs the wedged hook before it signals paplay, so that hook cannot replay a player the steal itself stopped.
- Holder dead, record empty or truncated, unparsable, or pid failing verification: no signal. Keep polling take.
- `lock refused:` (symlink, non-regular, other owner) stays as before: one log line, return None, no write, no signal.

### 4.5 Hold while recording

In queue mode and not `remote_audio`, the line does not take its turn while `voxtype_busy()` (state `recording` or `transcribing`). Unknown, idle, stopped, empty, unreadable, or a dead daemon pid is not busy. Log `voxtype: waiting (state=<s>)` and `voxtype: idle after N.Ns`. `remote_audio` hosts keep today's order (no pre-lock VoxType wait; the in-lock gate only) but still use the FIFO queue.

The transcript watch is gone. There is no own-prompt drop, no held phase, and no deferred-SIGTERM grace. A prompt does not drop a held line.

SIGTERM in queue mode: from the moment queue mode is decided in `main()`, `handle_sigterm` logs once `queue mode: SIGTERM ignored, line will finish` and returns. No exit, no player terminate, no lock release. This holds in every phase: pre-lock wait, queued, synthesis, playback, and replay wait. Before queue mode is decided, and whenever queue mode is off, `handle_sigterm` cleans up and exits as before. SIGKILL (the stale-steal path) still ends a wedged hook.

`voice-shutup.sh` (outside this repo) still runs `pkill -x paplay` and SIGTERM on the hook when Enter is pressed. Queue mode ignores that SIGTERM, so the hook stays alive. If the player then dies from a signal this hook did not send (negative wait status), the line is not lost: log `voxtype: player stopped by outside signal <N>, will replay from start (<k>)`, wait until VoxType is idle on the shared budget, and play the same PCM from the start. A clean exit or a positive exit code (paplay error) is not an interruption and is not replayed. Outside-kill replays stop at 5. The next outside kill logs `voxtype: player stopped by outside signal again, giving up after <n> replays` and returns without raising. A line that has not started yet is unchanged: it waits and plays after the recording goes idle.

Dictation that starts during playback stops our player, logs `voxtype: dictation started during playback, stopped; will replay (<k>)`, waits until idle, and replays the same PCM from the start. It repeats as often as the shared budget allows. Dictation has no replay-count limit and no "interrupted the line N times" drop. Outside kills do not consume that unlimited budget of attempts; they have their own cap of 5.

If the shared budget runs out while VoxType is still busy (pre-lock, in-lock gate before the first play, or a replay wait): never play over the recording. Log `voxtype: cap reached after <N>s total wait while still busy, line dropped` and return normally. A dropped line returns None, no exception, and no English fallback.

### 4.6 Gated local player (Linux only)

`play_raw_pcm()` Linux branch:

1. If `not voxtype_present()`: run the current non-VoxType code (`subprocess.run([...paplay...], input=pcm, capture_output=True, timeout=30)`). Same args, same timeout, same exceptions. This is the Day/Dawn/Dusk path and the VoxType-stopped path.
2. Else call `_play_pcm_voxtype_gated(pcm, srate, channels)`:
   - The first gate wait and every replay wait draw on the one shared budget. `VOXTYPE_MAX_WAIT_S` is read at call time and further limits that call.
   - If the allowance passes while VoxType is still busy, log the cap line and return. No player starts. The same drop applies to every replay wait.
   - Playback starts only after a wait that ended idle, so interrupt is armed from the first poll.
   - There is no dictation attempt limit. Dictation may interrupt as often as the budget allows. Outside-kill replays are capped at `VOXTYPE_MAX_OUTSIDE_REPLAYS` (5).
     - `Popen(paplay ..., stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL)`; register it in `_current_player`; feed the PCM from a daemon thread. The poll loop calls the heartbeat helper. Overall per-attempt timeout 30 s: on timeout kill the player and raise `subprocess.TimeoutExpired`. That timeout kill is not an outside kill and is not replayed.
     - A clean exit, or a positive non-zero exit (paplay error), is not an interruption: no replay, return normally.
     - If this hook did not stop the player and `returncode` is negative (an outside signal, for example `voice-shutup.sh` ran `pkill -x paplay`): that is an interruption. Log `voxtype: player stopped by outside signal <N>, will replay from start (<k>)` once, wait again on the shared budget, then play the same PCM from the start. `<N>` is the signal number. At most 5 such replays. The next outside kill logs `voxtype: player stopped by outside signal again, giving up after <n> replays` and returns without raising. A cap that fires on the replay wait drops the line with the existing busy-cap log and does not play over the recording.
     - If dictation interrupted this attempt: log `voxtype: dictation started during playback, stopped; will replay (<k>)`, wait again on the shared budget, then play the same PCM from the start.
   - Clear `_current_player` in `finally`.
3. Return contract unchanged: returns `None`; raises only where today's code would raise. A dropped line returns normally, so the Dutch lane reports PLAYED and does not trigger an English fallback (no double speech).

### 4.7 ElevenLabs streaming branch (Linux)

- `voxtype_present()` False -> current streaming `Popen` code unchanged.
- True -> read the whole stream into memory (as the macOS branch already does), then call `_play_pcm_voxtype_gated(pcm, 24000, 1)`. No padding change. CASE uses pocket; this is a fallback path. The outside-kill replay rule applies because this branch uses the gated player.
- False (state file absent) keeps the streaming `Popen`. That path is not queue mode. An outside `pkill` can still cut it, and it is not replayed. It does not fit the gated replay loop, so it is left unchanged.

### 4.8 Lock hardening (unchanged rules)

Speech-lock steal is hardened against a stale or forged `/tmp/sonia-tts.lock`. A recorded pid is accepted only when it is an int greater than 1 and not this process. Before any signal to that holder, `_is_our_hook` must match: on Linux `/proc/<pid>/cmdline` contains `voice-stop-hook` and the real uid in `/proc/<pid>/status` equals this user; on macOS `ps -o uid=,command= -p <pid>` must show the same. Anything else is not signalled. On the queue stale path, SIGTERM is sent only after a second `_is_our_hook` check immediately before the signal (after the children scan). If that check fails, send no SIGTERM and do not call `_stop_players`; treat the holder like the dead or unverified case. The SIGKILL is verified again immediately before it is sent. On the non-queue path the same rule applies to that SIGKILL: a second `_is_our_hook` check immediately before the signal, and no signal if it fails. The lock is opened with `O_NOFOLLOW | O_NONBLOCK` and kept only when `fstat` shows a regular file owned by this user. A FIFO cannot block the open. A symlink (`ELOOP`), another owner, or a non-regular file is refused with one `lock refused:` line and `acquire_lock` returns None without writing, signalling, or polling. Lock age comes from that fd's `st_mtime`. A paplay child is signalled only when its ppid matches, its comm is still `paplay`, its cmdline argv[0] basename is exactly `paplay`, and its real uid is this user. Never a blocking flock. `_stop_players` still SIGKILLs the wedged hook before it stops that hook's paplay, so the killed hook cannot replay.

On the non-queue path, `queue_wait_s=0` is one decision: a yield returns at once. `queue_wait_s>0` polls until that budget passes, then logs `Yielding lock`.

### 4.9 What does not change

`send_audio_remote()` and every remote/Dusk relay path, `find_audible_path()`, engine selection, Dutch lane routing, sanitising, `voice-hook.sh`, `voice-shutup.sh` (see the note in 4.5: it still kills `paplay`; queue mode replays that line), `tts.py`, `voice_server.py`, `ack-play.sh`, macOS behaviour. The non-VoxType steal is main's SIGKILL plus one immediate retry, with the second verification in 4.8. The ElevenLabs streaming `Popen` used when VoxType is absent is unchanged (section 4.7).

## 5. Files

- `scripts/voice-stop-hook.py` (only code file changed).
- `tests/test_voxtype_wait.py` (unittest, imports the hook with `importlib`).
- `docs/voxtype-wait-SPEC.md` (this file).

Development happens in a git worktree, not in the live checkout. The live checkout changes only when the reviewed branch lands on `main`. Never `git reset --hard`, checkout or stash in `~/projects/claude-voice` (it is the deployed copy).

## 6. Tests (`tests/test_voxtype_wait.py`; no real audio)

Fixtures: a temp dir with `state` and `pid` files, `SHELBY_VOXTYPE_STATE_FILE` pointing at it, `SHELBY_VOICE_QUEUE_DIR` pointing at a temp queue dir, `LOCKFILE_PATH` and `LOG_PATH` patched to temp files. setUp resets `_wait_spent_s`, `_queue_mode`, the ticket path and stamp, and the heartbeat clock. A fake `paplay` executable placed first on `PATH`: it appends `start <pid>` (and a timestamp where order matters) to a log, reads stdin to EOF, sleeps `FAKE_PLAY_S`, appends `end`. Real `paplay` and real `voxtype` never run. Tests never write `/tmp/claude-tts.log` or `/tmp/sonia-tts.lock`, never touch `/run/user/1000/voxtype/state` or `pid`, and never signal a process they did not start. Child hooks get the temp paths. Every wait has a bound. Children are cleaned up.

Ordering tests run real child processes. Both lines play, the older `end` is logged before the newer `start`, and a fresh verified holder is not sent SIGTERM or SIGKILL.

Covered behaviour:

1. No state file: zero-cost idle check; `play_raw_pcm` uses today's `subprocess.run` args and `timeout=30`; no queue directory is created; SIGTERM exits.
2. `idle` / `recording` / `transcribing` / `stopped` / empty / garbage / dead daemon pid.
3. Cap while recording: no player, the drop log line, normal return. The ticket is removed.
4. Dictation during playback replays. More than five interruptions still replay until idle. `start` count is interruptions + 1.
5. A clean exit or a positive paplay error is not replayed. An outside signal, including an external kill while idle, is replayed from the start. An outside kill while busy waits until idle, then plays the whole line. A killer that stops every attempt gives up after 5 outside-kill replays. Dictation interruptions stay unlimited inside the shared budget.
6. Lock and ticket heartbeats advance during an in-lock wait and during gated playback. `heartbeat=False` does not touch the lock.
7. macOS and `voxtype_wait: false` do not read the state file and do not touch the queue. Main's steal is one immediate SIGKILL.
8. ElevenLabs: VoxType present -> buffered then gated; absent -> the streaming `Popen`.
9. Queue order across two sessions and within one session: both play, older first, no overlap.
10. A fresh verified holder (same session and other session) is not signalled. A stale verified holder is stopped, and its paplay is gone before the new line starts. SIGTERM on that path is sent only after a second verification immediately before the signal. If that check fails, no signal is sent and the recorded pid stays alive. A dead holder is taken with no signal. An unverified live pid is never signalled. The non-queue SIGKILL has the same second check.
11. A line held while recording survives SIGTERM, logs the ignored-SIGTERM line once, and plays after idle.
12. One shared cap: a pre-lock wait plus a replay wait, and a pre-lock wait plus a queue wait, drop on the total budget rather than a fresh cap per wait.
13. Unsafe queue dir (symlink, group-writable, other owner): one `queue: registry unsafe, ordering by lock only` line, then lock-only ordering. A dead ticket is unlinked. An unverified live ticket is left. A stale ticket is left. The ticket is removed on exit, including the drop path.

## 7. Acceptance checks (CASE, run from the worktree unless stated)

Runner: `PY=~/claude-voice-venv/.venv/bin/python3`, `WT=~/projects/claude-voice-voxtype`, `S=<scratch dir>`.

- **B1 tests**: `cd $WT && $PY -m unittest discover -s tests -p 'test_*.py'` -> OK.
- **B2 zero cost without VoxType**: `SHELBY_VOXTYPE_STATE_FILE=$S/absent/state $PY -c` (import the hook via importlib, time 1000 calls of `wait_for_voxtype_idle()`) -> mean < 0.05 ms per call.
- **B3 live, idle**: real state file reads `idle`. A voice line is heard; the log has no `voxtype: waiting` line for this run.
- **B4 live, simulated busy**: state `recording` with the live daemon pid. After a few seconds `paplay` is empty and the log shows `voxtype: waiting (state=recording)`. Write `idle`: the line is heard.
- **B5 live, simulated interruption**: state `idle`; after `paplay` appears write `recording`: `paplay` disappears; write `idle`: the line replays from the start.
- **B6 untouched real state**: the real state file's mtime is unchanged by the suite. No real `voxtype record` command is run.
- **B7 no regressions in wiring**: after landing on `main`, a normal Claude Code turn on CASE speaks its `<voice>` line as before. `voice-shutup` on prompt submit still kills `paplay` (see 4.5). Queue mode ignores the hook SIGTERM and replays the cut line from the start once VoxType is idle, at most 5 outside-kill replays. A line that has not started playing yet is held and spoken after.

## 8. Failure modes

| Situation | Behaviour |
|---|---|
| VoxType not installed / daemon stopped (state file absent) | Exactly main's path. No queue IO. SIGTERM exits. |
| Daemon crashed while `recording` | PID check -> not busy; otherwise the shared 300 s cap. |
| Dictation longer than the shared 300 s | Line dropped. Never spoken over the recording. The text stays on screen. |
| Aragorn submits a prompt while a line is held or queued | Queue mode ignores SIGTERM. The line plays after the current line and the recording. |
| A line is already playing when Enter is pressed | `voice-shutup.sh` still kills `paplay`. Queue mode ignores SIGTERM, so the hook stays up and replays that line from the start once VoxType is idle, inside the shared cap, at most 5 outside-kill replays. A further outside kill gives up. The line is not dropped. |
| State file unreadable / garbage | Not busy; speak. |
| Dictation starts again during the replay | Replay from the start, as often as the shared budget allows. |
| Another session's line arrives during playback | It waits. It does not steal a live holder with a fresh lock. It plays after, older first. |
| Second Stop in the same session | Both lines play, older first. A fresh holder is not stolen. |
| Holder lock older than 180 s and still verified | Graceful stop. SIGTERM only after a second verification immediately before the signal; if that check fails, no signal. Then SIGKILL if it still verifies, and paplay stopped, before the new line starts. |
| Queue directory missing, unsafe, or no `XDG_RUNTIME_DIR` | Lock-only ordering. One unsafe log line when the directory is unsafe. The flock still prevents overlap. |

## 9. Rollback

1. Instant, per host, no code change: set `"voxtype_wait": false` in that host's `scripts/config.json`. That turns queue mode off and restores main's path.
2. Code: `git revert <merge commit>` on `main` and push; on CASE run `git -C ~/projects/claude-voice pull --ff-only` (never `reset --hard` in the deployed checkout). Other hosts pick it up on their normal pull; for them the change was a no-op anyway.
