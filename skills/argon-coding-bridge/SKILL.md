---
name: argon-coding-bridge
description: Delegate complex coding, debugging, and refactoring tasks from any local coding agent (Claude Code, Cursor, Gemini CLI, Antigravity, OpenCode, MiniMax Code, ...) to Gemini 4 Argon via Task mode in Gemini Web. Packages the codebase into a clean, gitignore-aware ZIP (filtering build dependencies, node_modules, and secrets), generates surgical zero-UI-regression prompts, continues follow-up tasks in the same conversation, and automatically applies the returned PowerShell patch script.
---

# Argon Coding Bridge (Agent Plugins 1.0 - for any coding agent)

Bridge **any local AI coding agent** with **Gemini 4 Argon** running in Gemini Web (**Task Mode**). Gemini Web is the only supported workspace - Google AI Studio was removed because it consistently fails on agentic work.

## Why Use This?
- **Massive Reasoning Multiplier**: Delegate heavy, multi-file architectural refactoring, complex bug diagnosis, and deep code synthesis to Gemini 4 Argon while continuing your main workflow locally.
- **Zero UI Regression Guarantee**: Strict surgical instructions prevent Gemini from rewriting whole files or altering existing CSS, Tailwind, Flutter widgets, or HTML layouts.
- **Strict Dependency & GitIgnore Filtering (Up to 100 MB, Zero Code Compromise)**: Accurately packages the complete related codebase up to **100 MB** (`DEFAULT_MAX_ARCHIVE_MB = 100`, `DEFAULT_MAX_ARCHIVE_ENTRIES = 6000`), stripping `node_modules`, `vendor`, `build`, `.gradle`, `Pods`, `.next`, binaries, and private secrets (`.env*`) while never compromising on source code size.
- **Linear Ordinal PowerShell Patch (`IndexOf` + `StringBuilder`)**: Changes are delivered in a two-phase script that validates every target block across every file before writing anything, backs up touched files, and applies replacements via `[System.StringComparison]::Ordinal` `IndexOf` + `[System.Text.StringBuilder]` (never `.NET String.Replace` / `String.Contains` on large needles, which hangs for 25+ minutes on 500 KB files).
- **Session Continuity**: A follow-up task on the same project continues in the SAME Gemini conversation, so the earlier codebase understanding, decisions and ideas stay available.
- **Self-Verification**: The delivered patch re-reads every file it writes and prints `[Verified] <path>`, so Argon proves its own work instead of merely claiming success.

---

## Working rules

These rules are how the agent that drives this bridge works. They are not
suggestions.

### 1. Dispatch preparation is quick, and the archive is complete (Up to 100 MB, No Compromise on Code Size)

The agent focuses on the USER'S PROMPT and the intent behind it.

- **Premium 100 MB Archive Rule**: The package size may be up to **100 MB** (`DEFAULT_MAX_ARCHIVE_MB = 100`, `DEFAULT_MAX_ARCHIVE_ENTRIES = 6000`) and never more. The maximum code size is always tried, with **no compromise on code size**: a large codebase or many files may be sent whenever needed, and necessary information or source files must never be reduced. If the 100 MB hard cap is ever reached, `TASK_BRIEF.md` states plainly that the archive is incomplete and lists every omitted file.
- The upload timeout window scales automatically with the archive byte size (`_upload_timeout_for_archive()`, with a 600 s floor and ~900 s for a 100 MB archive) so a large archive is never cut off mid-upload.
- The analysis that settles the subject finishes in **seconds to a few
  minutes**. It never turns into a long analysis phase, and the agent does not
  keep analysing after the subject is clear.
- This does NOT mean "no analysis at all". It means the analysis is short,
  it is aimed at the user's prompt, and it stops.

### 2. Compare fast, apply the merged good parts

When both patch files have arrived, the agent compares them quickly and works
out which one is better and which good parts of the two can be combined.

- The **merged result of the good sides of both** is what gets applied.
- If both patches carry errors and neither side is good, then the **winner
  alone** is applied.
- All of this is done quickly. The comparison is a step, not a project.

### 3. Errors after the apply

- Errors that appear after the apply are fixed by the agent.
- But when the NUMBER of errors is large, that is no longer a local problem:
  a **follow-up is sent to Argon** instead.
- Once a prompt has been sent, the agent does **not** do extra work of its own
  in the meantime.
- It does not step outside its scope, it does not brainstorm and it does not
  invent ideas. Analysing, brainstorming and hunting for the real problem are
  **Argon's** work.

### 4. Read the step screenshot before deciding anything

- Every meaningful step of a bridge run writes a numbered screenshot to
  `.argon_backup/shots/<NN>-<step>.png` and prints:
  `[+] <step> - <state> - Screenshot: <absolute path>`
- **Before deciding anything, the agent MUST read the screenshot of that step
  with its image reader.** The terminal text is only a summary; the screenshot
  is the measured evidence. Never decide from the terminal text alone.

### 5. Per-step rule verification & workflow flexibility (Rule 7)

- **Re-read each step's rules before finishing**: Before any workflow step (packaging, brief preparation, dispatch/send, capture/download, or patch acceptance/apply) finishes, its own specific rules must be read again carefully and verified.
- **Mandatory self-verification on file-send, file-download, and patch-accept**:
  1. **File-send (`attach_zip` & `_execute_composer_send_sequence`)**: Verifies before every send that the archive is <= 100 MB, contains `TASK_BRIEF.md` and `API_INDEX.md`, contains zero credentials or binaries, that Task mode and `4 Argon` (never Flash) are active, and clicks Send the moment the upload finishes.
  2. **File-download (`capture_generated_file` & `_validate_and_save_download`)**: Verifies every time that the patch comes from the viewer's **Download** button (never the truncated preview), is non-empty, and is not a leftover digest in `DeliveryLedger`.
  3. **Patch-accept (`refuse_invalid_patch`, `_looks_like_patch`, `_patch_is_complete`, `Run-ArgonPatch.ps1`)**: Verifies every time before and after running that all here-strings are balanced, no raw control bytes or unbalanced braces were introduced, every target block was pre-flight checked in Phase 1 before writing in Phase 2, and every written file was re-read from disk.
- **Flexible by design**: Step ordering (`_execute_composer_send_sequence`), archive caps (`--max-archive-mb`, `--max-entries`, `--max-size-mb`), per-agent state paths (`ARGON_TASK_STATE_PATH`), slot selection (`--slot`, `--second-slot`), and verification commands (`--verify-cmd`, `--skip-verify`) remain configurable per run without modifying core code.

### 6. The retry ladder & immediate manual hand-off on every attempt (Rule 8)

Both the **send phase** and the **delivery phase** retry at most two or three times (`MAX_LADDER_ATTEMPTS = 3`), and **every single attempt (`attempt 1`, `attempt 2`, `attempt 3`) writes and prints the manual hand-off pack (`manual_handoff.md`, prompt file path, and `argon_context.zip` location) upfront** so a human can click and send by hand in the open browser at any moment without waiting for retries to finish:
1. Normal attempt (`attempt 1 of 3`) — writes the manual hand-off pack (`dispatch_prompt.md` + `argon_context.zip` path) immediately and runs the unified send sequence.
2. Same state once more (`attempt 2 of 3`) — updates the manual hand-off pack, reads the step screenshot and exact failure reason, and retries.
3. Root-cause fix attempt (`attempt 3 of 3`) — updates the manual hand-off pack, fixes the underlying cause, records the fix, and runs the final automatic retry.
4. **Self-executed manual send fallback**: If the automatic attempts still fail, the bridge performs the manual send **itself** in the browser using the exact same composer, scaled upload wait, and hard Flash model guard, and then waits for the delivery as usual.

