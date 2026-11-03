# Claude Voice — Voice Interface for Claude Code

## Overview
Bidirectional voice interface: STT (faster-whisper GPU) + TTS (Edge TTS / ElevenLabs / Kokoro).

## Architecture
Mic → PulseAudio → Python recorder → faster-whisper → clipboard/stdin → Claude Code
Claude Code → hook → TTS engine → PulseAudio → speakers

## Key Files
- `scripts/record.py` — Push-to-talk audio capture (sounddevice + VAD)
- `scripts/transcribe.py` — faster-whisper GPU transcription
- `scripts/tts.py` — TTS engine abstraction (edge/elevenlabs/kokoro)
- `scripts/voice_server.py` — Background daemon combining all pieces
- `scripts/config.json` — Voice/engine preferences
- `hooks/voice-hook.sh` — Post-response hook for TTS
- `commands/listen.md` — Claude Code slash command

## Dependencies
- Python 3.12, CUDA 12.x, faster-whisper, sounddevice, edge-tts, numpy
- System: ffmpeg, libportaudio2, mpv

## Session gate (speak_sessions)

`speak_sessions` is `all` (default) or `co_only`. Missing, null, and `all` speak for every session, which is how CASE and any host without the key behave. `co_only` means only CO-Shelby. Identity is the registry name `Host · co-shelby-x` set by `--remote-control`, where `x` is exactly one lowercase ASCII letter, with `nameSource` `user`, `entrypoint` `cli`, and `kind` `interactive`. A registry entry counts only while its process is alive; if one live entry for the session is CO and another live entry is not, the gate denies with `registry_conflict`. Any other value fails closed to `co_only`. A Claude Code registry schema change makes every session silent (fail silent), visible as `voice-gate deny reason=not_registered|registry_not_co|registry_conflict|registry_too_large|registry_unreadable` in `/tmp/claude-tts.log`.

A `<pid>.json` registry file that is too large or unreadable is ignored when its stem is an ASCII pid greater than 1 and that process is dead. Registry discovery fails closed: an existing `~/.claude-accounts` tree or sessions directory that cannot be inspected denies with `registry_unreadable`. A live producer's too-large or unreadable `.json` is re-read once after 50 ms.

`registry_too_large` and `registry_unreadable` append `detail=<path>:<status>` (home shown as `~`) for the first offending file, and a gate log line given the hook environment also appends sanitized `ep=` (`CLAUDE_CODE_ENTRYPOINT`) and `att=` (`CLAUDE_CODE_SESSION_ATTENDED`).

`pocket_tts_url_speech` is the fast Stop hook server and uses the resolved Shelby persona (CASE-O by default). The light server needs both `case-o-reference.wav` and `jarvis-reference.wav`. Content voice keeps `pocket_tts_url`. A `pocket_tts_url_speech` equal to `pocket_tts_url` after stripping trailing slashes is treated as unset. The Stop hook waits `pocket_tts_speech_timeout_s` seconds (default 12, clamped to 1..60) on that light server, then uses `pocket_tts_url` for `pocket_tts_fallback_timeout_s` seconds (default 45, clamped to 1..90). Each endpoint budget is an overall deadline for its persona request and any same-server jarvis retry. The retry stays within the light budget. The light budget is not capped by `SHELBY_POCKET_TIMEOUT`. After a light-lane failure, that speech URL is skipped for `pocket_tts_speech_suppress_s` seconds (default 120, clamped to 0..3600; 0 disables the breaker) and the hook uses only the heavy server. Any registry `.json` entry that cannot be parsed as a regular-file JSON object denies (registry_too_large / registry_unreadable), apart from the dead-producer skip above, and the breaker marker is ignored unless it is a regular file owned by this user.

Each endpoint budget bounds the entire HTTP transaction by wall clock using a worker thread, and malformed, truncated, or non-WAV light replies fall through to heavy.

The session gate runs before VoxType queueing, so denied sessions never enter the FIFO. Arrival order applies after the gate allows a session.

Explain a decision without speaking:

```
python3 scripts/voice_session_gate.py --explain --session-id SID [--env KEY=VAL ...] [--env-file PATH] [--home DIR]
```

`--env-file PATH` reads `KEY=VALUE` lines, ignoring blank lines and lines that start with `#`, and merges them after process `HOME` and `PATH` and before each `--env`.
