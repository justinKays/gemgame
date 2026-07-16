from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
import hashlib
from itertools import combinations_with_replacement, permutations
import json
import logging
import mimetypes
import os
import posixpath
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
from urllib.parse import unquote, urlsplit


ROUND_COUNT = 8
GEMS = ("a", "b", "c", "d")
TARGET_POOLS = {
    "p1": ("a", "b"),
    "p2": ("c", "d"),
}
MIN_PER_GEM_PER_ROUND = 1
MAX_PER_GEM_PER_ROUND = 7
COPIES_PER_GEM = 25
ROUND_TOTALS = (6, 8, 10, 12, 14, 15, 17, 18)
ROUND_SPREAD_RANGES = (
    (1, 2),
    (0, 2),
    (1, 3),
    (1, 4),
    (2, 5),
    (3, 6),
    (4, 6),
    (4, 6),
)
ROUND_PROFILE_OPTIONS = tuple(
    tuple(
        profile
        for profile in combinations_with_replacement(
            range(MIN_PER_GEM_PER_ROUND, MAX_PER_GEM_PER_ROUND + 1),
            len(GEMS),
        )
        if sum(profile) == round_total and min_spread <= profile[-1] - profile[0] <= max_spread
    )
    for round_total, (min_spread, max_spread) in zip(ROUND_TOTALS, ROUND_SPREAD_RANGES)
)
ROUND_PROFILE_PERMUTATIONS = tuple(
    tuple(
        dict.fromkeys(
            permutation
            for profile in profile_options
            for permutation in permutations(profile)
        )
    )
    for profile_options in ROUND_PROFILE_OPTIONS
)
AUTO_ADVANCE_DELAY_SECONDS = 6.0
EMPTY_ROOM_TTL_SECONDS = 60 * 60
MAX_WEBSOCKET_PAYLOAD_BYTES = 16 * 1024
WEBSOCKET_CLOSE_PROTOCOL_ERROR = 1002
WEBSOCKET_CLOSE_INVALID_PAYLOAD = 1007
WEBSOCKET_DETACH_GRACE_SECONDS = 0.25
PLAYERS = ("p1", "p2")
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PUBLIC_STATIC_PATHS = frozenset(
    (
        "/",
        "/index.html",
        "/styles.css",
        "/src/app.js",
        *(f"/assets/gem-{gem_id}.svg" for gem_id in GEMS),
    )
)


class GameError(Exception):
    """Raised for user-correctable game errors."""


class WebSocketProtocolError(ConnectionError):
    """Raised when a client sends a frame this small server does not support."""


def empty_counts() -> dict[str, int]:
    return {gem_id: 0 for gem_id in GEMS}


def clone_counts(counts: dict[str, int]) -> dict[str, int]:
    return {gem_id: int(counts.get(gem_id, 0)) for gem_id in GEMS}


def normalize_room_code(room_code: str) -> str:
    return "".join(ch for ch in room_code.upper() if ch.isalnum())


def normalize_request_path(path: str) -> str:
    request_path = unquote(urlsplit(path).path)
    if request_path == "/":
        return request_path
    normalized = posixpath.normpath(request_path)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def is_valid_websocket_key(key: str) -> bool:
    try:
        return len(base64.b64decode(key, validate=True)) == 16
    except (binascii.Error, ValueError):
        return False


def is_allowed_websocket_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    parsed_origin = urlsplit(origin)
    return (
        parsed_origin.scheme in ("http", "https")
        and parsed_origin.netloc.casefold() == host.casefold()
        and parsed_origin.path in ("", "/")
        and not parsed_origin.query
        and not parsed_origin.fragment
    )