### 7. One Dedicated Argon Bridge Browser, `/u/<index>/` Account Switching, and Safe Tab Close (Round 17)

- **One Browser for the Whole Bridge**: The bridge uses **ONE dedicated Chrome browser** ("the Argon Bridge browser") backed by the shared profile `~/.gemini/argon_browser_profile`, and **every pooled Google account lives inside it**. The owner's own daily browser is **never** touched by the bridge.
- **Verified `/u/<index>/` Account Switching**: Accounts are switched with Google's own `/u/<index>/app` path, where `<index>` is the account's verified 0-based position inside that browser's `Default/Preferences` (`account_info`) list (`PoolConfig.get_account_index(slot)` / `PoolConfig.set_account_index(slot, index)`). It is verified against `Default/Preferences` and recorded — it is **not** guessed from the pool slot number.
- **Flat RAM and Disk Usage**: Running one Argon Bridge browser instead of one browser per account keeps resource usage flat (~12–13 Chrome processes, ~1.4 GB RAM instead of 7 GB, and ~2–3 GB disk instead of 68 GB) no matter how many accounts or tasks are active.
- **Closing a Slot Closes Only That Account's Tabs (`--close-browser --slot /u/N/`)**:
  **Never kill the browser with `taskkill /F`.** In shared mode, `python dispatch_argon.py --project-path . --close-browser --slot /u/N/` closes **only the tabs of that account** (reusing `--close-tab`) and leaves the Argon Bridge browser running for the other accounts. The browser itself is closed politely (flushing cookies) only when no task tab is left.
  To close a single tab by ID, use `python dispatch_argon.py --project-path . --close-tab --slot /u/N/ --tab <tab-id>`, and inspect running tabs with `python dispatch_argon.py --project-path . --list-browsers`.
- **Reclaiming the 68 GB of Old Per-Slot Profiles (`--reclaim-slot-profiles`)**:
  Run `python ./skills/argon-coding-bridge/scripts/dispatch_argon.py --reclaim-slot-profiles` (or `argon_pool_login.py --reclaim-slot-profiles`) to safely reclaim the old `~/.gemini/argon_profiles/slot-uN/` directories. It verifies that each slot's Google account is signed into `~/.gemini/argon_browser_profile` first, refuses to touch any slot whose account is missing from the shared profile, moves verified slot profiles to `~/.gemini/argon_reclaimed_profiles/<timestamp>/`, and prints the exact MB/GB reclaimed.

### 8. Extending a running task (`--append-followup`)

> **A running task can be extended.** To add a requirement to a task that is already
> running, send it with `--append-followup`. The running task is not stopped, and the single
> delivery contains both the original and the added requirement.

Command:
`python dispatch_argon.py --project-path . --append-followup "<file.md>" --lane <lane> --slot /u/N/`

### 9. Open the browser BEFORE you send anything (Rule 10)

> **Nothing may be sent into a browser that is not open.** A browser tool call on a
> browser that is not running FAILS, and the prompt - or the attached archive - never
> reaches the chat. The run then looks like it is working while nothing was delivered.

Before EVERY prompt - the follow-up, the second lane, a new session, or a follow-up
message - open the browser of that account first and PROVE that it is up:

```
python ./skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path . \
       --open-browser --slot /u/N/
```

The command prints the profile, the CDP endpoint, the opened URL, the page title and
the session state, and it ends with `Browser status: OPEN and reachable - a prompt can
be sent now`. **Only then** send the prompt. If the command cannot open the browser,
fix that FIRST - sending into a closed browser always fails.

Why this matters in practice: a Chrome that was started WITHOUT a debugging endpoint
takes the profile directory's lock and publishes no endpoint, so the bridge cannot
attach to that profile and every later browser call on it fails. `--open-browser`
detects that case, closes the stray Chrome the safe way (the cookies are snapshotted
and restored, so the account stays signed in) and starts the profile's single browser
host with a debugging endpoint, which the dispatcher can then attach to.

### 10. Hand over the manual-send material FIRST, then send automatically (Rule 11)

> **For every new request the manual-send material is handed over BEFORE the automatic
> send starts.** Not at the end, not only when something failed - up front.

The order is strict:

1. **Open the browser** (Rule 9) and prove it is up.
2. **Hand over the manual-send material**: the exact prompt file, the prompt text and
   the `argon_context.zip` path, so a human can click and send it by hand in the open
   browser at any moment. The bridge writes that pack itself - `manual_handoff.md` and
   `handoff.json` under `.argon_backup/<session>/` - and prints the paths.
3. **THEN start the automatic send**, and keep the manual-send material updated on
   every attempt of the ladder (Rule 8).

Handing the material over first is what saves the long wait: when the automatic path
stalls, the operator can send by hand immediately instead of waiting for the retries
to run out.

### 11. Companion MV3 Extension Accelerator, Preflight Self-Test, and the Custom Chromium Build Option (Round 16)

- **Why the Companion Extension Fixes the Four Fragile Operations (`skills/argon-coding-bridge/extension/`)**:
  Moving file attachment (`DataTransfer` `File` injection into `input[type=file]` without opening the Windows OS file dialog), prompt pasting (synthetic `ClipboardEvent('paste')` with `text/plain` in a `DataTransfer` without origin clipboard permissions), semantic model/mode/send matching (`MutationObserver` + composer-scoped semantic matchers instead of brittle CSS classes), and paged Drive `viewer/text` delivery capture into the unpacked MV3 companion extension fixes all four recurring DOM and OS failures inside existing Chrome today.
- **What a Custom Chromium Build Would Add (Documented Option, Not the DOM Fix)**:
  A custom Chromium build does not fix Gemini's DOM or the Windows file dialog on its own, so it is documented strictly as an optional hardening layer that would only add browser version pinning, a pre-installed companion extension immune to Chrome auto-update permission changes, and `--remote-debugging-pipe` transport in place of a local TCP debugging port.
- **Automatic Pure-Python Fallback**:
  The companion extension is loaded automatically when `skills/argon-coding-bridge/extension/manifest.json` is present, and the bridge logs which path (`extension` vs `python_fallback`) executed each step. If the extension is absent or `--no-extension` (`ARGON_DISABLE_COMPANION_EXTENSION=1`) is passed, the bridge falls back cleanly to the pure-Python path.
- **Preflight Self-Test in Seconds (`--self-test`)**:
  Run `python ./skills/argon-coding-bridge/scripts/dispatch_argon.py --self-test` to verify all four operations (attach without OS dialog, synthetic paste into the lowest composer skipping the floating bar, `3.8 Flash` -> `4 Argon` switch, semantic send after upload clears the composer, and paged Drive `viewer/text` stitching) plus fault-injection checks (`no_file_input`) in seconds.

---

## The production workflow

Work through these steps ONE AT A TIME. Do not fire a batch of commands.

