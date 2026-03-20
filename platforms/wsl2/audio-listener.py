#!/usr/bin/env python3
"""Persistent audio listener for remote TTS playback.

Runs on a WSL2 machine that SSHes/Moshes into a remote Mac. Receives WAV audio
over TCP and plays via paplay (WSLg PulseAudio).

The remote Mac's TTS hook auto-detects SSH sessions and sends audio to
the SSH client's IP (via SSH_CONNECTION env var), so no SSH tunnel is needed.

Requirements:
  - WSL2 mirrored networking (.wslconfig: networkingMode=mirrored)
    OR netsh portproxy from Windows to WSL2
  - Tailscale on both machines (so the IPs are routable)

Auto-start via /etc/wsl.conf:
    [boot]
    command=su -c 'nohup python3 /home/USER/scripts/audio-listener.py > /tmp/audio-listener.log 2>&1 &' USER

Manual start:
    python3 ~/scripts/audio-listener.py &
"""

import os
import socket
import subprocess
import sys
import tempfile
import time

PORT = 12345
os.environ.setdefault("PULSE_SERVER", "unix:/mnt/wslg/PulseServer")


def play_wav(path: str):
    """Play WAV file via paplay, aplay, or afplay (whichever is available)."""
    for player in ["paplay", "aplay", "afplay"]:
        try:
            subprocess.Popen(
                [player, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except FileNotFoundError:
            continue
    sys.stderr.write("audio-listener: no audio player found\n")


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        srv.bind(("0.0.0.0", PORT))
    except OSError as e:
        sys.stderr.write(f"audio-listener: bind failed on port {PORT}: {e}\n")
        sys.exit(1)

    srv.listen(5)
    sys.stderr.write(f"audio-listener: listening on 0.0.0.0:{PORT}\n")

    while True:
        try:
            conn, addr = srv.accept()
            sys.stderr.write(f"audio-listener: connection from {addr[0]}:{addr[1]}\n")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                path = f.name
            conn.close()

            if os.path.getsize(path) > 44:  # WAV header is 44 bytes
                sys.stderr.write(f"audio-listener: playing {os.path.getsize(path)} bytes\n")
                play_wav(path)
            else:
                os.unlink(path)

        except Exception as e:
            sys.stderr.write(f"audio-listener: error: {e}\n")
            time.sleep(1)


if __name__ == "__main__":
    main()
