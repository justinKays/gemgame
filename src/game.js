(function attachGame(root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.GemGame = factory();
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function buildGame() {
  "use strict";

  var ROUND_COUNT = 8;
  var MIN_PER_GEM_PER_ROUND = 1;
  var MAX_PER_GEM_PER_ROUND = 7;
  var COPIES_PER_GEM = 25;
  var EXTRA_COPIES_PER_GEM = COPIES_PER_GEM - (ROUND_COUNT * MIN_PER_GEM_PER_ROUND);
  var ROUND_PROFILES = [
    [1, 1, 2, 2],
    [2, 2, 2, 2],
    [2, 2, 3, 3],
    [2, 3, 3, 4],
    [2, 3, 4, 5],
    [1, 3, 5, 6],
    [1, 4, 5, 7],
    [1, 3, 7, 7]
  ];
  var ROUND_MAX_COUNTS = ROUND_PROFILES.map(function mapRoundMax(profile) {
    return Math.max.apply(null, profile);
  });
  var ROUND_PROFILE_PERMUTATIONS = ROUND_PROFILES.map(uniquePermutations);
  var PLAYERS = ["p1", "p2"];
  var TARGET_POOLS = {
    p1: ["a", "b"],
    p2: ["c", "d"]
  };
  var GEM_DEFINITIONS = [
    { id: "a", label: "A", name: "Amber", color: "#f2a51f" },
    { id: "b", label: "B", name: "Emerald", color: "#2fbf71" },
    { id: "c", label: "C", name: "Sapphire", color: "#2d6cdf" },
    { id: "d", label: "D", name: "Ruby", color: "#d84c74" }
  ];
  var GEMS = GEM_DEFINITIONS.map(function mapGem(gem) {
    return gem.id;
  });

  function getGem(gemId) {
    var gem = GEM_DEFINITIONS.find(function findGem(candidate) {
      return candidate.id === gemId;
    });
    if (!gem) {
      throw new Error("Unknown gem: " + gemId);
    }
    return gem;
  }

  function emptyCounts() {
    return GEMS.reduce(function reduceCounts(counts, gemId) {
      counts[gemId] = 0;
      return counts;
    }, {});
  }

  function cloneCounts(counts) {
    return GEMS.reduce(function reduceClone(copy, gemId) {
      copy[gemId] = Number(counts && counts[gemId]) || 0;
      return copy;
    }, {});
  }

  function xmur3(value) {
    var text = String(value);
    var h = 1779033703 ^ text.length;
    for (var i = 0; i < text.length; i += 1) {
      h = Math.imul(h ^ text.charCodeAt(i), 3432918353);
      h = (h << 13) | (h >>> 19);
    }
    return function nextHash() {
      h = Math.imul(h ^ (h >>> 16), 2246822507);
      h = Math.imul(h ^ (h >>> 13), 3266489909);
      h ^= h >>> 16;
      return h >>> 0;
    };
  }

  function mulberry32(seed) {
    return function random() {
      var t = seed += 0x6d2b79f5;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function createRng(seed) {
    return mulberry32(xmur3(seed)());
  }

  function randomInt(rng, maxExclusive) {
    return Math.floor(rng() * maxExclusive);
  }

  function pickOne(items, rng) {
    return items[randomInt(rng, items.length)];
  }

  function shuffle(items, rng) {
    var copy = items.slice();
    for (var i = copy.length - 1; i > 0; i -= 1) {
      var j = randomInt(rng, i + 1);
      var tmp = copy[i];
      copy[i] = copy[j];
      copy[j] = tmp;
    }
    return copy;
  }

  function normalizeSeed(seed) {
    if (seed !== undefined && seed !== null && seed !== "") {
      return String(seed);
    }
    return String(Date.now()) + "-" + Math.random().toString(16).slice(2);
  }

  function fallbackRounds() {
    return [
      { a: 1, b: 1, c: 2, d: 2 },
      { a: 2, b: 2, c: 2, d: 2 },
      { a: 3, b: 3, c: 2, d: 2 },
      { a: 4, b: 3, c: 3, d: 2 },
      { a: 2, b: 4, c: 3, d: 5 },
      { a: 5, b: 6, c: 3, d: 1 },
      { a: 1, b: 5, c: 7, d: 4 },
      { a: 7, b: 1, c: 3, d: 7 }
    ].map(cloneCounts);
  }

  function uniquePermutations(items) {
    var results = [];
    var seen = Object.create(null);

    function permute(prefix, remaining) {
      if (remaining.length === 0) {
        var key = prefix.join(",");
        if (!seen[key]) {
          seen[key] = true;
          results.push(prefix);
        }
        return;
      }

      for (var i = 0; i < remaining.length; i += 1) {
        permute(
          prefix.concat(remaining[i]),
          remaining.slice(0, i).concat(remaining.slice(i + 1))
        );
      }
    }

    permute([], items);
    return results;
  }

  function generateRounds(rng) {
    var remaining = GEMS.reduce(function addTarget(counts, gemId) {
      counts[gemId] = COPIES_PER_GEM;
      return counts;
    }, {});
    var rounds = [];

    if (assignRoundProfiles(rng, 0, remaining, rounds)) {
      return rounds;
    }

    return fallbackRounds();
  }

  function assignRoundProfiles(rng, roundIndex, remaining, rounds) {
    if (roundIndex === ROUND_COUNT) {
      return GEMS.every(function isComplete(gemId) {
        return remaining[gemId] === 0;
      });
    }

    var candidates = shuffle(ROUND_PROFILE_PERMUTATIONS[roundIndex], rng);
    candidates.sort(function compareCandidates(a, b) {
      return candidateScore(b, remaining) - candidateScore(a, remaining);
    });

    for (var i = 0; i < candidates.length; i += 1) {
      var candidate = candidates[i];
      var nextRemaining = {};
      var valid = true;
      var round = {};

      GEMS.forEach(function assignCandidate(gemId, gemIndex) {
        var remainingCount = remaining[gemId] - candidate[gemIndex];
        nextRemaining[gemId] = remainingCount;
        round[gemId] = candidate[gemIndex];
        if (remainingCount < 0) {
          valid = false;
        }
      });

      if (!valid || !canCompleteProfile(roundIndex + 1, nextRemaining)) {
        continue;
      }

      rounds.push(round);
      if (assignRoundProfiles(rng, roundIndex + 1, nextRemaining, rounds)) {
        return true;
      }
      rounds.pop();
    }

    return false;
  }

  function candidateScore(candidate, remaining) {
    return GEMS.reduce(function sumScore(total, gemId, gemIndex) {
      return total + (candidate[gemIndex] * remaining[gemId]);
    }, 0);
  }

  function canCompleteProfile(roundIndex, remaining) {
    var futureProfiles = ROUND_PROFILES.slice(roundIndex);
    var futureTotal = futureProfiles.reduce(function sumFuture(total, profile) {
      return total + profile.reduce(function sumProfile(profileTotal, count) {
        return profileTotal + count;
      }, 0);
    }, 0);
    var remainingTotal = GEMS.reduce(function sumRemaining(total, gemId) {
      return total + remaining[gemId];
    }, 0);
    if (remainingTotal !== futureTotal) {
      return false;
    }

    var minFuture = futureProfiles.reduce(function sumMin(total, profile) {
      return total + Math.min.apply(null, profile);
    }, 0);
    var maxFuture = futureProfiles.reduce(function sumMax(total, profile) {
      return total + Math.max.apply(null, profile);
    }, 0);

    return GEMS.every(function canFillGem(gemId) {
      return remaining[gemId] >= minFuture && remaining[gemId] <= maxFuture;
    });
  }

  function validateSchedule(rounds) {
    var totals = emptyCounts();
    var errors = [];

    if (!Array.isArray(rounds)) {
      return {
        valid: false,
        totals: totals,
        errors: ["Schedule is not an array."]
      };
    }

    if (rounds.length !== ROUND_COUNT) {
      errors.push("Schedule must contain " + ROUND_COUNT + " rounds.");
    }

    rounds.forEach(function validateRound(round, index) {
      var roundTotal = 0;
      GEMS.forEach(function validateGem(gemId) {
        var value = Number(round && round[gemId]);
        if (!Number.isInteger(value) || value < MIN_PER_GEM_PER_ROUND) {
          errors.push("Round " + (index + 1) + " must include at least one " + gemId + " gem.");
          value = 0;
        } else if (value > MAX_PER_GEM_PER_ROUND) {
          errors.push("Round " + (index + 1) + " has more than " + MAX_PER_GEM_PER_ROUND + " " + gemId + " gems.");
        }
        totals[gemId] += value;
        roundTotal += value;
      });
      if (roundTotal <= 0) {
        errors.push("Round " + (index + 1) + " has no gems.");
      }
    });

    var totalValues = GEMS.map(function mapTotal(gemId) {
      return totals[gemId];
    });
    var firstTotal = totalValues[0];
    var equalTotals = totalValues.every(function compareTotal(total) {
      return total === firstTotal;
    });
    if (!equalTotals) {
      errors.push("Gem appearance totals must be equal.");
    }
    var expectedTotals = totalValues.every(function compareExpectedTotal(total) {
      return total === COPIES_PER_GEM;
    });
    if (!expectedTotals) {
      errors.push("Each gem must appear exactly " + COPIES_PER_GEM + " times.");
    }

    return {
      valid: errors.length === 0,
      totals: totals,
      errors: errors
    };
  }

  function assertPlayer(player) {
    if (PLAYERS.indexOf(player) === -1) {
      throw new Error("Unknown player: " + player);
    }
  }

  function assertGem(gemId) {
    if (GEMS.indexOf(gemId) === -1) {
      throw new Error("Unknown gem: " + gemId);
    }
  }

  function createGame(options) {
    var config = options || {};
    var seed = normalizeSeed(config.seed);
    var rng = createRng(seed);
    var targets = {
      p1: pickOne(TARGET_POOLS.p1, rng),
      p2: pickOne(TARGET_POOLS.p2, rng)
    };
    var rounds = config.rounds ? config.rounds.map(cloneCounts) : generateRounds(rng);
    var validation = validateSchedule(rounds);

    if (!validation.valid) {
      throw new Error("Invalid round schedule: " + validation.errors.join(" "));
    }

    return {
      seed: seed,
      phase: "choosing",
      roundIndex: 0,
      targets: targets,
      rounds: rounds,
      selections: { p1: null, p2: null },
      collected: {
        p1: emptyCounts(),
        p2: emptyCounts()
      },
      log: []
    };
  }

  function getCurrentRound(state) {
    return state.rounds[state.roundIndex] || null;
  }

  function availableGems(state) {
    var round = getCurrentRound(state);
    if (!round) {
      return [];
    }
    return GEMS.filter(function isAvailable(gemId) {
      return round[gemId] > 0;
    });
  }

  function selectGem(state, player, gemId) {
    assertPlayer(player);
    assertGem(gemId);

    if (state.phase !== "choosing") {
      throw new Error("Selections are closed for this round.");
    }
    if (state.selections[player]) {
      throw new Error(player + " already locked a choice.");
    }
    if (availableGems(state).indexOf(gemId) === -1) {
      throw new Error("Gem " + gemId + " is not available this round.");
    }

    state.selections[player] = gemId;
    return state;
  }

  function allPlayersSelected(state) {
    return PLAYERS.every(function selected(player) {
      return Boolean(state.selections[player]);
    });
  }

  function getTargetScore(state, player) {
    assertPlayer(player);
    return state.collected[player][state.targets[player]];
  }

  function resolveRound(state) {
    if (state.phase !== "choosing") {
      throw new Error("Round cannot be resolved in the current phase.");
    }
    if (!allPlayersSelected(state)) {
      throw new Error("Both players must choose before the round resolves.");
    }

    var p1Choice = state.selections.p1;
    var p2Choice = state.selections.p2;
    var offered = cloneCounts(getCurrentRound(state));
    var collision = p1Choice === p2Choice;
    var gains = { p1: null, p2: null };
    var gainCounts = { p1: 0, p2: 0 };

    if (!collision) {
      gainCounts.p1 = offered[p1Choice];
      gainCounts.p2 = offered[p2Choice];
      state.collected.p1[p1Choice] += gainCounts.p1;
      state.collected.p2[p2Choice] += gainCounts.p2;
      gains.p1 = p1Choice;
      gains.p2 = p2Choice;
    }

    var result = {
      roundNumber: state.roundIndex + 1,
      offered: offered,
      choices: { p1: p1Choice, p2: p2Choice },
      collision: collision,
      gains: gains,
      gainCounts: gainCounts,
      targetScores: {
        p1: getTargetScore(state, "p1"),
        p2: getTargetScore(state, "p2")
      }
    };

    state.log.push(result);
    state.phase = state.roundIndex === ROUND_COUNT - 1 ? "complete" : "resolved";
    return result;
  }

  function advanceRound(state) {
    if (state.phase !== "resolved") {
      throw new Error("The match is not ready for the next round.");
    }
    state.roundIndex += 1;
    state.phase = "choosing";
    state.selections = { p1: null, p2: null };
    return state;
  }

  function getWinner(state) {
    if (state.phase !== "complete") {
      return null;
    }
    var p1Score = getTargetScore(state, "p1");
    var p2Score = getTargetScore(state, "p2");
    if (p1Score > p2Score) {
      return "p1";
    }
    if (p2Score > p1Score) {
      return "p2";
    }
    return "draw";
  }

  return {
    ROUND_COUNT: ROUND_COUNT,
    COPIES_PER_GEM: COPIES_PER_GEM,
    MIN_PER_GEM_PER_ROUND: MIN_PER_GEM_PER_ROUND,
    MAX_PER_GEM_PER_ROUND: MAX_PER_GEM_PER_ROUND,
    EXTRA_COPIES_PER_GEM: EXTRA_COPIES_PER_GEM,
    ROUND_PROFILES: ROUND_PROFILES.map(function cloneProfile(profile) {
      return profile.slice();
    }),
    ROUND_MAX_COUNTS: ROUND_MAX_COUNTS.slice(),
    PLAYERS: PLAYERS.slice(),
    TARGET_POOLS: {
      p1: TARGET_POOLS.p1.slice(),
      p2: TARGET_POOLS.p2.slice()
    },
    GEM_DEFINITIONS: GEM_DEFINITIONS.map(function cloneGem(gem) {
      return Object.assign({}, gem);
    }),
    GEMS: GEMS.slice(),
    createGame: createGame,
    createRng: createRng,
    emptyCounts: emptyCounts,
    cloneCounts: cloneCounts,
    validateSchedule: validateSchedule,
    getCurrentRound: getCurrentRound,
    availableGems: availableGems,
    selectGem: selectGem,
    allPlayersSelected: allPlayersSelected,
    resolveRound: resolveRound,
    advanceRound: advanceRound,
    getTargetScore: getTargetScore,
    getWinner: getWinner,
    getGem: getGem
  };
});
