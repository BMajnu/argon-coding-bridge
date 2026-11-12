# Multi-Account Browser Workflow & Gemini 4 Argon Dispatch Guide

This guide details how to leverage 8–12 Gemini Pro / Advanced accounts across browser sessions (Demon Browser, Chrome profiles, Edge, or automation agents) to execute complex coding workloads using **Gemini 4 Argon**, from any local coding agent — Claude Code, Cursor, Gemini CLI, Antigravity, OpenCode, MiniMax Code, or others.

Two rules matter more than everything else in this file:

1. **The account slot is part of the URL.** Strip it and you land in a different session.
2. **The UI language follows the account.** This pool renders in Bangla, so an English-only click misses *silently* — and a missed click is not a visible error, it is a wrong run.

---

## 🔑 1. One Argon Bridge Browser, Every Account Inside It, and Verified `/u/<index>/` Switching

The bridge uses **ONE dedicated Chrome browser** — the **Argon Bridge browser** (`~/.gemini/argon_browser_profile`) — and **every pooled Google account lives inside it**. The owner's own daily browser is **never** touched by the bridge.

- **Gemini Web**: `/u/<index>/app`

```text
https://gemini.google.com/u/0/app
```

> **Google AI Studio was removed.** It was tried by hand on real agentic work and
> it failed every time, so the whole AI Studio workflow was deleted from this
> project: no `--mode aistudio`, no AI Studio URL, no AI Studio button. Gemini Web
> is the only workspace. Do not re-add it.

- **`/u/<index>/` is the verified browser account index, NOT the pool slot number.** Inside the shared Argon Bridge browser, `<index>` is the 0-based position of that Google account inside `~/.gemini/argon_browser_profile/Default/Preferences` (`account_info`). After each account is signed in, `verify_account_index_in_preferences()` reads its real index and `PoolConfig.set_account_index(slot, index)` records it (`account_index`).
- **One browser means flat RAM and flat disk.** Because all accounts and task tabs share one Argon Bridge browser process (~1.4 GB RAM and ~2–3 GB disk instead of 62 processes, 7 GB RAM, and 68 GB disk across per-slot profiles), memory and disk usage stay flat no matter how many accounts or concurrent tasks are running.
- **Never strip `/u/<index>/`.** Each signed-in account has its own Gemini session under its verified `/u/<index>/` path inside the Argon Bridge browser.

---

## 🌍 2. The UI Language Follows the Account

Gemini renders its interface in the language of the signed-in Google account. **This pool is Bangla**, so the Gemini surface is in Bangla — while the model name (`4 Argon`) and the archive name (`argon_context.zip`) stay in English.

These are the controls the dispatcher touches, as they appear on screen:

| Control | Label on screen | English meaning |
|---|---|---|
| Chat / Task mode toggle | `চ্যাট` / `টাস্ক` | Chat / Task |
| Thinking level chip (shows the current level) | `আপনি হাই` | Thinking: High |
| Upload button next to the composer | `+ Gemini-কে ডিজাইসো করুন` | Upload to Gemini |
| Model entry in the dropdown | `4 Argon (Complex tasks)` | 4 Argon (Complex tasks) |

### ⚠️ Why this matters more than it looks

An English-only text selector ("Task", "4 Argon", "Upload") does **not** fail loudly on a Bangla page. The click simply never lands, and the run keeps going:

- the workspace stays in **Chat mode**,
- the model stays whatever it was (Flash — not Argon),
- the zip is attached best-effort and may not be attached at all,
- and the prompt goes out anyway.

Gemini then answers *"argon_context.zip did not come through with your message"* and the round trip is wasted. That failure is exactly what the bilingual selectors and the pre-flight gate in §6 exist to prevent.

---

## 🧭 3. Gemini Web UI ("Task" Mode + 4 Argon)

### UI Landmarks & Unified Send Order (Rule 5):
- **1st Step — Find Composer, Switch to Task Mode & Select `4 Argon` (Hard Flash Guard)**:
  - Look for the pill selector directly below or beside the main prompt box.
  - Switch from **Chat** (`চ্যাট`) to **Task** (`টাস্ক`).
  - Click the model dropdown (which often defaults or reverts to `3.8 Flash`) and select **`4 Argon`** (`Complex tasks`). A hard guard aborts the send if `4 Argon` is not active on the chip.
