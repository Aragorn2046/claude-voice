#!/bin/bash
# Kill any running TTS audio playback and clean up lockfile
pkill -f "paplay" 2>/dev/null || true
pkill -f "mpv.*--no-video" 2>/dev/null || true
pkill -f "ffplay.*-nodisp" 2>/dev/null || true
pkill -f "edge-tts" 2>/dev/null || true
pkill -f "voice-stop-hook" 2>/dev/null || true
rm -f /tmp/sonia-tts.lock 2>/dev/null || true
exit 0
