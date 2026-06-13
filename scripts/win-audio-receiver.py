#!/usr/bin/env python3
"""Windows-native TTS audio receiver for Shelby voice.

Plays received WAV audio through the default *Windows* playback device via
winsound (stdlib), bypassing the WSLg RDPSink whose per-stream cold-start was
clipping ~1.5s off every speech onset. Drop-in protocol match for the WSL
receiver the sender already speaks to:

    GET  /health  -> 200 "OK"
    POST /tts     -> body is WAV (audio/wav); play it

Usage:  python win-audio-receiver.py [port]   (default 9876)
"""
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import winsound

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9876
LOG_PATH = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "win-audio-receiver.log")
_play_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass


def play_wav(data: bytes) -> None:
    # Serialize playback; winsound is process-global (a 2nd call would cut the 1st).
    with _play_lock:
        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # SND_FILENAME plays synchronously through the default Windows device.
            winsound.PlaySound(path, winsound.SND_FILENAME)
        except Exception as e:
            log(f"play error: {e}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default stderr access logs
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(n) if n > 0 else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        if data:
            log(f"play {len(data)} bytes")
            threading.Thread(target=play_wav, args=(data,), daemon=True).start()


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Windows TTS receiver listening on 0.0.0.0:{PORT} (winsound, default device)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
