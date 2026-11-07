---
name: argon-coding-bridge
description: Delegate complex coding, debugging, and refactoring tasks from any local coding agent (Claude Code, Cursor, Gemini CLI, Antigravity, OpenCode, MiniMax Code, ...) to Gemini 4 Argon via Task mode in Gemini Web. Packages the codebase into a clean, gitignore-aware ZIP (filtering build dependencies, node_modules, and secrets), generates surgical zero-UI-regression prompts, continues follow-up tasks in the same conversation, and automatically applies the returned PowerShell patch script.
---

# Argon Coding Bridge (Agent Plugins 1.0 - for any coding agent)

Bridge **any local AI coding agent** with **Gemini 4 Argon** running in Gemini Web (**Task Mode**). Gemini Web is the only supported workspace - Google AI Studio was removed because it consistently fails on agentic work.

## Why Use This?
- **Massive Reasoning Multiplier**: Delegate heavy, multi-file architectural refactoring, complex bug diagnosis, and deep code synthesis to Gemini 4 Argon while continuing your main workflow locally.
- **Zero UI Regression Guarantee**: Strict surgical instructions prevent Gemini from rewriting whole files or altering existing CSS, Tailwind, Flutter widgets, or HTML layouts.
- **Strict Dependency & GitIgnore Filtering**: Accurately compresses only authentic source code, stripping `node_modules`, `vendor`, `build`, `.gradle`, `Pods`, `.next`, binaries, and private secrets (`.env*`).
- **PowerShell Exact Replacement Patch**: Changes are delivered in a clean script using `$content.Replace()` with safety checks, single-quoted Here-Strings (`@' ... '@`), and automatic backups with rollback.
- **Session Continuity**: A follow-up task on the same project continues in the SAME Gemini conversation, so the earlier codebase understanding, decisions and ideas stay available.
- **Self-Verification**: The delivered patch re-reads every file it writes and prints `[Verified] <path>`, so Argon proves its own work instead of merely claiming success.

---

## Working rules

These rules are how the agent that drives this bridge works. They are not
suggestions.

### 1. Dispatch preparation is quick, and the archive is complete

The agent focuses on the USER'S PROMPT and the intent behind it.

- It works out which codebase can be related to the user's problem and puts
  **the whole of it** into the zip. The file set is never smaller than that.
  A few extra files are fine - a narrow archive is not.
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

### Step 3 - Send the FOLLOW-UP first
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --task "<task>" --slot "<the account that holds the chat>" --lane followup`
Why:  the follow-up runs in the conversation that already knows this codebase.
      Send this one FIRST and let it finish.

### Step 4 - CHECK the follow-up before you go on
Read the screenshot of the run (the path is printed) and check three things:
  a. the conversation was in TASK mode (not Chat),
  b. the model chip said 4 Argon,
  c. Argon produced a real answer, not a chat-style reply.
If anything is wrong, FIX IT FIRST and send the follow-up again.
Only when the follow-up is confirmed good do you continue to Step 5.

### Step 5 - Then run the NEW task
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --task "<task>" --new-session --slot "<another account>"`
Why:  a new task belongs in a new conversation, on a different account, so the
      two runs never collide.

### Step 6 - Compare and apply
Command:  `python "./skills/argon-coding-bridge/scripts/compare_argon_patches.py" --followup-patch "downloads/followup/apply_changes.ps1" --new-patch "downloads/new/apply_changes.ps1" --merge --pick new --apply`
Why:  compares the follow-up delivery and the new-session delivery side by side,
      combines their non-conflicting blocks (using `--pick new` or `--choices`
      when conflicts exist), applies the resulting single-run patch, and runs
      the local build verification once. Record the winning lane afterwards with:
      `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --record-winner followup|new`

### Step 7 - Mark the task completed
Command:  `python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --register-task-done --label "<same label>"`
Why:  releases the task in `~/.gemini/argon_tasks.json` so the next round sees
      accurate slot load counts.

### The rules that belong in this workflow

1. **The follow-up always goes to the account that holds the chat** - never
   rotated. Even when that account is already busy with two tasks, that chat
   lives there, so USE it and say so in one line. Do not refuse, do not block:
   the work needs that conversation.
2. **A new task prefers an account that is not busy.** When several accounts
   are free, pick one of those. When the only free slot is busy, or when no
   account is free, the run continues on the least loaded account and PRINTS
   the reason. **Never refuse to work because an account is busy.**
3. **At most two concurrent tasks per account** - that is the limit, not a
   wall. A third task on the same account should be avoided, not blocked.
