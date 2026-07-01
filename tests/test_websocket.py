import base64
import json
import os
import socket
import struct
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from server import GameHub, make_handler


class WebSocketTestClient:
    def __init__(self, host, port):
        self.socket = socket.create_connection((host, port), timeout=3)
        self.socket.settimeout(3)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = self._read_until(b"\r\n\r\n")
        if b" 101 " not in response:
            raise AssertionError(f"WebSocket handshake failed: {response!r}")

    def close(self):
        try:
            self.socket.close()
        except OSError:
            pass

    def send_json(self, payload):
        self._send_text(json.dumps(payload, separators=(",", ":")))

    def read_json(self):
        opcode, payload = self._read_frame()
        if opcode != 0x1:
            raise AssertionError(f"Expected text frame, got opcode {opcode}")
        return json.loads(payload.decode("utf-8"))

    def read_until(self, predicate, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.read_json()
            if predicate(message):
                return message
        raise AssertionError("Timed out waiting for WebSocket message")

    def _read_until(self, marker):
        chunks = bytearray()
        while marker not in chunks:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise ConnectionError("Socket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_exact(self, length):
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.socket.recv(length - len(chunks))
            if not chunk:
                raise ConnectionError("Socket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_frame(self):
        first_byte, second_byte = self._read_exact(2)
        opcode = first_byte & 0x0F
        length = second_byte & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        payload = self._read_exact(length) if length else b""
        return opcode, payload

    def _send_text(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        first_byte = 0x81
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first_byte, length | 0x80)
        elif length < 65536:
            header = struct.pack("!BBH", first_byte, 126 | 0x80, length)
        else:
            header = struct.pack("!BBQ", first_byte, 127 | 0x80, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(header + mask + masked)


class WebSocketIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.hub = GameHub(auto_advance_delay=0.05)
        handler = make_handler(self.hub, Path.cwd())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.host, self.port = self.server.server_address
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def client(self):
        client = WebSocketTestClient(self.host, self.port)
        self.clients.append(client)
        return client

    def test_two_clients_keep_choices_hidden_until_resolution(self):
        p1 = self.client()
        p2 = self.client()

        p1.send_json({"type": "create"})
        joined_p1 = p1.read_until(lambda message: message["type"] == "joined")
        room_code = joined_p1["roomCode"]
        p1_waiting = p1.read_until(lambda message: message["type"] == "state")

        p2.send_json({"type": "join", "roomCode": room_code, "player": "p2"})
        joined_p2 = p2.read_until(lambda message: message["type"] == "joined")
        p2_choosing = p2.read_until(lambda message: message["type"] == "state")
        p1_choosing = p1.read_until(lambda message: message["type"] == "state")

        self.assertEqual(joined_p1["player"], "p1")
        self.assertEqual(joined_p2["player"], "p2")
        self.assertEqual(p1_waiting["state"]["phase"], "waiting")
        self.assertEqual(p1_choosing["state"]["phase"], "choosing")
        self.assertEqual(p2_choosing["state"]["phase"], "choosing")
        self.assertIsNone(p1_choosing["state"]["opponentTarget"])
        self.assertIsNone(p2_choosing["state"]["opponentTarget"])
        self.assertEqual(len(p1_choosing["state"]["availableGems"]), 4)
        self.assertTrue(all(count >= 1 for count in p1_choosing["state"]["offer"].values()))

        p1.send_json({"type": "choose", "gem": "a"})
        p2_private = p2.read_until(
            lambda message: message["type"] == "state" and message["state"]["ready"]["p1"]
        )
        self.assertIsNone(p2_private["state"]["lastResult"])
        self.assertIsNone(p2_private["state"]["ownSelection"])
        self.assertTrue(p2_private["state"]["ready"]["p1"])
        self.assertFalse(p2_private["state"]["ready"]["p2"])

        p2.send_json({"type": "choose", "gem": "a"})
        p1_resolved = p1.read_until(
            lambda message: message["type"] == "state" and message["state"]["phase"] == "resolved"
        )
        p2_resolved = p2.read_until(
            lambda message: message["type"] == "state" and message["state"]["phase"] == "resolved"
        )

        self.assertTrue(p1_resolved["state"]["lastResult"]["collision"])
        self.assertEqual(p1_resolved["state"]["lastResult"]["choices"], {"p1": "a", "p2": "a"})
        self.assertEqual(p1_resolved["state"]["lastResult"]["gainCounts"], {"p1": 0, "p2": 0})
        self.assertEqual(p2_resolved["state"]["lastResult"]["choices"], {"p1": "a", "p2": "a"})

        p1_next = p1.read_until(
            lambda message: (
                message["type"] == "state"
                and message["state"]["phase"] == "choosing"
                and message["state"]["roundNumber"] == 2
            )
        )
        p2_next = p2.read_until(
            lambda message: (
                message["type"] == "state"
                and message["state"]["phase"] == "choosing"
                and message["state"]["roundNumber"] == 2
            )
        )
        self.assertEqual(p1_next["state"]["roundNumber"], 2)
        self.assertEqual(p2_next["state"]["roundNumber"], 2)

    def test_restart_requires_both_clients(self):
        p1 = self.client()
        p2 = self.client()

        p1.send_json({"type": "create"})
        joined_p1 = p1.read_until(lambda message: message["type"] == "joined")
        room_code = joined_p1["roomCode"]
        p1.read_until(lambda message: message["type"] == "state")

        p2.send_json({"type": "join", "roomCode": room_code, "player": "p2"})
        p2.read_until(lambda message: message["type"] == "joined")
        p1.read_until(lambda message: message["type"] == "state" and message["state"]["phase"] == "choosing")
        p2.read_until(lambda message: message["type"] == "state" and message["state"]["phase"] == "choosing")

        p1.send_json({"type": "choose", "gem": "a"})
        p2.read_until(lambda message: message["type"] == "state" and message["state"]["ready"]["p1"])
        p1.send_json({"type": "restart"})
        p2_restart_waiting = p2.read_until(
            lambda message: message["type"] == "state" and message["state"]["restartReady"]["p1"]
        )
        self.assertEqual(p2_restart_waiting["state"]["phase"], "choosing")
        self.assertFalse(p2_restart_waiting["state"]["restartReady"]["p2"])

        p2.send_json({"type": "restart"})
        p1_restarted = p1.read_until(
            lambda message: (
                message["type"] == "state"
                and message["state"]["phase"] == "choosing"
                and not message["state"]["restartReady"]["p1"]
                and not message["state"]["ready"]["p1"]
            )
        )
        p2_restarted = p2.read_until(
            lambda message: (
                message["type"] == "state"
                and message["state"]["phase"] == "choosing"
                and not message["state"]["restartReady"]["p2"]
                and not message["state"]["ready"]["p1"]
            )
        )
        self.assertEqual(p1_restarted["state"]["roundNumber"], 1)
        self.assertEqual(p2_restarted["state"]["log"], [])


if __name__ == "__main__":
    unittest.main()
