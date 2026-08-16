from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from websocket_transport import (  # noqa: E402
    SharedAppServerProcess,
    WebSocketConnection,
    validate_loopback_ws_url,
    websocket_accept_value,
)


def recv_until(sock: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def recv_client_text(sock: socket.socket) -> str:
    first, second = sock.recv(2)
    if first & 0x0F != 0x1 or not second & 0x80:
        raise AssertionError("expected a masked client text frame")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    mask = sock.recv(4)
    payload = bytearray()
    while len(payload) < length:
        payload.extend(sock.recv(length - len(payload)))
    decoded = bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload)
    )
    return decoded.decode("utf-8")


def send_server_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    if len(payload) < 126:
        header = bytes([0x81, len(payload)])
    else:
        header = bytes([0x81, 126]) + struct.pack("!H", len(payload))
    sock.sendall(header + payload)


class WebSocketTransportTests(unittest.TestCase):
    def test_shared_server_reuses_ready_listener(self):
        manager = SharedAppServerProcess("codex.exe", "ws://127.0.0.1:18766")
        with (
            mock.patch.object(manager, "is_ready", return_value=True),
            mock.patch("websocket_transport.subprocess.Popen") as popen,
        ):
            self.assertFalse(manager.ensure_running())
        popen.assert_not_called()

    def test_shared_server_starts_codex_listener(self):
        manager = SharedAppServerProcess("codex.exe", "ws://127.0.0.1:18766")
        process = mock.MagicMock()
        process.poll.return_value = None
        with (
            mock.patch.object(manager, "is_ready", side_effect=[False, True]),
            mock.patch("websocket_transport.subprocess.Popen", return_value=process) as popen,
        ):
            self.assertTrue(manager.ensure_running())
        self.assertEqual(
            popen.call_args.args[0],
            ["codex.exe", "app-server", "--listen", "ws://127.0.0.1:18766"],
        )

    def test_loopback_url_validation(self):
        self.assertEqual(
            validate_loopback_ws_url("ws://127.0.0.1:18766/rpc?x=1"),
            ("127.0.0.1", 18766, "/rpc?x=1"),
        )
        with self.assertRaises(ValueError):
            validate_loopback_ws_url("ws://192.168.1.5:18766")
        with self.assertRaises(ValueError):
            validate_loopback_ws_url("wss://127.0.0.1:18766")

    def test_accept_value_matches_rfc_example(self):
        self.assertEqual(
            websocket_accept_value("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_json_round_trip(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        received: list[dict[str, object]] = []

        def serve() -> None:
            connection, _ = listener.accept()
            try:
                request = recv_until(connection, b"\r\n\r\n").decode("ascii")
                key_line = next(
                    line for line in request.split("\r\n")
                    if line.lower().startswith("sec-websocket-key:")
                )
                key = key_line.split(":", 1)[1].strip()
                response = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {websocket_accept_value(key)}\r\n\r\n"
                )
                connection.sendall(response.encode("ascii"))
                received.append(json.loads(recv_client_text(connection)))
                send_server_text(connection, json.dumps({"id": 7, "result": {"ok": True}}))
            finally:
                connection.close()
                listener.close()

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        client = WebSocketConnection(f"ws://127.0.0.1:{port}")
        client.send_json({"id": 7, "method": "initialize", "params": {}})
        messages = list(client.iter_text())
        client.close()
        server.join(timeout=2)

        self.assertEqual(received[0]["method"], "initialize")
        self.assertEqual(json.loads(messages[0]), {"id": 7, "result": {"ok": True}})


if __name__ == "__main__":
    unittest.main()