def generate_rounds(seed: str | None = None) -> list[dict[str, int]]:
    rng = random.Random(seed or f"{time.time_ns()}-{secrets.token_hex(4)}")
    remaining = {gem_id: COPIES_PER_GEM for gem_id in GEMS}
    rounds: list[dict[str, int]] = []
    if not _assign_round_profiles(rng, 0, remaining, rounds):
        raise RuntimeError("Round generator could not satisfy the configured constraints.")

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
    future_profile_options = ROUND_PROFILE_OPTIONS[round_index:]
    future_total = sum(ROUND_TOTALS[round_index:])
    if sum(remaining.values()) != future_total:
        return False

    min_future = sum(min(profile[0] for profile in options) for options in future_profile_options)
    max_future = sum(max(profile[-1] for profile in options) for options in future_profile_options)
    return all(min_future <= remaining[gem_id] <= max_future for gem_id in GEMS)


def validate_schedule(rounds: list[dict[str, int]]) -> tuple[bool, dict[str, int], list[str]]:
    totals = empty_counts()
    errors: list[str] = []

    if len(rounds) != ROUND_COUNT:
        errors.append(f"Schedule must contain {ROUND_COUNT} rounds.")

    for index, round_offer in enumerate(rounds, start=1):
        round_counts: list[int] = []
        for gem_id in GEMS:
            count = round_offer.get(gem_id)
            if not isinstance(count, int) or count < MIN_PER_GEM_PER_ROUND:
                errors.append(f"Round {index} must include at least one {gem_id.upper()} gem.")
                count = 0
            elif count > MAX_PER_GEM_PER_ROUND:
                errors.append(f"Round {index} has more than {MAX_PER_GEM_PER_ROUND} {gem_id.upper()} gems.")
            totals[gem_id] += count
            round_counts.append(count)

        if index <= ROUND_COUNT and round_profile(round_counts) not in ROUND_PROFILE_OPTIONS[index - 1]:
            round_total = ROUND_TOTALS[index - 1]
            min_spread, max_spread = ROUND_SPREAD_RANGES[index - 1]
            errors.append(
                f"Round {index} must total {round_total} gems with a spread between "
                f"{min_spread} and {max_spread}."
            )

    first_total = totals[GEMS[0]]
    if any(totals[gem_id] != first_total for gem_id in GEMS):
        errors.append("Gem appearance totals must be equal.")
    if any(totals[gem_id] != COPIES_PER_GEM for gem_id in GEMS):
        errors.append(f"Each gem must appear exactly {COPIES_PER_GEM} times.")

    return len(errors) == 0, totals, errors


def round_profile(counts: list[int]) -> tuple[int, ...]:
    return tuple(sorted(counts))


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
class ConnectResult:
    token: str
    replaced_clients: list["WebSocketConnection"] = field(default_factory=list)
    match_restarted: bool = False


