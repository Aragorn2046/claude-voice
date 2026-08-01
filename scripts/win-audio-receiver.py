#!/usr/bin/env python3
"""Windows-native TTS audio receiver for Shelby voice.

Plays received WAV audio through the default *Windows* playback device via
winsound (stdlib), bypassing the WSLg RDPSink whose per-stream cold-start was
clipping ~1.5s off every speech onset. Drop-in protocol match for the WSL
receiver the sender already speaks to:

    GET  /health  -> versioned JSON with receiver identity + LAN state
    POST /tts     -> body is WAV (audio/wav); play it

Usage:  python win-audio-receiver.py [port]   (default 9876)
"""
import json
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import winsound

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 9876
LOG_PATH = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "win-audio-receiver.log")
_play_lock = threading.Lock()
_profile_lock = threading.Lock()
_profile_cache = {"ts": 0.0, "profiles": [], "ready": False}
_audio_lock = threading.Lock()
_audio_cache = {"ts": 0.0, "ready": False}
_delivery_lock = threading.Lock()
_deliveries = {}
DELIVERY_LEDGER_PATH = os.environ.get(
    "SHELBY_DELIVERY_LEDGER_PATH",
    os.path.join(os.environ.get("TEMP", r"C:\Temp"), "shelby-voice-deliveries.json"),
)
RECEIVER_ID = (
    os.environ.get("SHELBY_RECEIVER_ID")
    or os.environ.get("COMPUTERNAME")
    or socket.gethostname()
).strip().lower().split(".", 1)[0]
HOME_NETWORK_NAMES = {
    name.strip().casefold()
    for name in os.environ.get("SHELBY_HOME_NETWORKS", "AragornHQ").split(",")
    if name.strip()
}
PHYSICAL_INTERFACE_PREFIXES = tuple(
    value.strip().casefold()
    for value in os.environ.get(
        "SHELBY_PHYSICAL_INTERFACE_PREFIXES", "wifi,wi-fi,ethernet"
    ).split(",")
    if value.strip()
)


def is_recognized_physical_interface(alias: str) -> bool:
    normalized = str(alias or "").strip().casefold()
    return any(
        normalized == prefix or normalized.startswith(prefix + " ")
        for prefix in PHYSICAL_INTERFACE_PREFIXES
    )


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


def _write_delivery_ledger_locked() -> None:
    """Persist ID tombstones atomically; no audio or text content is stored."""
    directory = os.path.dirname(DELIVERY_LEDGER_PATH) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = DELIVERY_LEDGER_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(_deliveries, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, DELIVERY_LEDGER_PATH)


def load_delivery_ledger() -> None:
    try:
        with open(DELIVERY_LEDGER_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    cleaned = {
        str(key): value for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, dict)
        and isinstance(value.get("status"), str)
    }
    with _delivery_lock:
        _deliveries.clear()
        _deliveries.update(cleaned)


def set_delivery_status(delivery_id: str, status: str) -> None:
    if not delivery_id:
        return
    now = time.time()
    with _delivery_lock:
        _deliveries[delivery_id] = {"status": status, "updated_at": now}
        _write_delivery_ledger_locked()


def get_delivery_status(delivery_id: str) -> dict | None:
    with _delivery_lock:
        value = _deliveries.get(delivery_id)
        return dict(value) if value else None


def reserve_delivery(delivery_id: str) -> tuple[bool, dict]:
    with _delivery_lock:
        existing = _deliveries.get(delivery_id)
        if existing:
            return False, dict(existing)
        value = {"status": "reserved", "updated_at": time.time()}
        _deliveries[delivery_id] = value
        _write_delivery_ledger_locked()
        return True, dict(value)


def accept_delivery(delivery_id: str) -> tuple[bool, dict]:
    """Atomically move a reservation to accepted, or dedupe a replay.

    Missing reservations are accepted for backward compatibility with older
    senders. A cancelled reservation is never resurrected, which makes a
    sender-side cancel safe even when a delayed upload arrives afterward.
    """
    with _delivery_lock:
        existing = _deliveries.get(delivery_id)
        if existing and existing.get("status") != "reserved":
            return False, dict(existing)
        value = {"status": "accepted", "updated_at": time.time()}
        _deliveries[delivery_id] = value
        _write_delivery_ledger_locked()
        return True, dict(value)


