#!/usr/bin/env python3
"""Self-test for sync_plugin.py - the plugin's own installer/updater.

Run it after changing the sync logic:

    python skills/argon-coding-bridge/scripts/test_sync_plugin.py

Every case works in a throwaway directory - it never touches the real plugin
roots, the real repo or the live account pool.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
SYNC = HERE / "sync_plugin.py"
sys.path.insert(0, str(HERE))
import sync_plugin as sp                                       # noqa: E402

RESULTS = []


def case(name: str, payload: dict):
    RESULTS.append((name, payload))
    print(f"  [PASS] {name}: {json.dumps(payload, ensure_ascii=False)}")


def fail(name: str, payload: dict):
    payload = dict(payload)
    payload["__fail__"] = True
    RESULTS.append((name, payload))
    print(f"  [FAIL] {name}: {json.dumps(payload, ensure_ascii=False)}")


def check(name: str, ok: bool, payload: dict) -> bool:
    (case if ok else fail)(name, payload)
    return ok


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root)] + list(args),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 and proc.stderr:
        print(f"    [git {root.name} {args[0]}] {proc.stderr.strip()[:200]}")
    return (proc.stdout or "").strip()


def make_repo(base: Path, name: str = "repo") -> Path:
    """A tiny git repo that looks like the plugin."""
    repo = base / name
    (repo / "skills" / "argon-coding-bridge" / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "skills" / "argon-coding-bridge" / "config").mkdir(parents=True, exist_ok=True)
    (repo / "skills" / "argon-coding-bridge" / "references").mkdir(parents=True, exist_ok=True)
    (repo / "skills" / "argon-coding-bridge" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (repo / "skills" / "argon-coding-bridge" / "scripts" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (repo / "skills" / "argon-coding-bridge" / "references" / "r.md").write_text("ref\n", encoding="utf-8")
    (repo / "skills" / "argon-coding-bridge" / "config" / "accounts_pool.json").write_text('{"live":true}\n', encoding="utf-8")
    (repo / "README.md").write_text("readme\n", encoding="utf-8")
    (repo / "plugin.json").write_text('{"name":"argon-coding-bridge","version":"1.0.0"}\n', encoding="utf-8")
    (repo / "API_INDEX.md").write_text("# api index\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.zip\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo


def make_minimax_root(base: Path, name: str = "minimax") -> Path:
    root = base / name
    (root / ".minimax-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".minimax-plugin" / "plugin.json").write_text('{"name":"argon-coding-bridge","version":"9.9.9"}\n', encoding="utf-8")
    (root / "skills" / "argon-coding-bridge").mkdir(parents=True, exist_ok=True)
    return root


def main() -> int:
    # never let the test touch the real source record
    os.environ["ARGON_PLUGIN_SOURCE_RECORD"] = str(
        Path(tempfile.gettempdir()) / "argon_sync_selftest" / "source_record.json")
    import sync_plugin as sp0
    sp0.SOURCE_RECORD = Path(os.environ["ARGON_PLUGIN_SOURCE_RECORD"])

    base = Path(tempfile.gettempdir()) / "argon_sync_selftest"
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)

    repo = make_repo(base)
    target = make_minimax_root(base)

    print("=== 1. the package is copied into a plain root ===")
    plain = base / "plain"
    (plain / "skills" / "argon-coding-bridge").mkdir(parents=True, exist_ok=True)
    rep: dict = {}
    files = sp.package_files(repo)
    sp.copy_package(repo, plain, files, False, rep)
    copied = rep.get("copied", [])
    check("the skill file arrived", (plain / "skills" / "argon-coding-bridge" / "SKILL.md").is_file(),
          {"copied": len(copied)})
    check("the README arrived", (plain / "README.md").is_file(), {})
    check("the API index arrived", (plain / "API_INDEX.md").is_file(), {})
    check("the manifest arrived", (plain / "plugin.json").is_file(), {})

    print()
    print("=== 2. the live account pool is NEVER copied ===")
    live = plain / "skills" / "argon-coding-bridge" / "config" / "accounts_pool.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text('{"live":true,"password":"keep-me"}\n', encoding="utf-8")
    rep = {}
    sp.copy_package(repo, plain, files, False, rep)
    check("the live pool survived the copy", '"keep-me"' in live.read_text(encoding="utf-8"),
          {"content": live.read_text(encoding="utf-8")[:60]})

    print()
    print("=== 3. a MiniMax root never gets the generic plugin.json ===")
    rep = {}
    sp.copy_package(repo, target, files, False, rep)
    check("plugin.json was NOT created in the MiniMax root",
          not (target / "plugin.json").is_file(), {})
    check("the MiniMax manifest was left alone",
          json.loads((target / ".minimax-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"] == "9.9.9", {})
    check("the skill files DID arrive",
          (target / "skills" / "argon-coding-bridge" / "SKILL.md").is_file(), {})

    print()
    print("=== 4. --dry-run changes nothing ===")
    before = sorted(p.name for p in target.rglob("*") if p.is_file())
    proc = subprocess.run([sys.executable, str(SYNC), "--repo", str(repo),
                           "--target", str(target), "--dry-run", "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    after = sorted(p.name for p in target.rglob("*") if p.is_file())
    check("the dry run reported no drift", proc.returncode == 0, {"exit": proc.returncode})
    check("the dry run wrote nothing", before == after, {})

    print()
    print("=== 5. --check finds drift and changes nothing ===")
    (target / "skills" / "argon-coding-bridge" / "SKILL.md").write_text("# drifted\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(SYNC), "--repo", str(repo),
                           "--target", str(target), "--check", "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("the check reported the drift (exit 1)", proc.returncode == 1, {"exit": proc.returncode})
    check("the check changed nothing", "# drifted" in (target / "skills" / "argon-coding-bridge" / "SKILL.md").read_text(encoding="utf-8"), {})

    print()
    print("=== 6. a real sync repairs it, and it is idempotent ===")
    proc = subprocess.run([sys.executable, str(SYNC), "--repo", str(repo),
                           "--target", str(target), "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("the sync finished cleanly", proc.returncode == 0, {"exit": proc.returncode})
    check("the drifted file is back to the repo's content",
          (target / "skills" / "argon-coding-bridge" / "SKILL.md").read_text(encoding="utf-8") == "# skill\n", {})
    proc2 = subprocess.run([sys.executable, str(SYNC), "--repo", str(repo),
                            "--target", str(target), "--json", "--quiet"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    data = json.loads(proc2.stdout) if proc2.stdout.strip().startswith("{") else {}
    second = data.get("targets", {}).get(str(target), {}).get("copied", [])
    check("a second run copies nothing (idempotent)", not second, {"copied": second})

    print()
    print("=== 7. a root that is its own git clone is aligned with origin/main ===")
    import time
    clone = base / f"clone-{int(time.time() * 1000)}"
    git(base, "clone", "-q", str(repo), str(clone))
    # a local-only commit and a file the working tree never received
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "config", "user.name", "test")
    (clone / "skills" / "argon-coding-bridge" / "scripts" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "local only")
    local_head = git(clone, "rev-parse", "--short", "HEAD")
    (clone / ".github").mkdir(exist_ok=True)
    (clone / ".github" / "workflow.yml").write_text("on: push\n", encoding="utf-8")
    git(clone, "rm", "-q", "-r", "--cached", ".github")
    rep = {}
    sp.align_git(clone, False, rep)
    head_after = git(clone, "rev-parse", "--short", "HEAD")
    check("the branch moved to origin/main", head_after != local_head, {"was": local_head, "now": head_after})
    check("the old commit is still reachable in a backup branch",
          "backup/pre-sync" in git(clone, "branch", "--list"), {"branches": git(clone, "branch", "--list")})
    check("the working tree was not wiped",
          (clone / "skills" / "argon-coding-bridge" / "SKILL.md").is_file(), {})

    print()
    print("=== 8. --install creates a root that does not exist yet ===")
    import time as _t
    fresh = base / f"fresh-{int(_t.time() * 1000)}"
    proc = subprocess.run([sys.executable, str(SYNC), "--repo", str(repo),
                           "--target", str(fresh), "--install", "--quiet"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("the install finished cleanly", proc.returncode == 0, {"exit": proc.returncode})
    check("the new root was created", fresh.is_dir(), {})
    check("the package landed in it",
          (fresh / "skills" / "argon-coding-bridge" / "SKILL.md").is_file(), {})

    print()
    print("=== 9. the source repo is found automatically ===")
    found = sp.detect_source_repo(None, start=repo)
    check("the repo was detected", found is not None and Path(found).resolve() == repo.resolve(), {"found": str(found)})

    print()
    passed = sum(1 for name, payload in RESULTS if not payload.get("__fail__"))
    failed = [name for name, payload in RESULTS if payload.get("__fail__")]
    ok = not failed
    print("-" * 70)
    print(f"  cases: {passed}/{len(RESULTS)}")
    print(f"  Overall status : {'PASS' if ok else 'FAIL'}")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