@dataclass
class GameRoom:
    code: str
    seed: str
    targets: dict[str, str]
    rounds: list[dict[str, int]]
    phase: str = "waiting"
    round_index: int = 0
    selections: dict[str, str | None] = field(default_factory=lambda: {"p1": None, "p2": None})
    restart_ready: dict[str, bool] = field(default_factory=lambda: {"p1": False, "p2": False})
    collected: dict[str, dict[str, int]] = field(
        default_factory=lambda: {"p1": empty_counts(), "p2": empty_counts()}
    )
    seats: dict[str, Seat | None] = field(default_factory=lambda: {"p1": None, "p2": None})
    log: list[dict[str, Any]] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)

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

    def connect(
        self,
        player: str,
        client: "WebSocketConnection",
        token: str | None = None,
    ) -> ConnectResult:
        if player not in PLAYERS:
            raise GameError("Unknown player seat.")

        seat = self.seats[player]
        replaced_clients: list["WebSocketConnection"] = []
        match_restarted = False
        if seat is None:
            seat = Seat(token=token or secrets.token_urlsafe(18), client=client)
            self.seats[player] = seat
        elif token and secrets.compare_digest(seat.token, token):
            if seat.client and seat.client is not client:
                replaced_clients.append(seat.client)
            seat.client = client
        elif seat.client is None:
            seat = Seat(token=secrets.token_urlsafe(18), client=client)
            self.seats[player] = seat
            match_restarted = True
        else:
            raise GameError("That player seat is already taken.")

        client.room_code = self.code
        client.player = player
        client.token = seat.token
        if match_restarted:
            self.restart_match()
        elif self.phase == "waiting" and all(self.seats[player_id] for player_id in PLAYERS):
            self.phase = "choosing"
        self.touch()
        return ConnectResult(
            token=seat.token,
            replaced_clients=replaced_clients,
            match_restarted=match_restarted,
        )

    def disconnect(self, client: "WebSocketConnection") -> None:
        for seat in self.seats.values():
            if seat and seat.client is client:
                seat.client = None
                self.touch()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def connected(self) -> dict[str, bool]:
        return {player: bool(self.seats[player] and self.seats[player].client) for player in PLAYERS}

    def player_for_token(self, token: str | None) -> str | None:
        if not token:
            return None
        for player in PLAYERS:
            seat = self.seats[player]
            if seat and secrets.compare_digest(seat.token, token):
                return player
        return None

    def seat_is_available(self, player: str) -> bool:
        seat = self.seats[player]
        return seat is None or seat.client is None

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
        self.touch()
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
        self.touch()

    def next_round(self) -> None:
        self.round_index += 1
        self.selections = {"p1": None, "p2": None}
        self.phase = "choosing"
        self.touch()

    def request_restart(self, player: str) -> bool:
        if player not in PLAYERS:
            raise GameError("Unknown player.")
        if not all(self.seats[player_id] for player_id in PLAYERS):
            raise GameError("Both player seats must be taken before restarting.")
        if not all(self.connected().values()):
            raise GameError("Both players must be connected to restart.")
        self.restart_ready[player] = True
        self.touch()
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
        self.restart_ready = {"p1": False, "p2": False}
        self.collected = {"p1": empty_counts(), "p2": empty_counts()}
        self.log = []
        self.touch()

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
            "ownTarget": self.targets[player],
            "ownTargetPool": list(TARGET_POOLS[player]),
            "opponentTargetPool": list(TARGET_POOLS[opponent]),
            "opponentTarget": self.targets[opponent] if self.phase == "complete" else None,
            "collected": {
                "p1": clone_counts(self.collected["p1"]),
                "p2": clone_counts(self.collected["p2"]),
            },
            "ready": {player_id: bool(self.selections[player_id]) for player_id in PLAYERS},
            "restartReady": dict(self.restart_ready),
            "ownSelection": self.selections[player],
            "lastResult": self.log[-1] if self.log else None,
            "log": list(self.log),
            "connected": self.connected(),
            "winner": winner,
            "targetScores": target_scores,
        }