- **2nd Step — Paste the Prompt**:
  - Paste the engineered prompt from `templates/argon_task_prompt_template.md` into the composer via the clipboard (`Control+V`).
- **3rd Step — Attach the Context Archive (`argon_context.zip`, up to 100 MB)**:
  - Click the **`+`** (plus) attachment icon (`+ Gemini-কে ডিজাইসো করুন`) in the real composer and upload `argon_context.zip` (up to **100 MB**, with the upload wait scaled automatically to the archive size).
- **4th Step — Click Send the Moment Upload Finishes**:
  - As soon as the attachment chip is present and the upload spinner clears, verify the model chip still reads `4 Argon` and click **Send** immediately without waiting for anything else.

### Session Continuity Across Tasks

A follow-up task on the same project continues in the **same Gemini chat**. The codebase
understanding, the decisions and the ideas from the first task stay in that conversation, so the
next prompt builds on them instead of re-explaining the project.

- The **first** task for a project opens the chat.
- Every **later** task on the same project **reuses** that chat (even if that account already has other tasks running).
- `--new-session` forces a **fresh** chat on an account that is not already at `MAX_TASKS_PER_SLOT`.

---

## ✅ 4. Manual Setup, Step by Step

Do these four steps in the browser window the dispatcher opened (or open the URL yourself). On every attempt (`attempt 1`, `attempt 2`, `attempt 3`), the dispatcher also writes `manual_handoff.md` with the prompt file path and `argon_context.zip` location so you can send manually at any moment:

### Gemini Web (the only supported workspace)
1. Switch the `চ্যাট` / `টাস্ক` toggle to **`টাস্ক` (Task)** and pick **`4 Argon (Complex tasks)`** (never Flash).
2. Paste the prompt into the prompt box.
3. Press `+` → **Upload files** → choose `argon_context.zip`.
4. The moment the upload finishes, click **Send** immediately.

### 🪟 The browser must be OPEN before anything is sent (Rule 10)

A browser tool call on a browser that is not running **fails**, and then the prompt and
the archive never reach the chat - the run looks like it is working while nothing was
delivered. So the browser is opened and proven first:

```
python ./skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path . \
       --open-browser --slot /u/N/
```

It prints the profile, the CDP endpoint, the opened URL, the page title and the session
state, and it ends with `Browser status: OPEN and reachable - a prompt can be sent now`.

**Why this is not optional.** A Chrome that was started WITHOUT `--remote-debugging-port`
takes the profile directory's lock and publishes no debugging endpoint. The bridge then
cannot attach to that profile, every later browser call on it fails, and the prompt is
never sent. `--open-browser` detects exactly that case, closes the stray Chrome the safe
way (the cookies are snapshotted and restored, so the account stays signed in) and
starts the profile's **single browser host** with a debugging endpoint - one Chrome per
profile, many tabs, and the dispatcher attaches to it instead of fighting it.

### 📦 The manual-send material is handed over FIRST (Rule 11)

For **every** new request the material for a manual send is handed over **before** the
automatic send starts - not at the end, not only after a failure:

1. open the browser and prove it is up (above),
2. hand over the manual-send material: the exact prompt file, the prompt text and the
   `argon_context.zip` path,
3. **then** start the automatic send, and keep the material updated on every attempt of
   the ladder.

Handing it over first is what saves the long wait: when the automatic path stalls, the
operator can send by hand immediately instead of waiting for the retries to run out.

---

## 🛡️ 5. The Dispatcher Pre-Flight Gate, Companion Extension & Per-Step Rule Verification (Rule 7 & Round 16)

`dispatch_argon.py` never sends a prompt into an unverified workspace. Before each step finishes, its rules are re-read and verified, and the file-send, file-download, and patch-accept steps verify their own rules every time:

