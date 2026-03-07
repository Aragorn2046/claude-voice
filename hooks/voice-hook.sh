#!/bin/bash
# Claude Code post-response hook — sends response text to TTS
# Install: Add to ~/.claude/settings.json hooks section
#
# Hook receives the assistant's response on stdin.
# Pipes it to the voice server's named pipe for TTS playback.

VOICE_DIR="$HOME/projects/claude-voice"
PIPE_PATH="$HOME/claude-voice-venv/run/voice.pipe"
CONFIG="$VOICE_DIR/scripts/config.json"
VENV="$HOME/claude-voice-venv/run.sh"

# Check if TTS is enabled in config
if command -v jq &>/dev/null; then
    TTS_ENABLED=$(jq -r '.tts_enabled // true' "$CONFIG" 2>/dev/null)
    if [ "$TTS_ENABLED" = "false" ]; then
        exit 0
    fi
fi

# Read response text from stdin
RESPONSE=$(cat)

# Skip empty responses
if [ -z "$RESPONSE" ]; then
    exit 0
fi

# Strip markdown formatting for cleaner speech
CLEAN=$(echo "$RESPONSE" | sed -E '
    s/```[^`]*```//g;
    s/`[^`]*`//g;
    s/\*\*([^*]*)\*\*/\1/g;
    s/\*([^*]*)\*/\1/g;
    s/^#+\s*//;
    s/^\s*[-*]\s*//;
    s/\[([^\]]*)\]\([^)]*\)/\1/g;
' | head -c 2000)

# Method 1: Send to voice server pipe (if daemon is running)
if [ -p "$PIPE_PATH" ]; then
    echo "$CLEAN" > "$PIPE_PATH" &
    exit 0
fi

# Method 2: Direct TTS call (if daemon not running)
if [ -x "$VENV" ]; then
    "$VENV" "$VOICE_DIR/scripts/tts.py" --stdin <<< "$CLEAN" &
    exit 0
fi

# Method 3: System Python fallback
if command -v python3 &>/dev/null; then
    python3 "$VOICE_DIR/scripts/tts.py" --stdin <<< "$CLEAN" &
fi
