#!/bin/bash
# Kill any running TTS playback on macOS (afplay)
# Used as a UserPromptSubmit hook to stop speech when the user starts typing
pkill -x afplay 2>/dev/null || true
exit 0