4. **Never send a prompt in Chat mode.** If the mode cannot be proven, the
   agent opens a NEW chat, switches it to Task, and checks the chip before it
   sends. If a follow-up is refused with `mode = Task : UNKNOWN`, inspect the
   conversation: if the user confirms it is a Task-mode conversation, record it with
   `python ./skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path . --record-workspace-mode <chat-id> --mode task`
   and retry. Never record a mode that has not been confirmed.
5. **Show the brief before it is sent.** The agent summarizes what it understood
   and waits for user approval.
6. **Record what you fixed.** When fixing defects, record debugging notes with
   `record_fix.py` and commit on a dedicated `fixes/<scope-slug>` branch.
5. **Read the step screenshot before every decision.** The terminal line is a
   summary; the screenshot is the evidence.

### Troubleshooting Table (Symptom -> Root Cause -> Action)

| Symptom | Root Cause | Action |
|---|---|---|
| **The answer is a chat-style reply instead of work** | The prompt went out in Chat mode. | Open a new chat (`--new-session`), switch it to Task mode, check the `4 Argon` chip in `.argon_backup/shots/<NN>-preflight.png`, and resend. |
| **Gemini says the archive is unreadable / binary data** | Binary files (fonts, images, media, or non-UTF-8 data) were included in the archive. | The packager must exclude every binary file by extension and 8 KB NUL/UTF-8 content check and enforce the archive cap; re-run `package_context.py` to rebuild `argon_context.zip`. |
| **The prompt landed in an old conversation** | The new chat was not verified empty after loading. | The run must verify after the conversation loads that there are zero user messages and zero assistant turns and report the `chat_id` it used; re-run with `--new-session`. |
| **Old delivery downloaded again** | A previous round's `apply_changes.ps1` chip remains in the conversation DOM and a capture path did not check `DeliveryLedger` or opened a chip before the last user message. | `DeliveryLedger` (`~/.gemini/argon_delivery_ledger.json`) and `_element_is_after_last_user_turn` refuse known SHA-256 digests and chips prior to the last user turn; keep waiting with `--resume` until the new turn's chip appears. |
| **No download after the task finished** | Argon finished answering inline or the viewer toolbar button did not trigger before timeout. | Read `.argon_backup/shots/<NN>-delivered.png` or `failed.png`; if a chip is visible in the newest turn, run `--resume` to capture it or download `apply_changes.ps1` from the viewer toolbar and run `Open_Argon_Apply.bat`. |
| **Browser opens again and again** | A retry loop re-opened the preview or re-dispatched without checking `TaskState` / `DeliveryLedger`. | Single-capture mode exits immediately after one valid toolbar download. Use `--list-tasks` to inspect active runs. |
| **A task is already running on this account (`EXIT_ALREADY_RUNNING = 9`)** | `TaskState` or `TaskRegistry` shows an in-flight `sent` or `waiting` task for this exact project/lane. | Read the printed screenshot path with your image reader; run `--resume` to wait for the running task, or pass `--force` to explicitly mark the old task `superseded` and start fresh. |
| **Account is quota-limited (`EXIT_QUOTA_LIMITED = 5`)** | The account hit its Gemini 4 Argon usage limit or `'4 Argon'` is `aria-disabled` in the model picker. | The bridge automatically marks the slot `quota_limited` in `accounts_pool.json` and rotates to the next free pool account. Add more accounts via `Open_Argon_Login.bat` (Import Box / Load from `.txt`) if all slots are exhausted. |
| **Model chip says something else than Argon** | Before send: the picker did not switch to `4 Argon`. After send: the chip naturally reverts to the account default (e.g. `Flash`). | Before send, pre-flight waits up to 45 s for the chip and refuses to send if it is not `4 Argon`. After send, check the answer evidence (`_verify_argon_answer_evidence`), not the post-send chip. |
| **Account is busy with running tasks** | Another task is already active on that slot in `TaskRegistry` or Chrome is open on the slot profile. | Never block: a follow-up uses the account that holds the chat and says so in one line; a new task picks a free account or falls back to the least-loaded account and prints the reason. |
| **Delivery is truncated or 0 bytes** | The browser handed over a 0-byte download or an incomplete script missing `[SUCCESS]` / balanced `@'...'@` blocks. | `_validate_and_save_download` stages downloads and rejects `< 64`-byte files without overwriting good patches, and `_patch_is_complete` verifies payload parsing. Re-run with `--resume` to re-capture from the viewer toolbar. |
| **Second prompt superseded an unfinished task** | Another prompt was sent into the same conversation while the earlier task was still `sent` or `waiting`. | `_check_superseded_prompt` detects the newer user message in the DOM, marks the earlier task `superseded` in `TaskState` and `TaskRegistry`, and stops waiting immediately. |
| **New session opened a foreign conversation** | Navigating to `/u/0/app` restored the account's previous chat from another project. | `_start_fresh_chat` waits for the conversation to finish loading, clicks the sidebar **New chat** button, verifies zero user/assistant turns and that the URL chat ID is not in `_known_chat_ids_for_slot`, and reports the chat ID if it cannot prove the chat is empty. |

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
`skills/argon-coding-bridge/config/accounts_pool.json`. Each slot `/u/N/` gets its own isolated
Chrome profile directory at `~/.gemini/argon_profiles/slot-uN/` (seeded from the master profile
`~/.gemini/argon_browser_profile`), and both `argon_pool_login.py` and `login_pool_accounts.py`
sign each account directly into that account's `slot-uN` profile so parallel runs never collide.

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
- Allows files up to 15MB (safely accommodates large code/schema/JSON data files).
- Injects `TASK_BRIEF.md` containing the project overview into the zip.

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

