# Argon Coding Bridge (Agent Plugins 1.0 - for any coding agent)

An open-standard AI agent plugin compliant with the **[Agent Plugins 1.0 Specification](https://agent-plugins.org/specification)**, built to bridge **ANY local coding agent** with **Gemini 4 Argon** — **Claude Code**, **Cursor**, **Gemini CLI**, **Antigravity**, **MiniMax Code**, and every other Agent Plugins 1.0 compatible client included.

---

## 🌟 Specification Compliance

This plugin adheres directly to the `agent-plugins.org` v1.0.0 standard:
- **Manifest:** `plugin.json` validates against `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`.
- **Skills Directory:** Located under `skills/argon-coding-bridge/` containing the standard `SKILL.md` with YAML frontmatter.
- **Cross-Platform:** Works natively across Claude Code, Cursor, Gemini CLI, Antigravity, OpenCode, MiniMax Code, and any Agent Plugins 1.0 compatible environment.

---

## 🚀 How to Install into Any Agent

### Method A: via a Git repository
1. Push this repository to your GitHub account:
   ```bash
   git init
   git add .
   git commit -m "feat: initial commit for argon-coding-bridge plugin"
   git remote add origin https://github.com/<your-username>/argon-coding-bridge.git
   git branch -M main
   git push -u origin main
   ```
2. Import it from that repository URL in your agent:

   | Agent | How to import the plugin |
   | --- | --- |
   | **MiniMax Code** | **Plugins & Skills** -> **+ Create** / **Manage** -> **Import plugin** -> **Repository URL** -> **Preview** -> **Import** |
   | **Gemini CLI** | Copy or clone the repository into `~/.gemini/config/plugins/<name>/` |
   | **Claude Code** | Copy the repository into `~/.claude/plugins/<name>/` and reference `skills/argon-coding-bridge/SKILL.md` from `CLAUDE.md` |
   | **Cursor** | Add the repository folder to the workspace and reference `skills/argon-coding-bridge/SKILL.md` in the rules |
   | **Antigravity, OpenCode, or any other agent** | Point the agent at `skills/argon-coding-bridge/SKILL.md` directly |

   The agent parses `plugin.json` and registers `argon-coding-bridge` as a skill.

### Method B: direct local usage
Open the repository root (where `skills/argon-coding-bridge/SKILL.md` lives) or reference that SKILL.md file directly in your agent prompt or project rules.

---

1. **Smart Dependency & GitIgnore Filtering (Up to 100 MB, Zero Code Compromise):**
   - Filters `node_modules`, `vendor`, `build`, `.gradle`, `Pods`, `.dart_tool`, `venv`, and binary media.
   - Strips private secrets (`.env*`, `*.pem`, `*.key`, `*credentials*.json`).
   - Supports archives up to **100 MB** (`DEFAULT_MAX_ARCHIVE_MB = 100`, `DEFAULT_MAX_ARCHIVE_ENTRIES = 6000`) and always tries the maximum code size with no compromise on source code.

2. **Zero-UI-Regression Guardrails & Linear Ordinal Patching:**
   - Enforces strict surgical editing so Gemini 4 Argon never wipes existing CSS, Tailwind styling, Flutter widgets, or HTML structures.
   - Instructs Argon to output a two-phase PowerShell patch that validates every target block before writing any file and applies replacements in linear time using `[System.StringComparison]::Ordinal` `IndexOf` + `[System.Text.StringBuilder]` (never `.NET String.Replace` on large needles).

3. **Single-Quoted Here-Strings & UTF-8 without BOM:**
   - Uses `@' ... '@` so PowerShell never mangles JavaScript/TypeScript `${variable}` template literals.

4. **Automated Backups & Rollback:**
   - `Apply-ArgonPatch.ps1` snapshots modified files to `.argon_backup/` before editing.
   - Rollback instantly with `.\skills\argon-coding-bridge\scripts\Apply-ArgonPatch.ps1 -Rollback`.

5. **Multi-Account Pool Automatic Sign-In, One Shared Browser & Safe Slot Use:**
   - `argon_pool_login.py` and `login_pool_accounts.py` sign each Google Pro account in `skills/argon-coding-bridge/config/accounts_pool.json` into the ONE Argon browser (`~/.gemini/argon_browser_profile`), which holds every account and is switched with Google's own `/u/<index>/` paths.
   - Automatically fills credentials via Google's `ServiceLogin` flow, completes account registration and Gemini consent, prefers Chrome's saved password autofill when present, and pauses with a Windows tray notification, audible alert, and in-app modal dialog whenever human 2FA/OTP approval is required.
   - Prevents parallel runs from colliding via per-slot lock files (`~/.gemini/argon_locks/slot-uN.lock`) with stale-PID reclamation, and keeps the pool's `/u/N` order in step with the browser's live account list on every run (`--resync-accounts`).

---

## 🔐 Account-Pool Automatic Sign-In & Management

### 1. Launch the Login Assistant GUI
Double-click `Open_Argon_Login.bat` at the repository root (or run `-LoginAssistant` via `Invoke-ArgonBridge.ps1`):
```powershell
.\Open_Argon_Login.bat
# Or via PowerShell wrapper:
powershell -ExecutionPolicy Bypass -File .\skills\argon-coding-bridge\scripts\Invoke-ArgonBridge.ps1 -LoginAssistant

```

* **Auto Login All:** Click **⚡ Auto Login All** to sequentially sign in every account in `accounts_pool.json` using a single persistent Chrome browser session on a background worker thread.
* **Pause-and-Notify on Verification / OTP:** If Google presents a two-step verification prompt, CAPTCHA (`input#ca`), or `/challenge/<x>` screen, the login flow triggers a Windows tray balloon notification (`System.Windows.Forms.NotifyIcon`), plays a system beep (`winsound.MessageBeep()`), and displays an in-app modal alert naming the account. It never attempts to guess or bypass the challenge—instead, it keeps the browser open and polls for up to `--wait-minutes` (default 6 minutes) until you approve the prompt on your phone, then resumes automatically.
* **Add Account:** Click **＋ Add Account** in the GUI to enter an email, password (with Show/Hide toggle), tier, and role. The credential is appended to `skills/argon-coding-bridge/config/accounts_pool.json` with the next free `/u/N/` slot and signed in automatically.

### 2. Command-Line Pool Sign-In (`argon_pool_login.py`)

```bash
# Sign in all configured pool accounts sequentially:
python skills/argon-coding-bridge/scripts/argon_pool_login.py --all

# Sign in a specific account slot:
python skills/argon-coding-bridge/scripts/argon_pool_login.py --slot /u/2/

# Add a new credential to accounts_pool.json and immediately sign it in:
python skills/argon-coding-bridge/scripts/argon_pool_login.py --add user@gmail.com "MyPassword" --tier PRO --role "Pool Member"

```

### 3. One-Click Task Dispatch (`Invoke-ArgonBridge.ps1`)

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\skills\argon-coding-bridge\scripts\Invoke-ArgonBridge.ps1 `
  -Task "Fix race condition in worker queue" `
  -ProjectPath . `
  -Mode gemini `
  -Slot "/u/0/" `
  -AutoApply

```

---

## 📋 The Brief Is Shown Before It Is Sent

To avoid misunderstandings before dispatch, `prepare_brief.py` summarizes what will be sent:
- Fenced, byte-exact prompt preview with SHA-256 and byte length.
- Context archive entry count and size.
- Target files parsed from the brief to show blast radius.
- Dispatch facts (slot, account email, Task mode, 4 Argon model).

```bash
python skills/argon-coding-bridge/scripts/prepare_brief.py --project-path . --brief <brief.md> --lane followup --slot /u/0/
```

---

## 🛠️ Fix Memory and Contribution Branches

Defect fixes and architectural improvements are tracked in structured debugging memory:
- **Record fixes:** `record_fix.py` saves structured notes to `~/.gemini/argon_fixes/fixes/<id>.md`, updates `index.json`, and copies notes to `knowledge/fixes/` for branch commit (`fixes/<scope-slug>`).
- **Analyze fixes:** `analyze_fixes.py` matches unmerged commits on `fixes/*` branches with recorded notes and outputs brief-ready instructions for subsequent rounds.
- See `CONTRIBUTING.md` and `knowledge/fixes/README.md` for contributor guidelines.

---

## 🧭 Workspace Pre-Flight (the dispatcher refuses to send a context-free prompt)

The Gemini UI renders in the **language of the signed-in Google account** — this pool shows it in
Bangla. English-only selectors silently miss, which is how a prompt used to go out in **Chat mode**,
on the **wrong model**, with **no zip attached**; Gemini then replied *"argon_context.zip did not come
through with your message"* and the round trip was wasted.

`dispatch_argon.py` now configures the workspace with bilingual selectors and **verifies every step
before sending**:

| Surface | URL | Must be |
| --- | --- | --- |
| Gemini Web | `https://gemini.google.com/u/<slot>/app` | mode `টাস্ক` (**Task**, not `চ্যাট`/Chat) + model `4 Argon (Complex tasks)` |

plus `argon_context.zip` really attached to the composer.

**If any check fails the prompt is NOT sent.** The run exits with code `3`, prints the bilingual
checklist, and saves `.argon_backup/preflight_failure.png` in the project so you can see what went
wrong. Fix the workspace by hand and re-run with `-ManualSetup` to skip only the two clicks — the
dispatcher still attaches the zip and sends the prompt.

---

## 🧠 Session Continuity

A follow-up task on the same project continues in the **SAME Gemini conversation**. The codebase
understanding Argon built on the first task, the decisions it took, and the ideas it set aside all
stay available in that conversation — so *"now also cover the tests"* needs no re-upload and no
re-briefing.

- The **first** task for a project opens the chat.
- Every **later** task on the same project **reuses** that chat.
- Pass `--new-session` to force a **fresh** chat when a clean context is wanted.
- The map lives at `~/.gemini/argon_sessions.json` — in the user profile, next to the browser
  profile — so every installation of this plugin shares it, and it is keyed by the absolute
  project path.

---

## 🔒 One Argon Browser, Lock Files & Slot Safety (Rounds 18-21)

There is **ONE** Chrome for the whole bridge: `~/.gemini/argon_browser_profile`. Every pooled Google
account lives inside it and is selected with Google's own `/u/<index>/` paths. The old per-slot
profile directories (`~/.gemini/argon_profiles/slot-uN/`) were **deleted** in Round 18 — they cost
68.23 GB and they were the reason two lanes could fight over a browser. Do not expect them to
exist, and do not restore them.

### 1. Where the Lock Files Live & How a Busy Slot Is Detected and Announced
- Every account slot `/u/N/` has a lock file at `~/.gemini/argon_locks/slot-uN.lock` (for example,
  `~/.gemini/argon_locks/slot-u0.lock`).
- Each lock file stores JSON with `pid`, `slot`, `started_at`, and `profile_dir`.
- Before touching a slot, `SlotLock.acquire()` checks whether `slot-uN.lock` exists and whether its
  `pid` is still alive (`_pid_alive`). If the PID is dead, the stale lock is reclaimed automatically
  (`[+] The lock on slot /u/N/ was stale (pid <PID>) - reclaiming it.`) so a crashed process never
  blocks the pool.
- If the PID is alive—or if `_profile_is_busy()` detects an active Chrome `SingletonLock`, locked
  Windows `lockfile`, or locked `Default/Network/Cookies`—the dispatcher announces:
  `[!] Slot /u/N/ is already in use by pid <PID> (started <timestamp>, profile <path>).`
- It **never** kills or touches the other run's browser and **never** launches Chrome on a busy
  profile directory. The lock is always released in a `finally` block when the run finishes.

### 2. Two lanes on the ONE browser (Round 20/21)
- The browser host state (`.argon_browser_endpoint`) is read-modify-written **under a file lock**
  (`update_host_state_locked`), so two lanes can never erase each other's tab entry.
- A lane that arrives while the host is still starting **waits up to 90 s** for the CDP endpoint and
  then attaches to it. It never falls back to a direct launch, which could never win the profile lock.
- A **busy slot is fatal for the requested slot** (exit code `7`, no rotation). Rotating would run
  the task under a *different* Google account than the one that was asked for.
- The prompt is checked against the live conversation before every re-send, and the model chip is
  read back before the send — a chip that is not Argon aborts the task instead of silently
  downgrading the model.

### 3. The browser never closes on its own
- Releasing a task's tab closes **only that tab**. The browser host is closed by exactly one thing:
  the explicit `--close-browser`.
- A browser opened for manual use (`--open-browser`, or the Login Assistant's **Open in Gemini Web**)
  is marked `persistent` in the host state and is never reaped by an idle closer.
- The on-disk session stays fresh through `snapshot_profile_session()` instead of through closing
  the browser, so nothing is lost by keeping it open.

### 4. The pool's `/u/N` order is always the browser's `/u/N` order
- The browser's `Default/Preferences` → `account_info` is the **only** authority for the account
  order. The pool file used to keep a static snapshot of those indexes, so it silently went stale —
  the pool pointed `/u/7/` at `/u/9/`, an account that did not exist in the browser at all.
- `PoolConfig.resync_account_indexes()` rewrites **every** slot from the live list, clears the stale
  index of any account that is not signed in (marking it `needs_signin`), and reports collisions and
  browser accounts the pool does not know about. `get_account_index()` calls it before every
  resolution, and the Login Assistant calls it before it draws the card or opens Gemini Web.
- A slot whose account is **not signed in** is refused loudly (`AccountNotSignedInError`, exit code
  `2`). The pool slot number is *not* the browser account index, so there is no safe fallback.
- See it at any time:
  ```powershell
  python skills\argon-coding-bridge\scripts\dispatch_argon.py --resync-accounts
  ```

---

## 🖐️ Live Browser Control: Drive ANY Website from Your Agent (Round 19)

The bridge can now drive **any** website, not only `gemini.google.com`. The browser pushes a live
interactive-element map to the agent, the agent sends back an action, and the browser pushes the next
map — one round trip, no human in the loop.

```
agent ──▶ browser : open / navigate / click element 12 / type / scroll
agent ◀── browser : the LIVE element map (the "HTML mapping")
```

- The push is a **true push**, not a poll: Playwright's `expose_binding` over the Chrome DevTools
  Protocol (`Runtime.addBinding`) plus one shared map builder
  (`skills/argon-coding-bridge/live_loop/live_map.js`) that a `MutationObserver` re-runs on every
  DOM change. Measured live on wikipedia.org: **3-18 ms median push latency**, and a 1,199-node page
  compressed to **36 interactive elements**.
- **Two lanes, one protocol, one map builder.** Lane A is the CDP lane on the Argon browser itself
  (zero install). Lane B is a separate, minimal MV3 extension under `live_loop/extension/` for your
  everyday Chrome. The Gemini-only companion extension is untouched and was **not** widened to
  `<all_urls>`.
- **The stale-element guard** rebuilds the map at action time and refuses an action whose
  fingerprint no longer matches, returning the fresh map instead. Element ids are positional, so this
  is what keeps an unattended loop from clicking the wrong thing.
- Safety: the daemon binds `127.0.0.1` only, authenticates every request with a 64-hex token,
  accepts only `http(s)` navigations, and keeps `eval` disabled unless `--browser-allow-eval` is
  passed.

```powershell
# start the control daemon (it prints the endpoint and the token)
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-loop-start

# ask the live browser what is on the page
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-act '{"op":"map"}'

# click element 12 - and say what you expect it to be, so a stale map cannot misfire
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-act '{"op":"click","id":12,"expect":{"tag":"button","name":"Sign in"}}'

# prove the whole loop end to end on a disposable Chrome
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-loop-selftest
```

---

## 🔁 Account Pool Selection & Busy-Slot Rotation

The pool distributes concurrent tasks across configured Google Pro accounts while preserving conversation continuity:

- **Follow-up tasks:** Always use the account slot that owns the existing conversation in `~/.gemini/argon_sessions.json`, even when that slot already has multiple tasks running.
- **New tasks:** `_pool_slots_in_order()` orders the pool in slot order (`/u/0/`, `/u/1/`, …) starting from `--slot` (or `active_slot`) and wrapping around. A new task skips a slot **only** when that slot is already running the maximum number of tasks (`MAX_TASKS_PER_SLOT = 2`).
- **Rotation on a broken slot (exit codes `2`, `3`, `6`):** When the requested account is not signed in, the pre-flight fails, or the browser cannot be launched, the dispatcher advances to the next available slot in the pool — and re-resolves that slot's account index against the browser's LIVE account list.
- **A busy slot is NOT rotated (exit code `7`):** a busy slot means another lane is using that account right now. The dispatcher stops and reports it, because rotating would run the task under a *different* Google account than the one that was asked for.
- **`--max-accounts N`:** Caps how many pool accounts are tried in a single invocation (`0` = all).

```powershell
python .\skills\argon-coding-bridge\scripts\dispatch_argon.py `
  --task "Fix the failing test" `
  --project-path . `
  --max-accounts 3
```

### Exit Codes (`dispatch_argon.py`)

| Exit Code | Meaning |
| --- | --- |
| `0` | Success (patch delivered and optionally applied) |
| `1` | General failure or timeout waiting for patch |
| `2` | Account not signed in |
| `3` | Workspace pre-flight verification failed or Flash guard aborted send (prompt was not sent) |
| `4` | Browser window was closed while waiting (`TargetClosedError`) |
| `6` | Browser failed to launch |
| `7` | Requested slot (or every slot in the pool) is busy |
| `8` | Local build/lint verification failed |
| `9` | Task already running for this project/lane |

---

## 🔄 Task State & Resuming a Closed Browser (`--resume`)

`~/.gemini/argon_task_state.json` records per-project task progress (`state` in `sent`, `waiting`,
`delivered`, `applied`, along with `chat_url`, `chat_id`, `slot`, `email`, and `task`).

If a browser window is closed mid-run (exit code `4`) or interrupted:
1. Run `dispatch_argon.py --project-path . --resume [--auto-apply]` (or
   `Invoke-ArgonBridge.ps1 -ProjectPath . -Resume -AutoApply`).
2. The dispatcher reads `~/.gemini/argon_task_state.json` for that project, **skips packaging and
   never sends a second prompt**, and reopens the recorded `chat_url` in that slot's profile.
3. If the generated `apply_changes.ps1` chip is already present in the conversation (or already
   downloaded), it captures and applies it immediately; otherwise it continues polling until Argon
   finishes delivering the file.

---

## 🗒️ Capturing Argon Notes & Suggestions

While inspecting a codebase, Argon records any architectural inconsistencies, risks, or improvement
suggestions as a trailing comment block at the end of `apply_changes.ps1`:

```powershell
# --- NOTES: inconsistencies and suggestions found while reading the project ---
# NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>
# --- END NOTES ---
```

When the dispatcher captures `apply_changes.ps1`, `_extract_notes()` and `_save_notes()` extract
that block, save it to `.argon_backup/argon_notes_<timestamp>.md` inside the target project, and
print every note line to the console so the advice is preserved alongside the backup rather than
buried inside the patch script.

---

## 📦 Delivery Contract: File In, One Click Applied

Argon does **not** paste the patch into the chat. It delivers `apply_changes.ps1` as a **downloadable
file**, and the prompt carries the **absolute target directory** so the delivered script writes
straight to the real project and applies the whole change set in a single run.

| Step | What happens |
| --- | --- |
| 1. Package | `package_context.py` / `Package-ArgonContext.ps1` build `argon_context.zip` and inject the **absolute project path** into `TASK_BRIEF.md`. |
| 2. Prompt | The prompt states that absolute directory, so the generated script pins `$TargetRoot` and resolves every path against it. |
| 3. Deliver | Argon attaches `apply_changes.ps1` - plus, when the round produced any, the generated images / SVG / audio / other artifacts as their own files. The chat reply stays short: what changed, which files changed, how to verify. |
| 3b. Bundle | The dispatcher downloads EVERY chip of the turn, puts the non-patch files into `assets/` next to the patch and writes `argon_delivery.zip` + `DELIVERY_MANIFEST.md` (name, byte size, SHA-256, one-line description of every file). |
| 4. Apply | Double-click **`Open_Argon_Apply.bat`** at the repository root. |
| 5. Verify | The runner prints every file it patched, plus the rollback command. |

### One delivery can carry more than the patch

A round is not always "code only". When Argon also generated an image, an SVG graphic, an icon set, a
UI card or a voice-over, the task prompt asks for every extra artifact as its own file, with a
self-describing name. The dispatcher then downloads **every** chip of the turn:

```
<delivery-dir>/apply_changes.ps1        the patch - applied by Open_Argon_Apply.bat
<delivery-dir>/assets/<artifact>         every other delivered file, by its own name
<delivery-dir>/argon_delivery.zip        the patch + assets/ + the manifest
<delivery-dir>/DELIVERY_MANIFEST.md      name, byte size, SHA-256 and one-line description per file
```

### `Open_Argon_Apply.bat` — the one-click applier

It runs `skills/argon-coding-bridge/scripts/Run-ArgonPatch.ps1`, which:

- auto-discovers the newest delivered `apply_changes*.ps1` in `<repo>\apply_changes.ps1`,
  `<repo>\downloads\`, or `%USERPROFILE%\Downloads\` (newest wins);
- strips a UTF-8 BOM if the download added one;
- runs a **PowerShell parse check** and refuses to run a truncated or corrupt file;
- prints the resolved target directory and the exact command to re-run it;
- applies the patch once, then lists the files that changed and the rollback command.

```powershell
# Apply only, no dispatch:
pwsh -NoProfile -ExecutionPolicy Bypass -File .\skills\argon-coding-bridge\scripts\Invoke-ArgonBridge.ps1 -ApplyOnly -ProjectPath .

# Or point at an explicit file:
pwsh -NoProfile -ExecutionPolicy Bypass -File .\skills\argon-coding-bridge\scripts\Run-ArgonPatch.ps1 -PatchFile "C:\Users\<you>\Downloads\apply_changes.ps1" -ProjectPath .
```

**Why:** a streamed code block is slow to render, easy to truncate, and forces an agent to hunt
through chat text and apply each edit one by one. A delivered file is downloaded, run once, and the
entire change set lands in place.

---

## ✅ Self-Verification

The delivered patch script does not trust its own output. After writing each file it **re-reads that
file from disk** and checks it against the intended content, printing one line per file:

```text
[Verified] <path>
```

If a file does not match, the script prints `[FAILED VERIFY] <path>` and exits with a **non-zero exit
code**, so a half-applied change set can never pass silently. Argon therefore **verifies its own
work** instead of merely claiming success.

---

## 📁 Project Structure

```
argon-coding-bridge/
├── plugin.json                                # Agent Plugins 1.0.0 Manifest
├── Open_Argon_Login.bat                       # Relative-path launcher for the Login Assistant GUI
├── Open_Argon_Apply.bat                       # Relative-path launcher for the one-click patch applier
├── README.md                                  # Documentation
├── LICENSE                                    # MIT License
└── skills/
    └── argon-coding-bridge/
        ├── SKILL.md                           # Master Agent Skill specification
        ├── config/
        │   └── accounts_pool.json             # Local multi-account pool config (/u/0/ .. /u/N/, gitignored & never packed)
        ├── scripts/
        │   ├── argon_pool_login.py            # Automatic Playwright sign-in & PoolConfig manager
        │   ├── login_pool_accounts.py         # Tkinter Account Pool Login Assistant GUI
        │   ├── dispatch_argon.py              # Automated packager, browser dispatcher & patch extractor
        │   ├── Invoke-ArgonBridge.ps1         # One-click PowerShell wrapper for dispatch & login
        │   ├── Package-ArgonContext.ps1       # Windows PowerShell context packager
        │   ├── package_context.py             # Cross-platform Python packager
        │   ├── Run-ArgonPatch.ps1             # One-click runner for the delivered patch script
        │   └── Apply-ArgonPatch.ps1           # Safe patch applicator with backup & rollback
        ├── templates/
        │   └── argon_task_prompt_template.md  # Surgical prompt template
        └── references/
            └── browser_automation_and_accounts.md # Gemini Task mode & pool login guide

```

---

## 📄 License

MIT License. Free to use and distribute across all AI agent platforms.
