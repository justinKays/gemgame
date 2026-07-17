(function bootMultiplayerGemDuel() {
  "use strict";

  var GEM_DEFINITIONS = [
    { id: "a", label: "A", name: "Amber" },
    { id: "b", label: "B", name: "Emerald" },
    { id: "c", label: "C", name: "Sapphire" },
    { id: "d", label: "D", name: "Ruby" }
  ];
  var GEMS = GEM_DEFINITIONS.map(function mapGem(gem) {
    return gem.id;
  });
  var socket = null;
  var latestState = null;
  var joinedRoom = null;
  var lastOfferSignature = null;

  var elements = {
    statusText: document.getElementById("statusText"),
    restartGameButton: document.getElementById("restartGameButton"),
    leaveButton: document.getElementById("leaveButton"),
    lobbyPanel: document.getElementById("lobbyPanel"),
    createRoomButton: document.getElementById("createRoomButton"),
    joinForm: document.getElementById("joinForm"),
    roomCodeInput: document.getElementById("roomCodeInput"),
    errorText: document.getElementById("errorText"),
    roomStrip: document.getElementById("roomStrip"),
    roomCodeLabel: document.getElementById("roomCodeLabel"),
    roomLinkInput: document.getElementById("roomLinkInput"),
    copyLinkButton: document.getElementById("copyLinkButton"),
    gameLayout: document.getElementById("gameLayout"),
    ledger: document.getElementById("ledger"),
    selfEyebrow: document.getElementById("selfEyebrow"),
    selfTitle: document.getElementById("selfTitle"),
    selfSeatBadge: document.getElementById("selfSeatBadge"),
    targetSlot: document.getElementById("targetSlot"),
    targetHint: document.getElementById("targetHint"),
    ownTargetScore: document.getElementById("ownTargetScore"),
    ownInventory: document.getElementById("ownInventory"),
    opponentTitle: document.getElementById("opponentTitle"),
    opponentSeatBadge: document.getElementById("opponentSeatBadge"),
    opponentTargetSlot: document.getElementById("opponentTargetSlot"),
    opponentHint: document.getElementById("opponentHint"),
    opponentReadyStatus: document.getElementById("opponentReadyStatus"),
    opponentInventory: document.getElementById("opponentInventory"),
    phaseBadge: document.getElementById("phaseBadge"),
    boardTitle: document.getElementById("boardTitle"),
    roundMessage: document.getElementById("roundMessage"),
    connectionBanner: document.getElementById("connectionBanner"),
    connectionBannerTitle: document.getElementById("connectionBannerTitle"),
    connectionBannerMessage: document.getElementById("connectionBannerMessage"),
    offerGrid: document.getElementById("offerGrid"),
    choiceStatus: document.getElementById("choiceStatus"),
    resultPanel: document.getElementById("resultPanel"),
    roundProgressText: document.getElementById("roundProgressText"),
    roundProgressCount: document.getElementById("roundProgressCount"),
    roundProgressTrack: document.getElementById("roundProgressTrack"),
    ownTargetMini: document.getElementById("ownTargetMini"),
    ownScoreMini: document.getElementById("ownScoreMini"),
    opponentReadyMini: document.getElementById("opponentReadyMini"),
    fairnessStrip: document.getElementById("fairnessStrip"),
    roundLog: document.getElementById("roundLog")
  };

  var queryParams = new URLSearchParams(window.location.search);
  var queryRoom = queryParams.get("room");
  var forceJoin = queryParams.get("join") === "1";
  if (queryRoom) {
    elements.roomCodeInput.value = queryRoom.toUpperCase();
  }

  function playerLabel(player) {
    return player === "p1" ? "Player 1" : "Player 2";
  }

  function opponentOf(player) {
    return player === "p1" ? "p2" : "p1";
  }

  function assetPath(gemId) {
    return "./assets/gem-" + gemId + ".svg";
  }

  function getGem(gemId) {
    var gem = GEM_DEFINITIONS.find(function findGem(candidate) {
      return candidate.id === gemId;
    });
    if (!gem) {
      throw new Error("Unknown gem: " + gemId);
    }
    return gem;
  }

  function gemLabel(gemId) {
    return getGem(gemId).label;
  }

  function gemName(gemId) {
    var gem = getGem(gemId);
    return gem.name + " " + gem.label;
  }

  function gemImage(gemId) {
    return '<img src="' + assetPath(gemId) + '" alt="' + gemName(gemId) + '">';
  }

  function canChooseGem(state) {
    return state.phase === "choosing" && state.connected.p1 && state.connected.p2 && !state.ownSelection;
  }

  function poolText(pool) {
    return pool.map(function mapGem(gemId) {
      return gemLabel(gemId);
    }).join(" / ");
  }

  function roomStorageKey(roomCode) {
    return "gem-duel-room-" + roomCode;
  }

  function saveSession(roomCode, player, token) {
    var payload = JSON.stringify({ player: player, token: token });
    window.sessionStorage.setItem(roomStorageKey(roomCode), payload);
    window.localStorage.setItem(roomStorageKey(roomCode), payload);
  }

  function loadSession(roomCode) {
    try {
      var raw = window.sessionStorage.getItem(roomStorageKey(roomCode)) ||
        window.localStorage.getItem(roomStorageKey(roomCode));
      return raw ? JSON.parse(raw) : null;
    } catch (error) {
      return null;
    }
  }

  function setError(message) {
    elements.errorText.textContent = message || "";
  }

  function setConnectionStatus(message, connected) {
    elements.statusText.textContent = message;
    elements.statusText.className = "connection-status" + (connected ? "" : " connection-status-offline");
  }

  function showConnectionBanner(tone, title, message) {
    elements.connectionBanner.className = "connection-banner connection-banner-" + tone;
    elements.connectionBannerTitle.textContent = title;
    elements.connectionBannerMessage.textContent = message;
    elements.connectionBanner.hidden = false;
  }

  function hideConnectionBanner() {
    elements.connectionBanner.hidden = true;
  }

  function send(message) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setError("Not connected to the multiplayer server.");
      return;
    }
    socket.send(JSON.stringify(message));
  }

  function connect() {
    if (window.location.protocol === "file:") {
      elements.statusText.textContent = "Run python server.py to play multiplayer.";
      setError("Open this through the Python server, not as a file, so WebSockets can connect.");
      return;
    }

    var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(protocol + "//" + window.location.host + "/ws");

    socket.addEventListener("open", function onOpen() {
      setConnectionStatus("Connected", true);
      setError("");

      if (queryRoom) {
        var saved = loadSession(queryRoom.toUpperCase());
        if (saved && saved.player && saved.token && !forceJoin) {
          send({ type: "join", roomCode: queryRoom, player: saved.player, token: saved.token });
        } else {
          send({ type: "join", roomCode: queryRoom });
        }
      }
    });

    socket.addEventListener("message", function onMessage(event) {
      var message = JSON.parse(event.data);
      if (message.type === "joined") {
        joinedRoom = {
          roomCode: message.roomCode,
          player: message.player,
          token: message.token
        };
        saveSession(message.roomCode, message.player, message.token);
        updateRoomUrl(message.roomCode);
      } else if (message.type === "state") {
        latestState = message.state;
        render();
      } else if (message.type === "error") {
        setError(message.message);
      }
    });

    socket.addEventListener("close", function onClose() {
      setConnectionStatus("Connection lost", false);
      if (latestState) {
        var disconnectedState = Object.assign({}, latestState);
        disconnectedState.connected = Object.assign({}, latestState.connected);
        disconnectedState.connected[latestState.player] = false;
        latestState = disconnectedState;
        lastOfferSignature = null;
        render();
      } else {
        setError("Connection lost. Refresh the page to reconnect.");
      }
    });
  }

  function updateRoomUrl(roomCode) {
    var nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("room", roomCode);
    nextUrl.searchParams.delete("join");
    nextUrl.searchParams.delete("player");
    window.history.replaceState(null, "", nextUrl.toString());
  }

  function showCopyResult(copied) {
    elements.copyLinkButton.textContent = copied ? "Copied" : "Copy failed";
    window.setTimeout(function resetCopyLabel() {
      elements.copyLinkButton.textContent = "Copy link";
    }, 1200);
  }

  function copyRoomLinkFallback() {
    elements.roomLinkInput.select();
    try {
      return document.execCommand("copy");
    } catch (error) {
      return false;
    }
  }

  function renderTarget(gemId) {
    return [
      '<div class="target-card">',
      gemImage(gemId),
      "<strong>Target " + gemLabel(gemId) + "</strong>",
      "<span>" + getGem(gemId).name + "</span>",
      "</div>"
    ].join("");
  }

  function renderHiddenTarget(pool) {
    return [
      '<div class="hidden-target">',
      "<span>Target could be</span>",
      "<strong>" + poolText(pool) + "</strong>",
      "</div>"
    ].join("");
  }

  function renderInventory(container, counts) {
    container.innerHTML = GEMS.map(function mapInventory(gemId) {
      return [
        '<div class="inventory-item">',
        gemImage(gemId),
        "<strong>" + gemLabel(gemId) + "</strong>",
        "<span>" + counts[gemId] + "</span>",
        "</div>"
      ].join("");
    }).join("");
  }

  function renderOffer(state) {
    var canChoose = canChooseGem(state);
    var signature = JSON.stringify([
      state.roundIndex,
      state.phase,
      state.offer,
      state.availableGems,
      state.ownSelection,
      canChoose
    ]);
    if (signature === lastOfferSignature) {
      return;
    }
    lastOfferSignature = signature;

    elements.offerGrid.innerHTML = GEMS.map(function mapOffer(gemId) {
      var isSelected = state.ownSelection === gemId;
      var disabled = !canChoose || state.availableGems.indexOf(gemId) === -1;
      return [
        '<button class="offer-tile' + (canChoose ? " offer-choice" : "") + (isSelected ? " selected" : "") + '" type="button" data-gem="' + gemId + '"' + (disabled ? " disabled" : "") + ' aria-pressed="' + isSelected + '" aria-label="Choose ' + gemName(gemId) + ', count ' + state.offer[gemId] + '">',
        '<span class="offer-gem">',
        gemImage(gemId),
        '<span class="offer-count">' + state.offer[gemId] + "</span>",
        "</span>",
        "<strong>" + gemLabel(gemId) + "</strong>",
        "</button>"
      ].join("");
    }).join("");
  }

  function renderProgress(state) {
    var totalRounds = state.totalRounds;
    var currentRound = state.phase === "waiting"
      ? 0
      : Math.min(state.roundNumber, totalRounds);
    var completedRounds = Math.min(state.log.length, totalRounds);
    var activeRound = state.phase === "choosing" ? currentRound : 0;
    var segments = [];

    for (var index = 1; index <= totalRounds; index += 1) {
      var segmentClass = index <= completedRounds
        ? "complete"
        : index === activeRound
          ? "current"
          : "";
      segments.push('<span class="' + segmentClass + '" aria-hidden="true"></span>');
    }

    elements.roundProgressText.textContent = state.phase === "complete"
      ? "Match complete"
      : currentRound > 0
        ? "Round " + currentRound + " of " + totalRounds
        : "Match progress";
    elements.roundProgressCount.textContent = completedRounds + " / " + totalRounds;
    elements.roundProgressTrack.innerHTML = segments.join("");
    elements.roundProgressTrack.setAttribute("aria-valuemin", "0");
    elements.roundProgressTrack.setAttribute("aria-valuemax", String(totalRounds));
    elements.roundProgressTrack.setAttribute("aria-valuenow", String(completedRounds));
  }

  function renderMobileSummary(state, ownTargetScore) {
    var opponent = opponentOf(state.player);
    var opponentOccupied = state.occupied ? state.occupied[opponent] : true;
    var opponentStatus = !opponentOccupied
        ? "Waiting"
        : !state.connected[opponent]
          ? "Offline"
          : state.ready[opponent]
            ? "Locked in"
            : "Choosing";

    elements.ownTargetMini.innerHTML = gemImage(state.ownTarget) + "<span>" + gemLabel(state.ownTarget) + "</span>";
    elements.ownScoreMini.textContent = ownTargetScore;
    elements.opponentReadyMini.textContent = opponentStatus;
    elements.opponentReadyMini.className = opponentStatus === "Offline" ? "is-offline" : "";
  }

  function renderChoices(state) {
    var self = state.player;
    var opponent = opponentOf(self);
    var opponentOccupied = state.occupied ? state.occupied[opponent] : true;

    if (!state.connected[self]) {
      elements.choiceStatus.textContent = "Connection lost - refresh to reconnect";
    } else if (opponentOccupied && !state.connected[opponent]) {
      elements.choiceStatus.textContent = "Paused - opponent disconnected";
    } else if (state.phase === "waiting") {
      elements.choiceStatus.textContent = "Waiting for player 2";
    } else if (state.phase === "choosing" && (!state.connected.p1 || !state.connected.p2)) {
      elements.choiceStatus.textContent = "Waiting for both players to connect";
    } else if (state.phase === "choosing" && state.ownSelection) {
      elements.choiceStatus.textContent = "Choice locked: " + gemLabel(state.ownSelection);
    } else if (state.phase === "choosing") {
      elements.choiceStatus.textContent = "Choose one gem";
    } else if (state.phase === "resolved") {
      elements.choiceStatus.textContent = "Next round starts automatically";
    } else {
      elements.choiceStatus.textContent = "Match complete";
    }
  }

  function describeGain(result, player) {
    if (result.collision) {
      return "No gem";
    }
    return "Took " + gemLabel(result.gains[player]) + " x" + gainCount(result, player);
  }

  function gainCount(result, player) {
    if (!result.gainCounts) {
      return result.gains && result.gains[player] ? 1 : 0;
    }
    return Number(result.gainCounts[player]) || 0;
  }

  function renderResult(state) {
    var self = state.player;
    var opponent = opponentOf(self);
    var restartVotes = Number(state.restartReady.p1) + Number(state.restartReady.p2);

    if (state.phase === "waiting") {
      elements.resultPanel.innerHTML = "<h3>Waiting</h3><p>Player 1 is in the room. Share the invite link with Player 2.</p>";
      return;
    }

    if (state.phase === "choosing") {
      var selfStatus = !state.connected[self]
        ? "disconnected"
        : state.ready[self]
          ? "locked"
          : "choosing";
      var opponentStatus = !state.connected[opponent]
        ? "disconnected"
        : state.ready[opponent]
          ? "locked"
          : "choosing";
      elements.resultPanel.innerHTML = [
        "<h3>Selections hidden</h3>",
        "<p>Your status: " + selfStatus + ".</p>",
        "<p>Opponent status: " + opponentStatus + ".</p>",
        restartVotes > 0 ? "<p>Restart votes: " + restartVotes + " / 2.</p>" : ""
      ].join("");
      return;
    }

    var result = state.lastResult;
    if (!result) {
      elements.resultPanel.innerHTML = "<h3>Ready</h3><p>No resolved rounds yet.</p>";
      return;
    }

    var summary = result.collision
      ? "Both players chose " + gemLabel(result.choices.p1) + ". No gems were collected."
      : "Choices were different, so each player collected the shown count of their chosen gem.";

    var html = [
      "<h3>Round " + result.roundNumber + " result</h3>",
      "<p>" + summary + "</p>",
      '<div class="result-grid">',
      '<div class="result-choice"><strong>Player 1</strong><br>' + gemImage(result.choices.p1) + " Chose " + gemLabel(result.choices.p1) + "<br>" + describeGain(result, "p1") + "</div>",
      '<div class="result-choice"><strong>Player 2</strong><br>' + gemImage(result.choices.p2) + " Chose " + gemLabel(result.choices.p2) + "<br>" + describeGain(result, "p2") + "</div>",
      "</div>"
    ];

    if (state.phase === "complete") {
      var p1Score = state.targetScores.p1;
      var p2Score = state.targetScores.p2;
      var selfScore = state.targetScores[self];
      var opponentScore = state.targetScores[opponent];
      var outcomeClass = state.winner === "draw"
        ? "outcome-draw"
        : state.winner === self
          ? "outcome-win"
          : "outcome-loss";
      var outcomeText = state.winner === "draw"
        ? "Draw"
        : state.winner === self
          ? "You won"
          : "You lost";
      var winnerText = state.winner === "draw"
        ? "Both target scores are " + p1Score + "."
        : playerLabel(state.winner) + " wins with target scores " + p1Score + " to " + p2Score + ".";
      html.push('<div class="final-outcome ' + outcomeClass + '"><strong>' + outcomeText + '</strong><span>Your target score: ' + selfScore + '. Opponent target score: ' + opponentScore + ".</span></div>");
      html.push('<div class="winner-callout">' + winnerText + "</div>");
    } else {
      html.push('<div class="' + (result.collision ? "winner-callout collision-callout" : "winner-callout") + '">' + (result.collision ? "Collision" : "Collected") + "</div>");
      html.push("<p>Next round starts automatically in a few seconds.</p>");
    }

    if (restartVotes > 0) {
      html.push("<p>Restart votes: " + restartVotes + " / 2.</p>");
    }

    elements.resultPanel.innerHTML = html.join("");
  }

  function renderFairness(state) {
    elements.fairnessStrip.innerHTML = GEMS.map(function mapFairness(gemId) {
      return [
        '<div class="fairness-item">',
        gemImage(gemId),
        "<strong>" + gemLabel(gemId) + "</strong>",
        "<span>appears " + state.fairnessTotals[gemId] + "</span>",
        "</div>"
      ].join("");
    }).join("");
  }

  function renderLog(state) {
    if (state.log.length === 0) {
      elements.roundLog.innerHTML = "<li>No rounds resolved yet.</li>";
      return;
    }

    elements.roundLog.innerHTML = state.log.map(function mapLog(result) {
      var text = result.collision
        ? "collision on " + gemLabel(result.choices.p1)
        : "P1 took " + gemLabel(result.gains.p1) + " x" + gainCount(result, "p1") + ", P2 took " + gemLabel(result.gains.p2) + " x" + gainCount(result, "p2");
      return "<li>Round " + result.roundNumber + ": " + text + ".</li>";
    }).join("");
  }

  function renderHeader(state) {
    var self = state.player;
    var opponent = opponentOf(self);
    var opponentOccupied = state.occupied ? state.occupied[opponent] : true;
    var opponentDisconnected = opponentOccupied && !state.connected[opponent];
    var phaseLabel = state.phase === "waiting"
      ? "Waiting"
      : state.phase === "choosing"
        ? "Choose"
        : state.phase === "resolved"
          ? "Reveal"
          : "Complete";

    if (!state.connected[self]) {
      phaseLabel = "Offline";
    } else if (opponentDisconnected) {
      phaseLabel = "Paused";
    }

    setConnectionStatus(
      state.connected[self] ? state.roomCode + " - " + playerLabel(self) : "Connection lost",
      state.connected[self]
    );
    elements.selfEyebrow.textContent = playerLabel(self);
    elements.selfTitle.textContent = "Your target";
    elements.selfSeatBadge.textContent = self.toUpperCase();
    elements.opponentSeatBadge.textContent = opponent.toUpperCase();
    elements.opponentTitle.textContent = "Opponent target";
    elements.phaseBadge.textContent = phaseLabel;

    if (!state.connected[self]) {
      elements.boardTitle.textContent = "Connection lost";
      elements.roundMessage.textContent = "Refresh this page to reconnect to the room.";
    } else if (opponentDisconnected) {
      elements.boardTitle.textContent = "Opponent disconnected";
      elements.roundMessage.textContent = "The match is paused until they reconnect.";
    } else if (state.phase === "waiting") {
      elements.boardTitle.textContent = "Waiting for player 2";
      elements.roundMessage.textContent = "Share the room link with another browser.";
    } else if (state.phase === "complete") {
      elements.boardTitle.textContent = state.winner === "draw"
        ? "Draw"
        : state.winner === self
          ? "You won"
          : "You lost";
      elements.roundMessage.textContent = "The match is complete.";
    } else if (state.phase === "resolved") {
      elements.boardTitle.textContent = "Round " + state.roundNumber + " result";
      elements.roundMessage.textContent = "Next round starts automatically in a few seconds.";
    } else {
      elements.boardTitle.textContent = "Round " + state.roundNumber + " / " + state.totalRounds;
      elements.roundMessage.textContent = "Every gem appears at least once this round. Locked choices stay private.";
    }

    elements.restartGameButton.hidden = false;
    elements.restartGameButton.disabled = Boolean(state.restartReady[self]) ||
      !state.connected[self] ||
      !state.connected[opponent];
    elements.restartGameButton.textContent = state.restartReady[self]
      ? "Restart requested"
      : "Restart match";
  }

  function renderConnectionState(state) {
    var self = state.player;
    var opponent = opponentOf(self);
    var opponentOccupied = state.occupied ? state.occupied[opponent] : true;

    if (!state.connected[self]) {
      showConnectionBanner(
        "danger",
        "You are disconnected",
        "Your choices are disabled. Refresh this page to reconnect to the room."
      );
    } else if (opponentOccupied && !state.connected[opponent]) {
      showConnectionBanner(
        "warning",
        "Opponent disconnected",
        "Choices are paused. Keep this page open while they reconnect."
      );
    } else {
      hideConnectionBanner();
    }
  }

  function renderRoomInfo(state) {
    var link = new URL(window.location.href);
    link.searchParams.set("room", state.roomCode);
    link.searchParams.set("join", "1");
    link.searchParams.delete("player");
    elements.roomCodeLabel.textContent = state.roomCode;
    elements.roomLinkInput.value = link.toString();
  }

  function render() {
    var state = latestState;
    if (!state) {
      return;
    }

    var self = state.player;
    var opponent = opponentOf(self);
    var ownTargetScore = state.collected[self][state.ownTarget];

    elements.lobbyPanel.hidden = true;
    elements.roomStrip.hidden = false;
    elements.gameLayout.hidden = false;
    elements.ledger.hidden = false;
    elements.restartGameButton.hidden = false;
    elements.leaveButton.hidden = false;
    setError("");

    renderRoomInfo(state);
    renderHeader(state);
    renderConnectionState(state);
    renderProgress(state);
    renderOffer(state);
    renderChoices(state);
    renderResult(state);
    renderFairness(state);
    renderLog(state);

    elements.targetSlot.innerHTML = renderTarget(state.ownTarget);
    elements.targetHint.textContent = "Opponent target is one of " + poolText(state.opponentTargetPool) + ".";
    elements.ownTargetScore.textContent = "Your target score: " + ownTargetScore;
    renderMobileSummary(state, ownTargetScore);
    renderInventory(elements.ownInventory, state.collected[self]);

    if (state.phase === "complete") {
      elements.opponentTargetSlot.innerHTML = renderTarget(state.opponentTarget);
      elements.opponentReadyStatus.textContent = "Target score: " + state.targetScores[opponent];
      elements.opponentReadyStatus.className = "target-score";
    } else {
      elements.opponentTargetSlot.innerHTML = renderHiddenTarget(state.opponentTargetPool);
      elements.opponentReadyStatus.textContent = state.connected[opponent]
        ? "Choice status: " + (state.ready[opponent] ? "locked" : "choosing")
        : "Opponent disconnected";
      elements.opponentReadyStatus.className = state.connected[opponent]
        ? "target-score"
        : "target-score opponent-status-offline";
    }
    elements.opponentHint.textContent = "They only know your target is one of " + poolText(state.ownTargetPool) + ".";
    renderInventory(elements.opponentInventory, state.collected[opponent]);
  }

  elements.createRoomButton.addEventListener("click", function createRoom() {
    setError("");
    send({ type: "create" });
  });

  elements.joinForm.addEventListener("submit", function joinRoom(event) {
    event.preventDefault();
    setError("");
    send({ type: "join", roomCode: elements.roomCodeInput.value });
  });

  elements.offerGrid.addEventListener("click", function chooseGem(event) {
    var button = event.target.closest(".offer-tile[data-gem]");
    if (!button || button.disabled) {
      return;
    }
    send({ type: "choose", gem: button.dataset.gem });
  });

  elements.restartGameButton.addEventListener("click", function restartGame() {
    send({ type: "restart" });
  });

  elements.copyLinkButton.addEventListener("click", function copyRoomLink() {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try {
        Promise.resolve(navigator.clipboard.writeText(elements.roomLinkInput.value)).then(
          function copySucceeded() {
            showCopyResult(true);
          },
          function copyFailed() {
            showCopyResult(copyRoomLinkFallback());
          }
        );
        return;
      } catch (error) {
        showCopyResult(copyRoomLinkFallback());
        return;
      }
    }
    showCopyResult(copyRoomLinkFallback());
  });

  elements.leaveButton.addEventListener("click", function leaveRoom() {
    if (joinedRoom) {
      window.sessionStorage.removeItem(roomStorageKey(joinedRoom.roomCode));
      window.localStorage.removeItem(roomStorageKey(joinedRoom.roomCode));
    }
    window.location.href = window.location.pathname;
  });

  connect();
})();
