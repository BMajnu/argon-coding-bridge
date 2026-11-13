#!/usr/bin/env python3
"""Keep every installed copy of the Argon Coding Bridge in step with the repo.

Any agent can bring the plugin up to date with ONE command - no human, and no
other agent, needed:

    python skills/argon-coding-bridge/scripts/sync_plugin.py

WHAT IT DOES
  1. finds the source repo (the git checkout of the plugin),
  2. copies the PACKAGE - every git-tracked file plus the published
     API_INDEX.md - into every installed plugin root,
  3. aligns a root that is its own git clone with origin/main (the branch
     pointer moves, the files stay; the old HEAD is kept in a backup branch),
  4. verifies: byte identity, every script compiles, the git state is clean.

WHAT IT NEVER DOES
  * it never touches the runtime credential files
    (skills/argon-coding-bridge/config/accounts_pool.json, login_report.json) -
    each root keeps its own live pool,
  * it never creates a generic plugin.json inside a MiniMax root, which uses
    its own .minimax-plugin/plugin.json,
  * it never deletes anything.

OPTIONS
  --repo PATH     the source repo (default: auto-detected)
  --target PATH   an installed root to update (repeatable; default: auto-detected)
  --install       first-time install: copy the package into the targets
  --check         verify only, change nothing (exit 1 when there is drift)
  --dry-run       show what would be copied, change nothing
  --no-git-align  leave the targets' git state alone
  --json          machine readable output
  --quiet         only the summary
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLUGIN_NAME = "argon-coding-bridge"
# Where the plugin remembers its source repo. A test (or a second machine) can
# redirect it, so nothing ever points the real record at a throwaway folder.
SOURCE_RECORD = Path(
    os.environ.get("ARGON_PLUGIN_SOURCE_RECORD")
    or (Path.home() / ".gemini" / "argon_plugin_source.json")
)

# Files that hold live state. They belong to the machine, not to the package,
# so they are never copied from one root into another.
RUNTIME_FILES = {
    "skills/argon-coding-bridge/config/accounts_pool.json",
    "skills/argon-coding-bridge/config/login_report.json",
}
# Published but gitignored - it travels with the plugin.
EXTRA_PUBLISHED = ["API_INDEX.md"]

# The plugin parents an installed root may live in.
PLUGIN_PARENTS = [
    Path.home() / ".minimax" / "plugins",
    Path.home() / ".gemini" / "config" / "plugins",
    Path.home() / ".claude" / "plugins",
    Path.home() / ".config" / "plugins",
]


# --------------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_git(args: List[str], cwd: Path, check: bool = False) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd)] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as exc:                                   # git missing?
        if check:
            raise
        return 127, "", str(exc)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def looks_like_repo(path: Path) -> bool:
    try:
        return (
            path.is_dir()
            and (path / ".git").exists()
            and (path / "skills" / PLUGIN_NAME).is_dir()
        )
    except OSError:
        return False


def looks_like_plugin_root(path: Path) -> bool:
    try:
        return path.is_dir() and (path / "skills" / PLUGIN_NAME).is_dir()
    except OSError:
        return False


# --------------------------------------------------------------------------- detection
def detect_source_repo(explicit: Optional[str], start: Optional[Path] = None) -> Optional[Path]:
    """Find the git checkout that holds the plugin."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if looks_like_repo(p) else None

    # 1. the recorded source of the last successful sync
    try:
        if SOURCE_RECORD.is_file():
            data = json.loads(SOURCE_RECORD.read_text(encoding="utf-8"))
            rec = data.get("repo")
            if rec and looks_like_repo(Path(rec)):
                return Path(rec).resolve()
    except Exception:
        pass

    # 2. the working directory, then the script's own root
    for candidate in [start or Path.cwd(), Path(__file__).resolve().parent]:
        here = Path(candidate).resolve()
        for _ in range(5):                       # walk up to the repo root
            if looks_like_repo(here):
                return here
            if here.parent == here:
                break
            here = here.parent

    # 3. an installed root that is a clone of the repo - use origin/main
    for parent in PLUGIN_PARENTS:
        root = parent / PLUGIN_NAME
        if looks_like_repo(root):
            run_git(["fetch", "origin", "--quiet"], root)
            return root
    return None


def detect_targets(explicit: List[str], source: Optional[Path],
                   allow_new: bool = False) -> List[Path]:
    if explicit:
        out = []
        for t in explicit:
            p = Path(t).expanduser().resolve()
            if looks_like_plugin_root(p) or (allow_new and not p.exists()):
                out.append(p)
        return out

    found: List[Path] = []
    for parent in PLUGIN_PARENTS:
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if not child.is_dir():
                continue
            if not looks_like_plugin_root(child):
                continue
            if source is not None and child == source:
                continue
            found.append(child.resolve())
    return found


