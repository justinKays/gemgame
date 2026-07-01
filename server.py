from __future__ import annotations

import argparse
import base64
import hashlib
from itertools import permutations
import json
import mimetypes
import os
import random
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROUND_COUNT = 8
GEMS = ("a", "b", "c", "d")
GEM_DEFINITIONS = (
    {"id": "a", "label": "A", "name": "Amber", "color": "#f2a51f"},
    {"id": "b", "label": "B", "name": "Emerald", "color": "#2fbf71"},
    {"id": "c", "label": "C", "name": "Sapphire", "color": "#2d6cdf"},
    {"id": "d", "label": "D", "name": "Ruby", "color": "#d84c74"},
)
TARGET_POOLS = {
    "p1": ("a", "b"),
    "p2": ("c", "d"),
}
MIN_PER_GEM_PER_ROUND = 1
MAX_PER_GEM_PER_ROUND = 7
COPIES_PER_GEM = 25
EXTRA_COPIES_PER_GEM = COPIES_PER_GEM - (ROUND_COUNT * MIN_PER_GEM_PER_ROUND)
ROUND_PROFILES = (
    (1, 1, 2, 2),
    (2, 2, 2, 2),
    (2, 2, 3, 3),
    (2, 3, 3, 4),
    (2, 3, 4, 5),
    (1, 3, 5, 6),
    (1, 4, 5, 7),
    (1, 3, 7, 7),
)
ROUND_MAX_COUNTS = tuple(max(profile) for profile in ROUND_PROFILES)
ROUND_PROFILE_PERMUTATIONS = tuple(
    tuple(dict.fromkeys(permutations(profile))) for profile in ROUND_PROFILES
)
AUTO_ADVANCE_DELAY_SECONDS = 3.0
PLAYERS = ("p1", "p2")
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class GameError(Exception):
    """Raised for user-correctable game errors."""


def empty_counts() -> dict[str, int]:
    return {gem_id: 0 for gem_id in GEMS}


def clone_counts(counts: dict[str, int]) -> dict[str, int]:
    return {gem_id: int(counts.get(gem_id, 0)) for gem_id in GEMS}


def normalize_room_code(room_code: str) -> str:
    return "".join(ch for ch in room_code.upper() if ch.isalnum())


def generate_rounds(seed: str | None = None) -> list[dict[str, int]]:
    rng = random.Random(seed or f"{time.time_ns()}-{secrets.token_hex(4)}")
    remaining = {gem_id: COPIES_PER_GEM for gem_id in GEMS}
    rounds: list[dict[str, int]] = []
    if not _assign_round_profiles(rng, 0, remaining, rounds):
        raise RuntimeError("Round generator could not satisfy the configured profiles.")

    return rounds


def _assign_round_profiles(
    rng: random.Random,
    round_index: int,
    remaining: dict[str, int],
    rounds: list[dict[str, int]],
) -> bool:
    if round_index == ROUND_COUNT:
        return all(remaining[gem_id] == 0 for gem_id in GEMS)

    candidates = list(ROUND_PROFILE_PERMUTATIONS[round_index])
    rng.shuffle(candidates)
    candidates.sort(
        key=lambda candidate: sum(candidate[index] * remaining[gem_id] for index, gem_id in enumerate(GEMS)),
        reverse=True,
    )

    for candidate in candidates:
        next_remaining = {
            gem_id: remaining[gem_id] - candidate[index]
            for index, gem_id in enumerate(GEMS)
        }
        if any(count < 0 for count in next_remaining.values()):
            continue
        if not _can_complete_profile(round_index + 1, next_remaining):
            continue

        rounds.append({gem_id: candidate[index] for index, gem_id in enumerate(GEMS)})
        if _assign_round_profiles(rng, round_index + 1, next_remaining, rounds):
            return True
        rounds.pop()

    return False


def _can_complete_profile(round_index: int, remaining: dict[str, int]) -> bool:
    future_profiles = ROUND_PROFILES[round_index:]
    future_total = sum(sum(profile) for profile in future_profiles)
    if sum(remaining.values()) != future_total:
        return False

    min_future = sum(min(profile) for profile in future_profiles)
    max_future = sum(max(profile) for profile in future_profiles)
    return all(min_future <= remaining[gem_id] <= max_future for gem_id in GEMS)