### Step 1 - Mark the task started
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --register-task-start --label "<one line: what you are about to ask for>"`
Why:  the registry is what tells you (and the next run) which account is
      already busy. It is the ONLY source of truth for "is this slot free".

### Step 2 - Prepare the brief and SHOW IT TO THE USER

Commands:
  python ./skills/argon-coding-bridge/scripts/prepare_brief.py --project-path . \
         --brief "<the brief file you wrote>" --lane followup --slot "<slot>"

Then show the user the contents of the summary file the command printed, and
say plainly:
  - what you understood the request to be,
  - the exact prompt text that is going to be sent (the brief, verbatim),
  - what the archive contains,
  - which files are going to change,
  - which account and which mode are going to be used.

WAIT for the user. If the user corrects anything, fix the brief file and run the
command again. Only when the user is happy do you continue to Step 3.

Never send a prompt the user has not seen.

### Step 3 - Open the browser and PROVE it is up (Rule 10)

Command:
`python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --open-browser --slot "<the account you are about to use>"`

Why: a browser tool call on a browser that is not running FAILS, so the prompt and the
      archive never reach the chat. The command opens that account's browser, attaches
      to it, navigates to Gemini Web and prints the page title and the session state.
      It must end with `Browser status: OPEN and reachable`.

Fix a browser that will not open BEFORE you go on. Never send into a closed browser.

### Step 4 - Hand over the manual-send material (Rule 11)

Command:
`python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --manual-handoff --lane followup --slot "<slot>"`

Why: for every new request the material for a MANUAL send is handed over BEFORE the
      automatic send starts - not at the end, not only after a failure. The pack holds
      the exact prompt file, the prompt text and the `argon_context.zip` path, and the
      browser of Step 3 is already open, so a human can click and send at any moment.

Give the user the prompt path and the zip path in your reply, and keep the pack
updated on every attempt of the ladder (Rule 8).

### Step 5 - Send the FOLLOW-UP first
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --task "<task>" --slot "<the account that holds the chat>" --lane followup`
Why:  the follow-up runs in the conversation that already knows this codebase.
      Send this one FIRST and let it finish.

### Step 6 - CHECK the follow-up before you go on
Read the screenshot of the run (the path is printed) and check three things:
  a. the conversation was in TASK mode (not Chat),
  b. the model chip said 4 Argon,
  c. Argon produced a real answer, not a chat-style reply.
If anything is wrong, FIX IT FIRST and send the follow-up again.
Only when the follow-up is confirmed good do you continue to Step 7.

### Step 7 - Then run the NEW task
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --task "<task>" --new-session --slot "<another account>"`
Why:  a new task belongs in a new conversation, on a different account, so the
      two runs never collide.

### The bounded retry ladder (send failures AND delivery failures)

When EITHER a **SEND** fails (ZIP upload failed, paste failed, send failed, Task-mode or
4 Argon pre-flight failed, or the run stopped before the prompt was delivered) OR a
**DELIVERY** fails (the delivery chip could not be downloaded after 3 per-chip attempts,
the downloaded file failed `_looks_like_patch` or `_patch_is_complete` and was quarantined
in `.argon_quarantine/`, or the response finished without a usable patch), the agent follows
the bounded ladder (`MAX_LADDER_ATTEMPTS = 3`):

1. **Step 1 - Attempt 1 (`attempt 1 of 3`) + Upfront Manual Hand-Off Pack:** The dispatcher writes `manual_handoff.md` and `handoff.json` (with the prompt file path and `argon_context.zip` path) on attempt 1 so the operator can send by hand at any moment.
2. **Step 2 - Attempt 2 (`attempt 2 of 3`) + Updated Manual Hand-Off Pack:** Read the step screenshot (`.argon_backup/shots/<NN>-<step>.png`) and exact failure reason, keep the manual hand-off pack updated, and retry once in the same state.
3. **Step 3 - Attempt 3 (`attempt 3 of 3`) + Root-Cause Fix:** Inspect the exact check that failed and any kept file in `.argon_quarantine/`, fix the root cause in the script or process, record the fix with `record_fix.py` on a `fixes/<scope-slug>` branch, and run attempt 3.
4. **Step 4 - Self-Executed Manual Send Fallback:** If automatic attempts 1..3 still fail to send, the bridge performs the manual send **itself** in the open browser (using the same composer, the same scaled upload wait, and the same hard Flash guard) and waits for the delivery, while keeping the manual hand-off pack and browser available for the operator.

| Lane | When the hand-off pack is written | What is handed over on EVERY attempt |
|---|---|---|
| **follow-up** | Attempts 1, 2, 3, and self-manual fallback | account e-mail, chat link, prompt file, ZIP path, progress, screenshot path |
| **new** | Attempts 1, 2, 3, and self-manual fallback | the same complete set so a human can click and send in the browser at any moment |

### Step 8 - Compare and apply
Command:  `python "./skills/argon-coding-bridge/scripts/compare_argon_patches.py" --followup-patch "downloads/followup/apply_changes.ps1" --new-patch "downloads/new/apply_changes.ps1" --merge --pick new --apply`
Why:  compares the follow-up delivery and the new-session delivery side by side,
      combines their non-conflicting blocks (using `--pick new` or `--choices`
      when conflicts exist), applies the resulting single-run patch, and runs
      the local build verification once. Record the winning lane afterwards with:
      `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --record-winner followup|new`

### Step 9 - For the NEW lane: confirm the account e-mail and chat link

Tell the user the account e-mail and the chat link that the new run used and ask them to confirm or correct them. The agent already knows both - state them and ask for confirmation, not asking the user to find them:
- **Account e-mail:** `<email>`
- **Chat link:** `<chat-url>`
The next round's winner selection needs this information.

### Step 10 - Mark the task completed
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --register-task-done --label "<same label>"`
Why:  releases the task in `~/.gemini/argon_tasks.json` so the next round sees
      accurate slot load counts.

### The rules that belong in this workflow

1. **The follow-up always goes to the account that holds the chat** - never
   rotated. Even when that account is already busy with multiple tasks, that chat
   lives there, so USE it and say so in one line. Do not refuse, do not block:
   the work needs that conversation.
2. **A new task skips an account ONLY when that account already runs the maximum number of tasks (`MAX_TASKS_PER_SLOT = 2`).** There are no other account skip rules or usage-limit shutoffs of any kind. When all accounts are at capacity, the run continues on the least-loaded account and prints the reason.
3. **Never send a task on the Flash model (Hard Flash Guard).** Before every send (both on a new chat and on a follow-up), the dispatcher reads the model chip; if it is not `4 Argon`, it selects `4 Argon` again, and if it still is not `4 Argon`, it aborts the send immediately.
4. **Never send a prompt in Chat mode.** If the mode cannot be proven, the
   agent opens a NEW chat, switches it to Task, and checks the chip before it
   sends. If a follow-up is refused with `mode = Task : UNKNOWN`, inspect the
   conversation: if the user confirms it is a Task-mode conversation, record it with
   `python ./skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path . --record-workspace-mode <chat-id> --mode task`
   and retry. Never record a mode that has not been confirmed.
5. **Before each step finishes, re-read that step's own rules**; the file-send,
   file-download, and patch-accept steps verify their own rules every time before
   they run, and the workflow stays flexible for project-specific needs.
6. **Show the brief before it is sent.** The agent summarizes what it understood
   and waits for user approval.
