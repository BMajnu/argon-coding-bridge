# Round 24 — Bridge Bug Audit (2026-11-14)

**Trigger:** the owner asked to verify the two bugs another agent reported, and then
attached the Phase-7 lane screenshots ("see the bugs as well").

Everything below was **measured on the owner's machine**, not inferred.

---

## 1. The reported bug #1 — a duplicate `--attach-images` kills the bridge

**Reported by:** the other session (their four project-side patch scripts could create a
second `--attach-images` option, and the plugin then died with
`conflicting option string`).

**CONFIRMED — and it is worse than reported.** The repository's OWN copy of that script
had the same defect:

`skills/argon-coding-bridge/scripts/patches/patch_bridge_attach_images.py`

```python
if "def attach_images(" in raw and '"--attach-images"' in raw:   # <- never true
    return {"ok": True, "status": "already_applied", ...}
anchor = '    parser.add_argument("--files", nargs="*", default=None, help="Optional target file patterns")'
```

The helper is really `attach_images_to_composer` (it lives in `round23_reliability.py`),
so `def attach_images(` never matched, the guard never fired, and the script appended a
**second** `--attach-images` registration.

Measured with the real argparse:

```
'--attach-images' in file : 1 -> 2
'--help' before            : rc=0
'--help' after             : rc=1
argparse.ArgumentError: argument --attach-images: conflicting option string: --attach-images
```

**Impact:** every invocation of the bridge dies — `--self-test`, `--help`, the dispatch
itself. A plugin self-update followed by "re-apply the four fixes" bricks the bridge.

## 2. The reported bug #2 — the images were attached AFTER the prompt was sent

**CONFIRMED for the old code** (the attach sat behind `if just_sent_prompt:`), and it
is **already fixed upstream** by commit `408943d`
("attach the screenshots BEFORE the send, and forward --attach-images").

Current tree: the attach happens inside `_execute_composer_send_sequence()`
(`dispatch_argon.py:7810-7822`), between the archive attach and the send click; the
post-send attach is gone.

## 3. The bug the screenshots exposed — 8 PNGs promised, 1 delivered

The Phase-7 lane brief says *"I have also attached eight PNG screenshots"*. The lane
table says **"১টি ছবি"** — one image.

**Root cause:** `attach_images_to_composer()` called CDP `DOM.setFileInputFiles` **once
per image** with `files: [one_path]`. That call **REPLACES** the file input's file list.

Measured on a throwaway Chrome (hermetic, `scratch/dup_test/cdp_file_input_probe.py`):

```
A) 8 sequential DOM.setFileInputFiles calls -> input holds 1 file(s)
B) ONE DOM.setFileInputFiles call with all 8 paths -> input holds 8 file(s)
```

So the bridge printed *"Attached 8 image(s)"* while Gemini received **one**.

A second defect in the same function: the accept-matching compared the file **suffix**
(`".png"`) against the accept attribute (`"image/*,video/*"`), which never matches — so
the image input was never selected, and whichever input came first in the DOM won.

**Fixed** in `round23_reliability.py`: one CDP call with the whole list, a MIME-aware
input picker, and a read-back of the number the browser really holds. The bridge can no
longer report a lie (`attach_images_to_composer` returns the real count and fails loudly
when the composer holds fewer files than requested).

## 4. The bug the screenshots exposed — the wrong account slot

The owner: *"you said you sent the request on u5 and u9 but I can see the request on
u4, u5, and u8."*

**The bridge is right; the brief was wrong.** `--slot /u/N/` is a **pool slot**, not the
browser's `/u/<index>/`. The pool maps them like this (`config/accounts_pool.json`):

| pool slot | account | browser index |
|---|---|---|
| `/u/0/` … `/u/6/` | … | `/u/0/` … `/u/6/` |
| `/u/7/` | badiuzzamanmajnu786@gmail.com | **needs sign-in** |
| `/u/8/` | bmajnu786@gmail.com | `/u/7/` |
| `/u/9/` | majnu.charitabari@gmail.com | `/u/8/` |
| `/u/10/` | takitahamid1000@gmail.com | **needs sign-in** |

So `--slot /u/9/` correctly opens `https://gemini.google.com/u/8/app` — the Phase-7 prompt
was sent under **majnu.charitabari@gmail.com**, not under a browser `/u/9/`. **The browser
has no `/u/9/` at all** (9 accounts, indices 0–8).

The bridge already prints the mapping:

```
[+] Argon browser account index for slot /u/9/ (majnu.charitabari@gmail.com): /u/8/ ...
```

The lane brief labelled the lanes with pool-slot numbers as if they were browser URLs.
**Fix needed in the brief (and a clearer CLI), not in the resolver.**

## 5. The re-apply patch scripts were the real "full of bugs" — 3 of 4 could kill the bridge

Audited against the current tree (copies only):

| script | before | after |
|---|---|---|
| `patch_bridge_attach_images.py` | **+1 line → duplicate `--attach-images` → argparse conflict** | no-op |
| `patch_bridge_task_toggle.py` | no-op (correct) | no-op |
| `patch_bridge_large_archive.py` | **+5 lines → installs a cap that changes the archive attach route** | verify-only (`--apply` to install) |
| `patch_bridge_cdp_setfiles.py` | **+29 lines → writes invalid Python → SyntaxError → the bridge will not import** | no-op |

