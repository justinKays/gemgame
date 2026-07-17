import threading
import unittest

from server import (
    COPIES_PER_GEM,
    GEMS,
    MAX_PER_GEM_PER_ROUND,
    MIN_PER_GEM_PER_ROUND,
    ROUND_COUNT,
    ROUND_PROFILE_OPTIONS,
    ROUND_SPREAD_RANGES,
    ROUND_TOTALS,
    GameError,
    GameHub,
    GameRoom,
    empty_counts,
    generate_rounds,
    validate_schedule,
)


class DummyClient:
    def __init__(self):
        self.room_code = None
        self.player = None
        self.token = None
        self.detached = False
        self.alive = True

    def detach(self):
        self.detached = True


class RecordingClient(DummyClient):
    def __init__(self):
        super().__init__()
        self.messages = []
        self.outbox = []
        self.outbox_lock = threading.Lock()
        self.outbox_draining = False

    def send_json(self, payload):
        self.messages.append(payload)

    def queue_json(self, payload):
        with self.outbox_lock:
            self.outbox.append(payload)
            if self.outbox_draining:
                return False
            self.outbox_draining = True
            return True

    def drain_outbox(self):
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
                self.outbox.pop(0)


class FailingClient(RecordingClient):
    def send_json(self, payload):
        raise OSError("connection closed")


class PausingDrainHub(GameHub):
    def __init__(self):
        super().__init__(auto_advance_delay=60)
        self.first_batch_queued = threading.Event()
        self.release_first_batch = threading.Event()

    def drain_clients(self, clients):
        if threading.current_thread().name == "first-choice":
            self.first_batch_queued.set()
            self.release_first_batch.wait(timeout=2)
        super().drain_clients(clients)


def valid_rounds():
    return [
        {"a": 1, "b": 1, "c": 2, "d": 2},
        {"a": 2, "b": 2, "c": 2, "d": 2},
        {"a": 3, "b": 3, "c": 2, "d": 2},
        {"a": 4, "b": 3, "c": 3, "d": 2},
        {"a": 2, "b": 4, "c": 3, "d": 5},
        {"a": 5, "b": 6, "c": 3, "d": 1},
        {"a": 1, "b": 5, "c": 7, "d": 4},
        {"a": 7, "b": 1, "c": 3, "d": 7},
    ]


def connected_room(targets=None, rounds=None):
    room = GameRoom(
        code="TEST1",
        seed="test-seed",
        targets=targets or {"p1": "a", "p2": "c"},
        rounds=rounds if rounds is not None else valid_rounds(),
    )
    room.connect("p1", DummyClient())
    room.connect("p2", DummyClient())
    return room


