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

5. **Multi-Account Pool Automatic Sign-In, Per-Slot Isolation & Rotation:**
   - `argon_pool_login.py` and `login_pool_accounts.py` sign each Google Pro account in `skills/argon-coding-bridge/config/accounts_pool.json` into its own dedicated slot profile (`~/.gemini/argon_profiles/slot-uN/`), using `~/.gemini/argon_browser_profile` as the master seed profile.
   - Automatically fills credentials via Google's `ServiceLogin` flow, completes account registration and Gemini consent, prefers Chrome's saved password autofill when present, and pauses with a Windows tray notification, audible alert, and in-app modal dialog whenever human 2FA/OTP approval is required.
   - Prevents parallel runs from colliding via per-slot lock files (`~/.gemini/argon_locks/slot-uN.lock`) with stale-PID reclamation, SQLite online cookie backup seeding, and automatic rotation to the next free pool account.

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

## 🔒 Parallel Isolation, Per-Slot Profiles & Lock Files

When two coding agents (or you and an agent) use the bridge at the same time on different projects,
sharing a single Chrome `--user-data-dir` would cause the second launch to either fail with
`Opening in existing browser session` or hijack and kill the first run's browser mid-flight.

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

### 2. How the Per-Slot Profiles Work & SQLite Cookie Seeding
- Every slot gets its own isolated Chrome profile directory at `~/.gemini/argon_profiles/slot-uN/`
  (`slot-u0`, `slot-u1`, `slot-u2`, …). Two runs on two different slots never share a browser
  process or fight over `--user-data-dir`.
- The base profile (`~/.gemini/argon_browser_profile`) remains the master profile from which new
  slot profiles are seeded.
- Because Chrome holds `Default/Network/Cookies` open exclusively while running, a plain file copy of
  a live profile loses the signed-in Google session. `_seed_profile()` only seeds when the master
  profile is **not** busy (if the master is busy, it prints a clear warning instead of producing a
  signed-out copy) and copies `Default/Network/Cookies` with SQLite's online backup API
  (`sqlite3.connect(...).backup(...)`).
- `argon_pool_login.py` and `login_pool_accounts.py` sign each account directly into that account's
  own `~/.gemini/argon_profiles/slot-uN/` profile so `slot-u1` holds account 1's authenticated
  Google session.
- If a slot profile cannot be prepared, the dispatcher announces it and falls back to the shared
  master profile **only** when the master profile is actually free.

---

## 🔁 Account Pool Selection & Busy-Slot Rotation

The pool distributes concurrent tasks across configured Google Pro accounts while preserving conversation continuity:

- **Follow-up tasks:** Always use the account slot that owns the existing conversation in `~/.gemini/argon_sessions.json`, even when that slot already has multiple tasks running.
- **New tasks:** `_pool_slots_in_order()` orders the pool in slot order (`/u/0/`, `/u/1/`, …) starting from `--slot` (or `active_slot`) and wrapping around. A new task skips a slot **only** when that slot is already running the maximum number of tasks (`MAX_TASKS_PER_SLOT = 2`).
- **Rotation on busy slot (exit code `7`):** When a slot is busy on a new task, the dispatcher advances to the next available slot in the pool.
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
| 3. Deliver | Argon attaches `apply_changes.ps1`. The chat reply stays short: what changed, which files changed, how to verify. |
| 4. Apply | Double-click **`Open_Argon_Apply.bat`** at the repository root. |
| 5. Verify | The runner prints every file it patched, plus the rollback command. |

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