`patch_bridge_cdp_setfiles.py` wrote its helper as a plain (non-raw) triple-quoted
string, so the inner `\"` escapes were consumed and the generated file contained:

```python
"expression": "(() => { const i = document.querySelector("input[type='file']"); ...
```

→ `SyntaxError: invalid syntax. Perhaps you forgot a comma?`

The common root cause: **the guards looked for the fixes in `dispatch_argon.py`, but the
fixes moved to `round23_reliability.py`** — a broken guard means "apply again", not
"do nothing".

## 6. What was changed in this round

* `scripts/patches/_safe_patch.py` (new) — shared helpers. Every write is
  **compile-verified and atomic** (temp file → `py_compile` → `os.replace`), and the
  original line endings are preserved.
* `scripts/patches/patch_bridge_attach_images.py` — counts the real
  `parser.add_argument(...)` registrations, never adds a second one, and **repairs** a
  file that already carries the duplicate.
* `scripts/patches/patch_bridge_cdp_setfiles.py` — checks the fix where it lives
  (`round23_reliability.py`); the fallback snippet is a RAW string, so the generated
  Python is valid.
* `scripts/patches/patch_bridge_large_archive.py` — verify-only by default.
* `scripts/patches/patch_bridge_task_toggle.py` — same safe write path.
* `scripts/patches/apply_bridge_four_fixes.py` — checks each fix where it really lives
  (the old version matched `DOM.setFileInputFiles` inside a *help string* — a false
  positive — and missed the base64 cap — a false negative).
* `scripts/round23_reliability.py` — the multi-image CDP fix.
* `scripts/test_round24_bugfixes.py` (new) — 5 hermetic regression cases.

## 7. Still open (needs the owner's call / an Argon round)

1. **The 8 MB base64 cap is dead code.** `round23_reliability.py` exports
   `MAX_BASE64_ATTACH_BYTES` and `dispatch_argon.py` imports it (line 8688) — but
   `_attach_zip_via_datatransfer()` never reads it, so an archive up to 64 MB is still
   base64-encoded into the page. Installing the 15 MB cap changes the archive attach
   route for 15–64 MB archives (from the companion-extension route to the Playwright
   `set_input_files` route — which is the route that carried the 260 MB Phase-7
   archive). Owner decision.
2. **The pool-slot vs browser-index trap** — the brief must use the resolved browser
   URL, and the CLI should be able to say "browser index N" explicitly.
3. **A 260 MB archive in one lane** — the upload window and the chat state (a chat that
   already holds a big attachment cannot take another) need a rule.

## 8. Test evidence

```
test_round20_lifecycle_and_sync.py   PASS 5/5 (3 consecutive runs)
test_round21_dispatch_lane.py         PASS 5/5
test_round22_extension_install.py     PASS 5/5
test_round23_reliability.py            PASS (all 8 rules)
test_round24_bugfixes.py               PASS 5/5
```

## 9. Round 24 outcome + three NEW findings from the live run

The round-24 brief (`2026-11-14_round24_brief.md`) was sent ONCE at 10:41:18 and Argon's
delivery (140,865 bytes, sha256 `688317e5…`, pre-flighted 31/31 hunks, 0 glued
terminators) was applied with the bridge's own `Run-ArgonPatch.ps1`. It landed the three
work units (commit `ebc5627`) and four extra fixes Argon found while reading the project.

**NEW FINDING A — the composer is NOT cleared after a verified send.  → FIXED in this round.**
The 35,724-character prompt stayed in the composer after the send (the bridge's own log:
`prompt paste (extension) : OK (35724 chars in composer)` -> `sent` -> the composer still
held the text). It LOOKED like a pending turn, and the send arrow stayed armed - so any
later click or Enter fired the SAME prompt again. **Fixed:** the new helper
`_clear_leftover_prompt_after_send()` empties the composer after a VERIFIED send in BOTH
send paths (the companion extension's semantic matcher and the pure-Python fallback). It
only removes OUR OWN leftover (foreign text is left alone), and on the Python path it only
runs when the probe-snippet count grew, i.e. when the prompt is proven to have landed in
the transcript. Covered by `test_round24_bugfixes.py` case_6 (a synthetic Gemini page
whose composer keeps the text after the send - exactly Gemini's Task mode).

**NEW FINDING B — a second turn can appear as a `queued-steering-turn`.**
The live DOM of the chat (`gemini.google.com/u/2/app/c4d7ab7a723d592b`) held TWO
`user-query` elements: the sent one (parent class `turn-content-visibility`) and a second
one whose parent class is **`queued-steering-turn`** - a message submitted WHILE Gemini was
already working. Counting `user-query` elements is therefore NOT a send count; the
authoritative record is the bridge's own run log (`PROMPT SENT at <time>` appears once).
The bridge must never treat a queued steering turn as a new send.

**NEW FINDING C — `--resume` can pick up a STALE delivery.**
`dispatch_argon.py:11122` short-circuits when the task state still carries a `delivered`
path from an earlier round: this run recorded round 13's byte-identical
`downloads/dual/round13_new/apply_changes.ps1` as THIS round's delivery (the
`_is_leftover_delivery` defect Argon fixed in this round). **Recovery:** clear
`delivered` / `delivery_hash` for the project and its lane in
`~/.gemini/argon_task_state.json` (keep a `.bak`), then re-run
`--resume --no-carry-over` - it then waits for the real chip and captures it.

