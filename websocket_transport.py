from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
from typing import Any, Iterator
from urllib.parse import urlsplit


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def validate_loopback_ws_url(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "ws":
        raise ValueError("Shared Codex app-server URL must use ws://")
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Shared Codex app-server must listen on loopback")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return host, port, path


def websocket_accept_value(key: str) -> str:
    digest = hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocketConnection:
    def __init__(self, url: str, timeout_seconds: float = 10.0):
        host, port, path = validate_loopback_ws_url(url)
        self.socket = socket.create_connection((host, port), timeout=timeout_seconds)
        self.socket.settimeout(timeout_seconds)
        self.send_lock = threading.Lock()
        self.closed = False
        self.receive_buffer = bytearray()
        self._handshake(host, port, path)
        self.socket.settimeout(None)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        host_header = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise ConnectionError("Codex app-server closed during WebSocket handshake")
            response.extend(chunk)
            if len(response) > 65536:
                raise ConnectionError("Codex app-server returned an oversized handshake")
        header_bytes, remainder = bytes(response).split(b"\r\n\r\n", 1)
        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        if len(lines) < 1 or " 101 " not in f" {lines[0]} ":
            raise ConnectionError(f"Codex app-server WebSocket handshake failed: {lines[0]}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != websocket_accept_value(key):
            raise ConnectionError("Codex app-server returned an invalid WebSocket accept key")
        self.receive_buffer.extend(remainder)

    def _recv_exact(self, size: int) -> bytes:
        while len(self.receive_buffer) < size:
            chunk = self.socket.recv(max(4096, size - len(self.receive_buffer)))
            if not chunk:
                raise EOFError("Codex app-server WebSocket connection closed")
            self.receive_buffer.extend(chunk)
        result = bytes(self.receive_buffer[:size])
        del self.receive_buffer[:size]
        return result

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise ConnectionError("Codex app-server WebSocket connection is closed")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self.send_lock:
            self.socket.sendall(bytes(header) + masked)

    def send_json(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
        self._send_frame(0x1, payload)

    def iter_text(self) -> Iterator[str]:
        fragments = bytearray()
        fragment_opcode: int | None = None
        try:
            while not self.closed:
                first, second = self._recv_exact(2)
                final = bool(first & 0x80)
                opcode = first & 0x0F
                masked = bool(second & 0x80)
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._recv_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._recv_exact(8))[0]
                if length > _MAX_MESSAGE_BYTES:
                    raise ConnectionError("Codex app-server WebSocket message is too large")
                mask = self._recv_exact(4) if masked else b""
                payload = self._recv_exact(length)
                if masked:
                    payload = bytes(
                        byte ^ mask[index % 4] for index, byte in enumerate(payload)
                    )
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in {0x1, 0x2}:
                    fragments = bytearray(payload)
                    fragment_opcode = opcode
                elif opcode == 0x0 and fragment_opcode is not None:
                    fragments.extend(payload)
                else:
                    raise ConnectionError(f"Unsupported WebSocket opcode: {opcode}")
                if final:
                    if fragment_opcode == 0x1:
                        yield fragments.decode("utf-8")
                    fragments = bytearray()
                    fragment_opcode = None
        except (EOFError, OSError):
            return

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()


class SharedAppServerProcess:
    def __init__(
        self,
        executable_path: str,
        websocket_url: str,
        start_timeout_seconds: float = 30.0,
    ):
        validate_loopback_ws_url(websocket_url)
        self.executable_path = executable_path
        self.websocket_url = websocket_url
        self.start_timeout_seconds = start_timeout_seconds
        self.process: subprocess.Popen[str] | None = None

    def is_ready(self, timeout_seconds: float = 1.0) -> bool:
        host, port, _ = validate_loopback_ws_url(self.websocket_url)
        connection = http.client.HTTPConnection(host, port, timeout=timeout_seconds)
        try:
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            response.read()
            return response.status == 200
        except OSError:
            return False
        finally:
            connection.close()

    def ensure_running(self) -> bool:
        if self.is_ready():
            return False
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [self.executable_path, "app-server", "--listen", self.websocket_url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + self.start_timeout_seconds
        while time.monotonic() < deadline:
            if self.is_ready():
                return True
            exit_code = self.process.poll()
            if exit_code is not None:
                raise RuntimeError(f"Shared Codex app-server exited with code {exit_code}")
            time.sleep(0.2)
        self.close()
        raise TimeoutError("Shared Codex app-server did not become ready")

    def close(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
