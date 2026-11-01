# Argon Coding Bridge (Agent Plugins 1.0 & MiniMax Edition)

An open-standard AI agent plugin compliant with the **[Agent Plugins 1.0 Specification](https://agent-plugins.org/specification)**, built to bridge **MiniMax Code**, **Antigravity**, **Cursor**, and **Claude Code** with **Gemini 4 Argon**.

---

## 🌟 Specification Compliance

This plugin adheres directly to the `agent-plugins.org` v1.0.0 standard:
- **Manifest:** `plugin.json` validates against `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`.
- **Skills Directory:** Located under `skills/argon-coding-bridge/` containing the standard `SKILL.md` with YAML frontmatter.
- **Cross-Platform:** Works natively across MiniMax Code, Antigravity, Cursor, and any Agent Plugins 1.0 compatible environment.

---

## 🚀 How to Import into MiniMax Code

MiniMax Code features a built-in Plugin Manager that imports plugins from public Git repositories (see MiniMax's **Import plugin** dialog):

### Method A: Via Git Repository (Recommended for MiniMax Code)
1. Push this repository to your GitHub account:
   ```bash
   git init
   git add .
   git commit -m "feat: initial commit for argon-coding-bridge plugin"
   git remote add origin https://github.com/<your-username>/argon-coding-bridge.git
   git branch -M main
   git push -u origin main
   ```
2. In **MiniMax Code**:
   - Go to **Plugins & Skills** -> Click **+ Create** or **Manage** -> Select **Import plugin**.
   - In the **Repository URL** field, paste:
     ```
     https://github.com/<your-username>/argon-coding-bridge.git
     ```
   - Click **Preview** and then **Import**.
   - MiniMax Code will instantly parse `plugin.json` and register `argon-coding-bridge` under your **Skills**!

### Method B: Direct Local Workspace Usage
Open `D:\Developing Projects\argon-coding-bridge` or reference the `skills/argon-coding-bridge/SKILL.md` file directly in your MiniMax prompt or project rules.

---

## 🛠️ Key Capabilities

1. **Smart Dependency & GitIgnore Filtering:**
   - Filters `node_modules`, `vendor`, `build`, `.gradle`, `Pods`, `.dart_tool`, `venv`, and binary media.
   - Strips private secrets (`.env*`, `*.pem`, `*.key`, `*credentials*.json`).
   - Allows genuine large source code files up to 15 MB.

2. **Zero-UI-Regression Guardrails:**
   - Enforces strict surgical editing so Gemini 4 Argon never wipes existing CSS, Tailwind styling, Flutter widgets, or HTML structures.
   - Instructs Argon to output safe `$content.Replace()` calls that throw an error if the snippet is not found.

3. **Single-Quoted Here-Strings & UTF-8 without BOM:**
   - Uses `@' ... '@` so PowerShell never mangles JavaScript/TypeScript `${variable}` template literals.
   - Writes clean cross-platform UTF-8 files using `[System.Text.UTF8Encoding]::new($false)`.

4. **Automated Backups & Rollback:**
   - `Apply-ArgonPatch.ps1` snapshots modified files to `.argon_backup/` before editing.
   - Rollback instantly with `.\Apply-ArgonPatch.ps1 -Rollback`.

---

## 📁 Project Structure

```
argon-coding-bridge/
├── plugin.json                                # Agent Plugins 1.0.0 Manifest
├── README.md                                  # Documentation
├── LICENSE                                    # MIT License
└── skills/
    └── argon-coding-bridge/
        ├── SKILL.md                           # Master Agent Skill specification
        ├── scripts/
        │   ├── Package-ArgonContext.ps1       # Windows PowerShell context packager
        │   ├── package_context.py             # Cross-platform Python packager
        │   └── Apply-ArgonPatch.ps1           # Safe patch applicator with backup & rollback
        ├── templates/
        │   └── argon_task_prompt_template.md  # Surgical prompt template
        └── references/
            └── browser_automation_and_accounts.md # Gemini Task mode & AI Studio guide
```

---

## 📄 License
MIT License. Free to use and distribute across all AI agent platforms.