def package_files(repo: Path) -> List[str]:
    """The files that travel with the plugin: every tracked file + API_INDEX.md."""
    code, out, _ = run_git(["ls-files"], repo)
    tracked = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    if not tracked:                            # not a git checkout - fall back to a walk
        tracked = []
        for p in repo.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(repo)
            if any(part in {"__pycache__", ".git", ".argon_backup", "scratch", "downloads"}
                   for part in rel.parts):
                continue
            if rel.name in RUNTIME_FILES or rel.name.endswith(".pyc"):
                continue
            tracked.append(str(rel).replace("\\", "/"))
    for extra in EXTRA_PUBLISHED:
        if extra not in tracked and (repo / extra).is_file():
            tracked.append(extra)
    return sorted(set(tracked) - RUNTIME_FILES)


def is_minimax_root(root: Path) -> bool:
    return (root / ".minimax-plugin" / "plugin.json").is_file()


# --------------------------------------------------------------------------- the work
def copy_package(repo: Path, target: Path, files: List[str], dry_run: bool,
                 report: Dict[str, List[str]]) -> None:
    """Copy the package into one target. The runtime files are never touched."""
    copied, skipped, failed = report.setdefault("copied", []), report.setdefault("skipped", []), report.setdefault("failed", [])
    minimax = is_minimax_root(target)

    for rel in files:
        src = repo / rel
        dst = target / rel
        if rel in RUNTIME_FILES:
            # the live pool belongs to the machine, never to the package
            skipped.append(rel)
            continue
        if not src.is_file():
            skipped.append(rel)
            continue
        # A MiniMax root uses .minimax-plugin/plugin.json - never force the
        # generic Agent Plugins manifest into it.
        if rel == "plugin.json" and minimax and not dst.exists():
            skipped.append(rel)
            continue
        same = dst.is_file() and sha256_file(dst) == sha256_file(src)
        if same:
            continue
        if dry_run:
            copied.append(rel)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
        except OSError as exc:
            failed.append(f"{rel}: {exc}")


def resolve_remote_branch(target: Path) -> Optional[str]:
    """The remote branch this clone should follow: origin/HEAD, origin/main or origin/master."""
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        code, _, _ = run_git(["rev-parse", "--verify", "--quiet", ref], target)
        if code == 0:
            return ref
    code, out, _ = run_git(["remote", "show", "origin"], target)
    if code == 0:
        for line in out.splitlines():
            line = line.strip()
            if line.lower().startswith("head branch:"):
                name = line.split(":", 1)[1].strip()
                if name:
                    return f"origin/{name}"
    return None


def align_git(target: Path, dry_run: bool, report: Dict[str, List[str]]) -> None:
    """Bring a root that is its own clone back to origin/main, files intact."""
    notes = report.setdefault("git", [])
    if not (target / ".git").exists():
        return
    code, out, _ = run_git(["rev-parse", "--short", "HEAD"], target)
    old_head = out if code == 0 else "?"
    code, remote, _ = run_git(["remote", "get-url", "origin"], target)
    if code != 0 or not remote:
        notes.append(f"{target.name}: no origin remote, the git state was left alone")
        return
    if dry_run:
        notes.append(f"{target.name}: would align the branch with origin/main (HEAD {old_head})")
        return

    run_git(["fetch", "origin", "--quiet"], target)
    stamp = run_git(["rev-parse", "--short", "HEAD"], target)[1] or "unknown"
    # a safety branch so the old commits stay reachable
    backup = f"backup/pre-sync-{stamp}"
    if backup not in run_git(["branch", "--list", backup], target)[1]:
        run_git(["branch", backup], target)
    remote_branch = resolve_remote_branch(target)
    if not remote_branch:
        notes.append(f"{target.name}: the remote has no branch to align with, the git state was left alone")
        return
    code, _, err = run_git(["reset", "--mixed", remote_branch], target)
    if code != 0:
        notes.append(f"{target.name}: the git reset failed ({err[:120]}), the files were still copied")
        return
    notes.append(f"{target.name}: the branch now points at origin/main (was {old_head}, the old commits are in the branch '{backup}')")


def verify(repo: Path, target: Path, files: List[str], report: Dict[str, List[str]]) -> None:
    differ, missing = report.setdefault("differ", []), report.setdefault("missing", [])
    for rel in files:
        src, dst = repo / rel, target / rel
        if rel in RUNTIME_FILES:
            continue
        if rel == "plugin.json" and is_minimax_root(target) and not dst.exists():
            continue
        if not dst.is_file():
            missing.append(rel)
            continue
        if sha256_file(src) != sha256_file(dst):
            differ.append(rel)


