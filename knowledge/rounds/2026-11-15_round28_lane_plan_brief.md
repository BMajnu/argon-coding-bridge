# ROUND 28 — Argon Coding Bridge: a follow-up MUST continue the recorded chat, the two-lane plan MUST be verified, and the delivery MUST never be the old file again

You are working in the real project:

```
G:\Developing\argon-coding-bridge
```

The complete project codebase is attached as `argon_context.zip` (it contains
`TASK_BRIEF.md` and `API_INDEX.md` — **read both in full before you touch anything**).

**This round is about the LANE PLAN and the DELIVERY SELECTION. Seven work units, one
`apply_changes.ps1`.** Every rule below exists because it broke a real run on the owner's
machine on 2026-11-15.

---

## PART A — WHAT THE OWNER SAW (measured, not guessed)

**F1 — A follow-up opened a NEW chat instead of continuing the recorded one.**
On 2026-11-15 the owner asked for a follow-up in the last winning conversation. The bridge
sent a **brand-new session** instead. Two screenshots show two different accounts
(`gemini.google.com/u/5/app` and `gemini.google.com/u/8/app`) each holding the SAME
prompt and the SAME `argon_context.zip` chip - i.e. **two NEW lanes**, never a follow-up.

**F2 — The lane label lied.** The run recorded itself as `followup` while it actually
opened a new chat. Round 27 added a per-lane `SessionStore.lookup(..., lane=..., slot=...)`
with a `TaskState` fallback, but when the lookup finds NOTHING it still **falls through to
the new-chat branch** instead of refusing.

**F3 — The delivery that gets downloaded is the OLD one, again and again.**
The owner's browser download list shows the same files coming down again and again:
`245 KB (1 minute ago)`, `236 KB (2 minutes ago)`, `533 KB (3 minutes ago)`,
`245 KB (1 hour ago)`, `17.1 KB (1 hour ago)`. The old `apply_changes.ps1` is re-downloaded
instead of the new one, so the new work can never be focused on.

**F4 — The one-click installer then applies the OLD file.**
`skills/argon-coding-bridge/scripts/Run-ArgonPatch.ps1:72`:

```powershell
$newest = ($items | Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1)
```

The patch is picked by **newest modification time**. A file that was just re-downloaded is
by definition the newest - so re-downloading the old delivery makes the installer apply the
OLD patch again. That is the "the focus never lands on the new work" loop.

**F5 — The manual path cannot attach and cannot focus either.** When the agent does the
send by hand, it opens the old chat history, sends there, and then downloads whatever the
browser offers - which is the previous round's chip.

---

## PART B — THE SEVEN WORK UNITS

### W1 — A follow-up NEVER opens a new chat

- **R1.1** `--lane followup` must open the conversation recorded for that lane
  (`~/.gemini/argon_task_state.json` -> `lanes['followup'].chat_url` / `chat_id`, or the
  per-lane `SessionStore` entry) and continue it.
- **R1.2** When there is NO recorded conversation, or the recorded one cannot be opened,
  the run must **STOP with a named reason**
  (`followup has no recorded conversation for this project - refusing to start a new chat`)
  and exit non-zero. It must **NOT** silently fall through to a new chat (F1, F2).
- **R1.3** The lane that actually ran must be printed truthfully, e.g.
  `lane: followup -> continuing chat 45c57d433a71461d (https://gemini.google.com/u/5/app/45c57d433a71461d)`
  or `lane: followup -> REFUSED (no recorded conversation)`. The label and the behaviour may
  never disagree.
- **R1.4** The same rule for `--append-followup`: it appends to the RUNNING task's chat; if
  there is no running task it must refuse by name.

### W2 — The two-lane plan is VERIFIED before anything is sent

- **R2.1** A two-lane run means EXACTLY: one lane that CONTINUES a recorded chat (the
  follow-up, on the account that holds the conversation) + one lane that opens a FRESH chat
  (the new lane, on another account). **Two new lanes are never correct.**