7. **Record what you fixed.** When fixing defects, record debugging notes with
   `record_fix.py` and commit on a dedicated `fixes/<scope-slug>` branch.
8. **Read the step screenshot before every decision.** The terminal line is a
   summary; the screenshot is the evidence.

### Troubleshooting Table (Symptom -> Root Cause -> Action)

| Symptom | Root Cause | Action |
|---|---|---|
| **A browser tool call fails and the prompt/archive never reaches the chat** | The browser of that account was not open (Rule 10). A browser call on a browser that is not running fails, and the run looks like it is working while nothing was delivered. | Run `python dispatch_argon.py --project-path . --open-browser --slot /u/N/` FIRST, and only send once it prints `Browser status: OPEN and reachable`. If it cannot open the browser, fix that first. |
| **The browser will not open and the bridge cannot attach to the profile** | A Chrome that was started WITHOUT `--remote-debugging-port` holds the profile directory: it takes the profile lock and publishes no endpoint, so every later browser call on that profile fails. | `--open-browser` detects this, closes the stray Chrome the SAFE way (cookies snapshotted and restored, so the account stays signed in) and starts the profile's single browser host with a debugging endpoint. Never start a second plain Chrome on a slot profile. |
| **A session snapshot or a safe close hangs** | The cookie database was read with a malformed SQLite URI (`file:C:/Users/<name with a space>/...`), so every read failed and the code fell back to a raw file copy of the file a live Chrome holds locked. | The snapshot now builds the URI with `Path.as_uri()` and reads `immutable=1` first, so it never blocks and never waits for the lock. |
| **The answer is a chat-style reply instead of work** | The prompt went out in Chat mode. | Open a new chat (`--new-session`), switch it to Task mode, check the `4 Argon` chip in `.argon_backup/shots/<NN>-preflight.png`, and resend. |
| **Gemini says the archive is unreadable / binary data** | Binary files (fonts, images, media, or non-UTF-8 data) were included in the archive. | The packager must exclude every binary file by extension and 8 KB NUL/UTF-8 content check and enforce the 100 MB archive cap; re-run `package_context.py` to rebuild `argon_context.zip`. |
| **The prompt landed in an old conversation** | The new chat was not verified empty after loading. | The run must verify after the conversation loads that there are zero user messages and zero assistant turns and report the `chat_id` it used; re-run with `--new-session`. |
| **Old delivery downloaded again** | A previous round's `apply_changes.ps1` chip was downloaded and matched a digest in `DeliveryLedger` or an existing patch. | `_is_leftover_delivery` refuses the duplicate, **keeps** the rejected file in `.argon_quarantine/<timestamp>_apply_changes.ps1` at a printed absolute path (never deleting it), names the exact duplicate SHA-256 check that failed, and stops capturing that chip. Follow the bounded retry ladder. |
| **No download after the task finished** | Argon finished answering inline or the viewer toolbar button did not trigger before timeout. | Read `.argon_backup/shots/<NN>-delivered.png` or `failed.png`; if a chip is visible in the newest turn, run `--resume` to capture it or download `apply_changes.ps1` from the viewer toolbar and run `Open_Argon_Apply.bat`. |
| **Browser opens again and again** | A retry loop re-opened the preview or re-dispatched without checking `TaskState` / `DeliveryLedger`. | Single-capture mode exits immediately after one valid toolbar download. Use `--list-tasks` to inspect active runs. |
| **A task is already running on this account (`EXIT_ALREADY_RUNNING = 9`)** | `TaskState` or `TaskRegistry` shows an in-flight `sent` or `waiting` task for this exact project/lane. | Read the printed screenshot path with your image reader; run `--resume` to wait for the running task, or pass `--force` to explicitly mark the old task `superseded` and start fresh. |
| **Model chip says Flash or something else than Argon** | After a message is sent, Gemini reverts the composer chip to the account default (often `Flash`). | The hard Flash guard (`ensure_argon_model` + `enforce_no_flash_before_send`) checks the chip before every send (new chat and follow-up), re-selects `4 Argon`, and aborts the send if `4 Argon` cannot be confirmed so a task never goes out on Flash. |
| **Account is busy with running tasks** | Another task is already active on that slot in `TaskRegistry` or Chrome is open on the slot profile. | A follow-up always uses the account that holds the chat even if busy; a new task skips an account only when it already runs `MAX_TASKS_PER_SLOT` (2) tasks, falling back to the least-loaded account without blocking. |
| **Delivery is truncated or 0 bytes** | The browser handed over an incomplete script or a file that failed `_looks_like_patch` / `_patch_is_complete`. | Rejected files are **kept** in `.argon_quarantine/` at a printed absolute path (never deleted), the terminal names the file, its size, and the exact check that failed (`[REFUSED] file=... label=... offset=... reason=...`), and per-chip downloads are capped at 3 attempts. |
| **Second prompt superseded an unfinished task** | Another prompt was sent into the same conversation while the earlier task was still `sent` or `waiting`. | `_check_superseded_prompt` detects the newer user message in the DOM, marks the earlier task `superseded` in `TaskState` and `TaskRegistry`, and stops waiting immediately. |
| **New session opened a foreign conversation** | Navigating to `/u/0/app` restored the account's previous chat from another project. | `_start_fresh_chat` waits for the conversation to finish loading, clicks the sidebar **New chat** button, verifies zero user/assistant turns and that the URL chat ID is not in `_known_chat_ids_for_slot`, and reports the chat ID if it cannot prove the chat is empty. |
| **Send failed on first attempt (Step 1)** | Transient browser, network, or UI race condition (stale composer focus, unsettled model chip, page still loading). | The manual hand-off pack (`manual_handoff.md` + prompt & ZIP paths) is already written on attempt 1. Read the step screenshot (`.argon_backup/shots/<NN>-<step>.png`) and retry once in the same state (Step 2) or send manually in the browser. |
| **Send failed twice in same state (Step 2)** | Concrete bug or limitation in dispatcher script, process, packaging, or command. | Find the REAL cause, update the script or process, record the fix with `record_fix.py` on a `fixes/<scope-slug>` branch, and run attempt 3 (with the updated manual hand-off pack). |
| **Automatic send attempts exhausted (Step 3 -> Step 4)** | All 3 automatic attempts did not complete the send. | The bridge automatically performs the manual send **itself** in the browser (using the same composer, scaled upload wait, and hard Flash guard) while keeping the manual hand-off pack available. |
| **Browser stuck or needs to be closed (`taskkill /F` logs account out)** | Killing Chrome with `taskkill /F` prevents `Browser.close` from flushing `Default/Network/Cookies` and destroys the signed-in session. | **Never** run `taskkill /F` on a slot profile. Close a slot browser safely with `python dispatch_argon.py --project-path . --close-browser --slot /u/N/` (or close one tab with `--close-tab --slot /u/N/ --tab <tab-id>` and inspect hosts with `--list-browsers`). |

---

## Workflow Guide

```
[Your project / issue]
             │
             ▼
1. Package Codebase (scripts/package_context.py or Package-ArgonContext.ps1)
   -> Creates argon_context.zip with TASK_BRIEF.md (including the absolute target directory)
             │
             ▼
2. Copy Surgical Prompt (templates/argon_task_prompt_template.md)
   -> Strict Zero-UI-Regression instructions + delivery + verification contract
             │
             ▼
3. Dispatch to Gemini Web (Task Mode -> 4 Argon)
   -> Upload argon_context.zip + paste prompt
   -> Reuses this project's existing conversation when there is one
             │
             ▼
4. Run apply_changes.ps1 locally
   -> Exact substring replacement with auto-backup & rollback & [Verified] output
             │
             ▼
5. Your agent picks up the modified files cleanly!
```

