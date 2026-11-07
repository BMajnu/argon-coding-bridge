#!/usr/bin/env python3
"""
analyze_fixes.py - Inspect recorded fixes and match candidate branches/commits for upcoming updates.

Reads ~/.gemini/argon_fixes/index.json, inspects git branches matching `fixes/*`,
and outputs actionable reports and brief-ready sections for next-round dispatches.
Does NOT modify the repository.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

GLOBAL_FIXES_DIR = Path.home() / ".gemini" / "argon_fixes"
FIXES_INDEX_PATH = GLOBAL_FIXES_DIR / "index.json"
FIXES_NOTES_DIR = GLOBAL_FIXES_DIR / "fixes"


def _run_git(args: List[str], cwd: Path) -> str:
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def _get_unmerged_fix_branches(repo_root: Path) -> Dict[str, List[Dict[str, str]]]:
    """Find branches named fixes/* and their commits that are not on main."""
    branch_output = _run_git(["branch", "-a"], repo_root)
    if not branch_output:
        return {}

    fix_branches: Set[str] = set()
    for line in branch_output.splitlines():
        line = line.strip().lstrip("* ").strip()
        if not line:
            continue
        # Strip remote prefix
        if line.startswith("remotes/origin/"):
            clean_b = line.replace("remotes/origin/", "")
        elif line.startswith("origin/"):
            clean_b = line.replace("origin/", "")
        else:
            clean_b = line
        if clean_b.startswith("fixes/"):
            fix_branches.add(clean_b)

    branch_commits: Dict[str, List[Dict[str, str]]] = {}
    for branch in sorted(fix_branches):
        log_out = _run_git(["log", f"main..{branch}", "--oneline"], repo_root)
        commits: List[Dict[str, str]] = []
        if log_out:
            for l in log_out.splitlines():
                parts = l.strip().split(" ", 1)
                sha = parts[0]
                msg = parts[1] if len(parts) > 1 else ""
                commits.append({"sha": sha, "msg": msg})
        branch_commits[branch] = commits

    return branch_commits


def analyze_fixes(
    project_path: str = ".",
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    as_json: bool = False,
    brief_only: bool = False,
) -> int:
    proj_root = Path(project_path).resolve()
    repo_root = Path(repo).resolve() if repo else proj_root

    fixes: List[Dict[str, Any]] = []
    if FIXES_INDEX_PATH.is_file():
        try:
            parsed = json.loads(FIXES_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("fixes"), list):
                fixes = parsed["fixes"]
        except Exception:
            pass

    if tag:
        tag_lower = tag.strip().lower()
        fixes = [
            f for f in fixes
            if any(tag_lower == str(t).strip().lower() for t in f.get("tags", []))
        ]

    unmerged_branch_commits = _get_unmerged_fix_branches(repo_root)

    candidates: List[Dict[str, Any]] = []
    memory_only: List[Dict[str, Any]] = []
    merged_items: List[Dict[str, Any]] = []

    for f in fixes:
        f_id = f.get("id", "")
        f_title = f.get("title", "")
        f_scope = f.get("scope", [])
        f_branch = f.get("branch", "")
        f_commit = f.get("commit", "")

        # Relevance check: does scope touch current project files?
        relevant_files = []
        for s in f_scope:
            if (proj_root / s).exists():
                relevant_files.append(s)
        is_relevant = bool(relevant_files) or (not f_scope)

        item = dict(f)
        item["relevant_to_project"] = is_relevant
        item["matching_local_files"] = relevant_files

        if not f_branch and not f_commit:
            item["status"] = "MEMORY ONLY - no commit to merge"
            memory_only.append(item)
            continue

        # Check git branch and commit status
        matched_branch_commits = unmerged_branch_commits.get(f_branch, [])
        is_unmerged = False
        if matched_branch_commits:
            if f_commit:
                is_unmerged = any(c["sha"].startswith(f_commit) or f_commit.startswith(c["sha"]) for c in matched_branch_commits)
            else:
                is_unmerged = True

        if is_unmerged:
            item["status"] = "CANDIDATE FIX - not merged"
            item["command"] = f"git cherry-pick {f_commit}" if f_commit else f"git merge {f_branch}"
            candidates.append(item)
        else:
            if f_commit or f_branch:
                item["status"] = "MERGED or branch up-to-date with main"
                item["command"] = f"git cherry-pick {f_commit}" if f_commit else f"git merge {f_branch}"
                merged_items.append(item)
            else:
                item["status"] = "MEMORY ONLY - no commit to merge"
                memory_only.append(item)

    # Format brief-ready section
    brief_lines = [
        "## Candidate Fixes from Debugging Memory",
        "",
        "The following fixes were recorded in earlier runs or on fix branches and are relevant to incorporate:",
        "",
    ]
    if candidates or memory_only:
        for c in candidates:
            scope_str = ", ".join(f"`{s}`" for s in c.get("scope", []))
            brief_lines.append(f"### Fix: {c.get('title')}")
            brief_lines.append(f"- **ID:** `{c.get('id')}`")
            brief_lines.append(f"- **Scope:** {scope_str}")
            brief_lines.append(f"- **Branch / Commit:** `{c.get('branch')}` (`{c.get('commit') or 'head'}`)")
            brief_lines.append(f"- **Symptom:** {c.get('symptom')}")
            brief_lines.append(f"- **Root Cause:** {c.get('root_cause')}")
            brief_lines.append(f"- **Fix:** {c.get('fix')}")
            brief_lines.append(f"- **Action:** `{c.get('command')}`")
            brief_lines.append("")
        for m in memory_only:
            scope_str = ", ".join(f"`{s}`" for s in m.get("scope", []))
            brief_lines.append(f"### Note (Memory Only): {m.get('title')}")
            brief_lines.append(f"- **ID:** `{m.get('id')}`")
            brief_lines.append(f"- **Scope:** {scope_str}")
            brief_lines.append(f"- **Root Cause:** {m.get('root_cause')}")
            brief_lines.append(f"- **Fix:** {m.get('fix')}")
            brief_lines.append("")
    else:
        brief_lines.append("(No pending candidate fixes or unmerged notes found in memory.)")
        brief_lines.append("")

    brief_section_text = "\n".join(brief_lines)

    if brief_only:
        print(brief_section_text)
        return 0

    if as_json:
        report_data = {
            "total_fixes": len(fixes),
            "candidates": candidates,
            "memory_only": memory_only,
            "merged": merged_items,
            "brief_section": brief_section_text,
        }
        print(json.dumps(report_data, indent=2))
        return 0

    # Print full human-readable report
    print("=" * 78)
    print("ARGON FIX MEMORY ANALYSIS REPORT")
    print(f"Project Path : {proj_root}")
    print(f"Total Fixes  : {len(fixes)}")
    print(f"Candidates   : {len(candidates)}")
    print(f"Memory Only  : {len(memory_only)}")
    print(f"Merged/Other : {len(merged_items)}")
    print("=" * 78)

    if candidates:
        print("\n[!] CANDIDATE FIXES (Found on fixes/* branches, not merged into main):")
        for c in candidates:
            print(f"  * ID: {c.get('id')} - {c.get('title')}")
            print(f"    Branch: {c.get('branch')} | Commit: {c.get('commit') or '(head)'}")
            print(f"    Scope : {', '.join(c.get('scope', []))}")
            print(f"    Apply : {c.get('command')}")
            print(f"    Cause : {c.get('root_cause')}")
    else:
        print("\n[+] No unmerged candidate fix branches found.")

    if memory_only:
        print("\n[i] MEMORY ONLY (Recorded notes without an unmerged commit):")
        for m in memory_only:
            print(f"  * ID: {m.get('id')} - {m.get('title')}")
            print(f"    Scope : {', '.join(m.get('scope', []))}")
            print(f"    Cause : {m.get('root_cause')}")
            print(f"    Fix   : {m.get('fix')}")

    print("\n" + "=" * 78)
    print("BRIEF-READY SECTION (for pasting into next task brief):")
    print("=" * 78)
    print(brief_section_text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze recorded fixes against repository branches")
    parser.add_argument("--project-path", default=".", help="Project path to inspect")
    parser.add_argument("--repo", default=None, help="Repository root path if different from project-path")
    parser.add_argument("--tag", default=None, help="Filter fixes by tag")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output report as JSON")
    parser.add_argument("--brief", dest="brief_only", action="store_true", help="Print only the brief-ready markdown section")

    args = parser.parse_args()
    return analyze_fixes(
        project_path=args.project_path,
        repo=args.repo,
        tag=args.tag,
        as_json=args.as_json,
        brief_only=args.brief_only,
    )


if __name__ == "__main__":
    sys.exit(main())