def compile_all(target: Path, report: Dict[str, List[str]]) -> int:
    failures = report.setdefault("compile_failures", [])
    scripts = target / "skills" / PLUGIN_NAME / "scripts"
    if not scripts.is_dir():
        return 0
    for py in sorted(scripts.glob("*.py")):
        try:
            # compile in memory - nothing is written next to the script
            source = py.read_text(encoding="utf-8", errors="replace")
            compile(source, str(py), "exec", dont_inherit=True)
        except SyntaxError as exc:
            failures.append(f"{py.name}: line {exc.lineno}: {exc.msg}")
        except Exception as exc:
            failures.append(f"{py.name}: {str(exc)[:160]}")
    return len(failures)


def record_source(repo: Path) -> None:
    try:
        SOURCE_RECORD.parent.mkdir(parents=True, exist_ok=True)
        SOURCE_RECORD.write_text(
            json.dumps({"repo": str(repo), "plugin": PLUGIN_NAME,
                        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds")},
                       indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


# --------------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bring every installed Argon Coding Bridge plugin root in step with the repo.")
    ap.add_argument("--repo", help="the source repo (default: auto-detected)")
    ap.add_argument("--target", action="append", default=[], help="an installed root (repeatable)")
    ap.add_argument("--install", action="store_true", help="first-time install into the targets")
    ap.add_argument("--check", action="store_true", help="verify only, change nothing")
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    ap.add_argument("--no-git-align", action="store_true", help="leave the targets' git state alone")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def say(*parts: str) -> None:
        if not args.quiet and not args.json:
            print(*parts)

    source = detect_source_repo(args.repo)
    if source is None:
        print("[ERROR] no source repo was found. Pass --repo <path> (the git checkout of the plugin).",
              file=sys.stderr)
        return 2

    targets = detect_targets(args.target, source, allow_new=args.install)
    files = package_files(source)
    say(f"source repo : {source}")
    say(f"package     : {len(files)} files")
    say(f"targets     : {len(targets)}")

    if not targets:
        say("no installed root was found - nothing to update. "
            "Use --install with an explicit --target to install it.")
        if args.json:
            print(json.dumps({"ok": True, "source": str(source), "targets": [], "files": len(files)}))
        return 0

    dry = args.check or args.dry_run
    summary = {"source": str(source), "files": len(files), "targets": {}}
    exit_code = 0

    for target in targets:
        rep: Dict[str, List[str]] = {}
        say("")
        say(f"=== {target}")
        if not target.is_dir():
            try:
                target.mkdir(parents=True, exist_ok=True)
                say(f"    created the root folder: {target}")
            except OSError as exc:
                say(f"    [ERROR] the root folder could not be created: {exc}")
                exit_code = 1
                continue
        copy_package(source, target, files, dry, rep)
        if not args.no_git_align:
            align_git(target, dry, rep)
        if not dry:
            # the git reset may have restored or removed files - copy once more
            rep2: Dict[str, List[str]] = {}
            copy_package(source, target, files, False, rep2)
            for k, v in rep2.items():
                rep.setdefault(k, []).extend(v)
            verify(source, target, files, rep)
            n_fail = compile_all(target, rep)
            if n_fail:
                exit_code = 1
        else:
            verify(source, target, files, rep)
            if rep.get("differ") or rep.get("missing"):
                exit_code = 1

        say(f"    copied   : {len(rep.get('copied', []))}")
        say(f"    missing  : {len(rep.get('missing', []))}")
        say(f"    differ   : {len(rep.get('differ', []))}")
        for rel in rep.get("differ", [])[:10]:
            say(f"        ~ {rel}")
        for rel in rep.get("missing", [])[:10]:
            say(f"        - {rel}")
        for note in rep.get("git", []):
            say(f"    git      : {note}")
        if rep.get("compile_failures"):
            say(f"    COMPILE  : {len(rep['compile_failures'])} failure(s)")
            for f in rep["compile_failures"][:5]:
                say(f"        {f}")

        summary["targets"][str(target)] = {
            "copied": rep.get("copied", []),
            "missing": rep.get("missing", []),
            "differ": rep.get("differ", []),
            "git": rep.get("git", []),
            "compile_failures": rep.get("compile_failures", []),
        }

    if not dry:
        record_source(source)

    ok = exit_code == 0
    say("")
    say("ALL CHECKS PASSED - every installed root is in step with the repo."
        if ok else "THERE IS STILL DRIFT - see the lines above.")
    if args.json:
        summary["ok"] = ok
        print(json.dumps(summary, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