class GameHub:
    def __init__(
        self,
        auto_advance_delay: float = AUTO_ADVANCE_DELAY_SECONDS,
        empty_room_ttl: float = EMPTY_ROOM_TTL_SECONDS,
    ) -> None:
        self.rooms: dict[str, GameRoom] = {}
        self.auto_advance_delay = auto_advance_delay
        self.empty_room_ttl = empty_room_ttl
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
            self.ensure_client_unassigned(client)
            self.cleanup_empty_rooms_locked()
            room = GameRoom.create(self.make_code())
            self.rooms[room.code] = room
            result = room.connect("p1", client)
            messages = [
                (
                    client,
                    {"type": "joined", "roomCode": room.code, "player": "p1", "token": result.token},
                ),
                *self.snapshot_messages_locked(room.code),
            ]
            clients_to_drain = self.queue_messages_locked(messages)
        self.drain_clients(clients_to_drain)
        self.detach_clients(result.replaced_clients)

    def join_room(
        self,
        client: "WebSocketConnection",
        room_code: str,
        player: str | None = None,
        token: str | None = None,
    ) -> None:
        room_code = normalize_room_code(room_code)
        if not room_code:
            raise GameError("Enter a room code.")
        with self.lock:
            self.ensure_client_unassigned(client)
            self.cleanup_empty_rooms_locked()
            room = self.rooms.get(room_code)
            if room is None:
                raise GameError("Room not found.")

            matching_token_player = room.player_for_token(token)
            chosen_player = player or matching_token_player
            if chosen_player is None:
                chosen_player = next(
                    (player_id for player_id in PLAYERS if room.seat_is_available(player_id)),
                    None,
                )
                if chosen_player is None:
                    raise GameError("Room is full.")
            elif chosen_player not in PLAYERS:
                raise GameError("Unknown player seat.")
            elif matching_token_player != chosen_player and not room.seat_is_available(chosen_player):
                raise GameError("That player seat is already taken.")

            connect_token = token if matching_token_player == chosen_player else None
            result = room.connect(
                chosen_player,
                client,
                token=connect_token,
            )
            if result.match_restarted:
                self.cancel_auto_advance_locked(room.code)
            messages = [
                (
                    client,
                    {"type": "joined", "roomCode": room.code, "player": chosen_player, "token": result.token},
                ),
                *self.snapshot_messages_locked(room.code),
            ]
            clients_to_drain = self.queue_messages_locked(messages)
        self.drain_clients(clients_to_drain)
        self.detach_clients(result.replaced_clients)

    def choose(self, client: "WebSocketConnection", gem_id: str) -> None:
        with self.lock:
            room = self.require_room(client)
            room.choose(client.player, gem_id)
            should_auto_advance = room.phase == "resolved"
            resolved_round_index = room.round_index
            messages = self.snapshot_messages_locked(room.code)
            clients_to_drain = self.queue_messages_locked(messages)
            if should_auto_advance:
                self.schedule_auto_advance_locked(room.code, resolved_round_index)
        self.drain_clients(clients_to_drain)

    def restart(self, client: "WebSocketConnection") -> None:
        with self.lock:
            room = self.require_room(client)
            restarted = room.request_restart(client.player)
            if restarted:
                self.cancel_auto_advance_locked(room.code)
            messages = self.snapshot_messages_locked(room.code)
            clients_to_drain = self.queue_messages_locked(messages)
        self.drain_clients(clients_to_drain)

    def disconnect(self, client: "WebSocketConnection") -> None:
        with self.lock:
            room_code = client.room_code
            if not room_code:
                return
            room = self.rooms.get(room_code)
            if room:
                room.disconnect(client)
            client.room_code = None
            client.player = None
            client.token = None

            if room:
                self.cleanup_empty_rooms_locked()
                messages = self.snapshot_messages_locked(room.code) if room.code in self.rooms else []
                clients_to_drain = self.queue_messages_locked(messages)
            else:
                clients_to_drain = []
        self.drain_clients(clients_to_drain)

    def ensure_client_unassigned(self, client: "WebSocketConnection") -> None:
        if client.room_code or client.player:
            raise GameError("This connection is already in a room.")

    def require_room(self, client: "WebSocketConnection") -> GameRoom:
        if not client.room_code or not client.player:
            raise GameError("Join a room first.")
        room = self.rooms.get(client.room_code)
        if room is None:
            raise GameError("Room not found.")
        seat = room.seats.get(client.player)
        if not seat or seat.client is not client:
            raise GameError("This connection no longer controls that player seat.")
        return room

    def snapshot_messages_locked(
        self,
        room_code: str,
    ) -> list[tuple["WebSocketConnection", dict[str, Any]]]:
        room = self.rooms[room_code]
        messages: list[tuple[WebSocketConnection, dict[str, Any]]] = []
        for player in PLAYERS:
            seat = room.seats[player]
            if seat and seat.client:
                messages.append((seat.client, {"type": "state", "state": room.snapshot_for(player)}))
        return messages

    def queue_messages_locked(
        self,
        messages: list[tuple["WebSocketConnection", dict[str, Any]]],
    ) -> list["WebSocketConnection"]:
        # Queue under the hub lock so delivery order matches room mutation order.
        clients_to_drain = []
        for client, message in messages:
            if client.queue_json(message):
                clients_to_drain.append(client)
        return clients_to_drain

    def drain_clients(self, clients: list["WebSocketConnection"]) -> None:
        stale_clients = []
        for client in clients:
            try:
                client.drain_outbox()
            except OSError:
                client.alive = False
                stale_clients.append(client)

        for client in stale_clients:
            self.disconnect(client)

    def detach_clients(self, clients: list["WebSocketConnection"]) -> None:
        for client in clients:
            client.detach()

    def cleanup_empty_rooms(self) -> None:
        with self.lock:
            self.cleanup_empty_rooms_locked()

    def cleanup_empty_rooms_locked(self) -> None:
        now = time.monotonic()
        expired_codes = [
            code
            for code, room in self.rooms.items()
            if not any(room.connected().values()) and now - room.last_activity >= self.empty_room_ttl
        ]
        for code in expired_codes:
            self.cancel_auto_advance_locked(code)
            del self.rooms[code]

    def schedule_auto_advance_locked(self, room_code: str, round_index: int) -> None:
        self.cancel_auto_advance_locked(room_code)
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

    def cancel_auto_advance_locked(self, room_code: str) -> None:
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
            messages = self.snapshot_messages_locked(room_code)
            clients_to_drain = self.queue_messages_locked(messages)
        self.drain_clients(clients_to_drain)