class ScheduleTests(unittest.TestCase):
    def test_gameplay_fixture_is_a_valid_production_schedule(self):
        valid, totals, errors = validate_schedule(valid_rounds())

        self.assertTrue(valid, errors)
        self.assertEqual(totals, {gem_id: COPIES_PER_GEM for gem_id in GEMS})

    def test_generated_rounds_are_diverse_fair_and_include_each_gem(self):
        schedules = set()
        profiles_seen = [set() for _ in range(ROUND_COUNT)]

        for index in range(100):
            rounds = generate_rounds(f"schedule-{index}")
            valid, totals, errors = validate_schedule(rounds)
            round_totals = [sum(round_offer.values()) for round_offer in rounds]
            round_profiles = [
                tuple(sorted(round_offer[gem_id] for gem_id in GEMS))
                for round_offer in rounds
            ]
            schedules.add(tuple(tuple(round_offer[gem_id] for gem_id in GEMS) for round_offer in rounds))

            self.assertTrue(valid, errors)
            self.assertEqual(len(rounds), ROUND_COUNT)
            self.assertEqual(round_totals, list(ROUND_TOTALS))
            for gem_id in GEMS:
                self.assertEqual(totals[gem_id], COPIES_PER_GEM)
            for round_index, round_offer in enumerate(rounds):
                profiles_seen[round_index].add(round_profiles[round_index])
                self.assertIn(round_profiles[round_index], ROUND_PROFILE_OPTIONS[round_index])
                spread = round_profiles[round_index][-1] - round_profiles[round_index][0]
                min_spread, max_spread = ROUND_SPREAD_RANGES[round_index]
                self.assertGreaterEqual(spread, min_spread)
                self.assertLessEqual(spread, max_spread)
                for gem_id in GEMS:
                    self.assertGreaterEqual(round_offer[gem_id], MIN_PER_GEM_PER_ROUND)
                    self.assertLessEqual(round_offer[gem_id], MAX_PER_GEM_PER_ROUND)

        self.assertGreaterEqual(len(schedules), 90)
        for round_index, seen in enumerate(profiles_seen):
            expected_variety = max(2, len(ROUND_PROFILE_OPTIONS[round_index]) // 2)
            self.assertGreaterEqual(len(seen), expected_variety)

    def test_validation_rejects_rounds_that_do_not_match_constraints(self):
        rounds = generate_rounds("valid-base")
        rounds[1] = {"a": 2, "b": 1, "c": 1, "d": 1}

        valid, _, errors = validate_schedule(rounds)

        self.assertFalse(valid)
        self.assertTrue(any("must total 8 gems" in error for error in errors), errors)


class MultiplayerPrivacyTests(unittest.TestCase):
    def test_snapshots_reveal_only_own_target_before_complete(self):
        room = connected_room(targets={"p1": "a", "p2": "d"})

        p1_snapshot = room.snapshot_for("p1")
        p2_snapshot = room.snapshot_for("p2")

        self.assertEqual(p1_snapshot["ownTarget"], "a")
        self.assertEqual(p2_snapshot["ownTarget"], "d")
        self.assertIsNone(p1_snapshot["opponentTarget"])
        self.assertIsNone(p2_snapshot["opponentTarget"])
        self.assertNotIn("targets", p1_snapshot)
        self.assertNotIn("targets", p2_snapshot)

    def test_single_locked_choice_is_not_revealed_to_opponent(self):
        room = connected_room()

        room.choose("p1", "a")
        p2_snapshot = room.snapshot_for("p2")

        self.assertEqual(room.phase, "choosing")
        self.assertTrue(p2_snapshot["ready"]["p1"])
        self.assertFalse(p2_snapshot["ready"]["p2"])
        self.assertIsNone(p2_snapshot["ownSelection"])
        self.assertIsNone(p2_snapshot["lastResult"])

    def test_choices_are_revealed_after_both_players_choose(self):
        room = connected_room()

        room.choose("p1", "a")
        room.choose("p2", "a")
        p1_snapshot = room.snapshot_for("p1")

        self.assertEqual(room.phase, "resolved")
        self.assertTrue(p1_snapshot["lastResult"]["collision"])
        self.assertEqual(p1_snapshot["lastResult"]["choices"], {"p1": "a", "p2": "a"})
        self.assertEqual(p1_snapshot["lastResult"]["gainCounts"], {"p1": 0, "p2": 0})
        self.assertEqual(p1_snapshot["collected"]["p1"], empty_counts())
        self.assertEqual(p1_snapshot["collected"]["p2"], empty_counts())

    def test_disconnected_second_player_seat_can_be_reclaimed(self):
        hub = GameHub()
        room = GameRoom(
            code="STALE",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        stale_p2_client = RecordingClient()
        new_p2_client = RecordingClient()

        room.connect("p1", p1_client)
        old_token = room.connect("p2", stale_p2_client).token
        original_seed = room.seed
        room.disconnect(stale_p2_client)
        p1_snapshot = room.snapshot_for("p1")

        self.assertEqual(p1_snapshot["occupied"], {"p1": True, "p2": True})
        self.assertEqual(p1_snapshot["connected"], {"p1": True, "p2": False})

        hub.join_room(new_p2_client, room.code)

        self.assertEqual(new_p2_client.messages[0]["type"], "joined")
        self.assertEqual(new_p2_client.messages[0]["player"], "p2")
        self.assertNotEqual(new_p2_client.messages[0]["token"], old_token)
        self.assertIs(room.seats["p2"].client, new_p2_client)
        self.assertTrue(room.connected()["p2"])
        self.assertNotEqual(room.seed, original_seed)
        self.assertEqual(room.round_index, 0)
        self.assertEqual(room.log, [])

    def test_disconnected_first_player_seat_can_be_filled_without_token(self):
        hub = GameHub(auto_advance_delay=60)
        room = GameRoom(
            code="OPEN1",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        old_p1_client = RecordingClient()
        p2_client = RecordingClient()
        new_p1_client = RecordingClient()
        old_token = room.connect("p1", old_p1_client).token
        room.connect("p2", p2_client)
        room.choose("p1", "a")
        room.choose("p2", "c")
        original_seed = room.seed
        room.disconnect(old_p1_client)

        hub.join_room(new_p1_client, room.code)

        self.assertEqual(new_p1_client.messages[0]["player"], "p1")
        self.assertNotEqual(new_p1_client.messages[0]["token"], old_token)
        self.assertIs(room.seats["p1"].client, new_p1_client)
        self.assertNotEqual(room.seed, original_seed)
        self.assertEqual(room.phase, "choosing")
        self.assertEqual(room.round_index, 0)
        self.assertEqual(room.log, [])
        self.assertEqual(room.collected, {"p1": empty_counts(), "p2": empty_counts()})

    def test_token_reconnect_preserves_the_current_match(self):
        hub = GameHub(auto_advance_delay=60)
        room = GameRoom(
            code="RESUME",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        old_p2_client = RecordingClient()
        new_p2_client = RecordingClient()
        room.connect("p1", p1_client)
        p2_token = room.connect("p2", old_p2_client).token
        room.choose("p1", "a")
        original_seed = room.seed
        room.disconnect(old_p2_client)

        hub.join_room(new_p2_client, room.code, player="p2", token=p2_token)

        self.assertEqual(room.seed, original_seed)
        self.assertEqual(room.selections["p1"], "a")
        self.assertIs(room.seats["p2"].client, new_p2_client)

    def test_active_second_player_seat_cannot_be_reclaimed_without_token(self):
        hub = GameHub()
        room = GameRoom(
            code="GHOST",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        ghost_p2_client = RecordingClient()
        new_p2_client = RecordingClient()

        room.connect("p1", p1_client)
        room.connect("p2", ghost_p2_client)

        with self.assertRaisesRegex(GameError, "Room is full"):
            hub.join_room(new_p2_client, room.code)

        self.assertFalse(ghost_p2_client.detached)
        self.assertEqual(new_p2_client.messages, [])
        self.assertIs(room.seats["p2"].client, ghost_p2_client)
        self.assertTrue(room.connected()["p2"])

    def test_explicit_second_player_takeover_requires_stale_seat(self):
        hub = GameHub()
        room = GameRoom(
            code="BUSY2",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        room.connect("p1", RecordingClient())
        room.connect("p2", RecordingClient())

        with self.assertRaisesRegex(GameError, "already taken"):
            hub.join_room(RecordingClient(), room.code, player="p2")

    def test_replaced_client_cannot_act_for_its_previous_seat(self):
        hub = GameHub(auto_advance_delay=60)
        room = GameRoom(
            code="REPLACE",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        old_p2_client = RecordingClient()
        new_p2_client = RecordingClient()
        room.connect("p1", p1_client)
        p2_token = room.connect("p2", old_p2_client).token
        original_seed = room.seed

        hub.join_room(new_p2_client, room.code, player="p2", token=p2_token)

        with self.assertRaisesRegex(GameError, "no longer controls"):
            hub.choose(old_p2_client, "a")
        self.assertIsNone(room.selections["p2"])
        self.assertTrue(old_p2_client.detached)
        self.assertEqual(room.seed, original_seed)

    def test_empty_rooms_are_removed_after_ttl(self):
        hub = GameHub(empty_room_ttl=0)
        room = GameRoom(
            code="EMPTY",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()

        room.connect("p1", p1_client)
        room.disconnect(p1_client)
        hub.cleanup_empty_rooms()

        self.assertNotIn(room.code, hub.rooms)


class MultiplayerGameplayTests(unittest.TestCase):
    def test_state_delivery_preserves_mutation_order(self):
        hub = PausingDrainHub()
        room = GameRoom(
            code="ORDER",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        p2_client = RecordingClient()
        room.connect("p1", p1_client)
        room.connect("p2", p2_client)

        first_choice = threading.Thread(
            target=hub.choose,
            args=(p1_client, "a"),
            name="first-choice",
        )
        first_choice.start()
        self.assertTrue(hub.first_batch_queued.wait(timeout=2))

        hub.choose(p2_client, "c")
        hub.release_first_batch.set()
        first_choice.join(timeout=2)

        self.assertFalse(first_choice.is_alive())
        for client in (p1_client, p2_client):
            phases = [message["state"]["phase"] for message in client.messages]
            self.assertEqual(phases, ["choosing", "resolved"])

    def test_connection_cannot_create_or_join_multiple_rooms(self):
        hub = GameHub()
        client = RecordingClient()
        other_host = RecordingClient()

        hub.create_room(client)
        first_room_code = client.room_code
        hub.create_room(other_host)
        other_room_code = other_host.room_code

        with self.assertRaisesRegex(GameError, "already in a room"):
            hub.create_room(client)
        with self.assertRaisesRegex(GameError, "already in a room"):
            hub.join_room(client, other_room_code, player="p2")

        self.assertEqual(len(hub.rooms), 2)
        self.assertIs(hub.rooms[first_room_code].seats["p1"].client, client)
        self.assertIsNone(hub.rooms[other_room_code].seats["p2"])

    def test_send_failure_disconnects_client_and_updates_opponent(self):
        hub = GameHub()
        room = GameRoom(
            code="FAILED",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=valid_rounds(),
        )
        hub.rooms[room.code] = room
        failed_client = FailingClient()
        opponent = RecordingClient()
        room.connect("p1", failed_client)
        room.connect("p2", opponent)

        hub.choose(opponent, "c")

        self.assertFalse(failed_client.alive)
        self.assertIsNone(room.seats["p1"].client)
        presence_updates = [message["state"]["connected"]["p1"] for message in opponent.messages]
        self.assertEqual(presence_updates, [True, False])

    def test_different_choices_award_shown_gem_counts(self):
        room = connected_room()

        room.choose("p1", "a")
        room.choose("p2", "c")

        self.assertEqual(room.phase, "resolved")
        self.assertFalse(room.log[-1]["collision"])
        self.assertEqual(room.log[-1]["gainCounts"], {"p1": 1, "p2": 2})
        self.assertEqual(room.collected["p1"]["a"], 1)
        self.assertEqual(room.collected["p2"]["c"], 2)

    def test_match_completes_after_eight_rounds_and_reveals_targets(self):
        room = connected_room(targets={"p1": "a", "p2": "c"})

        for round_index in range(ROUND_COUNT):
            room.choose("p1", "a")
            room.choose("p2", "c" if round_index == 0 else "d")
            if round_index < ROUND_COUNT - 1:
                self.assertEqual(room.phase, "resolved")
                room.next_round()

        p2_snapshot = room.snapshot_for("p2")

        self.assertEqual(room.phase, "complete")
        self.assertEqual(p2_snapshot["opponentTarget"], "a")
        self.assertEqual(p2_snapshot["targetScores"], {"p1": 25, "p2": 2})
        self.assertEqual(p2_snapshot["winner"], "p1")

    def test_restart_waits_for_both_players_and_resets_match(self):
        room = connected_room()
        room.choose("p1", "a")
        room.choose("p2", "c")
        old_seed = room.seed

        room.request_restart("p1")
        p2_snapshot = room.snapshot_for("p2")

        self.assertEqual(room.phase, "resolved")
        self.assertTrue(p2_snapshot["restartReady"]["p1"])
        self.assertFalse(p2_snapshot["restartReady"]["p2"])

        room.request_restart("p2")

        self.assertEqual(room.phase, "choosing")
        self.assertNotEqual(room.seed, old_seed)
        self.assertEqual(room.round_index, 0)
        self.assertEqual(room.log, [])
        self.assertEqual(room.collected, {"p1": empty_counts(), "p2": empty_counts()})
        self.assertEqual(room.restart_ready, {"p1": False, "p2": False})


if __name__ == "__main__":
    unittest.main()
