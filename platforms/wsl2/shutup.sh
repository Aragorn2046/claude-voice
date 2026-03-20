#!/bin/bash
# Kill any running TTS playback on WSL2 (paplay/aplay)
# Used as a UserPromptSubmit hook to stop speech when the user starts typing
pkill -x paplay 2>/dev/null || true
pkill -x aplay 2>/dev/null || true
exit 0