def validate_schedule(rounds: list[dict[str, int]]) -> tuple[bool, dict[str, int], list[str]]:
    totals = empty_counts()
    errors: list[str] = []

    if len(rounds) != ROUND_COUNT:
        errors.append(f"Schedule must contain {ROUND_COUNT} rounds.")

    for index, round_offer in enumerate(rounds, start=1):
        for gem_id in GEMS:
            count = round_offer.get(gem_id)
            if not isinstance(count, int) or count < MIN_PER_GEM_PER_ROUND:
                errors.append(f"Round {index} must include at least one {gem_id.upper()} gem.")
                count = 0
            elif count > MAX_PER_GEM_PER_ROUND:
                errors.append(f"Round {index} has more than {MAX_PER_GEM_PER_ROUND} {gem_id.upper()} gems.")
            totals[gem_id] += count

    first_total = totals[GEMS[0]]
    if any(totals[gem_id] != first_total for gem_id in GEMS):
        errors.append("Gem appearance totals must be equal.")
    if any(totals[gem_id] != COPIES_PER_GEM for gem_id in GEMS):
        errors.append(f"Each gem must appear exactly {COPIES_PER_GEM} times.")

    return len(errors) == 0, totals, errors


def get_winner(targets: dict[str, str], collected: dict[str, dict[str, int]]) -> str:
    p1_score = collected["p1"][targets["p1"]]
    p2_score = collected["p2"][targets["p2"]]
    if p1_score > p2_score:
        return "p1"
    if p2_score > p1_score:
        return "p2"
    return "draw"


@dataclass
class Seat:
    token: str
    client: "WebSocketConnection | None" = None


