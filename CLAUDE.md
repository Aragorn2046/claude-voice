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

`speak_sessions` is `all` (default) or `co_only`. Missing, null, and `all` speak for every session, which is how CASE and any host without the key behave. `co_only` means only CO-Shelby. Identity is the registry name `Host · co-shelby-x` set by `--remote-control`, with `nameSource` `user`, `entrypoint` `cli`, and `kind` `interactive`. A registry entry counts only while its process is alive; if one live entry for the session is CO and another live entry is not, the gate denies with `registry_conflict`. Any other value fails closed to `co_only`. A Claude Code registry schema change makes every session silent (fail silent), visible as `voice-gate deny reason=not_registered|registry_not_co|registry_conflict|registry_too_large` in `/tmp/claude-tts.log`.

`pocket_tts_url_speech` is the fast JARVIS server for the Stop hook. Content voice keeps `pocket_tts_url`. The Stop hook waits `pocket_tts_speech_timeout_s` seconds (default 12, clamped to 1..60) on that light server, then uses `pocket_tts_url` for `pocket_tts_fallback_timeout_s` seconds (default 45, clamped to 1..90). After a light-lane failure, that speech URL is skipped for `pocket_tts_speech_suppress_s` seconds (default 120, clamped to 0..3600; 0 disables the breaker) and the hook uses only the heavy server.

Explain a decision without speaking:

```
python3 scripts/voice_session_gate.py --explain --session-id SID [--env KEY=VAL ...] [--home DIR]
```