---

## Step 0: Ensure the Account Pool Is Logged In (optional)

The dispatcher rotates across a pool of Google accounts configured in
`skills/argon-coding-bridge/config/accounts_pool.json`. By default, every account is signed into
**ONE dedicated Argon Bridge browser profile** (`~/.gemini/argon_browser_profile`, leaving your
personal daily browser completely untouched), and each account's verified browser index in
`Default/Preferences` (`account_info`) is recorded via `PoolConfig.set_account_index(slot, index)`
so tasks switch accounts with `/u/<index>/app` inside that single browser.

**Log in every account, fully automatic:**
```bash
python "./skills/argon-coding-bridge/scripts/argon_pool_login.py" --all --wait-minutes 6
```

**Log in one account:**
```bash
python "./skills/argon-coding-bridge/scripts/argon_pool_login.py" --slot /u/2/ --wait-minutes 6
```

**Add a new credential and log it in immediately:**
```bash
python "./skills/argon-coding-bridge/scripts/argon_pool_login.py" --add "someone@gmail.com" "their-password" --tier PRO --role "Pool Member" --wait-minutes 6
```

**What the automatic flow does**
1. Opens the Google `AddSession` page in the pool profile.
2. Fills the email field and presses Enter.
3. Fills the password field and presses Enter. When Chrome's password manager has already autofilled
   a value, that value wins, because a stored password in the config can be stale.
4. Watches for a human challenge: a 2-Step approval push, an OTP, or a CAPTCHA.
5. On a challenge it **pauses**, raises a Windows tray notification plus an in-app alert, and waits
   for you to approve the prompt on your phone. It resumes by itself once you approve. It never
   solves or bypasses a challenge.

**Graphical assistant** — account navigation, copy buttons, an "Auto Login All" button, and an
"Add Account" dialog that appends a new email and password to the pool and logs it in:
```bash
python "./skills/argon-coding-bridge/scripts/login_pool_accounts.py"
```
Double-clicking `Open_Argon_Login.bat` at the repository root starts the same thing.

**One-click dispatcher** — `skills/argon-coding-bridge/scripts/Invoke-ArgonBridge.ps1` runs the
whole chain (package → prompt → dispatch → apply) and can start the login assistant with
`-LoginAssistant`.

---

## Step 1: Package the Project Context

Run the packager in your terminal inside your project root:

**Using PowerShell (Windows):**
```powershell
powershell -ExecutionPolicy Bypass -File "./skills/argon-coding-bridge/scripts/Package-ArgonContext.ps1" -TaskDescription "Explain what you want fixed or built" -OutputZip "argon_context.zip"
```

**Using Python (Cross-platform):**
```bash
python "./skills/argon-coding-bridge/scripts/package_context.py" --task "Explain what you want fixed or built" --output-zip "argon_context.zip"
```

**What it does:**
- Strictly respects `.gitignore`.
- Filters out all dependency and build packages (`node_modules`, `vendor`, `.dart_tool`, `Pods`, `.gradle`, `venv`, `dist`, `build`, etc.).
- Strips secrets (`.env*`, `*.pem`, `*.key`, `*credentials*.json`).
- Supports archives up to **100 MB** (`DEFAULT_MAX_ARCHIVE_MB = 100`, `DEFAULT_MAX_ARCHIVE_ENTRIES = 6000`) and always tries the maximum code size with no compromise on code size.
- Injects `TASK_BRIEF.md` and `API_INDEX.md` containing the complete declaration index into the zip.

---

## Step 2: Submit the Surgical Prompt to Gemini 4 Argon

Provide this surgical prompt when uploading `argon_context.zip` to Gemini:

```markdown
I have uploaded the project codebase as a ZIP archive (`argon_context.zip`). Please review `TASK_BRIEF.md` inside.

### Task:
<Provide a clear description of the task or bug to fix>

### Strict Surgical Modification Rules (CRITICAL - DO NOT VIOLATE):
1. **Minimal Scope Only**: Modify ONLY the specific functions or lines required.
2. **Zero UI Changes**: Do NOT redesign, restyle, reorder, or alter any existing UI layouts, CSS/Tailwind classes, spacing, colors, or JSX/Flutter/HTML widgets unless explicitly requested in the task.
3. **No Truncation**: Never use placeholder comments like `// ... rest of code remains the same`.
4. **Preserve Logic**: Keep all existing imports, state, props, helper functions, and comments 100% intact.

### Required Output Format (Automated PowerShell Patch):
Deliver the work as a **downloaded file**, never as a streamed code block. Write a single,
self-contained PowerShell script named `apply_changes.ps1`, **attach it to the reply** so it can be
downloaded with one click, and keep the chat reply short: what changed, which files changed and why,
the reference to the attached file, and how to verify. Do NOT paste the script body into the chat.

### Target Directory (use these exact paths):
The target codebase lives at this absolute directory:

```
<ABSOLUTE_PROJECT_PATH>
```

The delivered script must pin it and resolve every path against it, so it applies the whole change
set in a single run from any working directory:

```powershell
$TargetRoot = '<ABSOLUTE_PROJECT_PATH>'
```

- For edits in existing files, NEVER use `.NET String.Replace(String, String)` or `String.Contains(String)` on large files or multi-kilobyte needles (on Windows PowerShell 5.1 they are `O(file x needle)` and hung for 25 minutes on a 500 KB file, whereas ordinal `IndexOf` + `StringBuilder` takes 0.0 seconds). Use a two-phase script: Phase 1 checks every target block of every file with `[System.StringComparison]::Ordinal` BEFORE any file is written, and Phase 2 backs up, applies with `StringBuilder`, writes UTF-8 no-BOM, and verifies:
  ```powershell
  $relPath = "relative/path/to/file.ext"
  $path = Join-Path $TargetRoot $relPath
  $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
  $oldTarget = @'
// EXACT existing lines
'@
  $newReplacement = @'
// Updated replacement lines
'@
  # Phase 1 (Pre-flight loop across ALL files before any file is written):
  $idx = $content.IndexOf($oldTarget, [System.StringComparison]::Ordinal)
  if ($idx -lt 0) { throw "Target block not found in $relPath. Aborting before any file is written." }

  # Phase 2 (Backup, linear StringBuilder replacement, UTF-8 no-BOM write, and re-read verify loop):
  $backupSession = Join-Path (Join-Path $TargetRoot '.argon_backup') (Get-Date -Format 'yyyyMMdd_HHmmss')
  $backupDest = Join-Path $backupSession $relPath
  $backupParent = [System.IO.Path]::GetDirectoryName($backupDest)
  if ($backupParent -and -not (Test-Path $backupParent)) { New-Item -ItemType Directory -Path $backupParent -Force | Out-Null }
  Copy-Item -LiteralPath $path -Destination $backupDest -Force
  $idx = $content.IndexOf($oldTarget, [System.StringComparison]::Ordinal)
  if ($idx -lt 0) { throw "Target block not found in $relPath. Aborting before any file is written." }
  $sb = [System.Text.StringBuilder]::new($content.Length + $newReplacement.Length)
  [void]$sb.Append($content, 0, $idx)
  [void]$sb.Append($newReplacement)
  [void]$sb.Append($content, $idx + $oldTarget.Length, $content.Length - $idx - $oldTarget.Length)
  $content = $sb.ToString()
  [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
  $verify = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
  if ($verify.IndexOf($newReplacement, [System.StringComparison]::Ordinal) -lt 0) { Write-Host "[FAILED VERIFY] $relPath" -ForegroundColor Red; exit 1 }
  Write-Host "[Verified] $relPath" -ForegroundColor Cyan
  Write-Host "[Patched] $relPath" -ForegroundColor Green
  ```