@dataclass
class GameRoom:
    code: str
    seed: str
    targets: dict[str, str]
    rounds: list[dict[str, int]]
    phase: str = "waiting"
    round_index: int = 0
    selections: dict[str, str | None] = field(default_factory=lambda: {"p1": None, "p2": None})
    next_ready: dict[str, bool] = field(default_factory=lambda: {"p1": False, "p2": False})
    restart_ready: dict[str, bool] = field(default_factory=lambda: {"p1": False, "p2": False})
    collected: dict[str, dict[str, int]] = field(
        default_factory=lambda: {"p1": empty_counts(), "p2": empty_counts()}
    )
    seats: dict[str, Seat | None] = field(default_factory=lambda: {"p1": None, "p2": None})
    log: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, code: str) -> "GameRoom":
        seed = f"{code}-{time.time_ns()}-{secrets.token_hex(4)}"
        rng = random.Random(seed)
        rounds = generate_rounds(seed)
        valid, _, errors = validate_schedule(rounds)
        if not valid:
            raise RuntimeError("Generated invalid schedule: " + " ".join(errors))
        return cls(
            code=code,
            seed=seed,
            targets={
                "p1": rng.choice(TARGET_POOLS["p1"]),
                "p2": rng.choice(TARGET_POOLS["p2"]),
            },
            rounds=rounds,
        )

    def connect(self, player: str, client: "WebSocketConnection", token: str | None = None) -> str:
        if player not in PLAYERS:
            raise GameError("Unknown player seat.")

        seat = self.seats[player]
        if seat is None:
            seat = Seat(token=token or secrets.token_urlsafe(18), client=client)
            self.seats[player] = seat
        elif token and secrets.compare_digest(seat.token, token):
            if seat.client and seat.client is not client:
                seat.client.detach()
            seat.client = client
        else:
            raise GameError("That player seat is already taken.")

        client.room_code = self.code
        client.player = player
        client.token = seat.token
        if self.phase == "waiting" and all(self.seats[player_id] for player_id in PLAYERS):
            self.phase = "choosing"
        return seat.token

    def disconnect(self, client: "WebSocketConnection") -> None:
        for seat in self.seats.values():
            if seat and seat.client is client:
                seat.client = None

    def connected(self) -> dict[str, bool]:
        return {player: bool(self.seats[player] and self.seats[player].client) for player in PLAYERS}

    def current_round(self) -> dict[str, int]:
        return clone_counts(self.rounds[self.round_index])

    def available_gems(self) -> list[str]:
        offer = self.current_round()
        return [gem_id for gem_id in GEMS if offer[gem_id] > 0]

    def choose(self, player: str, gem_id: str) -> None:
        if self.phase != "choosing":
            raise GameError("Choices are not open right now.")
        if not all(self.connected().values()):
            raise GameError("Both players must be connected before choices open.")
        if player not in PLAYERS:
            raise GameError("Unknown player.")
        if gem_id not in GEMS:
            raise GameError("Unknown gem.")
        if gem_id not in self.available_gems():
            raise GameError("That gem is not available this round.")
        if self.selections[player]:
            raise GameError("Your choice is already locked.")

        self.selections[player] = gem_id
        if all(self.selections[player_id] for player_id in PLAYERS):
            self.resolve_round()

    def resolve_round(self) -> None:
        p1_choice = self.selections["p1"]
        p2_choice = self.selections["p2"]
        if not p1_choice or not p2_choice:
            raise RuntimeError("Cannot resolve until both players choose.")

        offered = self.current_round()
        collision = p1_choice == p2_choice
        gains = {"p1": None, "p2": None}
        gain_counts = {"p1": 0, "p2": 0}
        if not collision:
            gain_counts = {"p1": offered[p1_choice], "p2": offered[p2_choice]}
            self.collected["p1"][p1_choice] += gain_counts["p1"]
            self.collected["p2"][p2_choice] += gain_counts["p2"]
            gains = {"p1": p1_choice, "p2": p2_choice}

        result = {
            "roundNumber": self.round_index + 1,
            "offered": offered,
            "choices": {"p1": p1_choice, "p2": p2_choice},
            "collision": collision,
            "gains": gains,
            "gainCounts": gain_counts,
            "targetScores": {
                "p1": self.collected["p1"][self.targets["p1"]],
                "p2": self.collected["p2"][self.targets["p2"]],
            },
        }
        self.log.append(result)
        self.phase = "complete" if self.round_index == ROUND_COUNT - 1 else "resolved"
        self.next_ready = {"p1": False, "p2": False}

    def request_next_round(self, player: str) -> None:
        if player not in PLAYERS:
            raise GameError("Unknown player.")
        raise GameError("Next rounds advance automatically.")

    def next_round(self) -> None:
        self.round_index += 1
        self.selections = {"p1": None, "p2": None}
        self.next_ready = {"p1": False, "p2": False}
        self.phase = "choosing"

    def request_restart(self, player: str) -> bool:
        if player not in PLAYERS:
            raise GameError("Unknown player.")
        if not all(self.seats[player_id] for player_id in PLAYERS):
            raise GameError("Both player seats must be taken before restarting.")
        if not all(self.connected().values()):
            raise GameError("Both players must be connected to restart.")
        self.restart_ready[player] = True
        if all(self.restart_ready.values()):
            self.restart_match()
            return True
        return False

    def restart_match(self) -> None:
        self.seed = f"{self.code}-{time.time_ns()}-{secrets.token_hex(4)}"
        rng = random.Random(self.seed)
        self.targets = {
            "p1": rng.choice(TARGET_POOLS["p1"]),
            "p2": rng.choice(TARGET_POOLS["p2"]),
        }
        self.rounds = generate_rounds(self.seed)
        valid, _, errors = validate_schedule(self.rounds)
        if not valid:
            raise RuntimeError("Generated invalid schedule: " + " ".join(errors))
        self.phase = "choosing" if all(self.connected().values()) else "waiting"
        self.round_index = 0
        self.selections = {"p1": None, "p2": None}
        self.next_ready = {"p1": False, "p2": False}
        self.restart_ready = {"p1": False, "p2": False}
        self.collected = {"p1": empty_counts(), "p2": empty_counts()}
        self.log = []

    def fairness_totals(self) -> dict[str, int]:
        totals = empty_counts()
        for round_offer in self.rounds:
            for gem_id in GEMS:
                totals[gem_id] += round_offer[gem_id]
        return totals

    def snapshot_for(self, player: str) -> dict[str, Any]:
        if player not in PLAYERS:
            raise GameError("Unknown player.")
        opponent = "p2" if player == "p1" else "p1"
        winner = get_winner(self.targets, self.collected) if self.phase == "complete" else None
        target_scores = None
        if self.phase == "complete":
            target_scores = {
                "p1": self.collected["p1"][self.targets["p1"]],
                "p2": self.collected["p2"][self.targets["p2"]],
            }

        return {
            "roomCode": self.code,
            "player": player,
            "phase": self.phase,
            "roundIndex": self.round_index,
            "roundNumber": self.round_index + 1,
            "totalRounds": ROUND_COUNT,
            "offer": self.current_round(),
            "availableGems": self.available_gems(),
            "fairnessTotals": self.fairness_totals(),
            "gemDefinitions": list(GEM_DEFINITIONS),
            "ownTarget": self.targets[player],
            "ownTargetPool": list(TARGET_POOLS[player]),
            "opponentTargetPool": list(TARGET_POOLS[opponent]),
            "opponentTarget": self.targets[opponent] if self.phase == "complete" else None,
            "collected": {
                "p1": clone_counts(self.collected["p1"]),
                "p2": clone_counts(self.collected["p2"]),
            },
            "ready": {player_id: bool(self.selections[player_id]) for player_id in PLAYERS},
            "nextReady": dict(self.next_ready),
            "restartReady": dict(self.restart_ready),
            "ownSelection": self.selections[player],
            "lastResult": self.log[-1] if self.log else None,
            "log": list(self.log),
            "connected": self.connected(),
            "winner": winner,
            "targetScores": target_scores,
        }


