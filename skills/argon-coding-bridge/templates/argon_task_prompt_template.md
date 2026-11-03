# Gemini 4 Argon Surgical Code Task Prompt Template

Use this prompt template when interacting with **Gemini 4 Argon** in:
1. **Gemini Web (`gemini.google.com`)**: Mode set to **Task**, Model selected: **4 Argon (Complex tasks)**.

---

## 📋 Copy-Paste Prompt Template for Gemini 4 Argon

```markdown
I have uploaded the relevant codebase context as a zip archive (`argon_context.zip`). Inside the archive, you will find `TASK_BRIEF.md` containing the project overview and file structure.

### 🎯 Primary Objective:
{{USER_TASK_OR_BUG_DESCRIPTION}}

{{CONTEXT_NOTE}}

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

### 📦 OUTPUT DELIVERY REQUIREMENT (STRICT — DELIVER A FILE, DO NOT PASTE THE SCRIPT):
1. **WRITE THE SCRIPT TO A FILE:** Put the complete patch script in a file named **`apply_changes.ps1`**.
2. **ATTACH THAT FILE TO YOUR REPLY:** Deliver `apply_changes.ps1` as an attached file so it can be downloaded with one click (no copy-paste, no re-typing).
3. **KEEP THE CHAT REPLY SHORT** — it must contain only:
   - One line stating what changed.
   - A table of the files that changed and why.
   - A reference to the attached `apply_changes.ps1`.
   - How to verify the change.
4. **DO NOT PASTE THE BODY OF `apply_changes.ps1` INTO THE CHAT:** An inline fenced `powershell` block is a fallback used ONLY when file delivery is unavailable; in that case output the full script in one block. A short inline excerpt (the `$TargetRoot` line plus the list of touched files) is fine.
5. **WHY:** A delivered file is applied with one click, while a streamed code block is slow to render and can be truncated — which silently corrupts the patch.

### 🗂️ TARGET DIRECTORY (USE THESE EXACT PATHS):
The target codebase lives at the absolute directory pinned in `$TargetRoot` below. The generated script MUST begin by pinning it and MUST resolve every path against it:

   ```powershell
   $TargetRoot = '{{TARGET_DIRECTORY}}'
   ```

   Rules:
   - Every file the script reads or writes is resolved with `Join-Path $TargetRoot '<relative path>'`.
   - Never depend on the current working directory.
   - Never guess, discover, or "search for" a different directory.
   - The script must apply EVERY change in one run.

### 💻 PATCH SCRIPT CONTENT REQUIREMENTS:
The delivered `apply_changes.ps1` is a single, self-contained PowerShell script that applies the ENTIRE change set in one pass.

1. **PIN THE TARGET DIRECTORY FIRST:**
   The first meaningful line of the script pins the target root, then the backup session is derived from it. Every later path comes from that root:

   ```powershell
   $TargetRoot = '<absolute target directory>'
   $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
   $backupSession = Join-Path (Join-Path $TargetRoot '.argon_backup') (Get-Date -Format 'yyyyMMdd_HHmmss')
   ```

2. **BACK UP EVERY FILE BEFORE OVERWRITING IT:**
   Before a file is written, copy its current content into `<target>/.argon_backup/<yyyyMMdd_HHmmss>/<relative path>` (same relative layout), so the whole session can be restored later:

   ```powershell
   $backupDest = Join-Path $backupSession $relPath
   $backupParent = [System.IO.Path]::GetDirectoryName($backupDest)
   if ($backupParent -and -not (Test-Path $backupParent)) {
       New-Item -ItemType Directory -Path $backupParent -Force | Out-Null
   }
   Copy-Item -LiteralPath $path -Destination $backupDest -Force
   ```

3. **EXACT SUBSTRING REPLACEMENT (NO FULL-FILE REWRITE):**
   For modifications inside existing files, use safe exact substring replacements and throw an error if the snippet is not found:

   ```powershell
   $relPath = 'src/path/to/file.ext'
   $path = Join-Path $TargetRoot $relPath
   $content = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
   $oldTarget = @'
   // EXACT existing lines to replace
   '@
   $newReplacement = @'
   // Updated replacement lines
   '@
   if (-not $content.Contains($oldTarget)) {
       throw "Target block not found in $relPath. Aborting before any file is written."
   }
   ```

4. **VALIDATE FIRST, APPLY AFTER (A PARTIAL APPLY MUST NEVER HAPPEN):**
   Abort with `throw` BEFORE touching anything if a target block is not found. Read and verify EVERY target block first; only after all of them are confirmed do you start writing files. A failed check must leave the project untouched.

5. **WRITE + REPORT PER FILE:**
   Write the patched content back with UTF-8 without BOM, then print exactly one progress line per changed file:

   ```powershell
   $content = $content.Replace($oldTarget, $newReplacement)
   [System.IO.File]::WriteAllText($path, $content, $utf8NoBom)
   Write-Host "[Patched] $relPath" -ForegroundColor Green
   ```

6. **SINGLE-QUOTED HERE-STRINGS (`@' ... '@`):**
   ALWAYS use single-quoted Here-Strings (`@' ... '@`) rather than double-quoted (`@" ... "@`), so that JavaScript/TypeScript template literals (`${variable}`) and shell variables are NEVER evaluated or corrupted by PowerShell.

7. **UTF-8 WITHOUT BOM:**
   Always write files using:
   `[System.IO.File]::WriteAllText($fullPath, $newContent, [System.Text.UTF8Encoding]::new($false))`

8. **PROGRESS LINES + FINAL SUMMARY LINE:**
   Print one `[Patched] <relative path>` line per changed file, and finish with a single summary line:

   ```powershell
   Write-Host "[SUCCESS] $patchedCount file(s) patched." -ForegroundColor Green
   ```

### ✅ VERIFY YOUR WORK (REQUIRED)
The patch is applied by RUNNING your script, so the script must prove that it worked:
1. After writing each file, re-read it from disk and assert that the new content is really
   there.
2. Print `[Verified] <relative path>` for every file that now matches, and
   `[FAILED VERIFY] <relative path>` for every file that does not.
3. If any verification fails, exit with a non-zero code so the run is reported as failed.
4. Never report success for a file you have not re-read from disk.

### 🗒️ RECORD WHAT YOU FOUND (REQUIRED)
While reading the project you will often notice something inconsistent, stale, risky or simply
able to be done better. Do not silently fix it and do not leave it only in the chat reply - write
it down so it survives.

1. **In the script**: put every finding in a comment block at the very END of `apply_changes.ps1`,
   after the last write, using this exact shape so it can be extracted automatically:
   ```powershell
   # --- NOTES: inconsistencies and suggestions found while reading the project ---
   # NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>
   # NOTE: <file> - <second finding, and the concrete better approach>
   # --- END NOTES ---
   ```
2. **In the chat reply**: list the same findings in a short section titled `Notes and suggestions`.
3. Only write a NOTE for something you actually observed in the code. If you found nothing,
   write a single line `# NOTE: no inconsistencies found in the reviewed files.` - do not invent
   findings, and do not describe work you did not do.
4. A NOTE is advice, not a change: never edit a file just to record a NOTE about it.
5. Do not guess missing context:
   If something you need is not in this archive - a file, an API endpoint, a
   function, a configuration - DO NOT GUESS and DO NOT INVENT it. Say exactly
   which file or symbol is missing and stop there. A wrong guess is much worse
   than an incomplete answer.

### 📝 SUMMARY SECTION:
Keep the chat reply brief — it is a summary of the change, NOT the patch itself:
- Root cause or architecture of the fix.
- The exact files changed and why (as a table).
- How to verify (e.g., test command, dev server check).
- A pointer to the attached `apply_changes.ps1` as the artifact that actually applies the change.
```

---

### 🔁 Substitution Note

The whole copy-paste prompt above stays inside **one** fenced `markdown` block, and the nested snippets inside it are indented. Keep both properties: the renderer extracts that single fence, then substitutes the placeholders it contains — the task description (Primary Objective), the session continuity note and the absolute target directory (the `$TargetRoot` line) — before sending it to Argon. Each placeholder appears exactly once in the prompt body.