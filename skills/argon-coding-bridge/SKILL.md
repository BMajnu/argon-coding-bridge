---
name: argon-coding-bridge
description: Delegate complex coding, debugging, and refactoring tasks from MiniMax Code or any local agent to Gemini 4 Argon via Task mode or Google AI Studio. Packages the codebase into a clean, gitignore-aware ZIP (filtering build dependencies, node_modules, and secrets), generates surgical zero-UI-regression prompts, and automatically applies the returned PowerShell patch script.
---

# Argon Coding Bridge (MiniMax & Agent Plugins Edition)

Bridge **MiniMax Code** and other local AI coding agents with **Gemini 4 Argon** running in Gemini Web (**Task Mode**) or Google AI Studio (**`gemini-4-argon`**).

## Why Use This in MiniMax Code?
- **Massive Reasoning Multiplier**: Delegate heavy, multi-file architectural refactoring, complex bug diagnosis, and deep code synthesis to Gemini 4 Argon while continuing your main workflow in MiniMax Code.
- **Zero UI Regression Guarantee**: Strict surgical instructions prevent Gemini from rewriting whole files or altering existing CSS, Tailwind, Flutter widgets, or HTML layouts.
- **Strict Dependency & GitIgnore Filtering**: Accurately compresses only authentic source code, stripping `node_modules`, `vendor`, `build`, `.gradle`, `Pods`, `.next`, binaries, and private secrets (`.env*`).
- **PowerShell Exact Replacement Patch**: Changes are delivered in a clean script using `$content.Replace()` with safety checks, single-quoted Here-Strings (`@' ... '@`), and automatic backups with rollback.

---

## Workflow Guide for MiniMax Code

```
[MiniMax Code Project / Issue]
             │
             ▼
1. Package Codebase (scripts/Package-ArgonContext.ps1)
   -> Creates argon_context.zip with TASK_BRIEF.md
             │
             ▼
2. Copy Surgical Prompt (templates/argon_task_prompt_template.md)
   -> Strict Zero-UI-Regression instructions
             │
             ▼
3. Dispatch to Gemini Web (Task Mode -> 4 Argon) or Google AI Studio
   -> Upload argon_context.zip + paste prompt
             │
             ▼
4. Run apply_changes.ps1 locally
   -> Exact substring replacement with auto-backup & rollback
             │
             ▼
5. MiniMax Code automatically picks up modified files cleanly!
```

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
Provide a single, self-contained PowerShell script named `apply_changes.ps1` inside one ```powershell block:
- For edits in existing files, use safe exact substring replacements ($content.Replace) and throw an error if the snippet is not found:
  ```powershell
  $path = Join-Path (Get-Location) "relative/path/to/file.ext"
  $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
  $oldTarget = @'
// EXACT existing lines
'@
  $newReplacement = @'
// Updated replacement lines
'@
  if (-not $content.Contains($oldTarget)) { throw "Target block not found in $path. Aborting." }
  $content = $content.Replace($oldTarget, $newReplacement)
  [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
  Write-Host "[Patched] relative/path/to/file.ext" -ForegroundColor Green
  ```
- Use single-quoted Here-Strings (`@' ... '@`) so `${variable}` template literals are not corrupted.
- Write files using UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.
```

---

## Step 3: Dispatch in Browser

1. **Gemini Web (`https://gemini.google.com`)**:
   - Toggle from **Chat** to **Task**.
   - Select **4 Argon (Complex tasks)** from the model dropdown.
   - Click `+`, upload `argon_context.zip`, and send the prompt.
2. **Google AI Studio (`https://aistudio.google.com/prompts/new_chat?model=gemini-4-argon`)**:
   - Model: `gemini-4-argon`.
   - Thinking Level: `High`.
   - Enable Code execution, upload files, and send prompt.

---

## Step 4: Apply the Patch in MiniMax Code

Run the generated patch script in your project root:
```powershell
powershell -ExecutionPolicy Bypass -File .\apply_changes.ps1
```

If you ever need to rollback or use our automated applicator:
```powershell
# Rollback safely:
powershell -ExecutionPolicy Bypass -File "./skills/argon-coding-bridge/scripts/Apply-ArgonPatch.ps1" -Rollback
```
MiniMax Code will automatically refresh and reflect the updated files cleanly!