class GameHub:
    def __init__(self, auto_advance_delay: float = AUTO_ADVANCE_DELAY_SECONDS) -> None:
        self.rooms: dict[str, GameRoom] = {}
        self.auto_advance_delay = auto_advance_delay
        self.auto_advance_timers: dict[str, threading.Timer] = {}
        self.auto_advance_tokens: dict[str, tuple[int, str]] = {}
        self.lock = threading.RLock()

    def make_code(self) -> str:
        while True:
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(5))
            if code not in self.rooms:
                return code

    def create_room(self, client: "WebSocketConnection") -> None:
        with self.lock:
            room = GameRoom.create(self.make_code())
            self.rooms[room.code] = room
            token = room.connect("p1", client)
            client.send_json({"type": "joined", "roomCode": room.code, "player": "p1", "token": token})
            self.broadcast(room.code)

    def join_room(self, client: "WebSocketConnection", room_code: str, player: str | None = None, token: str | None = None) -> None:
        room_code = normalize_room_code(room_code)
        if not room_code:
            raise GameError("Enter a room code.")
        with self.lock:
            room = self.rooms.get(room_code)
            if room is None:
                raise GameError("Room not found.")

            chosen_player = player
            if chosen_player is None:
                if room.seats["p2"] is None:
                    chosen_player = "p2"
                elif token and room.seats["p1"] and secrets.compare_digest(room.seats["p1"].token, token):
                    chosen_player = "p1"
                elif token and room.seats["p2"] and secrets.compare_digest(room.seats["p2"].token, token):
                    chosen_player = "p2"
                else:
                    raise GameError("Room is full.")

            joined_token = room.connect(chosen_player, client, token=token)
            client.send_json(
                {"type": "joined", "roomCode": room.code, "player": chosen_player, "token": joined_token}
            )
            self.broadcast(room.code)

    def choose(self, client: "WebSocketConnection", gem_id: str) -> None:
        with self.lock:
            room = self.require_room(client)
            room.choose(client.player, gem_id)
            should_auto_advance = room.phase == "resolved"
            resolved_round_index = room.round_index
            self.broadcast(room.code)
            if should_auto_advance:
                self.schedule_auto_advance(room.code, resolved_round_index)

    def next_round(self, client: "WebSocketConnection") -> None:
        with self.lock:
            room = self.require_room(client)
            room.request_next_round(client.player)
            self.broadcast(room.code)

    def restart(self, client: "WebSocketConnection") -> None:
        with self.lock:
            room = self.require_room(client)
            restarted = room.request_restart(client.player)
            if restarted:
                self.cancel_auto_advance(room.code)
            self.broadcast(room.code)

    def disconnect(self, client: "WebSocketConnection") -> None:
        with self.lock:
            if not client.room_code:
                return
            room = self.rooms.get(client.room_code)
            if not room:
                return
            room.disconnect(client)
            self.broadcast(room.code)

    def require_room(self, client: "WebSocketConnection") -> GameRoom:
        if not client.room_code or not client.player:
            raise GameError("Join a room first.")
        room = self.rooms.get(client.room_code)
        if room is None:
            raise GameError("Room not found.")
        return room

    def broadcast(self, room_code: str) -> None:
        room = self.rooms[room_code]
        messages: list[tuple[WebSocketConnection, dict[str, Any]]] = []
        for player in PLAYERS:
            seat = room.seats[player]
            if seat and seat.client:
                messages.append((seat.client, {"type": "state", "state": room.snapshot_for(player)}))

        for client, message in messages:
            client.send_json(message)

    def schedule_auto_advance(self, room_code: str, round_index: int) -> None:
        self.cancel_auto_advance(room_code)
        token = secrets.token_hex(8)
        self.auto_advance_tokens[room_code] = (round_index, token)
        timer = threading.Timer(
            self.auto_advance_delay,
            self.auto_advance_round,
            args=(room_code, round_index, token),
        )
        timer.daemon = True
        self.auto_advance_timers[room_code] = timer
        timer.start()

    def cancel_auto_advance(self, room_code: str) -> None:
        timer = self.auto_advance_timers.pop(room_code, None)
        self.auto_advance_tokens.pop(room_code, None)
        if timer:
            timer.cancel()

    def auto_advance_round(self, room_code: str, round_index: int, token: str) -> None:
        with self.lock:
            room = self.rooms.get(room_code)
            if self.auto_advance_tokens.get(room_code) != (round_index, token):
                return
            self.auto_advance_timers.pop(room_code, None)
            self.auto_advance_tokens.pop(room_code, None)
            if not room or room.phase != "resolved" or room.round_index != round_index:
                return
            room.next_round()
            self.broadcast(room_code)