- For edits in existing files, use safe exact substring replacements ($content.Replace) and throw an error if the snippet is not found:
  ```powershell
  $path = Join-Path $TargetRoot "relative/path/to/file.ext"
  $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
  $oldTarget = @'
// EXACT existing lines
'@
  $newReplacement = @'
// Updated replacement lines
'@
  if (-not $content.Contains($oldTarget)) { throw "Target block not found in $path. Aborting." }
  $backupSession = Join-Path (Join-Path $TargetRoot '.argon_backup') (Get-Date -Format 'yyyyMMdd_HHmmss')
  $backupDest = Join-Path $backupSession $relPath
  $backupParent = [System.IO.Path]::GetDirectoryName($backupDest)
  if ($backupParent -and -not (Test-Path $backupParent)) { New-Item -ItemType Directory -Path $backupParent -Force | Out-Null }
  Copy-Item -LiteralPath $path -Destination $backupDest -Force
  $content = $content.Replace($oldTarget, $newReplacement)
  [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
  $verify = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
  if (-not $verify.Contains($newReplacement)) { Write-Host "[FAILED VERIFY] $relPath" -ForegroundColor Red; exit 1 }
  Write-Host "[Verified] $relPath" -ForegroundColor Cyan
  Write-Host "[Patched] relative/path/to/file.ext" -ForegroundColor Green
  ```
- Back up every file before overwriting it into `.argon_backup/<yyyyMMdd_HHmmss>/<relative path>`.
- Use single-quoted Here-Strings (`@' ... '@`) so `${variable}` template literals are not corrupted.
- Write files using UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.
```

---

## Step 3: Configure the Workspace, Then Dispatch

The workspace must be configured **before** the prompt is sent. A prompt sent in Chat mode, on the
wrong model, or **without the zip attached** makes Gemini answer *"argon_context.zip did not come
through with your message"* and wastes the whole round trip.

> **The UI language follows the signed-in Google account.** This pool renders Gemini in Bangla, so
> English-only text selectors silently miss. Both labels are given below.

### Gemini Web — `https://gemini.google.com/u/<slot>/app`

| # | Action | Bangla label | English |
| --- | --- | --- | --- |
| 1 | Switch the mode toggle to Task | `টাস্ক` | Task (`চ্যাট` = Chat) |
| 2 | Model dropdown | `4 Argon (Complex tasks)` | 4 Argon |
| 3 | Attach the context | `+ Gemini-কে ডিজাইসো করুন` | Upload to Gemini -> `argon_context.zip` |
| 4 | Send the prompt | — | — |

Chat mode cannot do this work — Task mode is mandatory.

### Google AI Studio was removed

Google AI Studio is **not** a supported workspace any more. It was tried by hand
on real agentic work and it failed every time, so the whole AI Studio workflow
was deleted from this project: there is no `--mode aistudio`, no AI Studio URL
and no AI Studio button. Gemini Web (`https://gemini.google.com/u/<slot>/app`,
Task mode + `4 Argon`) is the only workspace.

### The dispatcher enforces this for you

`scripts/dispatch_argon.py` performs all of the above and **verifies** every step before sending:

```bash
python "./skills/argon-coding-bridge/scripts/argon_pool_login.py" --all --wait-minutes 6   # sign the pool in
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Describe the change" --project-path . --slot /u/0/ --auto-apply
```

- Task mode active and the `4 Argon` model selected (Gemini Web)
- the zip really attached to the composer

