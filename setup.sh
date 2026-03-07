#!/bin/bash
# One-shot installer for claude-voice
# Run with: bash ~/projects/claude-voice/setup.sh

set -e

VENV_DIR="$HOME/claude-voice-venv/.venv"
PROJECT_DIR="$HOME/projects/claude-voice"

echo "=== Installing system dependencies ==="
sudo apt-get update -qq
sudo apt-get install -y ffmpeg libportaudio2 portaudio19-dev mpv

echo "=== Creating Python virtual environment (on Linux FS for performance) ==="
uv venv "$VENV_DIR" 2>/dev/null || python3 -m venv "$VENV_DIR"

PYTHON="$VENV_DIR/bin/python3"

echo "=== Installing Python dependencies ==="
uv pip install --python "$PYTHON" \
    numpy sounddevice edge-tts elevenlabs faster-whisper \
    nvidia-cublas-cu12 nvidia-cudnn-cu12

echo ""
echo "=== Setup complete ==="
echo "Venv: $VENV_DIR"
echo "Activate: source $VENV_DIR/bin/activate"
echo ""
echo "Test microphone: $PYTHON $PROJECT_DIR/scripts/record.py --test"
echo "Test TTS: $PYTHON $PROJECT_DIR/scripts/tts.py 'Hello from Claude Voice'"
echo "Test STT: $PYTHON $PROJECT_DIR/scripts/transcribe.py <audio_file>"
