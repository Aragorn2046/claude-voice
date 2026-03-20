# Claude Voice — Voice Interface for Claude Code

## Overview
Multi-engine TTS that gives Claude Code a voice. Supports macOS and WSL2 (Windows 11), with remote audio piping for headless servers.

## Architecture
Claude Code → `<voice>` block → Stop hook → TTS engine → audio output
User types → UserPromptSubmit hook → kills playback

Remote: Mac (SSH) → TTS → WAV over TCP (Tailscale) → laptop listener → speakers

## Key Files
- `platforms/macos/voice-stop-hook.py` — macOS TTS hook (with remote audio piping)
- `platforms/wsl2/voice-stop-hook.py` — WSL2 TTS hook (paplay via WSLg)
- `platforms/wsl2/audio-listener.py` — Persistent listener for remote audio
- `scripts/config.example.json` — Configuration template
- `scripts/record.py` — Push-to-talk audio capture (sounddevice + VAD)
- `scripts/transcribe.py` — faster-whisper GPU transcription
- `scripts/tts.py` — TTS engine abstraction
- `commands/mute.md` / `commands/unmute.md` — Mute/unmute slash commands

## Dependencies
- Python 3.10+, edge-tts
- WSL2: soundfile, numpy, paplay (WSLg)
- macOS: afplay (built-in), ffmpeg (for remote audio conversion)
- Remote piping: Tailscale, mosh
- Optional: ElevenLabs API key, Kokoro, faster-whisper + CUDA