1. **Mode & Model (1st step)** — `mode = Task` (`টাস্ক`) and `4 Argon (Complex tasks)` verified on the model chip (hard Flash guard aborts if the chip is Flash).
2. **Prompt Paste (2nd step)** — prompt pasted into the bottom-most composer via synthetic `DataTransfer` `ClipboardEvent('paste')` (companion extension / DOM `DataTransfer` first, clipboard `Control+V` fallback) and verified present.
3. **Attachment & Immediate Send (3rd & 4th steps)** — `argon_context.zip` (<= 100 MB, with `TASK_BRIEF.md` and `API_INDEX.md`) attached via `DataTransfer` `File` injection into `input[type=file]` without opening the Windows OS file dialog, upload spinner cleared within the size-scaled window, composer re-verified if cleared by upload, and Send clicked semantically inside the composer container.
4. **Companion Extension vs. Custom Chromium Build Option** — Moving attach, paste, semantic model/mode/send, and paged Drive `viewer/text` capture into `skills/argon-coding-bridge/extension/` fixes the four recurring DOM/OS failures in existing Chrome today (with automatic fallback to pure Python when the extension is absent). A custom Chromium build is an optional hardening step (adding browser version pinning, a pre-installed extension, and `--remote-debugging-pipe`), not the fix for Gemini's DOM.
5. **Preflight Self-Test (`--self-test`)** — `python skills/argon-coding-bridge/scripts/dispatch_argon.py --self-test` serves a local synthetic Gemini page and tests all four operations and fault injection (`the upload menu rendered no file input`, floating quick-action bar editor, Flash model chip, attachment chip without `.zip`, composer cleared by upload) in seconds.
### The prompt is PASTED, never typed - the hang rule

Typing a prompt into Gemini's Quill editor is what makes a run hang. Quill
renders every character, so a 26 KB prompt takes minutes of character-by-character
work: the run prints ``Composer populated (length=25693 chars). Sending...`` and
then sits there. It is not a network problem - it is the typing.

| step | rule |
|---|---|
| the text | pasted via synthetic ``DataTransfer`` ``paste`` event (companion extension) or ``Control+V`` fallback (ONE event, same speed at any size) |
| a big prompt | pasted in 8 KB chunks, every chunk verified |
| typing | only as a fallback for a prompt of 2 KB or less |
| the send | semantic composer container send button + ``Control+Enter`` - never waits for the editor to settle |
| the fallback send | the composer's **send button** - a plain button, so it is quick |
| the deadline | the whole send is bounded; on expiry each operation names its own failure in seconds |

Measured on the mock that behaves like Quill (pasting fast, typing slow): a
55 KB prompt is delivered in ~7 seconds and arrives complete.

Every run writes its session down **the moment it starts**, with the conversation
name, the exact link, the account, the profile number and the browser profile
directory. The state (`sent` / `waiting` / `delivered` / `applied`) says how far it
got.

```bash
python skills/argon-coding-bridge/scripts/dispatch_argon.py --list-sessions
```

prints every recorded session so a run can be found by its project, by its name, by
its link or by its profile. `--resume` reopens the recorded conversation; a Google AI
Studio link is flagged as **STALE** and `--forget-session <project>` removes it.

### A follow-up is as stable as a new chat

A follow-up happens in a conversation that already holds an earlier answer, its
delivery chip and a floating quick-action bar. To keep it as calm as a fresh chat:

| step | rule |
|---|---|
| the prompt box | the **bottom-most** editable - not the floating bar's editor |
| the `+` upload button | the **real composer's own** button - never the floating bar's |
| focusing the box | **DOM focus, no mouse click** - no cursor jumps, no stray clicks |
| the delivery chip | only the chip of the **new assistant turn** is opened |
| the prompt | pasted through the clipboard, the send is verified |
| the model | the double check runs again right before the send |
### The project is verified LOCALLY - no extra Gemini round

After the patch is applied the bridge runs the project's own build/lint command
**on this machine** - detected from the project root (`Cargo.toml`,
`package.json`, `pyproject.toml`, `go.mod`, `pom.xml`, `build.gradle`,
`*.sln`, `Makefile`, `mix.exs`, `Gemfile`) or given with `--verify-cmd`.

That check never talks to Gemini; it runs locally on this machine. When it fails, the
errors are printed and recorded (task state + notes file) and the run exits with
code `8`. **The agent that drove the bridge fixes those errors itself** - lint
problems and a lost brace are small and local. The bridge never sends a
follow-up to Argon for them. Ask Argon again only for a genuinely major or
difficult task.

Three checks stop a broken patch before it reaches the project:

| check | what it catches |
|---|---|
| `validate_patch_payloads()` | a raw control character, a line starting with `@'`, a `$` inside a double-quoted here-string, a typographic quote - refused BEFORE any file is written |
| `verify_written_files_bytes()` | a raw control byte that appeared in a written file |
| `verify_written_files_structure()` | a file that was balanced before the patch and is unbalanced after it - a lost brace |

`API_INDEX.md` is written into the archive by the packager: every public
declaration of every source file with its file and line, complete and not
truncated. The brief tells Argon to use it and never to invent a signature.
### The carry-over rule - a run without a delivery starts a NEW session

When a run ends **without a delivery** (Gemini stopped working, the chat died, or
the answer never arrived), the task is not lost: a new prompt that carries the
previous editing context is written, a **NEW session** is started, the project is
packed and attached again, and the prompt is sent there. The new conversation
replaces the stored one.

**Before that, the run is re-checked** - a conversation that is still LOADING or
still THINKING is not a failed run:

| signal | still working? |
|---|---|
| the stop button is on the page | yes |
| `Thinking it through` / `বিস্ময় নিয়ে ভাবছে` / `Gemini is typing` | yes |
| a spinner / `role=progressbar` (the chat is loading) | yes |
| the conversation text has not changed for 90 s | finished |

Only when none of these hold is the run treated as "finished without a delivery"
and carried over into a new session. `--no-carry-over` turns the rule off.
### The delivery rule - the whole file, never a truncated preview

A delivered patch is **always** taken in full. Two ways are used, and both must yield
the WHOLE file:

1. the viewer's **Download** button (the real bytes), and
2. Drive's `viewer/text` endpoint, **page by page**, when the button is not reachable.

The Drive viewer renders only what fits on screen, so a long patch read out of the
preview can stop half-way. A half-written patch then fails the apply step with *"Target
block not found"* - even though Argon's delivery was fine.

The second way matters even more, because the endpoint is **paged**:
`drive.google.com/viewer/text?id=<FILE_ID>&page=N` serves the file in PAGES, and
**page 0 simply stops mid-statement with no error at all**. A downloader that reads one
page accepts a HALF patch that still parses. So the bridge walks `page=0, 1, 2 ...`
until a page comes back empty and stitches the chunks. Measured on a 544 KB delivery:
page 0 = 262 114 characters, page 1 = 261 605, page 2 = 20 639 - the whole file is
544 927 bytes, and its SHA-256 matches the file the owner downloaded by hand.

The file id is not read from the preview: the Drive viewer **crashes in headless
Chromium**, so the id only ever appears in the request the Gemini page fires when the
chip is clicked (`viewer/meta?id=...`). The bridge records it from the network.

When neither way produces a complete file the run **fails loudly and writes nothing**;
a truncated file is never accepted as a patch. The same applies to a script that Argon
inlines in the chat bubble: it must be complete before it is accepted.

A downloaded file is checked for completeness: it has to carry the `[SUCCESS]` marker, a
`NOTES` block that was opened must be closed, and no here-string may be left unterminated. A
file that fails those checks is reported as truncated and is **not** written.
### The model double check

The model is verified twice and only against the **model chip** (the control next to the
composer that shows the model that will really answer):

1. **Before the prompt leaves** - `ensure_argon_model()` reads the chip; if it does not name Argon
   the picker is opened and the `4 Argon` entry is clicked (disabled entries are skipped), then the
   chip is read again. If it still does not read Argon the prompt is **not sent** and the run exits
   with code `3`.
2. **After the prompt was sent** - the chip is read once more and a warning is printed if the UI
   fell back to another model.

**Why the chip and not the page text:** the open model picker lists every model at once, so
`4 Argon` can be visible while `3.8 Flash` is the checked one, and a chat history entry can read
`Argon ...` as well. A loose "is there an Argon text on the page" check therefore passes while the
task is about to go to Flash - that was the real cause of tasks landing in the wrong model. The
loose check was removed; only the chip and the *checked* picker entry count.

### ❌ If any check fails, the prompt is NOT sent

- The run **exits with code 3**.
- The bilingual checklist from §5 is printed.
- A screenshot of the browser is written to `.argon_backup/preflight_failure.png` **inside the project**, so you can see which control the dispatcher missed.

This is deliberate. A context-free prompt is worse than no prompt: Gemini answers it with *"argon_context.zip did not come through with your message"*, and the round trip is wasted. Failing loudly, locally, and with a screenshot is cheaper than that.

