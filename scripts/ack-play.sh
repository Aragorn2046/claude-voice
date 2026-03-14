#!/bin/bash
# Play a random acknowledgment clip — near-instant playback from pre-generated PCM
ACKS_DIR="$HOME/claude-voice-venv/acks"

# Check if acks are enabled (config toggle)
CONFIG="$HOME/projects/claude-voice/scripts/config.json"
if [ -f "$CONFIG" ]; then
    ENABLED=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('ack_enabled', True))" 2>/dev/null)
    if [ "$ENABLED" = "False" ]; then
        exit 0
    fi
fi

# Ensure PulseAudio
if [ -z "$PULSE_SERVER" ] && [ -e /mnt/wslg/PulseServer ]; then
    export PULSE_SERVER="unix:/mnt/wslg/PulseServer"
fi

# Count available clips
CLIPS=("$ACKS_DIR"/ack_*.raw)
if [ ${#CLIPS[@]} -eq 0 ]; then
    exit 0
fi

# Pick random clip
IDX=$((RANDOM % ${#CLIPS[@]}))
RAW="${CLIPS[$IDX]}"
META="${RAW%.raw}.meta"

# Read sample rate and channels from meta file
if [ -f "$META" ]; then
    RATE=$(head -1 "$META")
    CHANNELS=$(tail -1 "$META")
else
    RATE=24000
    CHANNELS=1
fi

# Play (background, non-blocking — don't delay the prompt processing)
paplay --raw --rate="$RATE" --channels="$CHANNELS" --format=s16le < "$RAW" &
exit 0