class WebSocketConnection:
    def __init__(self, handler: SimpleHTTPRequestHandler, hub: GameHub) -> None:
        self.handler = handler
        self.hub = hub
        self.socket = handler.connection
        self.send_lock = threading.Lock()
        self.outbox_lock = threading.Lock()
        self.outbox: deque[dict[str, Any]] = deque()
        self.outbox_draining = False
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
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        self.send_close(WEBSOCKET_CLOSE_INVALID_PAYLOAD)
                        break
                    self.handle_text(text)
                elif opcode == 0x8:
                    self.send_frame(payload, opcode=0x8)
                    break
                elif opcode == 0x9:
                    self.send_frame(payload, opcode=0xA)
        except WebSocketProtocolError:
            self.send_close(WEBSOCKET_CLOSE_PROTOCOL_ERROR)
        except (ConnectionError, OSError, socket.timeout):
            pass
        finally:
            self.alive = False
            self.hub.disconnect(self)

    def detach(self) -> None:
        try:
            self.send_json({"type": "error", "message": "This seat was opened in another tab."})
        except OSError:
            pass
        self.send_close()
        self.alive = False
        close_timer = threading.Timer(WEBSOCKET_DETACH_GRACE_SECONDS, self.close_transport)
        close_timer.daemon = True
        close_timer.start()

    def close_transport(self) -> None:
        self.alive = False
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def handle_text(self, text: str) -> None:
        try:
            try:
                message = json.loads(text)
            except json.JSONDecodeError as error:
                raise GameError("Message must be valid JSON.") from error
            if not isinstance(message, dict):
                raise GameError("Message must be a JSON object.")

            message_type = message.get("type")
            if not isinstance(message_type, str):
                raise GameError("Message type must be text.")
            if message_type == "create":
                self.hub.create_room(self)
            elif message_type == "join":
                room_code = message.get("roomCode", "")
                player = message.get("player")
                token = message.get("token")
                if not isinstance(room_code, str):
                    raise GameError("Room code must be text.")
                if player is not None and not isinstance(player, str):
                    raise GameError("Player seat must be text.")
                if token is not None and not isinstance(token, str):
                    raise GameError("Reconnect token must be text.")
                self.hub.join_room(
                    self,
                    room_code,
                    player=player,
                    token=token,
                )
            elif message_type == "choose":
                gem_id = message.get("gem", "")
                if not isinstance(gem_id, str):
                    raise GameError("Gem must be text.")
                self.hub.choose(self, gem_id)
            elif message_type == "restart":
                self.hub.restart(self)
            else:
                raise GameError("Unknown message type.")
        except GameError as error:
            self.send_json({"type": "error", "message": str(error)})
        except Exception:
            logging.exception("Unhandled WebSocket message error")
            self.send_json({"type": "error", "message": "Internal server error."})

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def send_close(self, code: int | None = None) -> None:
        payload = struct.pack("!H", code) if code is not None else b""
        try:
            self.send_frame(payload, opcode=0x8)
        except OSError:
            pass

    def queue_json(self, payload: dict[str, Any]) -> bool:
        with self.outbox_lock:
            self.outbox.append(payload)
            if self.outbox_draining:
                return False
            self.outbox_draining = True
            return True

    def drain_outbox(self) -> None:
        while True:
            with self.outbox_lock:
                if not self.outbox:
                    self.outbox_draining = False
                    return
                payload = self.outbox[0]

            try:
                self.send_json(payload)
            except OSError:
                with self.outbox_lock:
                    self.outbox.clear()
                    self.outbox_draining = False
                raise

            with self.outbox_lock:
                self.outbox.popleft()

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
        fin = bool(first_byte & 0x80)
        rsv = first_byte & 0x70
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        length = second_byte & 0x7F

        if rsv:
            raise WebSocketProtocolError("WebSocket extensions are not supported.")
        if not fin:
            raise WebSocketProtocolError("Fragmented frames are not supported.")
        if opcode not in (0x1, 0x8, 0x9, 0xA):
            raise WebSocketProtocolError("Unsupported WebSocket opcode.")
        if not masked:
            raise WebSocketProtocolError("Client frames must be masked.")

        if length == 126:
            length = struct.unpack("!H", self.read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read_exact(8))[0]
        if length > MAX_WEBSOCKET_PAYLOAD_BYTES:
            raise WebSocketProtocolError("WebSocket frame is too large.")
        if opcode in (0x8, 0x9, 0xA) and length > 125:
            raise WebSocketProtocolError("Control frames must be 125 bytes or smaller.")

        mask = self.read_exact(4) if masked else b""
        payload = self.read_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        first_byte = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first_byte, length)
        elif length < 65536:
            header = struct.pack("!BBH", first_byte, 126, length)
        else:
            header = struct.pack("!BBQ", first_byte, 127, length)

        with self.send_lock:
            if not self.alive and opcode != 0x8:
                return
            self.socket.sendall(header + payload)


