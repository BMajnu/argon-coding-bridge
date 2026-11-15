# ROUND 28 - ADDENDUM A: re-emit the delivery (the file was lost) + make the ledger/capture immutable

Your round-28 delivery was produced and verified, but **the file never reached the disk**:
the capture reported `Delivered patch file: C:\Users\TechZone IT\Downloads\apply_changes.ps1`
with `sha256=21566cf026f682eeb0af4c5b92d2c133451585fe0fe969dc2507f9a45f295ede`, yet a
search of every candidate file found **no file with that digest** - the path the log named
now holds an OLD delivery (31,633 bytes, created 2026-11-03). Please **re-emit the complete
`apply_changes.ps1`** for this round, and fix the capture/ledger defect described in W9
below so this cannot happen again.

## YOUR OWN NOTES ARE THE SPEC - keep every fix in them

You recorded five findings; they are exactly what the owner still sees broken. All of them
must land:

1. `round23_reliability.py:1690 & :1763` - `_PageShim.__init__` assigns
   `self.keyboard = _KeyboardShim(self)` while a duplicate read-only
   `@property def keyboard` (no setter) shadows it ->
   `AttributeError: property 'keyboard' ... has no setter` on **every**
   `browser.new_page()` under the CDP Playwright fallback shim. Remove the duplicate
   property so the Round 24 / 26 / 27 hermetic suites run under the shim again.
2. `extension/content_script.js:1399-1455` - `handleCompanionCommand` had TWO
   `if (act === "disarm_prompt_guard")` blocks and TWO `if (act === "get_runtime_state")`
   blocks; the first blocks returned early, so lines 1427-1455 were dead code and
   `version` / `sharedModelLabels` never reached `get_runtime_state` (which is why the
   loaded-vs-repo extension version check could not read the version). Merge them into
   single canonical handlers and alias `ARGON_MODEL_LABELS = SHARED_ARGON_MODEL_LABELS`.
3. `extension/content_script.js:58 & :811` - `getEffectiveBase64AttachCap()` was never
   called because `attachArchiveViaDataTransfer` called `getEffectiveBase64CapBytes()`.
   Unify them so the effective cap is honoured on both sides.
4. `dispatch_argon.py:9547-9590` - the self-test ran the Round 27 suite twice and never ran
   the Round 26 suite. Run `run_round26_suite` once and the Round 27 suite once.
5. `scripts/dual_dispatch.py:303-315` - the fallback `followup_chat_url` hardcoded `/u/0/`
   instead of resolving `_url_slot_for_account(followup_slot, isolated_profile=False)`.
   **This is why the follow-up landed on the wrong account.**

## W9 - The ledger and the capture must describe ONE immutable file

**MEASURED on the owner's machine, 2026-11-15.** Every project in
`~/.gemini/argon_delivery_ledger.json` has entries whose `short_hash` and `sha256` come
from DIFFERENT files, and whose recorded `file_path` no longer holds the file that was
hashed:

```
[Success Academy] 2026-11-15 14:08:56 chat=2b8fe0b78c64c937
    path      : G:\Developing\Success Academy\downloads\followup\apply_changes.ps1
    ledger sha: 1b3230a0d7aabe1a3e18dbe6...   short: 1b3230a0d7aabe1a
    file sha  : 75229c51160ef2ed780dddf1...   -> the file at that path is a DIFFERENT file
[Penflow] 2026-11-15 19:28:28 chat=2c4f36962d44a203
    ledger sha: 314cb864e8665e4b495211b5...   short: ccd182cf22e33414
    file sha  : ccd182cf22e334144e477d75...   -> the short hash and the full hash disagree
```

The same pattern appears for Success School, Penflow and this project. Consequences:

* the "same digest as round <id>" check compares a digest that does not describe the stored
  file, so **every round looks new** - the old delivery is applied again and the new one is
  never focused (this is the owner's "the old apply_changes.ps1 keeps downloading and I
  cannot focus on the new work");
* the shared paths `downloads/new/apply_changes.ps1` and `downloads/followup/apply_changes.ps1`
  are **overwritten by the next capture**, so a ledger entry's path stops pointing at its own
  file;
* the browser re-download of an old chip **overwrites the just-delivered file** - which is
  how this round's delivery was lost.

**Requirements**

- **R9.1** Both digests of a ledger entry MUST come from the SAME bytes: compute
  `short_hash` and `sha256` from the file the capture has just written, then **re-read that
  file from disk** and verify both digests still match. If they do not, the capture must
  REFUSE the entry (and say so) instead of recording a digest it cannot vouch for.
- **R9.2** Every delivery must be stored under an **immutable, round-scoped path**, e.g.
  `downloads/rounds/<round_id>/apply_changes.ps1` - never a shared `downloads/new/...` or
  `downloads/followup/...` path that the next capture or a browser download can overwrite.
  The ledger entry points at that immutable path.
- **R9.3** The capture must write to its own round-scoped path and must NOT let a browser
  download clobber it (copy it out of `~/Downloads` into the round directory, then verify).
- **R9.4** The "same digest as round <id>" check must compare the digest of the file that is
  about to be applied against the ledger's entries; an entry whose file no longer matches
  its digest must be treated as STALE (and reported), never as a valid duplicate.
- **R9.5** The capture must print the proof for EVERY delivery: the immutable path, the byte
  size, the sha256, the short hash, the chat id, the turn index and the round id.

## Reminder of the round-28 scope (unchanged)

The lane plan and the delivery selection from the round-28 brief stay in force: a follow-up
NEVER opens a new chat (it refuses by name when there is no recorded conversation); a
two-lane run is one continue + one fresh chat, verified before sending; the capture only
touches the newest assistant turn's chip; the installer picks the delivery by the LEDGER,
never by mtime; and `SKILL.md` (the system prompt) states all of it so an agent cannot get
it wrong.

**Deliver ONE `apply_changes.ps1` with: your five note fixes + W9 + the round-28 work
units, and re-emit the complete file.**
