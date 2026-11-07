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

## CASE presence gate

On CASE, the Stop hook must not speak into an empty room. `case-presence` (not in this repo) publishes `$XDG_RUNTIME_DIR/case-presence/state.json`, or `/run/user/<uid>/case-presence/state.json` when `XDG_RUNTIME_DIR` is unset.

- `case_presence_gate` — set exactly to `true` to enable the gate. Default `false`. A line is held (not spoken) only when that state file is a JSON object, `updated_at` is a finite number no more than 30 seconds old and no more than 5 seconds in the future, `pid` is a live positive int whose command line is the case-presence daemon, `pid_start` matches when present, and `hold_voice` is exactly `true`. Held lines go to `held-voice.jsonl` in that same directory.
- `case_presence_held_max` — how many held lines to keep. Default `20`. Integers from 1 to 200 are kept; any other value uses 20.

gate disabled, missing/stale/foreign/dead/invalid state, or any error while deciding -> speak; once the decision is HOLD, a failure to record the line drops it silently.