def make_handler(hub: GameHub, directory: Path):
    class GemRequestHandler(SimpleHTTPRequestHandler):
        server_version = "GemDuel/1.0"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(directory), **kwargs)

        def do_GET(self) -> None:
            request_path = normalize_request_path(self.path)
            if request_path == "/ws":
                self.handle_websocket()
                return
            if request_path not in PUBLIC_STATIC_PATHS:
                self.send_error(HTTPStatus.NOT_FOUND, "File not found.")
                return
            super().do_GET()

        def do_HEAD(self) -> None:
            request_path = normalize_request_path(self.path)
            if request_path == "/ws":
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "WebSocket endpoint requires GET.")
                return
            if request_path not in PUBLIC_STATIC_PATHS:
                self.send_error(HTTPStatus.NOT_FOUND, "File not found.")
                return
            super().do_HEAD()

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
            if not is_valid_websocket_key(key):
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid WebSocket key.")
                return
            if self.headers.get("Sec-WebSocket-Version") != "13":
                self.send_error(HTTPStatus.BAD_REQUEST, "Unsupported WebSocket version.")
                return
            if not is_allowed_websocket_origin(
                self.headers.get("Origin"),
                self.headers.get("Host"),
            ):
                self.send_error(HTTPStatus.FORBIDDEN, "WebSocket origin is not allowed.")
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
