# Argon Coding Bridge Companion Extension (MV3)

This directory (`skills/argon-coding-bridge/extension/`) contains the unpacked Manifest V3
companion extension loaded into the bridge's Chrome slot profile (`--disable-extensions-except`
and `--load-extension`).

## Why the Companion Extension Exists (and What a Custom Chromium Build Adds)

Moving file attachment (`DataTransfer` `File` injection without the Windows OS file dialog),
prompt pasting (synthetic `DataTransfer` `paste` events without origin clipboard permissions),
semantic model/mode/send matching (`MutationObserver` + composer-scoped semantic matchers
instead of brittle CSS classes), and paged Drive `viewer/text` delivery capture into this
companion extension fixes all four recurring DOM and OS failures inside existing Chrome today.
A custom Chromium build does not fix Gemini's DOM or the Windows file dialog on its own, so it
is documented strictly as an optional hardening layer that would only add browser version
pinning, a pre-installed companion extension immune to Chrome auto-update permission changes,
and `--remote-debugging-pipe` transport in place of a local TCP debugging port.

## Operations Owned by the Extension

| Operation | Action Name | How It Works |
|---|---|---|
| **Attach archive** | `attach_archive` | Service worker fetches the archive bytes (or receives base64 chunks) and injects a `File` into `input[type=file]` via `DataTransfer` — never opening the native Windows file dialog. |
| **Paste prompt** | `paste_prompt` | Selects the lowest visible composer (skipping the floating quick-action bar editor) and dispatches a synthetic `ClipboardEvent('paste')` carrying `text/plain` in a `DataTransfer` — no clipboard permission or `Control+V`. |
| **Mode + Model + Send** | `ensure_mode_and_model`, `click_send` | Installs a `MutationObserver` guarding against model-chip reversion to `Flash` and upload composer clearing, verifies `Task` (`টাস্ক`) and `4 Argon` semantically, and clicks the semantic send control inside the composer's own container. |
| **Delivery capture** | `capture_delivery` | Calls Drive's `viewer/text?id=<FILE_ID>&authuser=0&page=<N>` endpoint with session cookies and walks `page=0, 1, 2...` until empty, stitching all pages into the full `apply_changes.ps1`. |
| **State persistence** | `get_state`, `set_state` | Stores run metadata and captured Drive file IDs in `chrome.storage.local`. |

## Automatic Pure-Python Fallback

The extension is an accelerator, never a single point of failure. When the extension is absent
or disabled (`ARGON_DISABLE_COMPANION_EXTENSION=1`), `dispatch_argon.py` logs that the fallback
path is active and executes the pure-Python path automatically.

Note: This companion extension stays strictly Gemini-only (`gemini.google.com` and `drive.google.com`); general-purpose `<all_urls>` live browser control lives in its own separate extension under `skills/argon-coding-bridge/live_loop/extension/`.