### 🖐️ The `--manual-setup` flag

```bash
python skills/argon-coding-bridge/scripts/dispatch_argon.py --task "Describe the change here" --project-path . --slot /u/0/ --manual-setup
```

`--manual-setup` skips the **automatic mode/model selection** — you do those two clicks yourself in the browser window. The dispatcher still attaches the zip and still sends the prompt, and it still verifies the attachment before sending.

---

## 🤖 6. Automation & Agentic Dispatch (Playwright / Demon Browser / Chrome Profiles)

If you drive the browser with a script or a browser extension:

1. **Target URL**: `https://gemini.google.com/u/<n>/app` — the slot is part of the URL, always.
2. **Selectors (Gemini Web)**: match the visible text in **both** languages. The dispatcher accepts `Task` / `টাস্ক` for the mode toggle, `4 Argon` / `আর্গন` (`আরগন`) for the model entry, `Upload` / `আপলোড` / `ডিজাইসো` for the upload control, and `High` / `উচ্চ` / `হাই` for the thinking level. An English-only matcher misses a Bangla page silently — see §2.
3. **Automation Safety**:
   - Always verify that the file uploaded is clean of sensitive `.env` files (enforced by `Package-ArgonContext.ps1`).
   - Verify every step **before** sending the prompt — see the pre-flight gate in §6.

---

## 👥 7. Managing 8–12 Pro Accounts & Task-Capacity Rotation

When running multiple coding tasks in parallel across large codebases, the pool distributes new tasks across accounts while keeping follow-ups in their existing conversations.

### Shared Argon Bridge Browser & Profile Reclaiming (Round 17):
1. **One Shared Argon Bridge Browser (`~/.gemini/argon_browser_profile`)**:
   - `PoolConfig.is_shared_profile_slot()` is `True` by default for every slot.
   - Every pooled Google account is signed into the single shared profile `~/.gemini/argon_browser_profile`, and its verified index in `Default/Preferences` (`account_info`) is stored as `account_index` via `PoolConfig.set_account_index(slot, index)`.
   - `--close-browser --slot /u/N/` closes **only that account's tabs** (reusing `--close-tab`) and leaves the Argon Bridge browser running for the other accounts; the browser itself closes politely (flushing cookies) only when no task tab is left.
   - Old per-slot profiles (`~/.gemini/argon_profiles/slot-u0/` through `slot-u11/`) are reclaimed on demand with `python skills/argon-coding-bridge/scripts/dispatch_argon.py --reclaim-slot-profiles`, which verifies each account is present in the shared profile before moving the directory to `~/.gemini/argon_reclaimed_profiles/<timestamp>/` and printing the reclaimed space.

### Slot Selection Policy (Rule 2):
- **New Tasks**:
  - Skip an account **only** when that account already runs the maximum number of concurrent tasks (`MAX_TASKS_PER_SLOT = 2`).
  - No account is ever shut down or skipped for any other status.
- **Follow-Up Tasks**:
  - Always use the account slot that was used before for that conversation, even when that account already has multiple tasks running.

```bash
# Picks an available slot under MAX_TASKS_PER_SLOT and rotates if a slot is busy:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "Describe the change" --project-path .

# Cap how many accounts are tried:
python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --task "..." --project-path . --max-accounts 3
```

### Parallel Isolation: Per-Slot Profiles, Lock Files, Busy Slot Detection & Next-Account Selection

1. **Where the Lock Files Live & How a Busy Slot Is Detected and Announced**:
   - Each slot `/u/N/` has a lock file at `~/.gemini/argon_locks/slot-uN.lock` storing `pid`,
     `slot`, `started_at`, and `profile_dir`.
   - When `SlotLock.acquire()` sees an existing lock file, it checks whether `pid` is still alive
     (`_pid_alive`). A dead PID is treated as stale and reclaimed immediately so a crashed run never
     blocks the slot.
   - If the PID is alive—or if `_profile_is_busy()` detects a live `SingletonLock`, Windows
     `lockfile`, or locked `Default/Network/Cookies`—the dispatcher announces that the slot is busy
     (`[!] Slot /u/N/ is already in use by pid <PID> ...`) and never touches or kills the other
     browser. Every lock is released in a `finally` block when the run ends.
