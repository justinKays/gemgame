import unittest

from server import (
    COPIES_PER_GEM,
    GEMS,
    MAX_PER_GEM_PER_ROUND,
    MIN_PER_GEM_PER_ROUND,
    ROUND_COUNT,
    ROUND_PROFILES,
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

    def detach(self):
        self.detached = True


class RecordingClient(DummyClient):
    def __init__(self):
        super().__init__()
        self.messages = []

    def send_json(self, payload):
        self.messages.append(payload)


def even_rounds():
    return [{gem_id: 1 for gem_id in GEMS} for _ in range(ROUND_COUNT)]


def connected_room(targets=None, rounds=None):
    room = GameRoom(
        code="TEST1",
        seed="test-seed",
        targets=targets or {"p1": "a", "p2": "c"},
        rounds=rounds or even_rounds(),
    )
    room.connect("p1", DummyClient())
    room.connect("p2", DummyClient())
    return room


class ScheduleTests(unittest.TestCase):
    def test_generated_rounds_are_fair_and_include_each_gem_each_round(self):
        expected_round_totals = [sum(profile) for profile in ROUND_PROFILES]
        expected_round_spreads = [max(profile) - min(profile) for profile in ROUND_PROFILES]

        for index in range(50):
            rounds = generate_rounds(f"schedule-{index}")
            valid, totals, errors = validate_schedule(rounds)
            round_totals = [sum(round_offer.values()) for round_offer in rounds]
            round_spreads = [
                max(round_offer[gem_id] for gem_id in GEMS) - min(round_offer[gem_id] for gem_id in GEMS)
                for round_offer in rounds
            ]

            self.assertTrue(valid, errors)
            self.assertEqual(len(rounds), ROUND_COUNT)
            self.assertEqual(round_totals, expected_round_totals)
            self.assertEqual(round_spreads, expected_round_spreads)
            self.assertLessEqual(max(round_spreads[:4]), 2)
            self.assertGreaterEqual(min(round_spreads[5:]), 5)
            for gem_id in GEMS:
                self.assertEqual(totals[gem_id], COPIES_PER_GEM)
            for round_offer in rounds:
                for gem_id in GEMS:
                    self.assertGreaterEqual(round_offer[gem_id], MIN_PER_GEM_PER_ROUND)
                    self.assertLessEqual(round_offer[gem_id], MAX_PER_GEM_PER_ROUND)
            for gem_id in GEMS:
                early_total = sum(round_offer[gem_id] for round_offer in rounds[:4])
                late_total = sum(round_offer[gem_id] for round_offer in rounds[4:])
                self.assertGreater(late_total, early_total)


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
            rounds=even_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        stale_p2_client = RecordingClient()
        new_p2_client = RecordingClient()

        room.connect("p1", p1_client)
        old_token = room.connect("p2", stale_p2_client)
        room.disconnect(stale_p2_client)

        hub.join_room(new_p2_client, room.code)

        self.assertEqual(new_p2_client.messages[0]["type"], "joined")
        self.assertEqual(new_p2_client.messages[0]["player"], "p2")
        self.assertNotEqual(new_p2_client.messages[0]["token"], old_token)
        self.assertIs(room.seats["p2"].client, new_p2_client)
        self.assertTrue(room.connected()["p2"])

    def test_second_player_join_transfers_existing_p2_seat(self):
        hub = GameHub()
        room = GameRoom(
            code="GHOST",
            seed="test-seed",
            targets={"p1": "a", "p2": "c"},
            rounds=even_rounds(),
        )
        hub.rooms[room.code] = room
        p1_client = RecordingClient()
        ghost_p2_client = RecordingClient()
        new_p2_client = RecordingClient()

        room.connect("p1", p1_client)
        room.connect("p2", ghost_p2_client)

        hub.join_room(new_p2_client, room.code)

        self.assertTrue(ghost_p2_client.detached)
        self.assertEqual(new_p2_client.messages[0]["type"], "joined")
        self.assertEqual(new_p2_client.messages[0]["player"], "p2")
        self.assertIs(room.seats["p2"].client, new_p2_client)
        self.assertTrue(room.connected()["p2"])


class MultiplayerGameplayTests(unittest.TestCase):
    def test_different_choices_award_shown_gem_counts(self):
        rounds = even_rounds()
        rounds[0] = {"a": 4, "b": 1, "c": 3, "d": 2}
        room = connected_room(rounds=rounds)

        room.choose("p1", "a")
        room.choose("p2", "c")

        self.assertEqual(room.phase, "resolved")
        self.assertFalse(room.log[-1]["collision"])
        self.assertEqual(room.log[-1]["gainCounts"], {"p1": 4, "p2": 3})
        self.assertEqual(room.collected["p1"]["a"], 4)
        self.assertEqual(room.collected["p2"]["c"], 3)

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
        self.assertEqual(p2_snapshot["targetScores"], {"p1": 8, "p2": 1})
        self.assertEqual(p2_snapshot["winner"], "p1")

    def test_next_round_request_is_no_longer_player_gated(self):
        room = connected_room()
        room.choose("p1", "a")
        room.choose("p2", "c")

        with self.assertRaisesRegex(GameError, "automatically"):
            room.request_next_round("p1")
        self.assertEqual(room.phase, "resolved")
        self.assertEqual(room.next_ready, {"p1": False, "p2": False})

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