def cancel_delivery(delivery_id: str) -> tuple[bool, dict]:
    """Cancel only a not-yet-accepted delivery reservation."""
    with _delivery_lock:
        existing = _deliveries.get(delivery_id)
        if existing and existing.get("status") not in {"reserved", "cancelled"}:
            return False, dict(existing)
        value = {"status": "cancelled", "updated_at": time.time()}
        _deliveries[delivery_id] = value
        _write_delivery_ledger_locked()
        return True, dict(value)


def register_delivery(delivery_id: str) -> tuple[bool, dict]:
    """Compatibility alias for older tests/callers."""
    return accept_delivery(delivery_id)


def validate_wav(data: bytes) -> float | None:
    """Return playback duration for a structurally valid, non-empty PCM WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frame_rate = reader.getframerate()
            frame_count = reader.getnframes()
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            if (
                reader.getcomptype() != "NONE"
                or frame_rate <= 0 or frame_count <= 0
                or channels not in {1, 2}
                or sample_width not in {1, 2, 3, 4}
            ):
                return None
            frames = reader.readframes(frame_count)
            if len(frames) != frame_count * channels * sample_width:
                return None
            return frame_count / frame_rate
    except (wave.Error, EOFError):
        return None


def play_wav(data: bytes, delivery_id: str = "", duration: float | None = None) -> None:
    # Serialize playback; winsound is process-global (a 2nd call would cut the 1st).
    with _play_lock:
        fd, path = tempfile.mkstemp(suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # SND_ASYNC returns only after Windows accepts playback. Keep both
            # the file and serialization lock for the validated WAV duration.
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            set_delivery_status(delivery_id, "playing")
            time.sleep(max(0.0, duration if duration is not None else 0.0) + 0.15)
            set_delivery_status(delivery_id, "played")
        except Exception as e:
            log(f"play error: {e}")
            set_delivery_status(delivery_id, "failed")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


def refresh_network_profiles() -> bool:
    """Refresh Windows network state off the HTTP request path."""
    names = []
    try:
        powershell = os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        script = (
            "$ErrorActionPreference='Stop'; "
            "$profiles=@(Get-NetConnectionProfile | Where-Object {"
            "$_.IPv4Connectivity -ne 'Disconnected' -or "
            "$_.IPv6Connectivity -ne 'Disconnected'"
            "}); "
            "$rows=@(foreach($p in $profiles){"
            "[PSCustomObject]@{name=[string]$p.Name;"
            "interface_alias=[string]$p.InterfaceAlias}});"
            "$rows | ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                profiles = []
                for item in parsed:
                    if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                        continue
                    interface_alias = str(item.get("interface_alias") or "").strip()
                    is_physical = is_recognized_physical_interface(interface_alias)
                    profiles.append({
                        "name": str(item["name"]).strip(),
                        "interface_alias": interface_alias,
                        "hardware_interface": is_physical,
                        "virtual": not is_physical,
                    })
                names = profiles
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return False

    with _profile_lock:
        _profile_cache.update(ts=time.time(), profiles=names, ready=True)
    return True


def refresh_audio_ready() -> bool:
    """Verify that Windows currently exposes at least one healthy sound device."""
    ready = False
    query_succeeded = False
    try:
        powershell = os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"),
            "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
        )
        script = (
            "$ErrorActionPreference='Stop'; "
            "$render=@(Get-PnpDevice -Class AudioEndpoint -Status OK | Where-Object {"
            "$_.InstanceId -like '*MMDEVAPI*{0.0.0.*'});"
            "if($render.Count -gt 0)"
            "{'true'}else{'false'}"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=6,
        )
        normalized = result.stdout.strip().lower()
        query_succeeded = result.returncode == 0 and normalized in {"true", "false"}
        ready = query_succeeded and normalized == "true"
    except (OSError, subprocess.SubprocessError):
        query_succeeded = False
    if not query_succeeded:
        # A transiently slow PowerShell query must not overwrite a recent,
        # verified-good device snapshot while another utterance is playing.
        with _audio_lock:
            age = max(0.0, time.time() - float(_audio_cache["ts"]))
            return bool(_audio_cache["ready"] and age <= 30)
    with _audio_lock:
        _audio_cache.update(ts=time.time(), ready=ready)
    return ready


def system_state_monitor() -> None:
    while True:
        refresh_network_profiles()
        refresh_audio_ready()
        time.sleep(10)


def active_network_profiles() -> tuple[list[dict], bool, float]:
    """Return the latest non-blocking network snapshot and readiness flag."""
    with _profile_lock:
        names = [dict(profile) for profile in _profile_cache["profiles"]]
        ready = bool(_profile_cache["ready"])
        observed_at = float(_profile_cache["ts"])
        return names, ready, observed_at


def health_payload() -> dict:
    profiles, profile_ready, observed_at = active_network_profiles()
    age = max(0.0, time.time() - observed_at) if profile_ready else None
    fresh = bool(profile_ready and age is not None and age <= 30)
    names = [profile["name"] for profile in profiles]
    physical_names = [
        profile["name"] for profile in profiles
        if profile.get("hardware_interface") is True and profile.get("virtual") is not True
    ]
    home_lan = any(name.casefold() in HOME_NETWORK_NAMES for name in physical_names)
    location_state = "home" if fresh and home_lan else (
        "away" if fresh and physical_names else "unknown"
    )
    with _audio_lock:
        audio_age = max(0.0, time.time() - float(_audio_cache["ts"]))
        audio_ready = bool(_audio_cache["ready"] and audio_age <= 30)
    return {
        "schema_version": "shelby.voice-receiver.health.v1",
        "receiver_id": RECEIVER_ID,
        "audio_ready": audio_ready,
        "location_ready": location_state != "unknown",
        "location_state": location_state,
        "location_age_seconds": round(age, 3) if age is not None else None,
        "network_profiles": names,
        "physical_network_profiles": physical_names,
        "home_lan": home_lan,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default stderr access logs
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(health_payload(), separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/delivery/"):
            delivery_id = self.path.removeprefix("/delivery/").strip()
            status = get_delivery_status(delivery_id)
            if status is None:
                body = json.dumps({"delivery_id": delivery_id, "status": "missing"}).encode()
                self.send_response(404)
            else:
                body = json.dumps({"delivery_id": delivery_id, **status}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/delivery/") and self.path.endswith("/reserve"):
            delivery_id = self.path.removeprefix("/delivery/").removesuffix("/reserve").strip("/")
            if not delivery_id or len(delivery_id) > 128:
                self.send_response(400)
                self.end_headers()
                return
            created, status = reserve_delivery(delivery_id)
            body = json.dumps({"delivery_id": delivery_id, **status}).encode()
            self.send_response(201 if created else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > 20_000_000:
            self.send_response(413)
            self.end_headers()
            return
        data = self.rfile.read(n) if n > 0 else b""
        duration = validate_wav(data)
        if duration is None:
            body = json.dumps({"status": "rejected", "reason": "invalid_wav"}).encode()
            self.send_response(415)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not refresh_audio_ready():
            body = json.dumps({"status": "rejected", "reason": "audio_not_ready"}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Bind remote Dusk delivery to current LAN state. A synchronous refresh
        # closes the health-to-POST race when Dusk joins AragornHQ after probing.
        client_ip = str(self.client_address[0])
        is_loopback = client_ip in {"127.0.0.1", "::1"}
        require_off_lan = self.headers.get("X-Shelby-Require-Off-Lan") == "1"
        if require_off_lan and (RECEIVER_ID != "dusk" or is_loopback):
            body = json.dumps({"status": "rejected", "reason": "invalid_off_lan_target"}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if require_off_lan or (RECEIVER_ID == "dusk" and not is_loopback):
            refreshed = refresh_network_profiles()
            current = health_payload()
            if not refreshed or current["location_state"] != "away":
                body = json.dumps({"status": "rejected", "reason": "dusk_not_off_lan"}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        delivery_id = self.headers.get("X-Shelby-Delivery-ID", "").strip() or uuid.uuid4().hex
        is_new, status = accept_delivery(delivery_id)
        if status.get("status") == "cancelled":
            body = json.dumps({"delivery_id": delivery_id, **status}).encode()
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if is_new:
            threading.Thread(
                target=play_wav, args=(data, delivery_id, duration), daemon=True
            ).start()
        body = json.dumps({"delivery_id": delivery_id, **status}).encode()
        self.send_response(202 if is_new else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if is_new:
            log(f"play {len(data)} bytes")

    def do_DELETE(self):
        if not self.path.startswith("/delivery/"):
            self.send_response(404)
            self.end_headers()
            return
        delivery_id = self.path.removeprefix("/delivery/").strip()
        cancelled, status = cancel_delivery(delivery_id)
        body = json.dumps({"delivery_id": delivery_id, **status}).encode()
        self.send_response(200 if cancelled else 409)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    # Warm both readiness snapshots before accepting health or TTS requests.
    load_delivery_ledger()
    refresh_network_profiles()
    refresh_audio_ready()
    threading.Thread(
        target=system_state_monitor, daemon=True, name="system-state-monitor"
    ).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Windows TTS receiver listening on 0.0.0.0:{PORT} (winsound, default device)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
