# ROUND 27 — Argon Coding Bridge: make the TWO-LANE system work, and kill the last duplicate sends

You are working in the real project:

```
G:\Developing\argon-coding-bridge
```

The complete project codebase is attached as `argon_context.zip` (it contains
`TASK_BRIEF.md` and `API_INDEX.md` — **read both in full before you touch anything**).

**This round is about the two-lane system and the duplicate send. Six work units, one
`apply_changes.ps1`.**

---

## PART A — THE MEASURED FACTS (2026-11-14, on the owner's machine)

**F1 — The follow-up lane opens a NEW conversation and still calls itself a follow-up.**
The round-26 task state after one round and one follow-up:

```
lane new      : chat_id=d0815dd9c06ea36d  sent_at=12:54:58
lane followup : chat_id=ba4a3e1d144bcbc4  sent_at=13:06:38
```

Two different chats, both on `/u/4/`, both recorded as the same project. The follow-up
was supposed to CONTINUE `d0815dd9c06ea36d`; it started a fresh conversation instead.
The owner sees it as "it says it is follow up but it is new lane" - and it is: the lane
LABEL and the actual conversation disagree. **The 2-lane system therefore collapses into
one lane.**

**F2 — Two lanes cannot run at the same time: the guard fires with EXIT 9.**
`dispatch_argon.py:14183` builds the running-task key as

```python
    running_task_id = str(reg_entry.get("task_id") or f"{proj_root.name}:{eff_lane}:{eff_slot.strip('/')}")
```

and `TaskRegistry.register_task_start()` (`dispatch_argon.py:4986`) uses
`f"{Path(proj_str).name}:{lane_str}:{norm_s.strip('/')}:{int(now_ep)}"`. The project-level
guard ("A task is ALREADY RUNNING for project ... (state='waiting')") then refuses the
second lane with **exit code 9** (`EXIT_TASK_ALREADY_RUNNING`), even when the second lane
runs on a DIFFERENT slot. Reproduced on 2026-11-14 at 12:54: a follow-up for a project
whose "new" lane was still waiting was refused with exit 9 and the message
*"Sending another prompt now would SUPERSEDE the unfinished task or block the account."*

**F3 — The registry is full of dead entries, so healthy accounts look busy.**
`~/.gemini/argon_tasks.json` holds **28-33 entries per slot**; most are from 2026-11-06/07
and were never closed. The bridge PRINTS them as stale on every single dispatch
(`[!] STALE tasks detected in ~/.gemini/argon_tasks.json (started but never marked completed)`)
and then counts them anyway. Measured live:

```
slot     active  available
/u/1/       3   False   <- the slot the owner asked for
/u/3/       2   False
/u/9/       2   False
/u/2/       0   True
```

and `_pool_slots_in_order()` (`dispatch_argon.py:14282-14292`) re-sorts the list by
registry load, so the requested slot was pushed to position 9 and `slots[0]` became
`/u/2/`. The lane then ran on ANOTHER Google account while the log claimed
`Chosen account slot: /u/2/ (explicitly requested slot /u/2/)` - which is false, the
owner had asked for `/u/1/`.

