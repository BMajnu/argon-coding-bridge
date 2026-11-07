#!/usr/bin/env python3
"""
record_fix.py - Record a defect fix into global fix memory (~/.gemini/argon_fixes/).

Stores structured debugging notes so agents and developers can track, analyze,
and commit fixes onto dedicated `fixes/<scope-slug>` branches.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

GLOBAL_FIXES_DIR = Path.home() / ".gemini" / "argon_fixes"
FIXES_NOTES_DIR = GLOBAL_FIXES_DIR / "fixes"
FIXES_INDEX_PATH = GLOBAL_FIXES_DIR / "index.json"

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9_-]+ PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    re.compile(r"\"password\"\s*:\s*\"[^\"]{4,}\"", re.IGNORECASE),
    re.compile(r"(?:api_key|client_secret|access_token|secret_key)\s*[:=]\s*['\"][^'\"]{6,}['\"]", re.IGNORECASE),
]


def _detect_secrets(texts: List[str]) -> bool:
    """Check text snippets for credential or secret patterns."""
    for text in texts:
        if not text:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                return True
    return False


def _slugify(text: str) -> str:
    """Convert title to clean filesystem / git slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return s.strip("-")[:40] or "fix"


def record_fix(
    title: str,
    scope: List[str],
    symptom: str,
    root_cause: str,
    fix: str,
    verified_by: str,
    tags: Optional[List[str]] = None,
    project: str = ".",
    branch: str = "",
    commit: str = "",
    also_copy_to_repo: bool = False,
) -> int:
    # 1. Validate secrets
    payload_texts = [title, symptom, root_cause, fix, verified_by, project, branch, commit]
    payload_texts.extend(scope)
    payload_texts.extend(tags or [])

    if _detect_secrets(payload_texts):
        print(
            "[!] Error: Refusing to record fix. The provided content appears to contain a secret, token, or password.",
            file=sys.stderr,
        )
        return 1

    # 2. Ensure directories exist
    FIXES_NOTES_DIR.mkdir(parents=True, exist_ok=True)

    # 3. Generate ID: <yyyyMMdd-HHmmss>-<slug>
    now_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_slug = _slugify(title)
    fix_id = f"{now_ts}-{base_slug}"

    counter = 2
    target_note_path = FIXES_NOTES_DIR / f"{fix_id}.md"
    while target_note_path.exists():
        fix_id = f"{now_ts}-{base_slug}-{counter}"
        target_note_path = FIXES_NOTES_DIR / f"{fix_id}.md"
        counter += 1

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    proj_resolved = str(Path(project).expanduser().resolve())
    clean_scope = [s.strip() for s in scope if s.strip()]
    clean_tags = [t.strip() for t in (tags or []) if t.strip()]

    # Scope slug for git branch
    scope_slug = _slugify(Path(clean_scope[0]).stem if clean_scope else base_slug)
    if not branch:
        branch = f"fixes/{scope_slug}"

    # 4. Write note markdown
    scope_bullets = "\n".join(f"- `{s}`" for s in clean_scope) if clean_scope else "- `(general)`"
    tags_str = ", ".join(clean_tags) if clean_tags else "(none)"

    md_content = f"""# {title}

- **ID:** `{fix_id}`
- **Created At:** {created_at}
- **Project:** `{proj_resolved}`
- **Branch:** `{branch}`
- **Commit:** `{commit or '(uncommitted)'}`
- **Tags:** {tags_str}

## Scope
{scope_bullets}

## Symptom
{symptom}

## Root Cause
{root_cause}

## Fix
{fix}

## Verified By
{verified_by}
"""
    target_note_path.write_text(md_content, encoding="utf-8")

    # 5. Update index.json
    index_data: Dict[str, Any] = {"version": "1.0.0", "fixes": []}
    if FIXES_INDEX_PATH.is_file():
        try:
            parsed = json.loads(FIXES_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and "fixes" in parsed:
                index_data = parsed
        except Exception:
            pass

    fix_record = {
        "id": fix_id,
        "created_at": created_at,
        "title": title,
        "scope": clean_scope,
        "symptom": symptom,
        "root_cause": root_cause,
        "fix": fix,
        "verified_by": verified_by,
        "tags": clean_tags,
        "project": proj_resolved,
        "branch": branch,
        "commit": commit,
    }

    index_data.setdefault("fixes", []).append(fix_record)
    FIXES_INDEX_PATH.write_text(json.dumps(index_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"[+] Recorded fix note: {fix_id}")
    print(f"    Global note : {target_note_path}")
    print(f"    Global index: {FIXES_INDEX_PATH}")

    # 6. Copy to repo if requested
    if also_copy_to_repo:
        repo_dir = Path(project).resolve()
        repo_fixes_dir = repo_dir / "knowledge" / "fixes"
        repo_fixes_dir.mkdir(parents=True, exist_ok=True)
        repo_note_path = repo_fixes_dir / f"{fix_id}.md"
        repo_note_path.write_text(md_content, encoding="utf-8")
        rel_repo_path = repo_note_path.relative_to(repo_dir)

        print(f"    Repo copy   : {rel_repo_path}")
        print("\nSuggested git commands to commit this fix on a dedicated branch:")
        print(f"    git checkout -b {branch}")
        print(f"    git add {rel_repo_path.as_posix()}")
        print(f"    git commit -m \"fix({clean_scope[0] if clean_scope else 'core'}): {title}\"")
        print(f"    git push -u origin {branch}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a defect fix into global debugging memory")
    parser.add_argument("--title", required=True, help="One-line summary of the defect and fix")
    parser.add_argument("--scope", action="append", required=True, help="File path or component affected")
    parser.add_argument("--symptom", required=True, help="What broke or was observed")
    parser.add_argument("--root-cause", required=True, help="Why the failure occurred")
    parser.add_argument("--fix", required=True, help="What change resolved the issue")
    parser.add_argument("--verified-by", required=True, help="Exact command or check that proved the fix")
    parser.add_argument("--tag", dest="tags", action="append", default=[], help="Optional tags for classification")
    parser.add_argument("--project", default=".", help="Project path (default: current directory)")
    parser.add_argument("--branch", default="", help="Git branch name (default: fixes/<scope-slug>)")
    parser.add_argument("--commit", default="", help="Git commit SHA if already committed")
    parser.add_argument("--also-copy-to-repo", action="store_true", help="Copy note to knowledge/fixes/<id>.md in repo")

    args = parser.parse_args()
    return record_fix(
        title=args.title,
        scope=args.scope,
        symptom=args.symptom,
        root_cause=args.root_cause,
        fix=args.fix,
        verified_by=args.verified_by,
        tags=args.tags,
        project=args.project,
        branch=args.branch,
        commit=args.commit,
        also_copy_to_repo=args.also_copy_to_repo,
    )


if __name__ == "__main__":
    sys.exit(main())
