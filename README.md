# Gem Duel

Gem Duel is a simple full multiplayer 1v1 browser game. A Python server hosts the page and owns the match state over WebSockets, so each player opens a separate browser view and cannot see the other player's locked move until the round resolves.

## Prompt Analysis And Rule Improvements

The original idea has a strong hidden-information mechanic: players know their own target gem, know the opponent's target pool, and must decide whether to take their own target or block the opponent. The first prototype was local same-screen multiplayer, which did not preserve hidden moves. This version fixes that by making the server authoritative.

Clarified rules:

- Player 1's target is randomly selected from A or B.
- Player 2's target is randomly selected from C or D.
- Each player receives only their own exact target until the match is complete.
- Each player only knows the opponent target is one of the opponent's two possible gems.
- A match lasts exactly 8 rounds.
- Each round offers at least one A, one B, one C, and one D.
- Each gem count per round is between 1 and 7.
- Round total copies climb as `6, 8, 10, 12, 14, 15, 17, 18`.
- Early rounds stay fairly even, while late rounds have larger and more uneven offers.
- Extra gem copies are distributed so every gem appears exactly 25 times across the full match.
- Each player chooses one available gem type. Choices are locked privately on the server.
- If both players choose the same gem type, neither player collects a gem that round.
- If players choose different gem types, each player collects the full shown count of the chosen type.
- After both players choose, the server broadcasts both choices and the result.
- After a short reveal delay, the server automatically moves to the next round.
- Restarting the match requires both players to click restart.
- Rooms are independent of either player; invite links fill whichever seat is available.
- A valid reconnect token preserves the current match, while a new occupant taking a disconnected seat starts a fresh match.
- The winner is the player with the higher count of their own target gem after 8 rounds. Equal target counts produce a draw.

## Implementation Plan

- Use `server.py` as a dependency-free Python HTTP/WebSocket server.
- Keep all authoritative state on the server: room seats, private targets, hidden selections, inventories, round logs, and winner calculation.
- Treat Player 1 and Player 2 as equal room seats rather than permanent room owners.
- Automatically advance after each resolved round, while keeping two-player agreement for match restart.
- Serve one browser client from `index.html`, `styles.css`, and `src/app.js`.
- Use room codes and invite links so two browser windows or two devices on the same network can join.
- Keep display-only gem metadata in `src/app.js`; all game rules remain authoritative in Python.
- Add Python tests for game rules, multiplayer behavior, HTTP serving, and WebSocket transport.

## Run

Start the multiplayer server from the project folder:

```powershell
cd C:\Users\tjdjs\Documents\codex_gemgame
python server.py --host 0.0.0.0 --port 8000
```

On the same computer, open:

```text
http://127.0.0.1:8000/
```

On another device on the same Wi-Fi/LAN, open the host computer's LAN IPv4 address:

```text
http://<your IPv4 address>:8000/
```

Example:

```text
http://172.30.1.1:8000/
```

Click `Create room`, then open the generated room link in another browser tab, another browser profile, or another device.

Quick IP notes:

- `127.0.0.1` / `localhost` means "this same device only."
- `0.0.0.0` is only for the server `--host`; it means "listen on all network interfaces." Do not use it as the browser URL.
- Your LAN IPv4 address is what other devices use, for example `172.30.1.1`.
- On Windows, find it with `ipconfig`, then look under `Wireless LAN adapter Wi-Fi` for `IPv4 Address`.
- A subnet mask like `255.255.255.0` is the same idea as `/24`; it describes which nearby IPs are on your local network.
- The gateway is usually the router/hotspot, often `.1` or `.254`. `.255` is commonly the broadcast address, so it is not normally assigned to a device.

If another device cannot connect, make sure both devices are on the same Wi-Fi/LAN and allow Python or TCP port `8000` through Windows Firewall on a Private network. This is still a LAN setup; internet play needs a publicly reachable hosted server with WebSocket support.

## App And Publishing Path

Current build:

- Works on the same machine with `127.0.0.1`.
- Works on the same Wi-Fi/LAN if the server binds to `0.0.0.0` and firewall rules allow the port.
- Does not automatically work over the public internet because home routers, NAT, firewalls, and dynamic IPs block inbound connections.

Recommended next steps:

- For web internet play: deploy the Python server to a VPS or platform that supports long-lived WebSockets, then serve the same frontend over HTTPS/WSS.
- For a quick hosted prototype: Replit can run this as a published web-server app. Use Reserved VM for the simplest always-on setup; use Autoscale only with one machine unless the room state is moved out of memory.
- For Android and iOS stores: first deploy the multiplayer server publicly, then add a configurable server URL and package the web client in a native wrapper such as Capacitor. Store icons, splash screens, privacy disclosures, signing, and release builds belong in that packaging phase.
- For a desktop app: wrap the browser client with Tauri, Electron, or a native shell, but still use a hosted matchmaking/game server for internet multiplayer.
- For Steam: package the desktop client, create a Steamworks app, integrate Steam sign-in/invites later if desired, and keep the game server hosted separately.

### Replit Hosting

This project includes a `.replit` file with both workspace and deployment run commands:

```bash
python3 server.py --host 0.0.0.0
```

Use a web-server deployment, not a static deployment, because the game needs `server.py` for rooms and WebSockets. In Replit, publish it as a Reserved VM for a small always-on multiplayer prototype. The included port mapping exposes local port `8000` as the public HTTPS site.

If the Publishing tool still says `Run command is required`, enter this manually in the deployment `Run command` field:

```bash
python3 server.py --host 0.0.0.0
```

Leave the build command empty.

### CI/CD

Recommended professional workflow for this project:

```text
GitHub pull request -> GitHub Actions CI -> merge to main -> Render CD
```

Roles:

- GitHub Actions is CI. It checks every push and pull request before code is trusted.
- Render is CD and hosting. It runs the public web server and can deploy automatically after CI passes.
- Replit is useful for quick prototypes and demos, but Render has a cleaner production-style GitHub deployment workflow for this app.

This repo includes `.github/workflows/ci.yml`. The workflow:

- Sets up Python 3.12.
- Installs `requirements.txt`.
- Runs the Python multiplayer/server tests.
- Sets up Node 22 and runs dependency-free browser-client runtime tests.

The `requirements.txt` file is intentionally empty right now because the Python server uses only the standard library. It is still included so CI and hosting are ready for future dependencies. If a package is added later, put it in `requirements.txt`, for example:

```text
redis==5.0.8
```

Render setup:

```text
Service type: Web Service
Build command: python -m pip install -r requirements.txt
Start command: python server.py --host 0.0.0.0
Auto-Deploy: After CI Checks Pass
Instances: 1
```

The build command is harmless while `requirements.txt` is empty. It becomes useful as soon as real dependencies are added. The start command does not specify a port because `server.py` reads the hosting platform's `PORT` environment variable and falls back to `8000` locally.

Keep the service at one instance for now. Rooms are stored in server memory, so multiple instances would split players across different room states unless room state is moved to Redis or a database.

Render replaces service instances during deploys and maintenance, which closes active WebSockets. Because rooms currently live only in process memory, a replacement also removes every active room. Before treating the app as production-ready, move room state to shared storage and add heartbeat plus automatic client reconnection. Free instances can also spin down while idle, so check the current Render plan limits before public launch.

## Test

Run authoritative server tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Run browser-client runtime tests when Node is available:

```bash
node --test tests/app.test.js
```

### Test on a phone

The simplest option is to open the deployed Render URL directly on the phone. Create a room on one device, then open the invite in a second browser, private tab, or another device.

For local Wi-Fi testing, run:

```powershell
python server.py --host 0.0.0.0 --port 8000
```

Find the computer's LAN IPv4 address with `ipconfig`, then open `http://<LAN-IP>:8000` on a phone connected to the same Wi-Fi. Allow Python through Windows Firewall on Private networks if the page cannot connect.

Mobile regression checklist:

- Test at 320 px width and in portrait and landscape orientation.
- Close the second player's tab or enable airplane mode; the remaining player should see the paused disconnect warning and disabled gem choices.
- Reopen the saved room URL; a token reconnect should preserve the match.
- Have both players request a restart; the round bar should return to `0 / 8` with no completed purple segments.
- Test a full eight-round match with a slow or unstable mobile connection.

## Project Structure

- `server.py` - HTTP server, WebSocket transport, room management, authoritative game rules.
- `index.html` - multiplayer lobby and game screen markup.
- `styles.css` - responsive game UI.
- `src/app.js` - browser WebSocket client, display metadata, and rendering.
- `assets/` - gem SVG artwork.
- `.github/workflows/ci.yml` - GitHub Actions CI for Python and browser-client tests.
- `requirements.txt` - Python dependency list, intentionally empty until external packages are added.
- `tests/test_server.py` - authoritative multiplayer unit tests.
- `tests/test_websocket.py` - HTTP and WebSocket integration tests.
- `tests/app.test.js` - dependency-free browser-client runtime tests.