2. **How the Next Account Is Chosen**:
   - `_pool_slots_in_order()` sorts configured accounts by slot number (`/u/0/`, `/u/1/`, …),
     rotates the list to start at `--slot` (or the project's stored session slot in
     `~/.gemini/argon_sessions.json`, or `active_slot`) and wraps around.
   - Accounts currently running fewer than `MAX_TASKS_PER_SLOT` tasks are placed ahead of accounts
     at `MAX_TASKS_PER_SLOT`.
   - When a slot returns exit code `7` (`slot busy`) or fails pre-flight on a new task,
     `main()` automatically advances to the next slot in the list (up to `--max-accounts`).
3. **How the Shared Argon Bridge Browser Host Works**:
   - Every slot `/u/N/` maps to the single shared Argon Bridge browser profile at `~/.gemini/argon_browser_profile` (`PoolConfig.is_shared_profile_slot()` is `True` by default; `--isolated-profile` / `ARGON_SHARED_PROFILE=0` remains available as an escape hatch).
   - `ensure_browser_host()` attaches to the running Argon Bridge browser via the CDP endpoint in `.argon_browser_endpoint` (or `DevToolsActivePort` / `--remote-debugging-port`), refusing only when a running Chrome publishes no endpoint.
   - Task and tab bookkeeping (`TaskRegistry`, `MAX_TASKS_PER_SLOT = 2`, and `host_state["tabs"]` with per-tab `target_id` and `window.name`) stays strictly per **account** (`slot`), so two tasks on the same or different accounts never steal each other's tab.

---

## 🔐 8. Automatic Pool Sign-In, Verification Pause-and-Notify & Adding Accounts

Every account slot `/u/N/` in `skills/argon-coding-bridge/config/accounts_pool.json` is signed into the single shared Argon Bridge browser profile at `~/.gemini/argon_browser_profile`, and its verified 0-based index in `Default/Preferences` (`account_info`) is recorded with `PoolConfig.set_account_index(slot, index)` and displayed next to the slot in `login_pool_accounts.py`.

### Automatic Sign-In Flow (`argon_pool_login.py` & `login_pool_accounts.py`)

