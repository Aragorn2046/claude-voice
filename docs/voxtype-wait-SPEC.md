# VoxType speech wait — SPEC

Status: architect spec 2026-10-31 (CASE), queue mode 2026-11-01. Code is written by the code worker from this spec.

Queue mode replaces the per-session arrival registry, the transcript watch, newest-line-wins, the per-wait fresh caps, and the dictation 5-replay limit. The hook registers its FIFO ticket at the start, before stdin and config, and drops that ticket on every exit. An outside stop of a line that is already playing ends the line. It is not replayed. A lock this user cannot open (EACCES or EPERM) is refused like a foreign or non-regular lock. A Unix socket at the lock path is refused at once (`ENXIO`, `not a regular file`).

## 1. Goal (Aragorn's words)

"The voice-feedback from Shelby/TTS should wait for me to finish when I'm recording with VoxType."

"Whenever the voice is telling me something, the Shelby Jarvis voice, and I press enter or any key, it is interrupted and stops speaking. It also still starts speaking when I'm actually using voxtype to record. I thought we agreed that it would hold messages until I stopped recording when I'm recording."

Chosen behaviour: JARVIS always finishes his line. The reply to the new prompt waits and plays after it. A message is not dropped because a newer line arrived or because Enter was pressed.

## 2. The bar

Like a good human assistant in the room: if Aragorn is talking (dictating), Shelby holds the line until he is done, then says it in full. If he starts dictating while Shelby talks, Shelby stops at once (so the mic does not capture Shelby), then repeats the line from the start once he is done. A line already queued is not thrown away when a new prompt arrives: it finishes, then the next line plays, older first, across sessions and within one session. On machines without VoxType nothing changes at all, not even by a millisecond that anyone could measure.

## 3. Facts (probed 2026-10-31 on CASE)

- VoxType 1.1.1, `/usr/bin/voxtype`, user service `voxtype.service`, config `~/.config/voxtype/config.toml` with `state_file = "auto"`.
- State file: `$XDG_RUNTIME_DIR/voxtype/state` = `/run/user/1000/voxtype/state`. Content is a bare word, no newline. VoxType 1.1.1 `StateReader.qml` documents `idle`, `recording`, `streaming`, and `transcribing`. UI themes also know `stopped`, which the status UI shows when no daemon runs (the daemon removes the state file on clean exit).
- PID file next to it: `/run/user/1000/voxtype/pid`, bare digits, no newline (today `135648` = `/usr/bin/voxtype daemon`).
- `voxtype status` prints `idle`; `voxtype status --format json` gives `"alt":"idle","class":"idle"`.
- Live wiring on CASE: Claude Code Stop hook -> `~/scripts/voice-hook.sh` -> background `~/claude-voice-venv/.venv/bin/python3 ~/projects/claude-voice/scripts/voice-stop-hook.py`. **The checkout `~/projects/claude-voice` IS the live install on CASE**: every edit there is live on the next Stop hook.
- UserPromptSubmit -> `~/scripts/voice-shutup.sh`. On 2026-11-01 Aragorn chose "Finish speaking". The script only prints the same-session voice-language drift reminder. It does not stop speech. It does not run `pkill paplay`. It does not signal `voice-stop-hook.py`. It does not remove `/tmp/sonia-tts.lock`. That change lives in the shelby-scripts repo, outside this repo, and is not committed there yet. The manual `scripts/shutup.sh` still stops speech on purpose.
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
- `VOXTYPE_MAX_WAIT_S = 300.0`. One shared wait budget per hook run, counted once across the pre-lock VoxType wait, the queue wait, the in-lock gate, and every dictation replay wait. Time spent synthesising or actually playing does not count. The cap is read at call time. An explicit `max_wait_s` further limits that call (the minimum of the argument and the budget still left). `max_wait_s <= 0` while busy logs the cap line once and returns without sleeping.
- `VOXTYPE_IDLE_STATES = frozenset({"idle", "stopped"})`. A state is busy when the text (at most 64 bytes, stripped, lowercased) is a well-formed word matching `^[a-z0-9_-]{1,32}$` and is not in that set. `recording`, `streaming`, `transcribing`, and any unknown future word such as `paused` are busy. Empty, unreadable, and malformed content are not busy. Malformed means the text does not match that pattern. A space inside, a control byte, non-ASCII, or a word longer than 32 characters is malformed.
- `VOXTYPE_HEARTBEAT_S = 5.0`. One helper, used by every poll loop in queue mode (pre-lock wait, queue wait, in-lock gate, and the gated playback poll in `_play_one_paplay`). At most once per interval it refreshes the mtime of the held lock fd (`os.utime` on the fd; skipped when no fd is held) and of this hook's ticket (`os.utime(path, follow_symlinks=False)`, OSError ignored).
- `LOCK_STALE_S = 180.0`. A verified live holder older than this is wedged. Read at call time. The heartbeat keeps a hook that is waiting or playing from looking stale.
- State path: env `SHELBY_VOXTYPE_STATE_FILE` if set (tests and the live simulation only; never set in any wrapper or config), else `os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "voxtype", "state")`. PID file = sibling `pid` in the same directory.
- Kill switch: config key `"voxtype_wait"` (default `true`). `false` -> queue mode off -> the hook behaves exactly as main's non-VoxType path. `config.json` is per host and gitignored, so this is the instant rollback without a code change.
- macOS (`IS_MACOS`): the whole feature is skipped; no state-file access and no queue IO.

