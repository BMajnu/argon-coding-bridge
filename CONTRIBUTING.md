# Contributing to Argon Coding Bridge

Welcome! Argon Coding Bridge connects your local terminal and developer agents directly to Gemini 4 Argon in verified Task Mode via automated browser orchestration.

This repository is public and open for contributions. Because the bridge is designed for high-reliability automated coding workflows, all contributions must adhere to our contribution workflows and core architectural invariants.

---

## Task Flow Overview

1. **Context Packaging:** `package_context.py` bundles eligible codebase files, strips secrets, excludes binaries and build artifacts, and generates `TASK_BRIEF.md` and `API_INDEX.md`.
2. **Brief Preparation & Review:** `prepare_brief.py` generates `brief_summary.md`, giving the user a byte-exact preview of the prompt, target files, and archive before dispatch.
3. **Verified Dispatch:** `dispatch_argon.py` navigates to Gemini Web, verifies signed-in session, ensures Task mode and the `4 Argon` model chip, uploads the archive, and sends the prompt.
4. **Patch Capture & Application:** The returned `apply_changes.ps1` patch is captured from the viewer toolbar download button, executes exact substring replacements, creates timestamped backups in `.argon_backup/`, and verifies post-patch file state.
5. **Local Verification:** Build and lint commands run once locally against the target codebase.

---

## Contributor Workflows

### 1. Contributing a Bug Fix

1. **Identify and Measure:** Measure the concrete defect in code or browser state. Name the exact file, line, DOM node, or value.
2. **Implement Surgically:** Apply only necessary modifications without full file rewrites or UI restyling.
3. **Record the Fix:**
   ```bash
   python skills/argon-coding-bridge/scripts/record_fix.py \
     --title "<one line summary>" \
     --scope "<affected file path>" \
     --symptom "<what was broken>" \
     --root-cause "<why it occurred>" \
     --fix "<what was changed>" \
     --verified-by "<exact verification command>" \
     --also-copy-to-repo
   ```
4. **Commit on a Dedicated Branch:**
   ```bash
   git checkout -b fixes/<scope-slug>
   git add <modified-files> knowledge/fixes/<fix-id>.md
   git commit -m "fix(<scope>): <one line summary>"
   git push -u origin fixes/<scope-slug>
   ```
5. **Open a Pull Request:** Describe the symptom, measured evidence, and how the fix was verified.

### 2. Contributing a Feature

1. Create a dedicated feature branch:
   ```bash
   git checkout -b feature/<feature-slug>
   ```
2. Implement your changes following surgical edit guardrails.
3. Run verification before committing:
   ```bash
   python -m py_compile skills/argon-coding-bridge/scripts/*.py
   ```
4. Open a Pull Request against `main`.

---

## How Your Contribution Is Picked Up

The owner does not merge branches by hand. The fix memory is the inbox:

```bash
python skills/argon-coding-bridge/scripts/analyze_fixes.py --project-path .
```

`analyze_fixes.py` reads the global fix memory (`~/.gemini/argon_fixes/index.json` and
`~/.gemini/argon_fixes/fixes/`), inspects every local and remote branch named
`fixes/*`, and reports the commits that are **not** in `main`:

- **CANDIDATE FIX - not merged** - a note whose branch and commit point at a commit
  that is still outside `main`, together with the exact command that brings it in
  (`git cherry-pick <sha>` or `git merge fixes/<branch>`).
- **MEMORY ONLY - no commit to merge** - a recorded fix that has no branch yet. It is
  still worth reading, but there is nothing to merge.

The report ends with a **brief-ready** section that is pasted straight into the next
task brief, so a fix you contribute can be folded into the next update. Use
`--brief` to print only that section, and `--json` for machine-readable output.

The script never modifies the repository - it only reads.

---

## Non-Negotiable Invariants

The following core rules MUST NOT be changed or bypassed in any contribution:

1. **Viewer Download Button:** Patch scripts (`apply_changes.ps1`) are always captured and downloaded from the viewer toolbar download button—never scraped from preview text or chat blocks.
2. **Single Local Verification:** Project build/lint checks run locally once after applying the patch; the bridge never loops back asking Argon to fix a build error.
3. **Account Policy:** A new task skips an account only when it already runs the maximum number of tasks (`MAX_TASKS_PER_SLOT`); a follow-up always uses its own conversation account even when busy.
4. **Zero UI Regression:** Maintain existing styling, layouts, and component structures.

## License

This project is licensed under the MIT License. By submitting contributions to this repository, you agree that your contributions are licensed under the same MIT terms.