class WebSocketConnection:
    def __init__(self, handler: SimpleHTTPRequestHandler, hub: GameHub) -> None:
        self.handler = handler
        self.hub = hub
        self.socket = handler.connection
        self.send_lock = threading.Lock()
        self.room_code: str | None = None
        self.player: str | None = None
        self.token: str | None = None
        self.alive = True

    def run(self) -> None:
        try:
            while self.alive:
                frame = self.read_frame()
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x1:
                    self.handle_text(payload.decode("utf-8"))
                elif opcode == 0x8:
                    break
                elif opcode == 0x9:
                    self.send_frame(payload, opcode=0xA)
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            self.alive = False
            self.hub.disconnect(self)

    def detach(self) -> None:
        self.alive = False
        try:
            self.send_json({"type": "error", "message": "This seat was opened in another tab."})
            self.send_frame(b"", opcode=0x8)
        except OSError:
            pass

    def handle_text(self, text: str) -> None:
        try:
            message = json.loads(text)
            message_type = message.get("type")
            if message_type == "create":
                self.hub.create_room(self)
            elif message_type == "join":
                self.hub.join_room(
                    self,
                    str(message.get("roomCode", "")),
                    player=message.get("player"),
                    token=message.get("token"),
                )
            elif message_type == "choose":
                self.hub.choose(self, str(message.get("gem", "")))
            elif message_type == "nextRound":
                self.hub.next_round(self)
            elif message_type == "restart":
                self.hub.restart(self)
            else:
                raise GameError("Unknown message type.")
        except GameError as error:
            self.send_json({"type": "error", "message": str(error)})
        except Exception as error:
            self.send_json({"type": "error", "message": "Server error: " + str(error)})

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def read_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.socket.recv(length - len(chunks))
            if not chunk:
                raise ConnectionError("Socket closed.")
            chunks.extend(chunk)
        return bytes(chunks)

    def read_frame(self) -> tuple[int, bytes] | None:
        header = self.read_exact(2)
        first_byte, second_byte = header
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        length = second_byte & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read_exact(8))[0]

        mask = self.read_exact(4) if masked else b""
        payload = self.read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.alive and opcode != 0x8:
            return
        first_byte = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first_byte, length)
        elif length < 65536:
            header = struct.pack("!BBH", first_byte, 126, length)
        else:
            header = struct.pack("!BBQ", first_byte, 127, length)

        with self.send_lock:
            self.socket.sendall(header + payload)


def make_handler(hub: GameHub, directory: Path):
    class GemRequestHandler(SimpleHTTPRequestHandler):
        server_version = "GemDuel/1.0"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == "/ws":
                self.handle_websocket()
                return
            super().do_GET()

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def guess_type(self, path: str) -> str:
            if path.endswith(".js"):
                return "text/javascript"
            return mimetypes.guess_type(path)[0] or "application/octet-stream"

        def handle_websocket(self) -> None:
            if self.headers.get("Upgrade", "").lower() != "websocket":
                self.send_error(HTTPStatus.BAD_REQUEST, "Expected WebSocket upgrade.")
                return
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(HTTPStatus.BAD_REQUEST, "Missing WebSocket key.")
                return

            accept = base64.b64encode(hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
            self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            WebSocketConnection(self, hub).run()

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{self.address_string()} - {format % args}")

    return GemRequestHandler


def run_server(host: str, port: int, directory: Path) -> None:
    hub = GameHub()
    handler = make_handler(hub, directory)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Gem Duel server running at http://{host}:{port}/")
    print("Open the page in two browsers or share the room link on your local network.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Gem Duel multiplayer server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Use 0.0.0.0 for LAN play.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port to listen on. Defaults to PORT from the environment, then 8000.",
    )
    parser.add_argument("--directory", default=os.getcwd(), help="Project directory to serve.")
    args = parser.parse_args()
    run_server(args.host, args.port, Path(args.directory).resolve())


if __name__ == "__main__":
    main()