**If any check fails the prompt is NOT sent.** The run exits with code `3`, prints the bilingual
checklist above, and writes a screenshot to `.argon_backup/preflight_failure.png` in the project. Fix
the workspace by hand and re-run with `--manual-setup` to skip only the mode/model clicks — the
dispatcher still attaches the zip and sends the prompt.

### The model double check (the most important guard)

Sending a task to **Flash** instead of **4 Argon** is the worst failure this bridge can have: the
work is lost and the quota is wasted. So the model is verified twice, and against the only signal
that cannot lie - the **model chip** next to the composer, which shows the model that will really
answer:

1. **Before the send** - `ensure_argon_model()` reads the chip. If it does not name Argon, the model
   picker is opened and `_inspect_option_in_picker()` inspects the `4 Argon` option:
   - If `4 Argon` is **present but disabled** (`aria-disabled="true"` or `disabled`), the bridge reads
     which account is signed in, reports `"this account has no Argon available"`, marks the slot
     `quota_limited` in `accounts_pool.json`, and returns exit code `5` (`EXIT_QUOTA_LIMITED`) so the
     dispatcher immediately **rotates to the next pool account** instead of stopping.
   - If `4 Argon` is **missing** (the picker did not open) and the chip still does not read Argon
     after repeated attempts, **the prompt is not sent** and the run stops with exit code `3`.
2. **After the send** - the chip is read once more, and a warning is printed if the UI fell back to
   another model.

The chip's own text is what counts. "4 Argon" being *visible* on the page proves nothing: the open
model picker lists every model at once, so `4 Argon` can be on screen while `3.8 Flash` is the
checked one - that false positive is what sent real tasks to Flash, so it was removed.

A follow-up in an existing conversation gets the same treatment: an existing chat can drop back to
Flash at any time, so the chip is re-read before every prompt, not only on the first one.

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
3. validates the result, then applies it through `Run-ArgonPatch.ps1`, which backs up every touched
   file first and prints the rollback command.

### The delivery rule: always the Download button, never the preview

**The preview is never used as the source of a delivery.** The Drive viewer only renders what fits
in the viewport, so a long patch read from the preview can stop half-way - and a half-written patch
fails the apply step with "Target block not found" even though Argon's delivery was perfectly fine.
That is why the file is always taken from the **Download** button: those are the real bytes.

When the download does not produce a file, the capture **fails loudly and writes nothing**:

```
[!] The delivery could NOT be downloaded - the viewer's download button did not produce a file.
    The preview text is deliberately NOT used as a substitute: a preview can be truncated, and a
    truncated patch fails the apply step.
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

That check costs **no quota at all** - it never talks to Gemini. When it passes,
the run reports `Project verification: <cmd> -> clean`. When it fails, the errors
are printed and recorded in the task state and in the notes file, and the run ends
with exit code `8`.

**The agent that drove the bridge fixes those errors itself.** Lint problems,
a lost brace, a wrong import - they are small and local, and fixing them costs
nothing. The bridge deliberately does NOT send a follow-up to Argon for them,
because every extra Gemini round burns the owner's hourly, weekly and monthly
quota.

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

### Quota rotation: when an account hits its limit, the next one takes over

Google limits each account's Gemini usage on a rolling 3-4 hour window. When the window is
exhausted, Gemini shows a **limited** state and never answers. The dispatcher detects that state
instead of waiting for an answer that will never come, marks the account as `quota_limited` in
`config/accounts_pool.json`, and automatically continues with the **next** account in the pool.

```bash
# Tries the accounts in pool order and moves on when one is rate limited:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Describe the change" --project-path .

# Limit how many accounts are tried:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "..." --project-path . --max-accounts 3
```

The pool is tried in slot order (`/u/0/`, `/u/1/`, ...) starting from `--slot` (or the project's
stored session slot in `~/.gemini/argon_sessions.json`, falling back to `active_slot`), wrapping
around. Healthy accounts are tried first, followed by accounts marked `quota_limited` so a limit
that clears on its own needs no manual intervention.

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
- **How per-slot profiles work**: Each slot runs in `~/.gemini/argon_profiles/slot-uN/`. When a new
  slot profile is created, `_seed_profile` copies the master profile (`~/.gemini/argon_browser_profile`)
  only if the master profile is not currently busy (announcing clearly if the master is in use
  rather than creating a signed-out copy) and snapshots `Default/Network/Cookies` via SQLite's
  online backup API (`sqlite3.connect(...).backup(...)`). `argon_pool_login.py` and
  `login_pool_accounts.py` also sign each account directly into its own `slot-uN` profile. If a slot
  profile cannot be prepared, the dispatcher falls back to the shared master profile only when it is
  free.

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
