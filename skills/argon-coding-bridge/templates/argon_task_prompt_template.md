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

## FIND THE ROOT CAUSE FIRST

If something in this project is broken, do not change anything until you can
state the root cause and the EVIDENCE for it (the exact file and line, the
exact DOM node you measured, the exact value you read).

- Every symptom must be traced to a concrete cause in THIS code base.
- If you cannot find the cause in the archive, say exactly what you need -
  do not invent a fix and do not guess a signature, a path or a value.
- Put the root cause and its evidence in the NOTES block of your patch, under
  the heading `## ROOT CAUSE`.

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

### 🎨 MULTI-ARTIFACT DELIVERY (WHEN THE TASK PRODUCED MORE THAN A PATCH):
The patch file is only ONE part of a delivery. A round often produces artifacts that a page file cannot carry at all - a generated image, an SVG graphic, an icon set, a UI card mock-up, a voice-over, a data file. **Deliver those too, as FILES.**

1. **ATTACH EVERY EXTRA ARTIFACT AS ITS OWN FILE**, in the SAME reply as `apply_changes.ps1`. Each attached file becomes a chip; the bridge downloads every chip of your turn and puts the non-patch ones into `assets/` next to the patch, then packs the whole set into `argon_delivery.zip` with a `DELIVERY_MANIFEST.md` (file name, byte size, SHA-256 and one-line description of every file).
2. **NAME EACH FILE SO IT EXPLAINS ITSELF** - the file name becomes the asset name:
   `logo-mark.svg`, `app-icon-1024.png`, `card-background.png`, `onboarding-screen.jpg`, `voice-over-intro.wav`, `icon-set.zip`.
   Always use the REAL format extension (`.png`, `.jpg`, `.webp`, `.svg`, `.wav`, `.mp3`, `.zip`, `.json`) so the bridge can tell the kinds apart.
3. **LIST EACH EXTRA ARTIFACT IN THE CHAT REPLY** - one line each: the file name, what it is and what it is for. The bridge records the size and the SHA-256, but only YOU know what the artifact means.
4. **`apply_changes.ps1` STAYS THE PRIMARY FILE** and must contain EVERY code change. The artifacts never replace it, and code that belongs in the patch never travels as an artifact.
5. **NEVER PASTE A BINARY INTO THE CHAT** - attach the file. An inline base64 blob is unreadable, huge and gets truncated.
6. **IF THE TASK NEEDED NO ARTIFACT, DELIVER ONLY THE PATCH.** Never invent files; the bundle is built from whatever you really produced.

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

3. **LINEAR ORDINAL SUBSTRING REPLACEMENT (NEVER `.Replace` OR `.Contains` ON LARGE NEEDLES):**
   On Windows PowerShell 5.1 (.NET Framework), `String.Replace(String, String)` and `String.Contains(String)` are `O(file x needle)`: on a 500 KB file with multi-kilobyte needles, a 55-block patch hung for **25 minutes** (CPU 501 s, 485 MB RAM) before being killed, whereas linear ordinal search + `StringBuilder` applies the same 55 blocks in **0.0 seconds**. With 100 MB archives and full-codebase context, files and needles are large, so you MUST use `[System.StringComparison]::Ordinal` index search + `[System.Text.StringBuilder]` and separate the script into two loops (Phase 1 pre-flight check loop across ALL files before any write, then Phase 2 backup + write + verify loop):

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
   $idx = $content.IndexOf($oldTarget, [System.StringComparison]::Ordinal)
   if ($idx -lt 0) {
       throw "Target block not found in $relPath. Aborting before any file is written."
   }
   ```

4. **VALIDATE FIRST, APPLY AFTER (TWO SEPARATE LOOPS — A PARTIAL APPLY MUST NEVER HAPPEN):**
    Phase 1 MUST read every target file and check EVERY `$oldTarget` block of EVERY file using `$content.IndexOf($oldTarget, [System.StringComparison]::Ordinal)` BEFORE the first file is backed up or written. A missing target aborts with `throw` while the project on disk is still 100% untouched (never check-and-write inside a single per-file loop).

5. **WRITE + REPORT PER FILE (LINEAR `StringBuilder` REBUILD):**
   In Phase 2, after all pre-flight checks have passed and the file has been backed up to `.argon_backup/<yyyyMMdd_HHmmss>/<relPath>`, replace each target block in linear time and write UTF-8 without BOM:

   ```powershell
   $idx = $content.IndexOf($oldTarget, [System.StringComparison]::Ordinal)
   if ($idx -lt 0) { throw "Target block not found in $relPath. Aborting before any file is written." }
   $sb = [System.Text.StringBuilder]::new($content.Length + $newReplacement.Length)
   [void]$sb.Append($content, 0, $idx)
   [void]$sb.Append($newReplacement)
   [void]$sb.Append($content, $idx + $oldTarget.Length, $content.Length - $idx - $oldTarget.Length)
   $content = $sb.ToString()
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

