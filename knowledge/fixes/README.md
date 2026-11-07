# Global Fix Memory & Fix Branches

This directory tracks structured debugging notes and root-cause analyses for defects discovered and resolved across tasks.

## Why This Exists

When coding agents and developers encounter bugs, regressions, or integration hurdles, they record the exact symptom, measured root cause, surgical fix, and verification steps using `record_fix.py`.

Recording fixes ensures that:
1. **Fix memory survives conversation resets:** The global debugging memory lives in `~/.gemini/argon_fixes/` alongside browser profiles and tasks.
2. **Fixes live on isolated branches:** Each fix is authored and committed on a dedicated `fixes/<scope-slug>` branch (e.g. `fixes/package-context-binary-cap` or `fixes/dispatch-task-mode-chip`).
3. **Automated update analysis:** When preparing subsequent updates, `analyze_fixes.py` scans candidate fix branches and formats brief-ready instructions to incorporate them cleanly into the next round.

## How to Record a Fix

To record a resolved bug:

```bash
python skills/argon-coding-bridge/scripts/record_fix.py \
  --title "exclude binary fonts from archive" \
  --scope "skills/argon-coding-bridge/scripts/package_context.py" \
  --symptom "Gemini reported that argon_context.zip contained binary data" \
  --root-cause "DEFAULT_EXCLUDED_EXTS lacked font and image extensions" \
  --fix "add font and media extensions to DEFAULT_EXCLUDED_EXTS" \
  --verified-by "python -m py_compile skills/...; package_context verified" \
  --also-copy-to-repo
```

Then commit the note on a dedicated branch:

```bash
git checkout -b fixes/package-context-binary-cap
git add knowledge/fixes/<fix-id>.md
git commit -m "fix(package_context): exclude binary fonts from archive"
git push -u origin fixes/package-context-binary-cap
```

## How Updates Incorporate Fixes

Run `analyze_fixes.py` to see unmerged fix branches and their commits:

```bash
python skills/argon-coding-bridge/scripts/analyze_fixes.py --project-path .
```

To output only the markdown block for your next task brief:

```bash
python skills/argon-coding-bridge/scripts/analyze_fixes.py --project-path . --brief
```