- **R2.2** Before the first send the bridge must resolve the plan and print it:
  `lane plan: followup -> continuing <chat id> on <slot> | new -> fresh chat on <slot>`.
  If the plan cannot be satisfied (no recorded chat for the follow-up, or the two lanes would
  use the same slot) the run must abort with a named reason BEFORE sending anything.
- **R2.3** After the send the bridge must verify the plan actually happened: the follow-up's
  chat id must be the RECORDED one and the new lane's chat id must be a NEW one. If the
  follow-up ended up on a new chat, that is a hard failure - print
  `lane plan VIOLATED: the follow-up opened a new chat` and do not accept the delivery.
- **R2.4** The EXIT 9 guard must stay scoped to the same lane AND the same slot, so a
  follow-up on one account and a new lane on another can run at the same time.

### W3 — The delivery capture may only take the NEWEST turn's chip

- **R3.1** The capture must consider ONLY the chips of the newest assistant turn of the
  target conversation. An earlier round's chip is never opened (the rule exists in
  `SKILL.md`; make it enforced in code and prove it with a test).
- **R3.2** A downloaded file whose SHA-256 equals a `DeliveryLedger` entry of an EARLIER
  round must be refused by name
  (`this is round <id>'s delivery (sha256 ...), not a new one - the capture refuses it`)
  instead of being accepted as the current delivery.
- **R3.3** The capture must print the proof on every success: chat id, turn index, file name,
  byte size, SHA-256 - and the ledger entry it created.

### W4 — The hand-off pack must make the right file unmistakable

- **R4.1** `manual_handoff.md` / `handoff.json` must carry: the exact chat URL to send in,
  the lane, the account e-mail, the prompt file path, the ZIP path, the expected delivery
  file name, and **the SHA-256 of every earlier delivery** so a human (or an agent) can tell
  the new file from the old ones at a glance.
- **R4.2** It must say, in one line, which chip to download: the chip in the NEWEST
  assistant turn of THAT conversation - never an earlier chip and never an item from the
  browser's download list (the download list is not the delivery).
- **R4.3** The pack must state that the ZIP has to be attached in the composer and that the
  attachment chip must be visible BEFORE the prompt is sent.

### W5 — The installer must pick the delivery by the LEDGER, never by mtime

- **R5.1** `Run-ArgonPatch.ps1` (and `Open_Argon_Apply.bat`) must NOT choose the patch by
  `LastWriteTime` (F4). It must ask the `DeliveryLedger` / `TaskState` which file belongs to
  the current task and use THAT file.
- **R5.2** When several candidate files exist, it must print the candidates with their
  SHA-256 and the ledger's decision, and refuse to guess. With `-PatchFile` given
  explicitly, that file wins.
- **R5.3** A file whose SHA-256 equals an OLDER ledger entry must be refused with the round
  it belongs to, unless the operator passes `-Force`.

### W6 — The SYSTEM PROMPT (`skills/argon-coding-bridge/SKILL.md`) must be rewritten

The agent reads this file instead of the code, so it must not be able to get the lane plan
wrong. Required changes:

- **R6.1** The follow-up step must use `--lane followup` EXPLICITLY (today the example omits
  it) and must state in bold: **a follow-up NEVER opens a new chat; if it cannot continue
  the recorded conversation it STOPS and reports - it never sends a new session instead.**
- **R6.2** The two-lane recipe must be stated explicitly: follow-up (recorded chat, its own
  account) + new (fresh chat, another account) - **never two new lanes** - and the agent must
  run one lane first, read its step screenshot, and only then the other.
- **R6.3** The manual-download rule must name the target chip (the newest assistant turn of
  that conversation), forbid the browser's download list, and require a SHA comparison
  against the ledger before applying.
- **R6.4** The manual send must attach the ZIP and verify the attachment chip is visible
  before sending, and it must send into the RECORDED conversation (quoted URL), not into
  whatever chat happens to be open.
