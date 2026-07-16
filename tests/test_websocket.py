import base64
import http.client
import json
import os
import socket
import struct
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from server import (
    WEBSOCKET_CLOSE_INVALID_PAYLOAD,
    WEBSOCKET_CLOSE_PROTOCOL_ERROR,
    GameHub,
    empty_counts,
    make_handler,
)


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
            f"Origin: http://{host}:{port}\r\n"
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

    def send_text(self, text):
        self._send_text(text)

    def send_unmasked_text(self, text):
        payload = text.encode("utf-8")
        length = len(payload)
        if length >= 126:
            raise ValueError("Test helper only supports short payloads")
        self.socket.sendall(struct.pack("!BB", 0x81, length) + payload)

    def send_reserved_bit_text(self, text):
        self._send_text(text, first_byte=0xC1)

    def send_invalid_utf8(self):
        self._send_payload(b"\xff")

    def read_json(self):
        opcode, payload = self._read_frame()
        if opcode != 0x1:
            raise AssertionError(f"Expected text frame, got opcode {opcode}")
        return json.loads(payload.decode("utf-8"))

    def read_close_code(self):
        opcode, payload = self._read_frame()
        if opcode != 0x8 or len(payload) < 2:
            raise AssertionError(f"Expected close frame with status, got opcode {opcode} and {payload!r}")
        return struct.unpack("!H", payload[:2])[0]

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

    def _send_text(self, text, first_byte=0x81):
        self._send_payload(text.encode("utf-8"), first_byte=first_byte)

    def _send_payload(self, payload, first_byte=0x81):
        mask = os.urandom(4)
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

    def http_status(self, path):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def websocket_handshake_status(self, key, version="13", origin=None):
        connection = socket.create_connection((self.host, self.port), timeout=3)
        try:
            request_origin = origin or f"http://{self.host}:{self.port}"
            request = (
                "GET /ws HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Origin: {request_origin}\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: {version}\r\n"
                "\r\n"
            )
            connection.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
            status_line = bytes(response).split(b"\r\n", 1)[0]
            return int(status_line.split()[1])
        finally:
            connection.close()

    def test_static_server_exposes_only_public_paths(self):
        self.assertEqual(self.http_status("/"), 200)
        self.assertEqual(self.http_status("/assets/gem-a.svg"), 200)
        self.assertEqual(self.http_status("/src/app.js"), 200)
        self.assertEqual(self.http_status("/src/game.js"), 404)
        self.assertEqual(self.http_status("/tests/browser-test.html"), 404)
        self.assertEqual(self.http_status("/server.py"), 404)
        self.assertEqual(self.http_status("/tests/test_server.py"), 404)
        self.assertEqual(self.http_status("/.git/config"), 404)
        self.assertEqual(self.http_status("/src/../server.py"), 404)

    def test_unmasked_websocket_frames_are_rejected(self):
        client = self.client()

        client.send_unmasked_text('{"type":"create"}')

        self.assertEqual(client.read_close_code(), WEBSOCKET_CLOSE_PROTOCOL_ERROR)

    def test_invalid_websocket_handshake_key_is_rejected(self):
        self.assertEqual(self.websocket_handshake_status("not-a-valid-key"), 400)

    def test_cross_origin_websocket_handshake_is_rejected(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")

        self.assertEqual(
            self.websocket_handshake_status(key, origin="https://example.com"),
            403,
        )

    def test_reserved_websocket_bits_are_rejected(self):
        client = self.client()

        client.send_reserved_bit_text('{"type":"create"}')

        self.assertEqual(client.read_close_code(), WEBSOCKET_CLOSE_PROTOCOL_ERROR)

    def test_invalid_utf8_text_is_closed_cleanly(self):
        client = self.client()

        client.send_invalid_utf8()

        self.assertEqual(client.read_close_code(), WEBSOCKET_CLOSE_INVALID_PAYLOAD)

    def test_malformed_messages_return_stable_client_errors(self):
        client = self.client()

        client.send_text("not-json")
        invalid_json = client.read_until(lambda message: message["type"] == "error")
        client.send_text("[]")
        invalid_envelope = client.read_until(lambda message: message["type"] == "error")
        client.send_json({"type": "join", "roomCode": []})
        invalid_field = client.read_until(lambda message: message["type"] == "error")

        self.assertEqual(invalid_json["message"], "Message must be valid JSON.")
        self.assertEqual(invalid_envelope["message"], "Message must be a JSON object.")
        self.assertEqual(invalid_field["message"], "Room code must be text.")

    def test_replaced_connection_is_closed(self):
        host = self.client()
        old_guest = self.client()
        new_guest = self.client()

        host.send_json({"type": "create"})
        host_joined = host.read_until(lambda message: message["type"] == "joined")
        host.read_until(lambda message: message["type"] == "state")
        old_guest.send_json({"type": "join", "roomCode": host_joined["roomCode"]})
        old_joined = old_guest.read_until(lambda message: message["type"] == "joined")
        old_guest.read_until(lambda message: message["type"] == "state")

        new_guest.send_json({
            "type": "join",
            "roomCode": host_joined["roomCode"],
            "player": "p2",
            "token": old_joined["token"],
        })

        replaced = old_guest.read_until(lambda message: message["type"] == "error")
        opcode, _ = old_guest._read_frame()
        self.assertEqual(replaced["message"], "This seat was opened in another tab.")
        self.assertEqual(opcode, 0x8)
        with self.assertRaises(ConnectionError):
            old_guest._read_exact(1)

    def test_disconnected_first_seat_is_filled_and_match_restarts(self):
        p1 = self.client()
        p2 = self.client()
        replacement = self.client()

        p1.send_json({"type": "create"})
        joined = p1.read_until(lambda message: message["type"] == "joined")
        p1.read_until(lambda message: message["type"] == "state")
        p2.send_json({"type": "join", "roomCode": joined["roomCode"]})
        p2.read_until(lambda message: message["type"] == "joined")
        p2.read_until(lambda message: message["type"] == "state")
        p1.read_until(lambda message: message["type"] == "state" and message["state"]["phase"] == "choosing")

        p1.send_json({"type": "choose", "gem": "a"})
        p2.send_json({"type": "choose", "gem": "c"})
        p1.read_until(lambda message: message["type"] == "state" and message["state"]["phase"] == "resolved")
        p2.read_until(lambda message: message["type"] == "state" and message["state"]["phase"] == "resolved")
        p1.close()
        p2.read_until(
            lambda message: message["type"] == "state" and not message["state"]["connected"]["p1"]
        )

        replacement.send_json({"type": "join", "roomCode": joined["roomCode"]})
        replacement_joined = replacement.read_until(lambda message: message["type"] == "joined")
        replacement_state = replacement.read_until(lambda message: message["type"] == "state")
        p2_state = p2.read_until(
            lambda message: (
                message["type"] == "state"
                and message["state"]["connected"]["p1"]
                and message["state"]["roundNumber"] == 1
                and message["state"]["log"] == []
            )
        )

        self.assertEqual(replacement_joined["player"], "p1")
        self.assertEqual(replacement_state["state"]["phase"], "choosing")
        self.assertEqual(replacement_state["state"]["roundNumber"], 1)
        self.assertEqual(replacement_state["state"]["log"], [])
        self.assertEqual(
            p2_state["state"]["collected"],
            {"p1": empty_counts(), "p2": empty_counts()},
        )
        time.sleep(self.hub.auto_advance_delay * 2)
        self.assertEqual(self.hub.rooms[joined["roomCode"]].round_index, 0)

    def test_connection_cannot_create_multiple_rooms(self):
        client = self.client()

        client.send_json({"type": "create"})
        client.read_until(lambda message: message["type"] == "joined")
        client.read_until(lambda message: message["type"] == "state")
        client.send_json({"type": "create"})
        error = client.read_until(lambda message: message["type"] == "error")

        self.assertIn("already in a room", error["message"])
        self.assertEqual(len(self.hub.rooms), 1)

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
