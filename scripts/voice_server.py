#!/usr/bin/env python3
"""Voice server daemon — combines STT + TTS for Claude Code integration.

Usage:
    python3 voice_server.py              # Start daemon (hotkey mode)
    python3 voice_server.py --once       # Record once, transcribe, print, exit
    python3 voice_server.py --speak "hi" # Speak text and exit
    python3 voice_server.py --listen     # Record + transcribe + copy to clipboard
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Add scripts dir to path
sys.path.insert(0, str(Path(__file__).parent))

from record import record, load_config
from transcribe import transcribe, get_model
from tts import speak

# Use Linux FS for named pipes (NTFS doesn't support mkfifo)
RUNTIME_DIR = Path.home() / "claude-voice-venv" / "run"
PIPE_PATH = RUNTIME_DIR / "voice.pipe"
PID_PATH = RUNTIME_DIR / "voice.pid"


def copy_to_clipboard(text: str):
    """Copy text to clipboard (works in WSL)."""
    try:
        process = subprocess.Popen(
            ["/mnt/c/Windows/System32/clip.exe"],
            stdin=subprocess.PIPE,
        )
        process.communicate(text.encode("utf-16-le"))
        return True
    except Exception as e:
        print(f"Clipboard copy failed: {e}", file=sys.stderr)
        return False


def type_to_terminal(text: str):
    """Write text to a named pipe for terminal consumption."""
    try:
        PIPE_PATH.parent.mkdir(exist_ok=True)
        with open(PIPE_PATH, "w") as f:
            f.write(text)
        return True
    except Exception:
        return False


def listen_once(copy: bool = True, quiet: bool = False) -> str:
    """Record audio, transcribe, return text."""
    if not quiet:
        print("🎤 Recording...", file=sys.stderr)

    path = record()
    if not path:
        return ""

    if not quiet:
        print("🔄 Transcribing...", file=sys.stderr)

    text = transcribe(path)

    if not quiet:
        print(f"📝 {text}", file=sys.stderr)

    if copy and text:
        copy_to_clipboard(text)
        if not quiet:
            print("📋 Copied to clipboard", file=sys.stderr)

    return text


def speak_text(text: str, engine: str = None):
    """Speak text through TTS."""
    if not text.strip():
        return
    # Truncate extremely long responses for TTS (safety net only)
    if len(text) > 5000:
        text = text[:5000] + "... (truncated for speech)"
    speak(text, engine=engine)


def daemon_mode():
    """Run as background daemon with hotkey support."""
    cfg = load_config()
    hotkey = cfg.get("hotkey", "ctrl+shift+v")

    print(f"Voice server starting... Hotkey: {hotkey}")
    print("Pre-loading whisper model...")
    get_model()  # Pre-load into GPU memory
    print("Ready. Press hotkey to record.")

    # Set up named pipe for receiving TTS requests
    PIPE_PATH.parent.mkdir(exist_ok=True)
    if PIPE_PATH.exists():
        PIPE_PATH.unlink()
    os.mkfifo(str(PIPE_PATH))

    # Save PID
    PID_PATH.write_text(str(os.getpid()))

    # TTS listener thread — reads from named pipe
    def tts_listener():
        while True:
            try:
                with open(PIPE_PATH, "r") as f:
                    text = f.read().strip()
                if text:
                    speak_text(text)
            except Exception as e:
                print(f"TTS pipe error: {e}", file=sys.stderr)
                time.sleep(1)

    tts_thread = threading.Thread(target=tts_listener, daemon=True)
    tts_thread.start()

    # Hotkey listener
    try:
        from pynput import keyboard

        hotkey_parts = hotkey.replace("ctrl", "<ctrl>").replace("shift", "<shift>") \
            .replace("alt", "<alt>")
        # Parse hotkey string like "ctrl+shift+v" → {Key.ctrl, Key.shift, KeyCode(v)}
        current_keys = set()
        target_keys = set()

        for part in hotkey.split("+"):
            part = part.strip().lower()
            if part == "ctrl":
                target_keys.add(keyboard.Key.ctrl_l)
            elif part == "shift":
                target_keys.add(keyboard.Key.shift)
            elif part == "alt":
                target_keys.add(keyboard.Key.alt_l)
            else:
                target_keys.add(keyboard.KeyCode.from_char(part))

        recording = False

        def on_press(key):
            nonlocal recording
            # Normalize key
            if hasattr(key, 'value') and key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                current_keys.add(keyboard.Key.ctrl_l)
            elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
                current_keys.add(keyboard.Key.shift)
            elif key in (keyboard.Key.alt_l, keyboard.Key.alt_r):
                current_keys.add(keyboard.Key.alt_l)
            else:
                current_keys.add(key)

            if target_keys.issubset(current_keys) and not recording:
                recording = True
                threading.Thread(target=do_recording, daemon=True).start()

        def on_release(key):
            current_keys.discard(key)
            # Also discard normalized versions
            if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                current_keys.discard(keyboard.Key.ctrl_l)
            elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
                current_keys.discard(keyboard.Key.shift)
            elif key in (keyboard.Key.alt_l, keyboard.Key.alt_r):
                current_keys.discard(keyboard.Key.alt_l)

        def do_recording():
            nonlocal recording
            try:
                text = listen_once(copy=True, quiet=False)
                if text:
                    print(f"\n>>> {text}\n")
            finally:
                recording = False

        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            listener.join()

    except ImportError:
        print("pynput not available — running in pipe-only mode", file=sys.stderr)
        print("Send text to TTS via: echo 'hello' > tmp/voice.pipe")
        # Just keep running for the TTS pipe
        signal.pause()
    except KeyboardInterrupt:
        print("\nShutting down voice server.")
    finally:
        if PIPE_PATH.exists():
            PIPE_PATH.unlink()
        if PID_PATH.exists():
            PID_PATH.unlink()


def main():
    parser = argparse.ArgumentParser(description="Voice server for Claude Code")
    parser.add_argument("--once", action="store_true",
                        help="Record once, transcribe, print, exit")
    parser.add_argument("--listen", action="store_true",
                        help="Record, transcribe, copy to clipboard")
    parser.add_argument("--speak", type=str, help="Speak text via TTS")
    parser.add_argument("--engine", help="TTS engine override")
    parser.add_argument("--no-copy", action="store_true",
                        help="Don't copy to clipboard")
    args = parser.parse_args()

    if args.speak:
        speak_text(args.speak, engine=args.engine)
    elif args.once or args.listen:
        text = listen_once(copy=not args.no_copy)
        if text:
            print(text)
        else:
            sys.exit(1)
    else:
        daemon_mode()


if __name__ == "__main__":
    main()