There is no dictation replay-count limit and no transcript watch. An outside stop of the player ends the line. It is not replayed.

### 4.2 Queue mode

Queue mode is `early_presence and _VOXTYPE_WAIT_ENABLED`. `early_presence` is one `os.path.exists` of the VoxType state file at the start of `main()`, and only when this is not macOS. The hook does not read presence a second time. That includes `remote_audio` hosts.

When queue mode is off the hook keeps main's path: `acquire_lock(queue_wait_s=0, graceful_steal=False)` SIGKILLs a verified same-session, dead, or stale holder and retries `LOCK_NB` once, immediately. It yields to a live other-session holder with a fresh lock. SIGTERM exits as before. There is no extra poll and no extra sleep. macOS and a missing state file do not touch the queue. `voxtype_wait: false` clears the early ticket before any wait or lock, then follows that same path.

### 4.3 Helpers

- `voxtype_present() -> bool`: `os.path.exists(state_path)`; False on macOS or when switched off.
- `voxtype_busy() -> bool`: read at most 64 bytes, strip, and lowercase. Busy when that text is a well-formed word (`^[a-z0-9_-]{1,32}$`) and is not in `VOXTYPE_IDLE_STATES`. Empty, unreadable, and malformed content are not busy. Any OSError is not busy. Stale guard: if the pid file exists, parses as an int, and `os.kill(pid, 0)` raises `ProcessLookupError`, return False (the daemon died while the state was a busy word). `PermissionError` means alive.
- `wait_for_voxtype_idle(max_wait_s=None, heartbeat=True) -> float`: draws on the shared budget. If not busy, return 0.0 after that single read (no sleep, no log line). Else log `voxtype: waiting (state=<value>)`, then loop `time.sleep(VOXTYPE_POLL_S)` until not busy or the allowance passes. Each sleep is charged to the shared budget. When `heartbeat` is true (the default), the shared helper refreshes the held lock fd, if there is one, and this hook's ticket. No fd means the lock file is not touched, so a pre-lock wait cannot hide a wedged holder. Log `voxtype: idle after N.Ns` or `voxtype: cap reached after <N>s total wait while still busy, line dropped`. `<N>` is the shared budget. Return seconds waited. A fired cap drops the line. It is never played while the state is busy. That is any busy word, not only `recording`. The logged `<value>` is the matched word.

### 4.4 FIFO ticket queue

One global queue replaces the per-session arrival registry. There is no session id in the ticket name or body, and there is no "newest line wins" rule. Same-session lines both play, older first.