**F4 — The delivery capture can pick the WRONG chip.** On 2026-11-14 the capture of the
round-26 "new" lane recorded `sha256=474c4aa5e30816a8...` - the digest of the FOLLOW-UP
lane's file. The ledger printed `Duplicate: round 20261114_133624 (chat=ba4a3e1d144bcbc4,
captured_at=13:36:24)`. The DOM order is not a reliable way to pick the newest chip.

**F5 — The size limits are THREE different numbers.** Measured in the code:
- the HARD archive cap: `MAX_ARCHIVE_BYTES` = **100 MB** (`round23_reliability.py`,
  enforced in `dispatch_argon.py:7020-7027` as `archive_size_cap_exceeded`);
- the base64 attach cap: `MAX_BASE64_ATTACH_BYTES` = **8 MB** (`round23_reliability.py:55`,
  also duplicated in `extension/content_script.js:17`) -> 8 MB of payload is ~8.4 MB of
  base64, which is the "8.4 MB" the other agent measured;
- the route split in `attach_zip()` (`dispatch_argon.py:7098`): `if sz <= 64 * 1024 * 1024`
  takes the base64 route, anything bigger takes the zero-copy route.

**F6 — The duplicate send is fixed in the common case, but the owner still sees it
SOMETIMES.** Round 26's hermetic case 5 now measures `sent_messages_count == 1`, so the
extension no longer re-pastes after a send. The residual path is the ONE the fix did not
cover: when the **pure-Python fallback** performs the send, the extension never performed
a send, so its `keepPromptAlive` guard is never disarmed and the observer can re-paste the
leftover afterwards. The leftover is then armed and any later click fires a duplicate.

**F7 — The registry key has no PROJECT PATH, only the folder name.** `proj_root.name` means
two different projects with the same folder name collide, and the stale entries are never
reaped.

---

## PART B — THE SIX WORK UNITS

### W1 — The follow-up lane must CONTINUE the recorded conversation

- **R1.1** `--lane followup` must open the conversation that the task state records for
  that lane and CONTINUE it. It must never start a new conversation while claiming to be
  a follow-up. If there is no recorded conversation for the lane, the lane must say so by
  name (`no recorded conversation for lane 'followup'`) instead of silently starting a new
  one.
- **R1.2** The lane that actually runs must be printed truthfully: `lane: followup ->
  continuing chat <id> (<url>)` or `lane: followup -> NEW chat <id> (no recorded
  conversation)`. The label and the behaviour must never disagree.
- **R1.3** The `SessionStore` lookup must be per LANE (the lane's own `chat_id` /
  `chat_url` in `~/.gemini/argon_task_state.json`), not just per project+mode.
- **R1.4** A follow-up MUST NOT attach the whole context archive again when the
  conversation already holds one; the archive is only attached when the conversation has
  no attachment yet (see W3).

### W2 — Two lanes must be able to run at the same time

- **R2.1** The "a task is ALREADY RUNNING for this project" guard must only refuse when
  the running task is in the SAME lane AND the same slot. A `followup` lane on slot B and
  a `new` lane on slot A must be allowed to run at the same time.
- **R2.2** The registry key must identify the project by its FULL normalised path, not by
  the folder name (`dispatch_argon.py:14183` and `:4986`).
- **R2.3** Two lanes on DIFFERENT slots must be able to send simultaneously: each lane
  gets its own tab, its own chat and its own delivery. The bridge already has the
  per-lane host lock and the per-slot tab registry - the guard is what blocks them.
- **R2.4** The `EXIT 9` refusal must name BOTH the running lane and the requested lane, so
  the owner can see WHY it was refused.

### W3 — The size limit is ALWAYS 100 MB, and one archive per conversation

- **R3.1** The hard archive limit is **100 MB** everywhere. Keep `MAX_ARCHIVE_BYTES` as
  the single source of truth and make the base64 cap and the route split follow the SAME
  number where they are about the archive limit. A 100 MB archive must be attachable.
- **R3.2** The base64 cap (`MAX_BASE64_ATTACH_BYTES`, 8 MB) is a BASE64-TRANSPORT limit,
  not an archive limit. It must be reported as such (the log must say "the base64 route
  can carry at most 8 MB; above that the zero-copy route is used"), never as "the archive
  is too big".
- **R3.3** In a CONTINUATION (a conversation that already holds an attachment) a second
  archive must NOT be attached at all - the conversation cannot take it. The bridge must
  say so by name and continue with a focused archive instead.
- **R3.4** The owner wants ONE number in the log for every attach:
  `archive <name>: <bytes> = <n> MB | hard limit 100 MB | route <x> | base64 <yes|no>`.

### W4 — The delivery capture must take the LAST turn's chip, by Export -> Download

- **R4.1** The capture must not rely on the DOM order. It must find the newest assistant
  turn, and inside it the generated-file chip, and download it with the REAL
  `Export -> Download` menu item - never the preview.
- **R4.2** When more than one candidate chip exists, the capture must pick the one from
  the LAST turn and prove it: print the chat id, the turn index, the file name and the
  SHA-256 it downloaded.
- **R4.3** If the downloaded file's SHA-256 equals the SHA-256 of a file captured earlier
  for the same project (Argon re-attached the same file), the capture must say so
  explicitly (`same digest as round <id> - Argon re-attached the same file`) instead of
  silently accepting it.

### W5 — The duplicate prompt must be impossible, and the residue must be disarmed

- **R5.1** After EVERY verified send - the extension's `click_send` path AND the
  pure-Python fallback - the extension's prompt guard must be DISARMED through a new
  content-script operation (e.g. `disarm_prompt_guard`), so the observer can never
  re-paste the prompt after the Python path sent it. (This is the residual path of F6.)
- **R5.2** The retry ladder must re-run the duplicate guard before EVERY attempt, not
  only once before the first attempt, so attempt 2 can never re-send a prompt that is
  already in the conversation.
- **R5.3** After the send, the bridge must count the user turns in the conversation. If
  the count grew by MORE than one, it must print a loud
  `DUPLICATE SEND DETECTED (<n> user messages for one prompt)` line with the chat id.
- **R5.4** The composer must be empty after a verified send (round 26 already does this)
  AND the leftover must not come back: the dispatcher should re-check the composer 2
  seconds after the clear and clear again if the observer re-pasted it.

### W6 — Remove the unnecessary blocks and the dead state

- **R6.1** Stale registry entries must NOT count against a slot's cap. An entry whose
  lane finished, whose browser PID is gone, or that is older than a bounded age must be
  excluded from `is_slot_available()` / `active_tasks_for_slot()`. Today three signed-in
  accounts look busy for no reason (F3).
- **R6.2** The stale entries must also be REAPED: `--register-task-done` and a
  `--prune-stale-tasks` option must remove them, and the pool must stop showing three
  dead accounts as busy.
- **R6.3** Remove the dead code that the last rounds left behind:
  - the duplicated `MAX_BASE64_ATTACH_BYTES` import in `dispatch_argon.py` (it is
    imported at `:9035` and never read there);
  - the leftover `*.bak` rollback copies of the patched files inside the plugin roots
    (`dispatch_argon.py.20261114_112942.bak` and friends) - the patch runner must put its
    backups somewhere that is not shipped, or clean them up;
  - any guard that is now unreachable (for example a duplicate-prompt branch that the
    round-24/26 changes made dead).
- **R6.4** Nothing may be removed that is still reachable: the model guard, the strict
  slot resolver, the wrong-conversation guard, the round-24 work units and the round-26
  composer/extension fix must all stay.

---

## PART C — THE OTHER AGENT'S REPORTED BUGS (all five are in scope)

| # | Report | Work unit |
|---|---|---|
| 1 | The dispatcher waits only ~12 s for the model chip on a CONTINUATION page, so a heavy conversation reads `(not visible)` | **W7 below** |
| 2 | In a continuation an archive over 8.4 MB does not attach, so a focused archive is needed | **W3** |
| 3 | The registry key is `PROJECT:lane:slot`, so a neighbouring session's lane gets EXIT 9 | **W2** |
| 4 | The delivery capture can grab the wrong chip (the DOM order is not reliable); it must do Export -> Download itself and take the LAST turn's chip | **W4** |
| 5 | Argon can re-attach the same file, so the SHA has to be compared as proof | **W4.3** |

### W7 — The chip wait on a continuation page

- **R7.1** The model chip lookup must WAIT (not take a single snapshot) on a continuation
  page: a heavy conversation hydrates slowly and the chip is not there for the first
  seconds. The wait must be the same for a new chat and a continuation (at least 45 s),
  and it must be reported (`waiting for the model chip on a continuation page ...`).
- **R7.2** When the chip is still not visible after the wait, the reason must name the
  page state (not just `(not visible)`), e.g.
  `model chip (not visible) after 45 s on a continuation page - the conversation is still
  hydrating`.

---

## PART D — HARD RULES

- **R8.1** Read `TASK_BRIEF.md` and `API_INDEX.md` from the archive before writing.
- **R8.2** **Surgical edits only.** Exact, unique anchors. Never rewrite a whole file,
  never reformat, never reorder imports. The bridge's UI must not change by one pixel.
- **R8.3** **Do not touch**: the live account pool file, the account model, the profile
  layout, the delivery bundle format, `knowledge/rounds/**`, or the round-24/round-26 work
  (commits `909a302`, `ebc5627`, `1bb891f`, `681d110`, `64eafc6`).
- **R8.4** No existing CLI flag may change its meaning. New flags are additive.
- **R8.5** Every write must be compile-verified before it lands, exactly like
  `scripts/patches/_safe_patch.py` does (temp file -> `py_compile` -> atomic replace).
- **R8.6** The model is `Code & analysis 4 Argon`. Never Flash, never Hydrogen.
- **R8.7** Add a hermetic suite `skills/argon-coding-bridge/scripts/test_round27_two_lane.py`
  with at least one case per work unit: two lanes on two slots can register and send at
  the same time; a follow-up lane continues the recorded chat; a stale registry entry does
  not make a slot busy; the capture picks the LAST turn's chip; the size log always shows
  100 MB; the duplicate counter reports a duplicate when there is one; the chip wait
  survives a slow-hydrating page. Wire it into `--self-test`.

---

## PART E — THE DELIVERY CONTRACT

One **linear ordinal PowerShell script**:
1. **Phase 1 - pre-flight**: every block located with
   `[System.StringComparison]::Ordinal` `IndexOf` + `[System.StringBuilder]`. If ANY block
   is missing or ambiguous, **abort the whole script, write NOTHING**, and print which
   block failed and why.
2. **Phase 2 - apply**: back up every file you touch, write it, then **re-read every
   written file from disk** and print `[Verified] <path>`.
3. Every here-string terminator `'@` must be on **its own line**.
4. Print a summary: the files touched, the new test file, and the verification commands.

### PART F — ACCEPTANCE CRITERIA (the owner runs these)

```
python skills/argon-coding-bridge/scripts/test_round27_two_lane.py    # NEW
python skills/argon-coding-bridge/scripts/test_round26_composer_clear.py # 5/5
python skills/argon-coding-bridge/scripts/test_round24_bugfixes.py     # 6/6
python skills/argon-coding-bridge/scripts/test_round24_workunits.py   # 3/3
python skills/argon-coding-bridge/scripts/test_round23_reliability.py  # all
python skills/argon-coding-bridge/scripts/test_round22_extension_install.py # 5/5
python skills/argon-coding-bridge/scripts/test_round21_dispatch_lane.py # 5/5
python skills/argon-coding-bridge/scripts/test_round20_lifecycle_and_sync.py # 5/5
python skills/argon-coding-bridge/scripts/dispatch_argon.py --self-test # exit 0
```

Deliver all seven work units in ONE `apply_changes.ps1`. Do not stop half way and do not
wait for a follow-up.
