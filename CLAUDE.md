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
