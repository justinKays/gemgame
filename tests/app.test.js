const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const projectRoot = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(projectRoot, "index.html"), "utf8");
const appSource = fs.readFileSync(path.join(projectRoot, "src", "app.js"), "utf8");

class FakeElement {
  constructor() {
    this.listeners = Object.create(null);
    this.hidden = false;
    this.disabled = false;
    this.innerHTML = "";
    this.textContent = "";
    this.value = "";
    this.selected = false;
  }

  addEventListener(type, listener) {
    this.listeners[type] = listener;
  }

  dispatch(type, event = {}) {
    this.listeners[type](event);
  }

  select() {
    this.selected = true;
  }
}

function createStorage() {
  const values = new Map();
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    }
  };
}

function createHarness(pageUrl = "http://127.0.0.1:8000/") {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  const timers = [];
  const location = new URL(pageUrl);
  const clipboardWrites = [];
  const navigator = {
    clipboard: {
      writeText(value) {
        clipboardWrites.push(value);
        return Promise.resolve();
      }
    }
  };
  const document = {
    copyResult: true,
    getElementById(id) {
      return elements[id] || null;
    },
    execCommand(command) {
      assert.equal(command, "copy");
      return this.copyResult;
    }
  };

  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.listeners = Object.create(null);
      this.sent = [];
      FakeWebSocket.instances.push(this);
    }

    addEventListener(type, listener) {
      this.listeners[type] = listener;
    }

    open() {
      this.readyState = FakeWebSocket.OPEN;
      this.listeners.open();
    }

    message(payload) {
      this.listeners.message({ data: JSON.stringify(payload) });
    }

    send(payload) {
      this.sent.push(JSON.parse(payload));
    }
  }
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.instances = [];

  const window = {
    location,
    history: {
      replaceState(_state, _title, nextUrl) {
        location.href = nextUrl;
      }
    },
    sessionStorage: createStorage(),
    localStorage: createStorage(),
    setTimeout(callback) {
      timers.push(callback);
      return timers.length;
    }
  };

  vm.runInNewContext(appSource, {
    console,
    document,
    JSON,
    navigator,
    Promise,
    URL,
    URLSearchParams,
    WebSocket: FakeWebSocket,
    window
  }, { filename: "src/app.js" });

  return {
    clipboardWrites,
    document,
    elements,
    navigator,
    socket: FakeWebSocket.instances[0],
    timers
  };
}

function choosingState() {
  const emptyCounts = { a: 0, b: 0, c: 0, d: 0 };
  return {
    roomCode: "ABCDE",
    player: "p1",
    phase: "choosing",
    roundIndex: 0,
    roundNumber: 1,
    totalRounds: 8,
    offer: { a: 1, b: 1, c: 2, d: 2 },
    availableGems: ["a", "b", "c", "d"],
    fairnessTotals: { a: 25, b: 25, c: 25, d: 25 },
    ownTarget: "a",
    ownTargetPool: ["a", "b"],
    opponentTargetPool: ["c", "d"],
    opponentTarget: null,
    collected: { p1: { ...emptyCounts }, p2: { ...emptyCounts } },
    ready: { p1: false, p2: false },
    restartReady: { p1: false, p2: false },
    ownSelection: null,
    lastResult: null,
    log: [],
    connected: { p1: true, p2: true },
    winner: null,
    targetScores: null
  };
}

test("browser client runs the multiplayer render and command flow", async () => {
  const harness = createHarness();
  const { elements, socket } = harness;

  assert.equal(socket.url, "ws://127.0.0.1:8000/ws");
  socket.open();
  elements.createRoomButton.dispatch("click");
  assert.deepEqual(socket.sent.pop(), { type: "create" });

  socket.message({ type: "joined", roomCode: "ABCDE", player: "p1", token: "secret" });
  socket.message({ type: "state", state: choosingState() });

  assert.equal(elements.lobbyPanel.hidden, true);
  assert.equal(elements.gameLayout.hidden, false);
  assert.match(elements.offerGrid.innerHTML, /data-gem="a"/);
  assert.equal(elements.boardTitle.textContent, "Round 1 / 8");
  const inviteUrl = new URL(elements.roomLinkInput.value);
  assert.equal(inviteUrl.searchParams.get("join"), "1");
  assert.equal(inviteUrl.searchParams.has("player"), false);

  elements.offerGrid.dispatch("click", {
    target: {
      closest() {
        return { disabled: false, dataset: { gem: "a" } };
      }
    }
  });
  assert.deepEqual(socket.sent.pop(), { type: "choose", gem: "a" });

  const complete = choosingState();
  const result = {
    roundNumber: 8,
    choices: { p1: "a", p2: "c" },
    collision: false,
    gains: { p1: "a", p2: "c" },
    gainCounts: { p1: 7, p2: 3 }
  };
  complete.phase = "complete";
  complete.roundNumber = 8;
  complete.opponentTarget = "c";
  complete.targetScores = { p1: 12, p2: 9 };
  complete.winner = "p1";
  complete.lastResult = result;
  complete.log = [result];
  socket.message({ type: "state", state: complete });

  assert.equal(elements.boardTitle.textContent, "You won");
  assert.match(elements.resultPanel.innerHTML, /Your target score: 12/);
  elements.restartGameButton.dispatch("click");
  assert.deepEqual(socket.sent.pop(), { type: "restart" });

  elements.copyLinkButton.dispatch("click");
  await Promise.resolve();
  assert.equal(harness.clipboardWrites.length, 1);
  assert.equal(elements.copyLinkButton.textContent, "Copied");
});

test("copy-link feedback reports a real failure", async () => {
  const harness = createHarness();
  harness.elements.roomLinkInput.value = "http://127.0.0.1:8000/?room=ABCDE";
  harness.document.copyResult = false;
  harness.clipboardWrites.length = 0;
  const rejectedWrite = () => Promise.reject(new Error("denied"));
  harness.socket.open();

  harness.navigator.clipboard.writeText = rejectedWrite;
  harness.elements.copyLinkButton.dispatch("click");
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(harness.elements.roomLinkInput.selected, true);
  assert.equal(harness.elements.copyLinkButton.textContent, "Copy failed");
});

test("invite links request whichever room seat is available", () => {
  const harness = createHarness("http://127.0.0.1:8000/?room=ABCDE&join=1&player=p2");

  harness.socket.open();

  assert.deepEqual(harness.socket.sent, [{ type: "join", roomCode: "ABCDE" }]);
});