- Always run the Phase 1 pre-flight check across EVERY target block of EVERY file before the Phase 2 write loop touches any file on disk.
- Back up every file before overwriting it into `.argon_backup/<yyyyMMdd_HHmmss>/<relative path>`.
- Use single-quoted Here-Strings (`@' ... '@`) so `${variable}` template literals are not corrupted.
- Write files using UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.
```

---

## Step 3: Configure the Workspace and Execute the Unified Send Order

The workspace must be configured **before** the prompt is sent. A prompt sent in Chat mode, on the
Flash model, or **without the zip attached** wastes the round trip.

> **The UI language follows the signed-in Google account.** This pool renders Gemini in Bangla, so
> English-only text selectors silently miss. Both labels are given below.

### Gemini Web — `https://gemini.google.com/u/<slot>/app` (New Send Order — Rule 5)

| # | Action | Bangla label | English |
| --- | --- | --- | --- |
| 1 | Find the prompt box, switch to Task mode, and select `4 Argon` | `টাস্ক` + `4 Argon (Complex tasks)` | Task (`চ্যাট` = Chat) + 4 Argon |
| 2 | Paste the prompt into the composer | — | Paste via clipboard (`Control+V`) |
| 3 | Attach the context archive | `+ Gemini-কে ডিজাইসো করুন` | Upload `argon_context.zip` and wait for upload spinner to clear |
| 4 | Click Send the moment upload finishes | `পাঠান` | Immediately click Send (after verifying the hard Flash guard) |

Chat mode cannot do this work — Task mode and `4 Argon` are mandatory.

### Google AI Studio was removed

Google AI Studio is **not** a supported workspace any more. It was tried by hand
on real agentic work and it failed every time, so the whole AI Studio workflow
was deleted from this project: there is no `--mode aistudio`, no AI Studio URL
and no AI Studio button. Gemini Web (`https://gemini.google.com/u/<slot>/app`,
Task mode + `4 Argon`) is the only workspace.

### The dispatcher enforces this for you

`scripts/dispatch_argon.py` performs the unified send sequence (`_execute_composer_send_sequence`) and **verifies** every step:

```bash
python "./skills/argon-coding-bridge/scripts/argon_pool_login.py" --all --wait-minutes 6   # sign the pool in
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Describe the change" --project-path . --slot /u/0/ --auto-apply
```

