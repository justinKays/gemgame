const test = require("node:test");
const assert = require("node:assert/strict");
const Game = require("../src/game.js");

function evenRounds() {
  return [
    { a: 1, b: 2, c: 1, d: 2 },
    { a: 2, b: 1, c: 1, d: 1 },
    { a: 1, b: 2, c: 3, d: 1 },
    { a: 3, b: 2, c: 2, d: 3 },
    { a: 4, b: 3, c: 4, d: 3 },
    { a: 4, b: 5, c: 3, d: 4 },
    { a: 5, b: 4, c: 7, d: 5 },
    { a: 5, b: 6, c: 4, d: 6 }
  ];
}

function playChoicePair(state, p1Gem, p2Gem) {
  Game.selectGem(state, "p1", p1Gem);
  Game.selectGem(state, "p2", p2Gem);
  return Game.resolveRound(state);
}

test("assigns targets from the correct private pools", () => {
  for (let i = 0; i < 50; i += 1) {
    const state = Game.createGame({ seed: "target-test-" + i });
    assert.ok(Game.TARGET_POOLS.p1.includes(state.targets.p1));
    assert.ok(Game.TARGET_POOLS.p2.includes(state.targets.p2));
  }
});

test("generates eight fair rounds with every gem present each round", () => {
  const expectedRoundTotals = Game.ROUND_PROFILES.map((profile) => profile.reduce((total, count) => total + count, 0));
  const expectedRoundSpreads = Game.ROUND_PROFILES.map((profile) => Math.max(...profile) - Math.min(...profile));

  for (let i = 0; i < 50; i += 1) {
    const state = Game.createGame({ seed: "schedule-test-" + i });
    const validation = Game.validateSchedule(state.rounds);
    const roundTotals = state.rounds.map((round) => Game.GEMS.reduce((total, gemId) => total + round[gemId], 0));
    const roundSpreads = state.rounds.map((round) => {
      const counts = Game.GEMS.map((gemId) => round[gemId]);
      return Math.max(...counts) - Math.min(...counts);
    });

    assert.equal(state.rounds.length, Game.ROUND_COUNT);
    assert.equal(validation.valid, true);
    assert.deepEqual(roundTotals, expectedRoundTotals);
    assert.deepEqual(roundSpreads, expectedRoundSpreads);
    assert.ok(Math.max(...roundSpreads.slice(0, 4)) <= 2);
    assert.ok(Math.min(...roundSpreads.slice(5)) >= 5);
    Game.GEMS.forEach((gemId) => {
      assert.equal(validation.totals[gemId], Game.COPIES_PER_GEM);
    });
    state.rounds.forEach((round) => {
      Game.GEMS.forEach((gemId) => {
        assert.ok(round[gemId] >= Game.MIN_PER_GEM_PER_ROUND);
        assert.ok(round[gemId] <= Game.MAX_PER_GEM_PER_ROUND);
      });
    });
    Game.GEMS.forEach((gemId) => {
      const earlyTotal = state.rounds.slice(0, 4).reduce((total, round) => total + round[gemId], 0);
      const lateTotal = state.rounds.slice(4).reduce((total, round) => total + round[gemId], 0);
      assert.ok(lateTotal > earlyTotal);
    });
  }
});

test("same gem choices collide and award no gems", () => {
  const state = Game.createGame({ seed: "collision", rounds: evenRounds() });
  const result = playChoicePair(state, "a", "a");

  assert.equal(result.collision, true);
  assert.deepEqual(result.gains, { p1: null, p2: null });
  assert.deepEqual(result.gainCounts, { p1: 0, p2: 0 });
  assert.equal(state.collected.p1.a, 0);
  assert.equal(state.collected.p2.a, 0);
});

test("different choices award the shown selected gem count to each player", () => {
  const state = Game.createGame({ seed: "collect", rounds: evenRounds() });
  const result = playChoicePair(state, "b", "d");

  assert.equal(result.collision, false);
  assert.deepEqual(result.gains, { p1: "b", p2: "d" });
  assert.deepEqual(result.gainCounts, { p1: 2, p2: 2 });
  assert.equal(state.collected.p1.b, 2);
  assert.equal(state.collected.p2.d, 2);
});

test("winner is based on each player's own target gem count", () => {
  const state = Game.createGame({ seed: "winner", rounds: evenRounds() });
  state.targets = { p1: "a", p2: "c" };

  for (let round = 0; round < Game.ROUND_COUNT; round += 1) {
    playChoicePair(state, "a", round === 0 ? "c" : "d");
    if (round < Game.ROUND_COUNT - 1) {
      Game.advanceRound(state);
    }
  }

  assert.equal(state.phase, "complete");
  assert.equal(Game.getTargetScore(state, "p1"), 25);
  assert.equal(Game.getTargetScore(state, "p2"), 1);
  assert.equal(Game.getWinner(state), "p1");
});

test("invalid and duplicate choices are rejected", () => {
  const state = Game.createGame({ seed: "invalid", rounds: evenRounds() });

  assert.throws(() => Game.selectGem(state, "p3", "a"), /Unknown player/);
  assert.throws(() => Game.selectGem(state, "p1", "z"), /Unknown gem/);
  Game.selectGem(state, "p1", "a");
  assert.throws(() => Game.selectGem(state, "p1", "b"), /already locked/);
});
