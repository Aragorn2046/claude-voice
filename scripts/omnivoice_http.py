"""Small deadline-bounded HTTP/1.0 transport for the private OmniVoice lane."""

import socket
import time
from urllib.parse import urlsplit


def post_json(base_url: str, payload: bytes, timeout: float,
              max_response_bytes: int, chunk_size: int = 16 * 1024,
              max_header_bytes: int = 64 * 1024) -> bytes:
    """POST JSON and return a verified RIFF/WAVE body, or raise on failure."""
    deadline = time.monotonic() + float(timeout)
    parsed = urlsplit(base_url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.port is None
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("OmniVoice endpoint must be http://host:port")

    host = parsed.hostname
    port = parsed.port
    host_label = f"[{host}]" if ":" in host else host
    request = (
        f"POST /tts HTTP/1.0\r\n"
        f"Host: {host_label}:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + payload

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OmniVoice request exceeded its total deadline")

    response_buffer = bytearray()
    response_body = bytearray()
    header_complete = False
    expected_length = None
    with socket.create_connection((host, port), timeout=remaining) as sock:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("OmniVoice request exceeded its total deadline")
        sock.settimeout(remaining)
        sock.sendall(request)
        if time.monotonic() >= deadline:
            raise TimeoutError("OmniVoice request exceeded its total deadline")

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OmniVoice response exceeded its total deadline")
            if header_complete:
                read_limit = min(
                    chunk_size, max_response_bytes - len(response_body) + 1
                )
            else:
                read_limit = min(
                    chunk_size, max_header_bytes - len(response_buffer)
                )
                if read_limit <= 0:
                    raise ValueError("OmniVoice response headers exceeded the limit")
            sock.settimeout(min(remaining, 0.25))
            try:
                chunk = sock.recv(read_limit)
            except socket.timeout:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("OmniVoice response exceeded its total deadline")
            if not chunk:
                break

            if not header_complete:
                response_buffer.extend(chunk)
                header_end = response_buffer.find(b"\r\n\r\n")
                if header_end < 0:
                    if len(response_buffer) >= max_header_bytes:
                        raise ValueError("OmniVoice response headers exceeded the limit")
                    continue
                if header_end + 4 > max_header_bytes:
                    raise ValueError("OmniVoice response headers exceeded the limit")

                lines = bytes(response_buffer[:header_end]).split(b"\r\n")
                status_parts = lines[0].split() if lines else []
                if len(status_parts) < 2 or not status_parts[0].startswith(b"HTTP/"):
                    raise ValueError("OmniVoice returned a malformed HTTP status line")
                try:
                    status = int(status_parts[1])
                except ValueError as exc:
                    raise ValueError("OmniVoice returned a malformed HTTP status") from exc
                if status != 200:
                    raise ValueError(f"OmniVoice returned HTTP {status}")

                for line in lines[1:]:
                    name, separator, value = line.partition(b":")
                    if separator and name.strip().lower() == b"content-length":
                        try:
                            expected_length = int(value.strip())
                        except ValueError as exc:
                            raise ValueError("OmniVoice returned an invalid Content-Length") from exc
                        if expected_length < 0:
                            raise ValueError("OmniVoice returned an invalid Content-Length")
                        if expected_length > max_response_bytes:
                            raise ValueError("OmniVoice response exceeded the byte limit")
                        break

                response_body.extend(response_buffer[header_end + 4:])
                response_buffer.clear()
                header_complete = True
            else:
                response_body.extend(chunk)

            if len(response_body) > max_response_bytes:
                raise ValueError("OmniVoice response exceeded the byte limit")
            if expected_length is not None:
                if len(response_body) > expected_length:
                    raise ValueError("OmniVoice response exceeded its Content-Length")
                if len(response_body) == expected_length:
                    break

    if not header_complete:
        raise ValueError("OmniVoice returned no HTTP response headers")
    if expected_length is not None and len(response_body) != expected_length:
        raise ValueError("OmniVoice response ended before its Content-Length")
    wav_data = bytes(response_body)
    if (len(wav_data) < 12 or wav_data[:4] != b"RIFF"
            or wav_data[8:12] != b"WAVE"):
        raise ValueError("OmniVoice returned a non-RIFF WAV payload")
    return wav_data
