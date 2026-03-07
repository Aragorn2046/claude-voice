Record audio from the microphone, transcribe it using faster-whisper, and use the transcribed text as input.

Do the following:
1. Run: `~/claude-voice-venv/run.sh ~/projects/claude-voice/scripts/voice_server.py --listen`
2. The transcribed text will be printed to stdout
3. Treat the transcribed text as the user's spoken request and respond to it
4. If transcription fails, tell the user and suggest checking microphone with `python3 ~/projects/claude-voice/scripts/record.py --test`
