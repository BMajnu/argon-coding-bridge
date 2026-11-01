# Gemini 4 Argon Surgical Code Task Prompt Template

Use this prompt template when interacting with **Gemini 4 Argon** in:
1. **Gemini Web (`gemini.google.com`)**: Mode set to **Task**, Model selected: **4 Argon (Complex tasks)**.
2. **Google AI Studio (`aistudio.google.com`)**: Model selected: **Gemini 4 Argon (`gemini-4-argon`)**, Thinking Level: **High**.

---

## 📋 Copy-Paste Prompt Template for Gemini 4 Argon

```markdown
I have uploaded the relevant codebase context as a zip archive (`argon_context.zip`). Inside the archive, you will find `TASK_BRIEF.md` containing the project overview and file structure.

### 🎯 Primary Objective:
{{USER_TASK_OR_BUG_DESCRIPTION}}

---

### 🛡️ STRICT SURGICAL EDIT GUARDRAILS (CRITICAL):
1. **NO UNNECESSARY FILE REWRITES:**
   - Do NOT rewrite entire files from scratch if only a function, class, or section needs modification.
   - Only modify what is strictly required to fulfill the primary objective.

2. **PRESERVE UI & STYLING INTEGRITY (ZERO VISUAL REGRESSION):**
   - Do NOT alter, refactor, or "modernize" existing UI layouts, margins, padding, color schemes, font sizes, or component styling unless explicitly requested in the objective.
   - Keep the design system, CSS/Tailwind classes, Flutter widgets, or HTML/JSX structures exactly as they are currently styled.

3. **PRESERVE EXISTING LOGIC & COMMENTS:**
   - Do NOT delete existing comments, docstrings, debug logging, or adjacent business logic.
   - Maintain the existing naming conventions, imports, and architectural patterns.

---

### 💻 OUTPUT FORMAT REQUIREMENT:
To allow instant and automated application without manual copy-pasting, please format your solution as a single, self-contained PowerShell script named `apply_changes.ps1` inside one ```powershell code block:

1. **Exact Substring Replacements for Existing Files:**
   For modifications inside existing files, use safe exact substring replacements and throw an error if the snippet is not found:
   ```powershell
   $path = Join-Path (Get-Location) "relative/path/to/file.ext"
   $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
   $oldTarget = @'
// EXACT existing lines to replace
'@
   $newReplacement = @'
// Updated replacement lines
'@
   if (-not $content.Contains($oldTarget)) {
       throw "Target block not found in $path. Aborting to prevent file corruption."
   }
   $content = $content.Replace($oldTarget, $newReplacement)
   [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
   Write-Host "[Patched] relative/path/to/file.ext" -ForegroundColor Green
   ```

2. **Single-Quoted Here-Strings (`@' ... '@`):**
   ALWAYS use single-quoted Here-Strings (`@' ... '@`) rather than double-quoted (`@" ... "@`), so that JavaScript/TypeScript template literals (`${variable}`) and shell variables are NEVER evaluated or corrupted by PowerShell.

3. **UTF-8 Without BOM:**
   Always write files using:
   `[System.IO.File]::WriteAllText($fullPath, $newContent, [System.Text.UTF8Encoding]::new($false))`

4. **Brief Summary:**
   At the end, explain:
   - Root cause or architecture of the fix.
   - Exact files changed and why.
   - How to verify (e.g., test command, dev server check).
```