- **R6.5** The troubleshooting table must gain rows for: *the follow-up opened a new chat*,
  *the old delivery was downloaded again*, and *the installer applied an older patch*.
- **R6.6** The workflow must state that the agent reads the step screenshot of EVERY step
  before acting, and that a lane-plan mismatch is fixed BEFORE the second lane is sent.

### W7 — Tests

- **R7.1** New hermetic suite `skills/argon-coding-bridge/scripts/test_round28_lane_plan.py` with at least one case per work unit:
  - the follow-up continues the recorded chat, and REFUSES (it does not open a new chat)
    when there is no recorded conversation;
  - a two-lane plan is one continue + one new, and a plan that cannot be satisfied aborts
    before sending;
  - the capture takes the newest turn's chip and refuses an older digest;
  - the installer picks the ledger's file and not the newest mtime;
  - the hand-off pack names the chat URL, the expected file and the older digests.
- **R7.2** Wire it into `--self-test` like the other suites.

---

## PART C — HARD RULES

- **R8.1** Read `TASK_BRIEF.md` and `API_INDEX.md` from the archive before writing.
- **R8.2** **Surgical edits only.** Exact, unique anchors. Never rewrite a whole file, never
  reformat. The bridge's UI must not change by one pixel.
- **R8.3** **Do not touch**: the live account pool file, the account model, the profile
  layout, the delivery bundle format (`argon_delivery.zip` + `DELIVERY_MANIFEST.md`),
  `skills/argon-coding-bridge/extension/` (the MV3 companion), `skills/argon-coding-bridge/live_loop/`,
  `knowledge/rounds/**`, or the round-24/26/27 work (commits `909a302`, `ebc5627`, `1bb891f`,
  `64eafc6`, `17d7033`).
- **R8.4** No existing CLI flag may change its meaning. New flags are additive.
- **R8.5** Every write must be compile-verified before it lands, exactly like
  `scripts/patches/_safe_patch.py` does (temp file -> `py_compile` -> atomic replace).
- **R8.6** The model is `Code & analysis 4 Argon`. Never Flash, never Hydrogen.
- **R8.7** Keep the model guard, the strict slot resolver, the wrong-conversation guard, the
  round-24 work units, the round-26 composer/extension fix and the round-27 two-lane work
  exactly as they are.

### PART D — THE DELIVERY CONTRACT

One **linear ordinal PowerShell script**: Phase 1 pre-flight with
`[System.StringComparison]::Ordinal` `IndexOf` + `[System.StringBuilder]` (abort the whole
script and write NOTHING if any block is missing or ambiguous); Phase 2 apply with a backup
per file and a `[Verified] <path>` line after re-reading every written file; every here-string
terminator `'@` on its own line; a short summary of the files touched, the new test file
and the verification commands.

### PART E — ACCEPTANCE CRITERIA (the owner runs these)

```
python skills/argon-coding-bridge/scripts/test_round28_lane_plan.py    # NEW
python skills/argon-coding-bridge/scripts/test_round27_two_lane.py     # 3/3
python skills/argon-coding-bridge/scripts/test_round26_composer_clear.py # 5/5
python skills/argon-coding-bridge/scripts/test_round24_bugfixes.py     # 6/6
python skills/argon-coding-bridge/scripts/test_round24_workunits.py   # 3/3
python skills/argon-coding-bridge/scripts/test_round23_reliability.py  # all
python skills/argon-coding-bridge/scripts/test_round22_extension_install.py # 5/5
python skills/argon-coding-bridge/scripts/test_round21_dispatch_lane.py # 5/5
python skills/argon-coding-bridge/scripts/test_round20_lifecycle_and_sync.py # 5/5
python skills/argon-coding-bridge/scripts/dispatch_argon.py --self-test # exit 0
```

**Acceptance = every command passes, the patch is surgical, and nothing outside the allowed
file list changed.** Deliver all seven work units in ONE `apply_changes.ps1`.
