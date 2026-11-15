# ROUND 27 ADDENDUM — the CORE and the WEB EXTENSION must be fixed AS ONE PIECE

This addendum completes Round 27. **Do not stop after the round-27 work: the fixes have to
land in BOTH codebases at once, or the product is still broken.**

## The two codebases - both are in the archive you received

| | path in `argon_context.zip` | what it is |
|---|---|---|
| **Argon Core** | `skills/argon-coding-bridge/scripts/` (`dispatch_argon.py`, `round23_reliability.py`, `safe_browser.py`, `argon_pool_login.py`, the `patches/` scripts) | the automation core: the CLI, the slot/pool policy, the send pipeline, the delivery capture |
| **Argon Web Extension** | `skills/argon-coding-bridge/extension/` (`content_script.js`, `service_worker.js`, `manifest.json`) | the MV3 extension that runs IN the Gemini page: paste, attach, model/mode/send, delivery capture |

Both are in the archive - verify it before you start:
`skills/argon-coding-bridge/extension/content_script.js` and
`skills/argon-coding-bridge/scripts/dispatch_argon.py`.

**Every item of Round 27 that touches the browser has a side in BOTH codebases.** A fix that
only lands in the Core leaves the bug alive, and a fix that only lands in the extension
leaves the bridge unable to use it. The six contracts below are the checklist.

---

## CONTRACT 1 — The send: the extension must disarm, the Core must never send twice

*Extension side* (`extension/content_script.js`): round 26 already turned `keepPromptAlive`
off and cleared `cachedPromptText` when `click_send` performs the send, and `click_send` now
reports `ok: true` once it has clicked. **Keep that.**

*Core side* (`dispatch_argon.py`): when the **pure-Python fallback** performs the send, the
extension never performed one - so its observer can still re-paste the leftover afterwards.

- **R1.1** Add ONE new content-script operation, e.g. `disarm_prompt_guard`, that sets
  `keepPromptAlive = false` and `cachedPromptText = ""` and stops the observer from
  re-pasting. The Core must call it after EVERY verified send - the extension's
  `click_send` path AND the pure-Python path - through the same
  `argon.companion.v1` protocol it already uses.
- **R1.2** The operation name, its payload and its return shape must be IDENTICAL on both
  sides. Document the protocol in `extension/README.md` (the operations table) so the two
  sides cannot drift apart again.
- **R1.3** The extension's observer must also stop re-pasting when the conversation already
  contains a `queued-steering-turn` (the Core detects that class - see Round 27 W5 and the
  Round 26 work). One DOM truth, understood by both sides.

## CONTRACT 2 — The sizes: 100 MB is the archive limit, 8 MB is only the base64 transport

- **R2.1** The hard archive limit is **100 MB** and lives in ONE place
  (`round23_reliability.MAX_ARCHIVE_BYTES`). The Core enforces it; the extension must NOT
  have a second, different number.
- **R2.2** The extension's `MAX_BASE64_ATTACH_BYTES` (8 MB, `content_script.js:17`) is a
  **base64 transport** limit, not an archive limit. When the extension's `attach_archive`
  refuses a file because of it, it must return the NAMED reason `size_cap_exceeded` so the
  Core can fall through to the zero-copy route instead of failing the attach. Today the two
  sides call the same situation different things, which is why the logs are confusing.
- **R2.3** Both sides must read the SAME effective cap (`get_effective_base64_attach_cap()`
  / the `ARGON_MAX_BASE64_ATTACH_MB` environment variable) - the extension must not hard-code
  8 MB while the Core says something else.

## CONTRACT 3 — The model chip: the Core must not parse the label on its own

- **R3.1** The Core's chip reader and the extension's `RE_ARGON` matcher must agree on the
  model names. The UI is localised: the chip reads `কোড ও বিশ্লেষণ 4 Argon` on a Bengali
  account and `Code & analysis 4 Argon` on an English one (measured 2026-11-14 on the same
  machine, two accounts). A single shared label list (or the Core asking the extension via
  `ensure_mode_and_model` instead of parsing the DOM) - one truth, both sides.
- **R3.2** The continuation-page chip wait (Round 27 W7) belongs to BOTH: the Core waits for
  the chip, the extension's `ensure_mode_and_model` must also wait on a slow-hydrating
  conversation instead of giving up after a single snapshot.

## CONTRACT 4 — The delivery capture: one chip, one proof

- **R4.1** The Core picks the LAST turn's generated-file chip and downloads it with the REAL
  `Export -> Download`; the extension's `capture_delivery` walks the Drive `viewer/text`
  pages. Both must agree on WHICH file is "the delivery" (the newest turn's
  `apply_changes.ps1`), and the Core prints the chat id, the turn, the file name and the
  SHA-256.
- **R4.2** When the SHA-256 equals a file captured earlier for the same project, the Core
  says so (`same digest as round <id> - Argon re-attached the same file`) instead of
  silently accepting it.

## CONTRACT 5 — The extension version must be checked against the repo

- **R5.1** This is the trap that cost the owner hours on 2026-11-14: the fixes were in the
  repo and in the plugin roots, but the RUNNING browser still had the OLD extension, so the
  duplicate kept happening. The Core must compare the version of the LOADED extension
  (`Extensions.getExtensions` + the content script's `data-argon-companion-extension`
  marker) with the version in `extension/manifest.json`, and when they differ it must say so
  loudly: `the browser is running extension 1.16.0 but the repo has 1.17.0 - restart the
  Argon browser before dispatching`.
- **R5.2** Bump `extension/manifest.json`'s version with this round's changes (the Core's
  copy of the expected version must match), so this trap is visible next time.

## CONTRACT 6 — One prompt, one turn, on both sides

- **R6.1** The Core counts the user turns after the send and prints
  `DUPLICATE SEND DETECTED (<n> user messages for one prompt)` when the count grew by more
  than one. The extension's observer must never be the cause (see Contract 1).
- **R6.2** The two-lane rule applies to both sides: the `followup` lane continues the
  recorded conversation, the `new` lane opens a new one, and both may run at the same time
  on different slots. The Core owns the routing; the extension must not care which lane it
  is in - but it must never re-paste a prompt that is already in the conversation.

---

## Acceptance (add to Round 27's list)

```
python skills/argon-coding-bridge/scripts/test_round27_two_lane.py   # NEW: both lanes + the contracts above
python skills/argon-coding-bridge/scripts/test_round26_composer_clear.py # 5/5
python skills/argon-coding-bridge/scripts/test_round24_bugfixes.py     # 6/6
python skills/argon-coding-bridge/scripts/test_round24_workunits.py   # 3/3
python skills/argon-coding-bridge/scripts/test_round23_reliability.py  # all
python skills/argon-coding-bridge/scripts/test_round22_extension_install.py # 5/5
python skills/argon-coding-bridge/scripts/test_round21_dispatch_lane.py # 5/5
python skills/argon-coding-bridge/scripts/test_round20_lifecycle_and_sync.py # 5/5
python skills/argon-coding-bridge/scripts/dispatch_argon.py --self-test # exit 0
```

The new suite must include at least one case PER CONTRACT above - especially: the Core
calls `disarm_prompt_guard` after a pure-Python send and the extension's observer stays
disarmed; the extension returns `size_cap_exceeded` (not a generic failure) for a file that
is too big for base64 but small enough for the 100 MB archive limit; the Core detects a
loaded extension that is OLDER than the repo and says so.

**Everything lands in ONE `apply_changes.ps1`**, with the Core files AND the extension files
in it. Deliver the whole thing; do not stop half way and do not wait for a follow-up.
