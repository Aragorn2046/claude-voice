#!/bin/bash
# Kill any running TTS audio playback and clean up lockfile
# Linux-only copy: intentionally leaves afplay alone.

# Anchor to the executable at the start of the command line. A bare
# substring match also kills shells whose argv merely mentions these names.
kill_anchored() {
    pkill -x paplay 2>/dev/null || true
    pkill -f -- '^([^ ]*/)?mpv( [^ ]+)* --no-video( |$)' 2>/dev/null || true
    pkill -f -- '^([^ ]*/)?ffplay( [^ ]+)* -nodisp( |$)' 2>/dev/null || true
    pkill -f -- '^([^ ]*/)?([Pp]ython[0-9.]*[a-z]*( -[A-Za-z]+)* ([^ ]*/)?)?edge-tts( |$)' 2>/dev/null || true
    pkill -f -- '^([^ ]*/)?[Pp]ython[0-9.]*[a-z]*( -[A-Za-z]+)* -m edge_tts( |$)' 2>/dev/null || true
    pkill -f -- '^([^ ]*/)?[Pp]ython[0-9.]*[a-z]*( -[A-Za-z]+)* ([^ ]*/)?voice-stop-hook\.py( |$)' 2>/dev/null || true
}

kill_anchored
rm -f /tmp/sonia-tts.lock 2>/dev/null || true
exit 0