9. **CREATING A FILE THAT DOES NOT EXIST YET (NEW FILES):**
   A file that is brand new is delivered as a file entry that has a `New` block and **NO `Old`
   block at all** - that is how the bridge recognises "create this file" (an `Old` block means
   "replace this exact existing text"). In the data-driven format that looks like this:

   ```powershell
   $PatchFiles = @(
       @{ RelPath = 'src/new_module.py'
          Blocks  = @( @{ New = @'
   ...the complete content of the new file...
   '@ } ) }
   )
   ```

   - The apply script MUST create the parent directory when it is missing, write the file as
     UTF-8 without BOM, then re-read it from disk and verify the content is really there.
   - There is nothing to back up for a new file, and the Phase-1 pre-flight must NOT try to find
     an `Old` block for it - a new file simply has no `Old` block to check.
   - A file entry that has BOTH `Old` and `New` replaces existing text; a file entry that has ONLY
     `New` creates the file. Never mix the two meanings inside one file entry.

### ✅ VERIFY YOUR WORK (REQUIRED)
The patch is applied by RUNNING your script, so the script must prove that it worked:
1. After writing each file, re-read it from disk (`$verify = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)`) and assert with `$verify.IndexOf($newReplacement, [System.StringComparison]::Ordinal) -ge 0` that the new content is really there.
2. Print `[Verified] <relative path>` for every file that now matches, and
   `[FAILED VERIFY] <relative path>` for every file that does not.
3. If any verification fails, exit with a non-zero code so the run is reported as failed.
4. Never report success for a file you have not re-read from disk.
5. **WHEN THE CHANGE ADDS A RUNNABLE BEHAVIOUR, THE PATCH MUST ALSO ADD A SELF-TEST THAT
   EXERCISES IT.** A new command, a new service, a new loop or a new endpoint is not delivered
   until one command runs it end to end and prints the real result. The self-test must run
   against a THROWAWAY fixture (its own temporary profile, its own port, its own directory) so it
   never touches real user data, real accounts or a running instance.
6. **Never report a number you did not measure.** If the self-test prints a latency, a count or a
   duration, that number must come from the run that just happened. A hard-coded or copied
   measurement is a false report, and a false report is worse than no report.

### 🗒️ RECORD WHAT YOU FOUND (REQUIRED)
While reading the project you will often notice something inconsistent, stale, risky or simply
able to be done better. Do not silently fix it and do not leave it only in the chat reply - write
it down so it survives.

1. **In the script**: put every finding in a comment block at the very END of `apply_changes.ps1`,
   after the last write, using this exact shape so it can be extracted automatically. The NOTES
   block of every delivered patch MUST start with a `## ROOT CAUSE` section naming the root cause
   and its evidence (even when the change is trivial, state what the problem was and why the
   change fixes it):
   ```powershell
   # --- NOTES: inconsistencies and suggestions found while reading the project ---
   # ## ROOT CAUSE
   # NOTE: <file>:<line> - <exact root cause, measured evidence, and why this change fixes it>
   # NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>
   # NOTE: <file> - <second finding, and the concrete better approach>
   # --- END NOTES ---
   ```
2. **In the chat reply**: list the same findings in a short section titled `Notes and suggestions`.
3. Only write a NOTE for something you actually observed in the code. If you found nothing,
   write a single line `# NOTE: no inconsistencies found in the reviewed files.` - do not invent
   findings, and do not describe work you did not do.
4. A NOTE is advice, not a change: never edit a file just to record a NOTE about it.
5. The exact API of this project (do not guess missing context):
   API_INDEX.md lists every public declaration with its file and line. Use THAT -
   never invent a function name, a parameter or a return type. If a symbol you need
   is not in API_INDEX.md and not in the archive, DO NOT GUESS: name the missing
   symbol and stop.
   If something you need is not in this archive - a file, an API endpoint, a
   function, a configuration - DO NOT GUESS and DO NOT INVENT it. Say exactly
   which file or symbol is missing and stop there. A wrong guess is much worse
   than an incomplete answer.
6. Byte-exact payload literals and escapes:
   Inside a patch, a Rust (or any language) char or string literal for a newline is
   the TWO characters backslash and n (\n) - never a real newline. The same applies
   to \t, \r, \\ and to every other escape. The patch must not contain a raw
   control character at all. A straight apostrophe ' must stay a straight
   apostrophe - never a typographic quote. Never start a payload line with @' or '@,
   never put $ inside a double-quoted here-string, and keep all opening and closing
   braces balanced in every replacement block.
7. Verification command:
   Name the exact command that builds/lints/tests this project under VERIFICATION COMMAND
   in your reply and in the NOTES block so the bridge can run it after applying the patch
   - for the bridge repository itself, run python -m py_compile on the bridge scripts and python skills/argon-coding-bridge/scripts/dispatch_argon.py --self-test.

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