- Directory: env `SHELBY_VOICE_QUEUE_DIR` if set (tests only), else `$XDG_RUNTIME_DIR/voice-stop-hook-queue`. No override and no `XDG_RUNTIME_DIR`: registry off (lock-only ordering). Never a `/tmp` or world-writable fallback. Read at call time.
- Create mode `0700`. `lstat` must show a real directory (not a symlink) owned by this uid with no group or other write bits. Otherwise log once `queue: registry unsafe, ordering by lock only` and run with the registry off.
- The stamp is `time.monotonic_ns()` taken at the start of `main()`, before stdin is read. When this is not macOS and the state file exists, the hook creates `<stamp 20 digits>-<pid>` immediately, before stdin and before config, with `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW` and mode `0600`.
- If config turns `voxtype_wait` off, the hook clears that ticket immediately, before any wait or lock. The ticket is removed on every exit after registration: empty stdin, a read error, an empty response, an automated session, `tts_enabled` false, no `<voice>` block, empty sanitized text, a cap drop, a refused or missing lock, normal speech, an exception, and best-effort on the non-queue SIGTERM path. One `try/finally` does that clear.
- "Older ticket ahead" is an entry matching `^\d{20}-\d+$` with `(stamp, pid) < (my_stamp, my_pid)` whose pid is > 1, not this process, alive (`os.kill(pid, 0)`, `PermissionError` means alive), `_is_our_hook(pid)` true, and whose ticket mtime is not older than `LOCK_STALE_S`. Dead pids are unlinked. A live pid that fails `_is_our_hook` is left in place. A stale-by-mtime live entry is skipped (not unlinked, not signalled). A vanished ticket skips only that entry. An OSError from one entry (the file disappearing between `listdir` and `lstat`, or from that entry's pid check or hook check) does not end the scan. The other entries are still checked. Only a failure of `os.listdir` itself or of the private-directory check is a registry failure. That still means there is no older ticket. The flock still prevents overlap.

A line takes its turn only when no older ticket is ahead and `flock(LOCK_EX|LOCK_NB)` succeeds. While either fails, it polls every `VOXTYPE_POLL_S`. Each sleep counts against the shared budget. The first time it queues it logs `queue: waiting for the line ahead to finish`. When the budget runs out while still queued: log `queue: cap reached after <N>s, line dropped` and speak nothing. The lock is not kept.

While the flock is held by someone else, the hook reads the lock record as before:

- Holder pid alive, verified as our hook, and lock age <= `LOCK_STALE_S`: never signal it, whatever its session (same session included). Keep waiting.
- Holder alive, verified, and lock age > `LOCK_STALE_S` (wedged): today's graceful stop. After the paplay children scan, SIGTERM is sent only if `_is_our_hook` still matches immediately before the signal. If that second check fails, send no signal and do not call `_stop_players`. Otherwise poll take up to `LOCK_STEAL_TERM_WAIT_S`, then SIGKILL only if it still verifies, and stop its verified paplay children via `_stop_players`, then poll take up to `LOCK_STEAL_RETRY_S`. The new line does not start before that stop has finished. `_stop_players` SIGKILLs the wedged hook before it signals paplay, so that hook cannot replay a player the steal itself stopped. `_stop_players` sends SIGTERM to each verified paplay child and waits up to `LOCK_STEAL_PLAYER_WAIT_S`. A child that exits in that wait is not sent SIGKILL. A child that is still alive and not a zombie is checked again immediately before the next signal: `comm` is `paplay` and `_is_signallable_paplay` is true. A child that fails that check is left alone and is not a survivor. Each child that still matches is sent SIGKILL. `_stop_players` then waits up to `LOCK_STEAL_KILL_WAIT_S` (0.5 s). It returns true only when every verified child is gone or a zombie. If a child survives SIGKILL, `acquire_lock` closes the lock fd it already took, logs one line `lock: stale player would not stop, line dropped`, and returns None. It does not poll again and it does not start the new line.
- Holder dead, record empty or truncated, unparsable, or pid failing verification: no signal. Keep polling take.
- `lock refused:` (symlink, non-regular, Unix socket, other owner, or permission denied) is one log line. `acquire_lock` returns None at once. It does not write, signal, sleep, or poll again. EACCES and EPERM are permission denied, the same refusal as a foreign or non-regular lock. A Unix socket makes `os.open` raise `ENXIO`. That error is refused as `not a regular file`, including when `lstat` races. Any other open error except `ENOENT`, `EINTR`, `EAGAIN`, and `EWOULDBLOCK` is the same refusal when `lstat` shows the path is not a regular file.

### 4.5 Hold while recording

In queue mode and not `remote_audio`, the line does not take its turn while `voxtype_busy()`. A well-formed state word other than `idle` or `stopped` is busy. That includes `recording`, `streaming`, `transcribing`, and an unknown future word. `idle`, `stopped`, empty, unreadable, malformed content, or a dead daemon pid is not busy. Log `voxtype: waiting (state=<s>)` and `voxtype: idle after N.Ns`. `<s>` is the matched word. `remote_audio` hosts keep today's order (no pre-lock VoxType wait, the in-lock gate only) but still use the FIFO queue.

The transcript watch is gone. There is no own-prompt drop, no held phase, and no deferred-SIGTERM grace. A prompt does not drop a held line.

SIGTERM in queue mode: from the moment queue mode is decided in `main()`, `handle_sigterm` logs once `queue mode: SIGTERM ignored, line will finish` and returns. No exit, no player terminate, no lock release. This holds in every phase: pre-lock wait, queued, synthesis, playback, and replay wait. Before queue mode is decided, and whenever queue mode is off, `handle_sigterm` cleans up and exits as before. SIGKILL (the stale-steal path) still ends a wedged hook.

Pressing Enter does not stop or touch the playing line. `voice-shutup.sh` only prints the same-session voice-language drift reminder. It does not kill `paplay`, signal the hook, or remove the lock. The manual `scripts/shutup.sh`, or any other outside stop of `paplay`, ends the line on purpose. The line is not replayed. Real `paplay` exits 0 on SIGTERM, so that stop looks like a clean end and the hook logs nothing extra. A negative status this hook did not cause (SIGKILL, or a player that does not catch the signal) also ends the line. The hook logs `voxtype: player stopped from outside (signal <N>), line ends` once and returns normally. A positive exit code ends the line with no extra log line. A line that has not started yet is unchanged. It waits and plays after the state goes idle.

Dictation that starts during playback stops our player, logs `voxtype: dictation started during playback, stopped; will replay (<k>)`, waits until idle, and replays the same PCM from the start. It repeats as often as the shared budget allows. Dictation has no replay-count limit and no "interrupted the line N times" drop. The hook's own stop still counts as dictation when `paplay` exits 0 on that SIGTERM.

If the shared budget runs out while VoxType is still busy (pre-lock, in-lock gate before the first play, or a replay wait): never play while the state is busy. That is any busy word, not only `recording`. Log `voxtype: cap reached after <N>s total wait while still busy, line dropped` and return normally. A dropped line returns None, no exception, and no English fallback.

### 4.6 Gated local player (Linux only)

`play_raw_pcm()` Linux branch:

1. If `not voxtype_present()`: run the current non-VoxType code (`subprocess.run([...paplay...], input=pcm, capture_output=True, timeout=30)`). Same args, same timeout, same exceptions. This is the Day/Dawn/Dusk path and the VoxType-stopped path.
2. Else call `_play_pcm_voxtype_gated(pcm, srate, channels)`:
   - The first gate wait and every dictation replay wait draw on the one shared budget. `VOXTYPE_MAX_WAIT_S` is read at call time and further limits that call.
   - If the allowance passes while VoxType is still busy, log the cap line and return. No player starts. The same drop applies to every dictation replay wait.
   - Playback starts only after a wait that ended idle, so interrupt is armed from the first poll.
   - There is no dictation attempt limit. Dictation may interrupt as often as the budget allows. An outside stop is not replayed.
     - `Popen(paplay ..., stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL)`; register it in `_current_player`; feed the PCM from a daemon thread. The poll loop calls the heartbeat helper. Overall per-attempt timeout 30 s: on timeout kill the player and raise `subprocess.TimeoutExpired`. That timeout kill is not an outside kill and is not replayed.
     - A clean exit, including real paplay's exit 0 after an outside SIGTERM, or a positive non-zero exit (paplay error), ends the line. No replay. No extra log line.
     - If this hook did not stop the player and `returncode` is negative (SIGKILL, or a player that does not catch SIGTERM): log `voxtype: player stopped from outside (signal <N>), line ends` once and return normally. `<N>` is the signal number. The line is not replayed. The Dutch lane reports played, so no English fallback runs.
     - If dictation interrupted this attempt: log `voxtype: dictation started during playback, stopped; will replay (<k>)`, wait again on the shared budget, then play the same PCM from the start. The hook's own SIGTERM counts as dictation even when the player exits 0.
   - Clear `_current_player` in `finally`.
3. Return contract unchanged: returns `None`; raises only where today's code would raise. A dropped line returns normally, so the Dutch lane reports PLAYED and does not trigger an English fallback (no double speech).

### 4.7 ElevenLabs streaming branch (Linux)

- `voxtype_present()` False -> current streaming `Popen` code unchanged.
- True -> read the whole stream into memory (as the macOS branch already does), then call `_play_pcm_voxtype_gated(pcm, 24000, 1)`. No padding change. CASE uses pocket; this is a fallback path. An outside stop ends the line, because this branch uses the gated player. It is not replayed.
- False (state file absent) keeps the streaming `Popen`. That path is not queue mode. An outside `pkill` can still cut it, and it is not replayed. It does not fit the gated player, so it is left unchanged.

### 4.8 Lock hardening (unchanged rules)

Speech-lock steal is hardened against a stale or forged `/tmp/sonia-tts.lock`. A recorded pid is accepted only when it is an int greater than 1 and not this process. Before any signal to that holder, `_is_our_hook` must match: on Linux `/proc/<pid>/cmdline` contains `voice-stop-hook` and the real uid in `/proc/<pid>/status` equals this user; on macOS `ps -o uid=,command= -p <pid>` must show the same. Anything else is not signalled. On the queue stale path, SIGTERM is sent only after a second `_is_our_hook` check immediately before the signal (after the children scan). If that check fails, send no SIGTERM and do not call `_stop_players`; treat the holder like the dead or unverified case. The SIGKILL is verified again immediately before it is sent. On the non-queue path the same rule applies to that SIGKILL: a second `_is_our_hook` check immediately before the signal, and no signal if it fails. The lock is opened with `O_NOFOLLOW | O_NONBLOCK` and kept only when `fstat` shows a regular file owned by this user. A FIFO cannot block the open. A symlink (`ELOOP`), another owner, a non-regular file, a Unix socket (`ENXIO` from `os.open`), or a lock this user cannot open (`EACCES` or `EPERM`) is refused with one `lock refused:` line and `acquire_lock` returns None without writing, signalling, sleeping, or polling. `ENXIO` is that refusal even when `lstat` races. Any other open error except `ENOENT`, `EINTR`, `EAGAIN`, and `EWOULDBLOCK` is the same refusal when `lstat` shows the path is not a regular file. Lock age comes from that fd's `st_mtime`. A paplay child is signalled only when its ppid matches, its comm is still `paplay`, its cmdline argv[0] basename is exactly `paplay`, and its real uid is this user. Never a blocking flock. `_stop_players` still SIGKILLs the wedged hook before it stops that hook's paplay, so the killed hook cannot replay. It then SIGTERMs each verified paplay child and waits `LOCK_STEAL_PLAYER_WAIT_S`. A child that is still alive is checked again and SIGKILLed, then waited on for `LOCK_STEAL_KILL_WAIT_S`. If a child survives SIGKILL, the hook closes the taken lock fd, logs `lock: stale player would not stop, line dropped`, and `acquire_lock` returns None. It does not start the new line.

On the non-queue path, `queue_wait_s=0` is one decision: a yield returns at once. `queue_wait_s>0` polls until that budget passes, then logs `Yielding lock`.

### 4.9 What does not change

`send_audio_remote()` and every remote/Dusk relay path, `find_audible_path()`, engine selection, Dutch lane routing, sanitising, `voice-hook.sh`, `voice-shutup.sh` (see the note in 4.5: Enter does not stop the playing line, and the script only prints the drift reminder. The manual `scripts/shutup.sh` or any other outside stop of `paplay` ends the line on purpose, and the line is not replayed.), `tts.py`, `voice_server.py`, `ack-play.sh`, macOS behaviour. The non-VoxType steal is main's SIGKILL plus one immediate retry, with the second verification in 4.8. The ElevenLabs streaming `Popen` used when VoxType is absent is unchanged (section 4.7).

## 5. Files

- `scripts/voice-stop-hook.py` (only code file changed).
- `tests/test_voxtype_wait.py` (unittest, imports the hook with `importlib`).
- `docs/voxtype-wait-SPEC.md` (this file).

Development happens in a git worktree, not in the live checkout. The live checkout changes only when the reviewed branch lands on `main`. Never `git reset --hard`, checkout or stash in `~/projects/claude-voice` (it is the deployed copy).

## 6. Tests (`tests/test_voxtype_wait.py`; no real audio)

Fixtures: a temp dir with `state` and `pid` files, `SHELBY_VOXTYPE_STATE_FILE` pointing at it, `SHELBY_VOICE_QUEUE_DIR` pointing at a temp queue dir, `LOCKFILE_PATH` and `LOG_PATH` patched to temp files. setUp resets `_wait_spent_s`, `_queue_mode`, the ticket path and stamp, and the heartbeat clock. A fake `paplay` executable placed first on `PATH`: it appends `start <pid>` (and a timestamp where order matters) to a log, reads stdin to EOF, sleeps `FAKE_PLAY_S`, appends `end`. Real `paplay` and real `voxtype` never run. Tests never write `/tmp/claude-tts.log` or `/tmp/sonia-tts.lock`, never touch `/run/user/1000/voxtype/state` or `pid`, and never signal a process they did not start. A pid read from the playback log or from a pid file is signalled only through a pidfd opened while that pid was still a live non-zombie `paplay` this test started. A pid that fails that check is left alone. Cleanup reaps the players it killed. Child hooks get the temp paths. Every wait has a bound. Children are cleaned up.

Ordering tests run real child processes. Both lines play, the older `end` is logged before the newer `start`, and a fresh verified holder is not sent SIGTERM or SIGKILL.

Covered behaviour:

1. No state file: zero-cost idle check; `play_raw_pcm` uses today's `subprocess.run` args and `timeout=30`; no queue directory is created; SIGTERM exits.
2. `idle`, `stopped`, empty, and malformed bytes are not busy. `recording`, `streaming`, `transcribing`, and an unknown word such as `paused` hold, then play when idle. The word `garbage` is busy. A busy word with a dead daemon pid is not busy. A cap while `streaming` drops the line and does not start the player.
3. Cap while recording: no player, the drop log line, normal return. The ticket is removed.
4. Dictation during playback replays. More than five interruptions still replay until idle. `start` count is interruptions + 1.
5. A clean exit or a positive paplay error is not replayed and adds no extra log line. Real paplay's exit 0 after an outside SIGTERM is the same: the hook cannot tell it from a natural end, so it logs nothing extra and does not replay. `test_outside_sigterm_exit0_ends_without_replay` is the idle case. `test_outside_kill_while_busy_exit0_ends_the_line` is the `recording` case: one start, no replay, and the hook returns while the state is still `recording`. An outside signal with a negative status logs one `voxtype: player stopped from outside (signal 15), line ends` line and does not replay. `test_outside_kill_while_idle_ends_the_line` covers idle. `test_outside_kill_while_busy_ends_the_line` covers `recording` with the real busy check, and the hook returns without waiting for idle. Dictation during playback still replays, including when the hook's own SIGTERM makes paplay exit 0. `test_arming_race_stops_player_spawned_into_dictation` waits for that player's `start` line, then flips the state to `recording`. Dictation interruptions stay unlimited inside the shared budget.
6. Lock and ticket heartbeats advance during an in-lock wait and during gated playback. `heartbeat=False` does not touch the lock.
7. macOS skips the feature: the helpers do not read the state file and do not touch the queue. `voxtype_wait: false` turns those helpers off the same way. After config is read, `main()` clears the early ticket and uses main's steal: one immediate SIGKILL. No `voxtype:` wait runs.
8. ElevenLabs: VoxType present -> buffered then gated; absent -> the streaming `Popen`.
9. Queue order across two sessions and within one session: both play, older first, no overlap.
10. A fresh verified holder (same session and other session) is not signalled. A stale verified holder is stopped, and its paplay is gone before the new line starts. SIGTERM on that path is sent only after a second verification immediately before the signal. If that check fails, no signal is sent and the recorded pid stays alive. A dead holder is taken with no signal. An unverified live pid is never signalled. The non-queue SIGKILL has the same second check. `test_slow_term_paplay_is_killed_before_the_new_line`: a paplay that logs SIGTERM and keeps running is SIGKILLed, and that pid is gone before the new line's first `start`. The two players do not overlap. `test_survivor_paplay_drops_the_line_and_frees_the_lock`: a verified child that never reports gone makes `acquire_lock(graceful_steal=True)` return None. The log has one `lock: stale player would not stop, line dropped` line. The lock is free afterwards.
11. A line held while recording survives SIGTERM, logs the ignored-SIGTERM line once, and plays after idle.
12. One shared cap: a pre-lock wait plus a replay wait, and a pre-lock wait plus a queue wait, drop on the total budget rather than a fresh cap per wait.
13. Unsafe queue dir (symlink, group-writable, other owner): one `queue: registry unsafe, ordering by lock only` line, then lock-only ordering. A dead ticket is unlinked. An unverified live ticket is left. A stale ticket is left. The ticket is removed on exit, including the drop path. A vanished ticket skips only that entry. One missing older ticket does not hide another older live ticket. If the only older ticket vanishes, nothing is ahead.
14. The ticket is registered at the start of the hook, before stdin is parsed and before config is loaded, when the state file exists. A newer hook does not take the lock or speak while an older hook's ticket exists, even while that older hook is still inside config. Every early return leaves no ticket: no `<voice>` block, `tts_enabled` false, a headless session, and `voxtype_wait` false.
15. A lock file this user cannot open (mode `0o000`, or `EPERM` from `os.open`) is refused at once. `acquire_lock` returns None with no sleep and one `lock refused: permission denied` line. `main()` drops the line, starts no player, and leaves no ticket.
16. A Unix socket at the lock path is refused at once. `test_socket_lock_is_refused_without_sleep`: `acquire_lock(graceful_steal=True)` and `acquire_lock(queue_wait_s=0, graceful_steal=False)` each return None in well under 1 s, `time.sleep` is not called, and the log has one `lock refused:` line. `test_main_drops_the_line_when_the_lock_is_a_socket`: state `idle` and a socket lock, `main()` starts no player and leaves no ticket.

## 7. Acceptance checks (CASE, run from the worktree unless stated)

Runner: `PY=~/claude-voice-venv/.venv/bin/python3`, `WT=~/projects/claude-voice-voxtype`, `S=<scratch dir>`.

- **B1 tests**: `cd $WT && $PY -m unittest discover -s tests -p 'test_*.py'` -> OK.
- **B2 zero cost without VoxType**: `SHELBY_VOXTYPE_STATE_FILE=$S/absent/state $PY -c` (import the hook via importlib, time 1000 calls of `wait_for_voxtype_idle()`) -> mean < 0.05 ms per call.
- **B3 live, idle**: real state file reads `idle`. A voice line is heard; the log has no `voxtype: waiting` line for this run.
- **B4 live, simulated busy**: state `recording` with the live daemon pid. After a few seconds `paplay` is empty and the log shows `voxtype: waiting (state=recording)`. Write `idle`: the line is heard.
- **B5 live, simulated interruption**: state `idle`; after `paplay` appears write `recording`: `paplay` disappears; write `idle`: the line replays from the start.
- **B6 untouched real state**: the real state file's mtime is unchanged by the suite. No real `voxtype record` command is run.
- **B7 no regressions in wiring**: after landing on `main`, a normal Claude Code turn on CASE speaks its `<voice>` line as before. `voice-shutup` on prompt submit does not stop speech (see 4.5). It only prints the drift reminder. The manual `shutup.sh` or any other outside stop of `paplay` ends the line on purpose. The line is not replayed. Real paplay exits 0 on SIGTERM. A line that has not started playing yet is held and spoken after.

## 8. Failure modes

| Situation | Behaviour |
|---|---|
| VoxType not installed / daemon stopped (state file absent) | Exactly main's path. No queue IO. SIGTERM exits. |
| Daemon crashed while the state is a busy word | The pid check says not busy. Otherwise the shared 300 s cap applies. |
| Dictation longer than the shared 300 s | Line dropped. Never spoken while the state is busy. The text stays on screen. |
| Aragorn submits a prompt while a line is held or queued | Queue mode ignores SIGTERM. The line plays after the current line and the recording. |
| A line is already playing when Enter is pressed | Enter does not stop or touch the playing line. `voice-shutup.sh` only prints the drift reminder. The manual `scripts/shutup.sh` or any other outside stop of `paplay` ends the line on purpose. The line is not replayed. Real paplay exits 0 on SIGTERM. |
| Lock file this user cannot open | Same as a foreign or non-regular lock. One `lock refused: permission denied` line. `acquire_lock` returns None. No write, no signal, no sleep. The line is dropped and the ticket is cleared. |
| Unix socket or other non-regular file at the lock path | `os.open` on a Unix socket raises `ENXIO`. One `lock refused: not a regular file` line. `acquire_lock` returns None at once. No write, no signal, no sleep. The line is dropped and the ticket is cleared. |
| State file unreadable / garbage | Unreadable or malformed content is not busy. Speak at once. A well-formed word such as `garbage` is busy when it is not `idle` or `stopped`. The shared cap still bounds that wait. |
| Dictation starts again during the replay | Replay from the start, as often as the shared budget allows. |
| Another session's line arrives during playback | It waits. It does not steal a live holder with a fresh lock. It plays after, older first. |
| Second Stop in the same session | Both lines play, older first. A fresh holder is not stolen. |
| Holder lock older than 180 s and still verified | Graceful stop. SIGTERM only after a second verification immediately before the signal; if that check fails, no signal. Then SIGKILL if it still verifies, and paplay stopped, before the new line starts. A paplay that ignores SIGTERM is SIGKILLed. If a paplay survives SIGKILL, the hook closes the taken lock, logs one `lock: stale player would not stop, line dropped` line, and drops the line. It does not retry. |
| Queue directory missing, unsafe, or no `XDG_RUNTIME_DIR` | Lock-only ordering. One unsafe log line when the directory is unsafe. The flock still prevents overlap. |

## 9. Rollback

1. Instant, per host, no code change: set `"voxtype_wait": false` in that host's `scripts/config.json`. That turns queue mode off and restores main's path.
2. Code: `git revert <merge commit>` on `main` and push; on CASE run `git -C ~/projects/claude-voice pull --ff-only` (never `reset --hard` in the deployed checkout). Other hosts pick it up on their normal pull; for them the change was a no-op anyway.
