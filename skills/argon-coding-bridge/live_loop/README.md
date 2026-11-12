# Argon Live Browser Control Loop (`live_loop/`)

This directory contains the general-purpose agent-to-browser live control loop introduced in Round 19 (`argon.live.v1`).

## Why Two Lanes Share One Map Builder

`live_map.js` is a standalone browser IIFE (zero Chrome APIs, zero Node APIs) that builds a compact map of interactive elements on any webpage (`a[href]`, `button`, `input`, `select`, `textarea`, interactive ARIA roles, `[contenteditable]`, `[onclick]`, `[tabindex]`), watches the DOM via a debounced `MutationObserver`, and pushes every update immediately to the agent:

1. **Lane A — CDP (Primary, Zero-Install on the Argon Browser)**:
   - `skills/argon-coding-bridge/scripts/browser_loop.py` attaches to the Argon browser's existing `--remote-debugging-port` endpoint (`.argon_browser_endpoint`) via `chromium.connect_over_cdp`.
   - On every tab (existing and newly opened via `context.on("page", ...)`), it installs `page.expose_binding("__argonPush", cb)` (`Runtime.addBinding` over CDP) and `page.add_init_script(live_map.js)`.
   - Whenever the DOM mutates, `live_map.js` calls `window.__argonPush(JSON.stringify(map))`, pushing the interactive element map to Python in ~12 ms without polling.
   - Every action (`click`, `type`, `press`, `select`, `scroll`, `navigate`, etc.) re-verifies the target element's `tag` and `name` against `expect` (the stale-element rule) and returns the **fresh map in the same response**.

2. **Lane B — Separate MV3 Extension (`live_loop/extension/`, Secondary for Everyday Chrome)**:
   - The companion extension in `skills/argon-coding-bridge/extension/` stays strictly Gemini-only (`gemini.google.com`, `drive.google.com`, `127.0.0.1`, `localhost`).
   - To drive an everyday Chrome window on any website without widening the Gemini companion extension, load `skills/argon-coding-bridge/live_loop/extension/` as a separate unpacked extension.
   - `live_content_script.js` builds the map using the exact same `live_map.js` logic and holds both an open `chrome.runtime.connect({ name: "argon.live.v1" })` port (resetting the MV3 service worker idle timer) and `chrome.runtime.sendMessage` relay.
   - `live_service_worker.js` holds a single WebSocket to `ws://127.0.0.1:<port>/live?token=<token>` (free from page CSP `connect-src` restrictions) and stores only the daemon URL and token in `chrome.storage.local`.

## Installing the Optional Lane B Extension in Everyday Chrome

1. Start the daemon and note the printed `ws_endpoint` and token prefix (the full token is written to `~/.gemini/argon_browser_loop.json`):
   ```powershell
   python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-loop-start
   ```
2. Open `chrome://extensions` in Chrome, enable **Developer mode**, click **Load unpacked**, and select:
   ```text
   skills/argon-coding-bridge/live_loop/extension
   ```
3. In the extension service worker console (or via `chrome.runtime.sendMessage`), configure the daemon WebSocket URL and token from `~/.gemini/argon_browser_loop.json`:
   ```js
   chrome.runtime.sendMessage({
     protocol: "argon.live.v1",
     type: "configure",
     wsUrl: "ws://127.0.0.1:<port>/live",
     token: "<64-char-hex-token>"
   });
   ```

## CLI Commands

```powershell
# Run the end-to-end self-test on a disposable Chrome instance:
python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-loop-selftest

# Start the live loop daemon against the Argon browser:
python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-loop-start

# Check daemon status and the live map per tab:
python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-loop-status

# Execute a single action (uses running daemon if active, or one-shot CDP attach):
python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-act '{"op":"map"}'
python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-act '{"op":"click","id":0,"expect":{"tag":"a","name":"Learn more"}}'

# Stop the running daemon:
python skills/argon-coding-bridge/scripts/dispatch_argon.py --browser-loop-stop
```