- 1st step: find the composer, verify Task mode active and the `4 Argon` model selected
- 2nd step: paste the surgical prompt into the composer
- 3rd step: attach `argon_context.zip` and wait until its upload spinner clears (using an upload window scaled to the archive's byte size)
- 4th step: the moment the upload finishes, verify the hard Flash guard and click Send immediately

**If any check fails the prompt is NOT sent.** Fix the workspace or let the retry ladder / self-executed manual send fallback handle it.

### The Hard Flash Guard (Rule 6 — A Task Must Never Go Out on Flash)

Sending a task to **Flash** instead of **4 Argon** is strictly forbidden: the task always runs on the **Argon** model, never on Flash, and a hard safety guard aborts the send if Argon cannot be selected:

1. **Before every send (both new chats and follow-ups)** - Because Gemini reverts the model chip to the account default (often `Flash`) after a message is sent, `ensure_argon_model()` and `enforce_no_flash_before_send()` inspect the **model chip** next to the composer on both new chats and follow-ups:
   - If the chip reads `Flash` or anything other than `4 Argon`, the model picker is opened and `_inspect_option_in_picker()` selects `4 Argon`.
   - If `4 Argon` is **disabled** (`aria-disabled="true"` or `disabled`) or **missing** and the chip still does not read `4 Argon` after retries, **the hard guard aborts the send immediately (`EXIT_PREFLIGHT_FAILED = 3`)** so nothing is ever sent on Flash, and a new task rotates to the next available pool account.
2. **After the send** - the chip is read once more for diagnostic logging.

The chip's own text is what counts. "4 Argon" being *visible* on the page proves nothing: the open
model picker lists every model at once, so `4 Argon` can be on screen while `3.8 Flash` is the
checked one - that false positive is what sent real tasks to Flash, so it was removed.

A follow-up in an existing conversation gets the exact same hard guard: an existing chat reverts to
Flash after a turn, so the chip is verified and switched back to `4 Argon` before every follow-up send.

### Three guards that keep the round trip honest

1. **The packager never packs the workflow's own artefacts.** `apply_changes.ps1`,
   `argon_context.zip`, `argon_task_prompt.md`, `.argon_backup/` and `downloads/` are always
   excluded, whatever `.gitignore` says. Otherwise a previous `apply_changes.ps1` would be uploaded
   as the "deliverable" and the round trip would feed on itself.
2. **A delivery must be newer than the prompt.** The dispatcher only accepts a file that appeared
   after the prompt was sent, so a stale `apply_changes.ps1` in the project root can never be
   mistaken for Argon's answer.
3. **A delivery must really be a patch script.** It has to pin `$TargetRoot` to a quoted path,
   contain a write or replace operation, and contain no markdown. Prose that merely quotes the
   prompt is rejected.

### How the delivered file is picked up

Gemini hands the patch over as a **generated-file chip** (`apply_changes` / `PS1`) with an "Open"
button, not as a browser download. Clicking that button opens Gemini's Drive viewer, which holds
the whole file. The dispatcher therefore:

1. clicks the chip's button and waits for the viewer to render;
2. presses the viewer's **Download** button and keeps the downloaded bytes;
3. if the button is not reachable, fetches the same file from Drive's `viewer/text`
   endpoint **page by page** and stitches the pages;
4. validates the result, then applies it through `Run-ArgonPatch.ps1`, which backs up every touched
   file first and prints the rollback command.

### The delivery rule: always the WHOLE file, never a truncated preview

**A truncated preview is never used as the source of a delivery.** The Drive viewer only renders
what fits in the viewport, so a long patch read from the preview can stop half-way - and a
half-written patch fails the apply step with "Target block not found" even though Argon's delivery
was perfectly fine.

The second source - Drive's `viewer/text` endpoint - is **paged**: it serves the file in PAGES and
**page 0 simply stops mid-statement with no error at all**. Reading one page therefore accepts a
HALF patch that still parses. The bridge walks `page=0, 1, 2 ...` until a page comes back empty and
stitches the chunks, so the file it writes is the whole file. Measured on a 544 KB delivery: page 0 =
262 114 characters, page 1 = 261 605, page 2 = 20 639 - the whole file is 544 927 bytes and its
SHA-256 matches the file downloaded by hand.

The file id cannot be read from the preview (the Drive viewer crashes headless), so it is recorded
from the network request the Gemini page fires when the chip is clicked.

When neither way produces a complete file, the capture **fails loudly and writes nothing**:

```
[!] The delivery could NOT be downloaded after 1/3 download attempt(s) for this chip - <the real reason>.
    A truncated file is never accepted as a patch: it fails the apply step.
    Download the file from the Gemini chat by hand and apply it with:
        Open_Argon_Apply.bat -PatchFile <path-to-apply_changes.ps1>
```

A downloaded file is also checked for **completeness** before it is accepted: it has to carry the
`[SUCCESS]` marker, a `NOTES` block that was opened must be closed, and no here-string may be left
unterminated. A file that fails those checks is reported as truncated and is **not** written, so a
partial patch can never be applied by mistake.

### The project is verified LOCALLY - no extra Gemini round

After the patch is applied, the bridge runs the project's own build/lint command
**on this machine** (`cargo check`, `npm run lint`, `ruff`, `go build`, `dotnet
build`, `make`, ... - detected from the project root, or given with
`--verify-cmd`).

That check runs locally and never talks to Gemini. When it passes,
the run reports `Project verification: <cmd> -> clean`. When it fails, the errors
are printed and recorded in the task state and in the notes file, and the run ends
with exit code `8`.

**The agent that drove the bridge fixes those errors itself.** Lint problems,
a lost brace, a wrong import - they are small and local, and fixing them locally is
immediate. The bridge deliberately does NOT send a follow-up to Argon for them.

**Ask Argon again only for a genuinely major or difficult task** - a redesign, a
migration, something that needs real reasoning. That decision belongs to the
agent, not to this script.

### Three checks that stop a broken patch before it reaches the project

| check | what it catches |
|---|---|
| `validate_patch_payloads()` | a raw control character, a line starting with `@'`, a `$` inside a double-quoted here-string, a typographic quote - the payload is refused BEFORE any file is written |
| `verify_written_files_bytes()` | a raw control byte that appeared in a written file although it was not there before |
| `verify_written_files_structure()` | a file that was balanced before the patch and is unbalanced after it - a brace, parenthesis or bracket was lost |

### Argon gets the real API, so it never has to guess

The packager writes **`API_INDEX.md`** into the archive: every public declaration
of every source file, with its file and line, complete and not truncated. The
task brief points Argon at it and says: use that, never invent a signature, and
name the missing symbol instead of guessing.
### The carry-over rule: a run without a delivery starts a NEW session

A run can end without a delivery: Gemini stops working, the chat dies mid-task, or
the answer never arrives. Losing the work there would be a waste, so the bridge
does not simply fail - it **carries the task over into a new session**:

1. a **new prompt** is written that carries the previous editing context - the
   original task, what the previous conversation already produced, and the notes
   it recorded - and it tells Argon to *continue* from that state instead of
   starting the work over;
2. a **NEW session** is started (a fresh chat, not the old one);
3. the project is packed again and re-attached, because the previous round may
   already have changed files;
4. the workspace is set up again (Task mode + 4 Argon, with the double check) and
   the carry-over prompt is sent there.

The new conversation replaces the stored one, so the next task continues there.

### Before that: "still loading" is not "failed"

A conversation that is **still loading** or **still thinking** must never be
declared finished. The old error banner of an earlier round stays in the chat,
and the sidebar shows a spinner while the conversation is loading - that looks
exactly like a failed run while the work is actually still going on.

So before anything is carried over, the run is re-checked:

- the **stop button** is gone, **"Thinking it through" / "বিস্ময় নিয়ে ভাবছে" / "Gemini is typing"** are gone and **no spinner / progressbar** is on the page, and
- the conversation has been **quiet for 90 seconds** - no new text at all.

Only then is the run treated as finished without a delivery. While the chat is
loading or thinking, the run simply keeps waiting.

### The prompt is PASTED, never typed - the hang rule

A prompt that is TYPED into Gemini's Quill editor does not get slower, it gets
**stuck**. `keyboard.insert_text` and `press_sequentially` dispatch one event per
character, and Quill renders every one of them. A 26 KB prompt therefore takes
minutes of character-by-character work - the run prints
`Composer populated (length=25693 chars). Sending...` and then sits there
forever. It looks exactly like a hang, and nothing is happening.

So the prompt is **always pasted**:

1. the prompt goes into `navigator.clipboard` and is pasted with `Control+V` -
   one event, so it is equally fast for 1 KB and for 26 KB;
2. a big prompt is pasted in **8 KB chunks**, and every chunk is verified, so a
   paste that loses part of the text is caught instead of sending half a prompt;
3. typing is only used as a fallback for a prompt of **2 KB or less** - that is
   the only size where typing is fast enough to be safe;
4. the send uses the **keyboard** (`Control+Enter`), which never waits for the
   editor to become "stable" - a huge editor can take minutes to settle, and
   `composer.press()` waits for exactly that. If the composer does not empty,
   the **send button** is clicked, which is quick;
5. the whole send has a **deadline**. When it expires the run fails loudly with
   a clear message instead of hanging.

A prompt larger than 8 KB is also worth a second look: it usually means the task
description is carrying the codebase, and the codebase belongs in
`argon_context.zip`.
### The session book: a run is always findable again

The moment a run starts, the session is written down - not when it finishes. A browser
that dies in the middle must not make the work unfindable.

Every entry records:

| what | where it is stored |
|---|---|
| the project it belongs to | the key of the entry |
| the **name of the conversation** (what the user sees in the sidebar) | `chat_title` |
| the **exact link** to the conversation | `chat_url` |
| the **account and the profile number** | `slot`, `profile_number` |
| the **browser profile directory** the run used | `profile_dir` |
| where the run got to | `state` (`sent` / `waiting` / `delivered` / `applied`) |
| when it was last touched | `updated_at` |

To find a run again:

```bash
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --list-sessions
```

It prints the project, the conversation name, the link, the account, the profile and
whether the work finished - so a session can be opened by its link, recognised by its
name, or resumed with `--resume`.

A link that points at the removed Google AI Studio is flagged as **STALE**, because it
cannot be opened any more. `--forget-session <project>` removes such leftovers.

### A follow-up is as stable as a new chat

A follow-up runs in a conversation that is already full: an earlier round's answer, its
delivery chip and a floating quick-action bar are all on the page. That is what used to
make follow-ups jump around and click the wrong things, so the flow was tightened to
match a fresh chat:

1. **the composer is the bottom-most editable** on the page - never the floating
   quick-action bar's editor in the middle;
2. **the upload menu opens on the real composer's own `+` button** - the floating
   bar's `+` is never clicked;
3. **the composer is focused through the DOM, without a mouse click** - no stray
   cursor movement, no accidental click on the conversation;
4. **only the delivery chip of the NEW assistant turn is opened** - an earlier round's
   chip is never clicked, so the old file can never be downloaded again;
5. the prompt is pasted through the clipboard, the send is verified, and the model
   double check runs right before it goes out.
### Argon verifies its own work

The delivered script does not stop at "the file was written". The prompt requires a verification
phase, so the script re-reads every file it just changed from disk and asserts that the new content
is really there:

- `[Verified] <relative path>` - the file on disk now matches what Argon intended;
- `[FAILED VERIFY] <relative path>` - it does not, and the script exits with a non-zero code.

The runner surfaces that output, so a run can never look green on an unverified change.

---

### Session continuity: the next task continues where the last one stopped

One Gemini conversation is kept **per project**. The first task on a project opens a chat; every
later task on the same project is sent into that same chat, so Argon still has the earlier codebase
understanding, the earlier decisions and the earlier ideas in front of it. Only the freshly packaged
archive is new - it is the current state of the files.

```bash
# Uses the project's existing conversation when there is one.
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Next change" --project-path .

# Force a brand new conversation for this project:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Next change" --project-path . --new-session

# Dual-session dispatch (follow-up in the recorded winning chat + brand-new chat on a second slot):
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --dual --task "Next change" --project-path .

# Record which lane won after comparing both delivered patches:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --record-winner followup
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --record-winner new

# Compare two delivered patches side by side, open comparison.html, and build/apply a single-run merged patch:
python "./skills/argon-coding-bridge/scripts/compare_argon_patches.py" --project-path . --open
python "./skills/argon-coding-bridge/scripts/compare_argon_patches.py" --project-path . --merge --choices "1=new,2=followup" --apply
```

The map lives at `~/.gemini/argon_sessions.json` - in the user profile, next to the browser profile,
so every copy of this plugin (the repo, the Gemini CLI plugin, the MiniMax plugin) shares it. It is
keyed by the absolute project path. The dispatcher records the conversation id automatically once
Gemini has created the chat, and prints the URL it will continue in.

---

### Pool slot selection: busy-task rotation for new tasks, session affinity for follow-ups

The pool is tried in slot order (`/u/0/`, `/u/1/`, ...) starting from `--slot` (or the project's
stored session slot in `~/.gemini/argon_sessions.json`, falling back to `active_slot`), wrapping
around:

- **Follow-up tasks**: Always use the account slot that owns the conversation, even when that
  account already has multiple tasks running.
- **New tasks**: Skip an account only when it is already running the maximum number of concurrent
  tasks (`MAX_TASKS_PER_SLOT = 2`). No account is ever shut down or skipped for any other status.

```bash
# Picks an available slot under MAX_TASKS_PER_SLOT and rotates if a slot fails pre-flight:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Describe the change" --project-path .

# Limit how many accounts are tried:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "..." --project-path . --max-accounts 3
```

---

### Parallel isolation: lock files, per-slot profiles, and SQLite cookie seeding

- **Where lock files live & how a busy slot is detected and announced**: Every slot `/u/N/` uses a
  lock file at `~/.gemini/argon_locks/slot-uN.lock` containing JSON with `pid`, `slot`,
  `started_at`, and `profile_dir`. If the recorded `pid` is no longer alive (`_pid_alive`), the
  stale lock is reclaimed automatically. If the PID is alive (or `_profile_is_busy` detects an
  active Chrome `SingletonLock`, Windows `lockfile`, or locked `Default/Network/Cookies`), the
  dispatcher announces `[!] Slot /u/N/ is already in use by pid <PID> ...`, refuses to touch the
  other run's browser, and either rotates to the next pool slot or exits with code `7` (`EXIT_SLOT_BUSY`)
  when no slot is free. Locks are always released in a `finally` block.
- **How the shared Argon Bridge browser and `/u/<index>/` switching work**: Every slot runs in the
  single shared Argon Bridge browser profile (`~/.gemini/argon_browser_profile`) by default
  (`PoolConfig.is_shared_profile_slot()` is `True`). `argon_pool_login.py` and `login_pool_accounts.py`
  sign every pooled account into that one profile using Google's `AddSession` flow, verify each
  account's position in `Default/Preferences` (`account_info`) via `verify_account_index_in_preferences`,
  and record it with `PoolConfig.set_account_index(slot, index)`. Because all accounts share one
  browser, RAM and disk stay flat regardless of how many accounts or concurrent tasks are running, and
  the owner's daily browser is never touched. Old isolated `~/.gemini/argon_profiles/slot-uN/`
  directories can be reclaimed on demand with `--reclaim-slot-profiles` once their accounts are
  verified in the shared profile.

---

### Resuming a closed browser (`--resume`) and capturing Argon notes

- **How a closed browser is resumed**: `~/.gemini/argon_task_state.json` stores per-project task
  progress (`state`, `chat_url`, `chat_id`, `slot`, `email`, `task`). If the browser is closed
  mid-run (exit code `4`), running `dispatch_argon.py --project-path . --resume [--auto-apply]`
  reopens the recorded conversation URL without re-packaging or sending a second prompt, and either
  captures and applies the already-delivered `apply_changes.ps1` chip or keeps waiting for the
  answer.
- **How notes are captured**: Argon appends a `# --- NOTES: ... # --- END NOTES ---` comment block
  at the end of `apply_changes.ps1`. The dispatcher extracts those lines via `_extract_notes` /
  `_save_notes`, writes them to `.argon_backup/argon_notes_<timestamp>.md` inside the project, and
  prints each note to the terminal.

---

## Step 4: Apply the Delivered Patch (one click)

Argon delivers `apply_changes.ps1` as a downloadable file. After downloading it, double-click
`Open_Argon_Apply.bat` at the repository root: it finds the newest delivered patch, validates it
(strips a BOM, runs a PowerShell parse check and refuses to run a truncated file), then applies the
whole change set to the project in a single run.

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File "./skills/argon-coding-bridge/scripts/Run-ArgonPatch.ps1" -ProjectPath "<ABSOLUTE_PROJECT_PATH>"
```

Roll back afterwards with:
```powershell
# Rollback safely:
pwsh -NoProfile -ExecutionPolicy Bypass -File "./skills/argon-coding-bridge/scripts/Apply-ArgonPatch.ps1" -ProjectPath "<ABSOLUTE_PROJECT_PATH>" -Rollback
```
Your agent will automatically refresh and reflect the updated files cleanly!

---

## Optional GenUI Visualizer

- **Companion Skill**: This plugin may ship an optional companion skill named `argon-coding-bridge-visualizer` alongside `argon-coding-bridge`.
- **Interactive Run Dashboard**: When the host coding agent supports GenUI widgets (such as MiniMax Code or other `mavis.widget.v1`-capable clients), `argon-coding-bridge-visualizer` renders the bridge run—the packaged context archive (`argon_context.zip`), the surgical prompt that was sent, the captured delivery (`apply_changes.ps1`), the applied files, and the blast radius—as an interactive dashboard instead of plain text.
- **Strictly Optional & Agent-Neutral**: The visualizer is completely optional. Any host agent that does not load or support `argon-coding-bridge-visualizer` (including Claude Code, Cursor, Gemini CLI, Antigravity, OpenCode, or text-only environments) completes the entire workflow with standard text output alone.
- **Immutable Forked Foundation (`mavis.widget.v1`)**: The companion skill carries its own `genui.json` (forked GenUI foundation, protocol `mavis.widget.v1`) together with `references/components.md`, `references/data-schema.json`, and `references/examples.md`. Do not modify any of those files, do not modify the forked foundation under `references/genui-widget/`, and do not alter the `sourceDigest` in `genui.json`—the foundation is verified by a strict digest check.
- **Zero Extra Dispatch**: The visualizer consumes the exact same artefacts that the text workflow already produces (`argon_context.zip`, `TASK_BRIEF.md`, `apply_changes.ps1`, and `.argon_backup/`), so rendering the dashboard must never trigger a second dispatch to Gemini 4 Argon.