1. **Browser Launch**: Uses the installed Google Chrome binary when available (falling back to Playwright's bundled Chromium), launched headed on `profile_dir` with `--disable-blink-features=AutomationControlled`, `--no-default-browser-check`, and `--no-first-run`.
2. **Google AddSession Flow**:
* Navigates to `https://accounts.google.com/v3/signin/identifier?flowName=GlifWebSignIn&flowEntry=AddSession`.
* Fills `input#identifierId` and presses `Enter`.
* On the password step (`input#c1`, fallback `input[name='Passwd']`, `input[name='Password']`), checks whether Chrome's password manager has already autofilled a password and preserves that value instead of overwriting it with a potentially stale stored password.
* Presses `Escape` before pressing `Enter` so any open autofill dropdown cannot swallow the submit keystroke.


3. **Pause-and-Notify on Verification / OTP Challenges**:
* Detects human challenges including any `.../challenge/<x>` URL other than `pwd`, a visible `input#ca` CAPTCHA field, a visible two-step OTP code input, or page text such as `"Verify it's you"`, `"unusual traffic"`, `"Check your phone"`, or `"two-step verification"`.
* When detected, `argon_pool_login.py` never attempts to solve, guess, or bypass the challenge. Instead, it triggers a non-blocking Windows tray balloon notification (`System.Windows.Forms.NotifyIcon` via PowerShell) with `winsound.MessageBeep()`, shows an in-app modal alert in `login_pool_accounts.py`, logs a clear instruction naming the account, keeps the browser open, and polls for up to `--wait-minutes` (default `6` minutes) until you approve the prompt on your phone.
* As soon as approval is detected, sign-in resumes automatically and records the updated status (`OK`, `NEEDS_HUMAN`, or `FAILED`).



### Adding a New Credential to the Pool

* **Via the Tkinter GUI (`login_pool_accounts.py` / `Open_Argon_Login.bat`)**:
Click **＋ Add Account**, enter the Google email, password (with Show/Hide toggle), tier (`PRO`), and role (`Pool Member`), and click **Save & Auto Login**. `PoolConfig.add_account()` appends the account with the next free `/u/N/` slot to `skills/argon-coding-bridge/config/accounts_pool.json` (UTF-8, 2-space indent, no BOM) and immediately signs it in.
* **Via the CLI (`argon_pool_login.py`)**:
```bash
python skills/argon-coding-bridge/scripts/argon_pool_login.py --add new.account@gmail.com "AccountPassword" --tier PRO --role "Pool Member"

```

---

## 📦 9. Delivery Contract: File In, One Click Applied

The old handoff was: Argon pastes the whole `apply_changes.ps1` into the chat, and the agent hunts through the chat text, extracts the right fence, and applies every edit one by one. That is slow, easy to truncate mid-stream, and error-prone.

The new contract replaces the pasted script with a single file handoff:

1. **Argon delivers a file, not a script body.** Gemini 4 Argon writes the complete patch to `apply_changes.ps1` and attaches it to its reply.
2. **The agent downloads it and runs it once.** One download, one execution — the entire change set is applied in a single pass. Afterwards the agent only has to verify.
3. **The prompt carries the absolute target directory.** `templates/argon_task_prompt_template.md` pins the absolute target path at the top of the generated script (`$TargetRoot = '<absolute path>'`) and resolves every path against it, so the script can be run from anywhere and still land in exactly the right place.

### Where the delivered file lands

- Browser download folder: `%USERPROFILE%\Downloads` (the default destination for a Gemini Web attachment), or
- `<repo>\downloads\` when the download is redirected into the project.

### One-click entry point

`Open_Argon_Apply.bat` at the repository root runs `skills/argon-coding-bridge/scripts/Run-ArgonPatch.ps1`, which:

- auto-discovers the newest delivered `apply_changes*.ps1`,
- validates it (strips a BOM, runs a PowerShell parse check and refuses to run a truncated file),
- then executes it against the project root.

The patch script itself backs up every file it overwrites into `.argon_backup/<yyyyMMdd_HHmmss>/<relative path>` before writing.

### Rollback after a successful apply

Restore the newest backup session — the one the patch script just created:

```powershell
.\skills\argon-coding-bridge\scripts\Apply-ArgonPatch.ps1 -Rollback
```

Run it from the repository root: it restores every file from the newest `.argon_backup\<yyyyMMdd_HHmmss>` folder.

### Why this matters

The whole point is to avoid searching chat text and applying edits one by one — one delivered file, one run, one verifiable result.

### Self-Verification by Argon

The delivered patch script verifies its own work. After it writes each file it **re-reads that file
from disk** and checks it against the intended content, printing one line per file:

```text
[Verified] <path>
```

If the re-read does not match, the script prints `[FAILED VERIFY] <path>` and exits with a **non-zero
exit code** instead of reporting success. Argon thus *verifies* its own output rather than merely
claiming it.

---

## 🔄 10. Resuming a Closed Browser (`--resume`) & Capturing Notes

### How a Closed Browser Is Resumed
- While a task is running, `dispatch_argon.py` writes its progress to `~/.gemini/argon_task_state.json`
  keyed by the resolved project path (`state` in `sent`, `waiting`, `delivered`, `applied`, plus
  `chat_url`, `chat_id`, `slot`, `email`, and `task`).
- If the browser window closes before the patch is applied (exit code `4`), re-run with `--resume`
  (or `Invoke-ArgonBridge.ps1 -Resume`):
  ```bash
  python "./skills/argon-coding-bridge/scripts/dispatch_argon.py" --project-path . --resume --auto-apply
  ```
- `--resume` reopens the recorded `chat_url` in that slot's browser profile without re-packaging the
  project or sending a second prompt. If the `apply_changes.ps1` file chip is already present in the
  chat (or already downloaded), it is captured and applied immediately; otherwise the dispatcher
  continues waiting for Argon's reply.

### How Notes and Suggestions Are Captured
- Argon records any codebase inconsistencies or improvement ideas in a trailing comment block at the
  end of `apply_changes.ps1`:
  ```powershell
  # --- NOTES: inconsistencies and suggestions found while reading the project ---
  # NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>
  # --- END NOTES ---
  ```
- When `apply_changes.ps1` is captured, `_extract_notes()` and `_save_notes()` extract that block,
  save it to `.argon_backup/argon_notes_<timestamp>.md` inside the target project, and print each
  note line to the console.
