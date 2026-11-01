#!/usr/bin/env python3
"""
Cross-platform Codebase Packager for Gemini 4 Argon
Strictly respects .gitignore, excludes dependencies/build packages (node_modules, venv, Pods, .gradle, etc.),
strips secrets (.env*), and enforces a realistic max code file size (default 15MB).
"""
import os
import sys
import fnmatch
import zipfile
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

# Complete blacklist for package managers, build outputs, and caches
DEFAULT_EXCLUDED_DIRS = {
    # VCS & IDEs
    ".git", ".svn", ".hg", ".idea", ".vscode",
    # JavaScript / Node / Bun / Deno
    "node_modules", ".next", ".nuxt", ".turbo", ".svelte-kit", ".output",
    ".npm", ".yarn", ".pnpm-store", "bower_components",
    # Python
    "venv", ".venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox", "site-packages",
    # Flutter / Dart
    ".dart_tool", ".pub-cache", ".pub",
    # Mobile / iOS / Android
    "Pods", "DerivedData", ".gradle", ".expo",
    # Java / Kotlin / Rust / Go / .NET
    "target", "bin", "obj", "packages", ".m2", "pkg",
    # Build outputs & coverage
    "dist", "build", "out", "coverage", ".cache", "logs"
}

DEFAULT_EXCLUDED_EXTS = {
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav",
    ".iso", ".bin", ".pyc", ".class", ".apk", ".aab", ".ipa",
    ".sqlite", ".sqlite3", ".db"
}

def is_secret(filename: str) -> bool:
    fn = filename.lower()
    if fn.startswith(".env"):
        return True
    if any(fn.endswith(ext) for ext in [".pem", ".key", ".keystore", ".pfx", ".p12"]):
        return True
    if "credentials" in fn and fn.endswith(".json"):
        return True
    if "id_rsa" in fn:
        return True
    return False

def is_dependency_or_build_path(rel_path: Path) -> bool:
    parts = set(p.lower() for p in rel_path.parts)
    return bool(parts & DEFAULT_EXCLUDED_DIRS)

def get_git_files(repo_path: str):
    try:
        res = subprocess.run(
            ["git", "-C", repo_path, "ls-files", "-c", "-o", "--exclude-standard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return None

def package_context(project_path: str, output_zip: str, task_description: str = "", target_files=None, max_size_mb: int = 15):
    proj_root = Path(project_path).resolve()
    if not proj_root.exists():
        print(f"[Error] Project root does not exist: {proj_root}", file=sys.stderr)
        sys.exit(1)

    max_bytes = max_size_mb * 1024 * 1024
    selected_files = []

    if target_files:
        print("[+] Packaging targeted files...")
        for pattern in target_files:
            for p in proj_root.rglob(pattern):
                if p.is_file() and not is_secret(p.name) and not is_dependency_or_build_path(p.relative_to(proj_root)):
                    selected_files.append(p.relative_to(proj_root))
    else:
        git_files = get_git_files(str(proj_root))
        if git_files is not None:
            print("[+] Using git ls-files respecting .gitignore and filtering package directories...")
            for rel_str in git_files:
                p = proj_root / rel_str
                rel = Path(rel_str)
                if is_dependency_or_build_path(rel):
                    continue
                if is_secret(p.name):
                    continue
                if p.suffix.lower() in DEFAULT_EXCLUDED_EXTS:
                    continue
                if p.is_file():
                    if p.stat().st_size > max_bytes:
                        print(f"  [-] Skipping oversized file (> {max_size_mb} MB): {rel}")
                        continue
                    selected_files.append(rel)
        else:
            print("[+] Scanning directory tree respecting dependency blacklist & .gitignore...")
            custom_ignores = []
            git_ign = proj_root / ".gitignore"
            if git_ign.is_file():
                with open(git_ign, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            custom_ignores.append(line.lstrip("/"))

            for root, dirs, files in os.walk(proj_root):
                dirs[:] = [d for d in dirs if d.lower() not in DEFAULT_EXCLUDED_DIRS]
                for file in files:
                    if is_secret(file):
                        continue
                    p = Path(root) / file
                    rel = p.relative_to(proj_root)
                    if is_dependency_or_build_path(rel):
                        continue
                    if p.suffix.lower() in DEFAULT_EXCLUDED_EXTS:
                        continue
                    if any(fnmatch.fnmatch(str(rel), ign) or fnmatch.fnmatch(file, ign) for ign in custom_ignores):
                        continue
                    if p.is_file():
                        if p.stat().st_size > max_bytes:
                            print(f"  [-] Skipping oversized file (> {max_size_mb} MB): {rel}")
                            continue
                        selected_files.append(rel)

    out_zip_path = Path(output_zip).resolve()
    print(f"[+] Total eligible codebase files: {len(selected_files)}")
    print(f"[+] Creating clean zip archive: {out_zip_path}")

    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in selected_files:
            full = proj_root / rel
            zf.write(full, arcname=str(rel))

        brief_text = f"""# Task Brief for Gemini 4 Argon
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Project Root:** {proj_root.name}
**Included Files Count:** {len(selected_files)}

## User Goal / Problem Statement:
{task_description}

---

## CRITICAL INSTRUCTIONS FOR GEMINI 4 ARGON:
1. **SURGICAL EDITS ONLY (ZERO UI REGRESSION):**
   - Do NOT rewrite or restructure entire files unless creating a brand new file.
   - Do NOT change existing UI styling, component hierarchies, or CSS rules unless explicitly commanded above.
   - For edits in existing files, use safe substring replacements (`$content.Replace(...)`) with an error throw if not found.
   - Use single-quoted Here-Strings (`@' ... '@`) so JS/TS template literals (`${{var}}`) are preserved.
   - Write files using UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.
"""
        zf.writestr("TASK_BRIEF.md", brief_text)

    size_kb = round(out_zip_path.stat().st_size / 1024, 2)
    print(f"[SUCCESS] Packaged {len(selected_files)} clean files ({size_kb} KB) to {out_zip_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Package codebase context for Gemini 4 Argon")
    parser.add_argument("--project-path", default=".", help="Root directory of the project")
    parser.add_argument("--output-zip", default="argon_context.zip", help="Output zip filename")
    parser.add_argument("--task", default="", help="Task description or goal")
    parser.add_argument("--files", nargs="*", default=None, help="Specific target files/patterns")
    parser.add_argument("--max-size-mb", type=int, default=15, help="Max file size in MB (default: 15)")
    args = parser.parse_args()

    package_context(args.project_path, args.output_zip, args.task, args.files, args.max_size_mb)
