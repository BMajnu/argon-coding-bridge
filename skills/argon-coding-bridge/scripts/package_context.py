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

import re

# Unambiguous dependency, build-output, and cache directories only.
# Ambiguous source directory names (packages, bin, out, build, pkg, obj, logs)
# must NEVER be dropped globally because they frequently contain real source code.
DEFAULT_EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    ".next",
    "venv",
    ".venv",
    "__pycache__",
    "site-packages",
    ".dart_tool",
    ".pub-cache",
    "Pods",
    "DerivedData",
    ".gradle",
    "target",
    "dist",
    "coverage",
    ".cache",
}
_DEFAULT_EXCLUDED_DIRS_LOWER = {d.lower() for d in DEFAULT_EXCLUDED_DIRS}

# Directories that can hold either source code or build output. Inside these
# directories, files with a recognized source extension are always included.
AMBIGUOUS_DIRS = {
    "packages",
    "bin",
    "out",
    "build",
    "pkg",
    "obj",
    "logs",
    ".svn",
    ".hg",
    ".idea",
    ".vscode",
    ".nuxt",
    ".turbo",
    ".svelte-kit",
    ".output",
    "env",
    ".m2",
    ".expo",
}

SOURCE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".kt",
    ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".dart", ".rb", ".php",
    ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".md", ".sql",
    ".sh", ".ps1", ".bat", ".cmd", ".html", ".css", ".scss", ".less",
    ".xml", ".proto", ".graphql", ".gql",
}

DEFAULT_EXCLUDED_EXTS = {
    ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".pdf", ".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav",
    ".iso", ".bin", ".pyc", ".class", ".apk", ".aab", ".ipa",
    ".sqlite", ".sqlite3", ".db", ".o", ".obj", ".pdb", ".lib", ".a"
}

NO_GUESSING_DIRECTIVE = (
    "If something you need is not in this archive - a file, an API endpoint, a\n"
    "function, a configuration - DO NOT GUESS and DO NOT INVENT it. Say exactly\n"
    "which file or symbol is missing and stop there. A wrong guess is much worse\n"
    "than an incomplete answer."
)


def is_secret(filename: str) -> bool:
    """Return True only when filename looks like genuine key or credential material.

    Do NOT drop a .json file merely because the word 'secret' or 'password'
    appears somewhere in its name; when in doubt, include the file.
    """
    fn = Path(filename).name.lower()
    if fn.startswith(".env"):
        return True
    if any(fn.endswith(ext) for ext in (".pem", ".key", ".keystore", ".pfx", ".p12")):
        return True
    if any(k in fn for k in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
        return True
    if fn.endswith(".json"):
        if re.search(r"accounts?[_-]?pool", fn):
            return True
        if fn in (
            "credentials.json",
            "credential.json",
            "secrets.json",
            "secret.json",
            "passwords.json",
            "password.json",
            "client_secret.json",
            "client_secrets.json",
            "service_account.json",
            "service-account.json",
        ):
            return True
        if re.match(
            r"^(?:google_?|gcp_?|aws_?|firebase_?|oauth_?|service[_-]account[_-]?)?credentials[_.-].*\.json$",
            fn,
        ):
            return True
        if re.match(r"^client_secrets?[_.-].*\.json$", fn):
            return True
    return False


def is_dependency_or_build_path(rel_path: Path) -> bool:
    parts = {p.lower() for p in rel_path.parts[:-1]} if len(rel_path.parts) > 1 else set()
    all_parts = {p.lower() for p in rel_path.parts}
    return bool((parts | all_parts) & _DEFAULT_EXCLUDED_DIRS_LOWER)


def is_in_ambiguous_dir(rel_path: Path) -> bool:
    dir_parts = {p.lower() for p in rel_path.parts[:-1]}
    return bool(dir_parts & AMBIGUOUS_DIRS)


def is_source_file(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext in SOURCE_EXTENSIONS:
        return True
    return path.name in ("Dockerfile", "Makefile", "LICENSE", "README", ".gitignore")


# Generated artefacts of the Argon workflow itself. They must NEVER be packed:
# an apply_changes.ps1 left in the project would be handed straight back to
# Argon as "the deliverable" and the round trip would feed on itself.
GENERATED_ARTEFACTS = {
    "apply_changes.ps1",
    "argon_context.zip",
    "argon_package.zip",
    "argon_task_prompt.md",
    "argon_response.txt",
    "argon_response.md",
    "task_brief.md",
    "accounts_pool.json",
}


def is_generated_artefact(rel_path: Path) -> bool:
    name = rel_path.name.lower()
    if name in GENERATED_ARTEFACTS:
        return True
    if name.startswith("apply_changes") and (name.endswith(".ps1") or ".ps1.bak-" in name):
        return True
    if re.search(r"accounts?[_-]?pool", name) and name.endswith(".json"):
        return True
    norm_rel = rel_path.as_posix().lower().lstrip("/")
    if norm_rel == "skills/argon-coding-bridge/config/accounts_pool.json" or norm_rel.endswith(
        "/config/accounts_pool.json"
    ):
        return True
    parts = {p.lower() for p in rel_path.parts}
    return bool(parts & {".argon_backup", "downloads", "__pycache__"})


def get_git_files(repo_path: str):
    try:
        res = subprocess.run(
            ["git", "-C", repo_path, "ls-files", "-c", "-o", "--exclude-standard"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=30,
        )
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        return None


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes:,} bytes ({num_bytes / (1024 * 1024):.2f} MB)"
    if num_bytes >= 1024:
        return f"{num_bytes:,} bytes ({num_bytes / 1024:.2f} KB)"
    return f"{num_bytes:,} bytes"


def _compose_task_brief(
    proj_root: Path,
     selected_files: list[Path],
    excluded_files: list[tuple[str, str]],
    base_brief: str,
) -> str:
    h2 = "#" * 2
    h3 = "#" * 3
    bullet = chr(45)
    tree_lines = [
        f"{h2} COMPLETE PACKED FILE TREE ({len(selected_files)} files - no truncation)",
        "",
        "Every file packed into this archive is listed below with its exact size:",
        "",
    ]
    for rel in sorted(selected_files, key=lambda p: p.as_posix().lower()):
        full = proj_root / rel
        try:
            sz = full.stat().st_size
        except OSError:
            sz = 0
        tree_lines.append(f"{bullet} {rel.as_posix():<56} ({_format_size(sz)})")

    excl_lines = [
        "",
        f"{h2} FILES THAT ARE NOT IN THIS ARCHIVE",
        "",
        "The following files exist in the project but were NOT packed. If you need one of",
        "them, say so - do not guess.",
        "",
    ]
    if excluded_files:
        for rel_str, reason in excluded_files:
            excl_lines.append(f"{bullet} {rel_str:<32} ({reason})")
    else:
        excl_lines.append(f"{bullet} (none - every eligible project file was packed)")

    no_guess_lines = [
        "",
        f"{h3} 6. DO NOT GUESS MISSING CONTEXT (CRITICAL)",
        NO_GUESSING_DIRECTIVE,
        "",
    ]
    nl = chr(10)
    sections = [
        base_brief.rstrip(),
        "",
        nl.join(no_guess_lines),
        nl.join(tree_lines),
        nl.join(excl_lines),
    ]
    return nl.join(sections) + nl


def package_context(
    project_path: str,
    output_zip: str,
    task_description: str = "",
    target_files=None,
    max_size_mb: int = 50,
):
    proj_root = Path(project_path).resolve()
    if not proj_root.exists():
        print(f"[Error] Project root does not exist: {proj_root}", file=sys.stderr)
        sys.exit(1)

    max_bytes = max_size_mb * 1024 * 1024
    selected_files: list[Path] = []
    seen_rel: set[str] = set()
    excluded_files: list[tuple[str, str]] = []

    def _record_excluded(rel: Path, reason: str) -> None:
        rel_posix = rel.as_posix()
        print(f"  [-] Excluded: {rel_posix:<36} ({reason})")
        excluded_files.append((rel_posix, reason))

    if target_files:
        print("[+] Packaging targeted files...")
        for pattern in target_files:
            for p in sorted(proj_root.rglob(pattern)):
                if not p.is_file():
                    continue
                rel = p.relative_to(proj_root)
                rel_key = rel.as_posix().lower()
                if rel_key in seen_rel or is_dependency_or_build_path(rel):
                    continue
                if is_secret(p.name):
                    _record_excluded(rel, "looks like key material")
                    continue
                if is_generated_artefact(rel):
                    _record_excluded(rel, "Argon workflow artefact")
                    continue
                if p.suffix.lower() in DEFAULT_EXCLUDED_EXTS:
                    _record_excluded(rel, f"binary/archive extension: {p.suffix.lower()}")
                    continue
                sz = p.stat().st_size
                if sz > max_bytes:
                    mb = max(1, int(round(sz / (1024 * 1024))))
                    _record_excluded(rel, f"oversized: {mb} MB")
                    continue
                seen_rel.add(rel_key)
                selected_files.append(rel)
    else:
        print("[+] Scanning project tree (preserving all source files and reporting exclusions)...")
        git_files = get_git_files(str(proj_root))
        git_tracked_set = {Path(gf).as_posix().lower() for gf in (git_files or [])}
        custom_ignores: list[str] = []
        git_ign = proj_root / ".gitignore"
        if git_ign.is_file():
            with open(git_ign, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("!"):
                        custom_ignores.append(line.lstrip("/"))

        for root, dirs, files in os.walk(proj_root):
            dirs[:] = sorted(
                d
                for d in dirs
                if d.lower() not in _DEFAULT_EXCLUDED_DIRS_LOWER
                and d.lower() not in {".argon_backup", "downloads", "__pycache__"}
            )
            for file in sorted(files):
                p = Path(root) / file
                if not p.is_file():
                    continue
                rel = p.relative_to(proj_root)
                rel_key = rel.as_posix().lower()
                if rel_key in seen_rel or is_dependency_or_build_path(rel):
                    continue
                if is_secret(file):
                    _record_excluded(rel, "looks like key material")
                    continue
                if is_generated_artefact(rel):
                    _record_excluded(rel, "Argon workflow artefact")
                    continue
                if p.suffix.lower() in DEFAULT_EXCLUDED_EXTS:
                    _record_excluded(rel, f"binary/archive extension: {p.suffix.lower()}")
                    continue
                if is_in_ambiguous_dir(rel) and not is_source_file(p):
                    _record_excluded(rel, f"non-source file in ambiguous dir ({p.suffix.lower() or 'no ext'})")
                    continue
                # Never allow .gitignore to drop a real source file (such as a
                # generated API client); only apply .gitignore filtering to
                # non-source files that git did not list.
                if not is_source_file(p):
                    git_unlisted = git_files is not None and rel_key not in git_tracked_set
                    matched_ign = any(
                        fnmatch.fnmatch(rel.as_posix(), ign) or fnmatch.fnmatch(file, ign)
                        for ign in custom_ignores
                    )
                    if git_unlisted or matched_ign:
                        _record_excluded(rel, "excluded by .gitignore")
                        continue
                sz = p.stat().st_size
                if sz > max_bytes:
                    mb = max(1, int(round(sz / (1024 * 1024))))
                    _record_excluded(rel, f"oversized: {mb} MB")
                    continue
                seen_rel.add(rel_key)
                selected_files.append(rel)

    out_zip_path = Path(output_zip).resolve()
    print(f"[+] Total eligible codebase files: {len(selected_files)} (excluded: {len(excluded_files)})")
    print(f"[+] Creating clean zip archive: {out_zip_path}")

    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in selected_files:
            full = proj_root / rel
            zf.write(full, arcname=str(rel))

        brief_text = f"""# Task Brief for Gemini 4 Argon
**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Absolute Target Directory:** {proj_root}
**Project Root:** {proj_root.name}
**Included Files Count:** {len(selected_files)}

## User Goal / Problem Statement:
{task_description}

---

## CRITICAL INSTRUCTIONS FOR GEMINI 4 ARGON:

### 0. TARGET DIRECTORY (USE THESE EXACT PATHS)
The codebase you received in the archive is a copy of the real project, which lives at this absolute
directory on disk:

```
{proj_root}
```

Your `apply_changes.ps1` MUST pin that directory and resolve every path against it, so the script
applies the changes in one run no matter where it is executed from:

```powershell
$TargetRoot = '{proj_root}'
```

- Every file the script reads or writes is resolved with `Join-Path $TargetRoot '<relative path>'`.
- Never depend on the current working directory, and never guess or search for another directory.

### 1. DELIVER THE RESULT AS A FILE, NOT AS A CHAT CODE BLOCK
- Write the complete patch to a file named `apply_changes.ps1` and **attach it to your reply** so it
  can be downloaded with one click.
- Do NOT paste the body of the script into the chat. Keep the reply short: what changed, which files
  changed and why, the reference to the attached file, and how to verify.
- An inline code block is a fallback used only when file delivery is unavailable.

### 2. SURGICAL EDITS ONLY (ZERO UI REGRESSION):
   - Do NOT rewrite or restructure entire files unless creating a brand new file.
   - Do NOT change existing UI styling, component hierarchies, or CSS rules unless explicitly commanded above.
   - For edits in existing files, use safe substring replacements (`$content.Replace(...)`) with an error throw if not found.
   - Use single-quoted Here-Strings (`@' ... '@`) so JS/TS template literals (`${{var}}`) are preserved.
   - Write files using UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.

### 3. BACK UP BEFORE WRITING, AND APPLY EVERYTHING IN ONE RUN
   - Back up every file you overwrite into `.argon_backup/<yyyyMMdd_HHmmss>/<relative path>`.
   - Print one `[Patched] <relative path>` line per changed file and a final
     `[SUCCESS] N file(s) patched.` summary line.
   - Abort with `throw` before touching anything if a target block is not found, so a partial apply
     can never happen.

### 4. VERIFY YOUR WORK (REQUIRED)
   - After writing each file, re-read it from disk and assert that the intended new content is present.
   - Print `[Verified] <relative path>` for every file that matches and `[FAILED VERIFY] <relative path>`
     (exiting non-zero) if any file does not match.

### 5. RECORD WHAT YOU FOUND (REQUIRED)
   - Append a trailing comment block at the very end of `apply_changes.ps1` after the last write:
     `# --- NOTES: inconsistencies and suggestions found while reading the project ---`
     `# NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>`
     `# --- END NOTES ---`
"""
        brief_text = _compose_task_brief(proj_root, selected_files, excluded_files, brief_text)
        zf.writestr("TASK_BRIEF.md", brief_text)
        try:
            local_brief = proj_root / "TASK_BRIEF.md"
            if local_brief.is_file():
                local_brief.write_text(brief_text, encoding="utf-8")
        except Exception:
            pass

    size_kb = round(out_zip_path.stat().st_size / 1024, 2)
    print(f"[SUCCESS] Packaged {len(selected_files)} clean files ({size_kb} KB) to {out_zip_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Package codebase context for Gemini 4 Argon")
    parser.add_argument("--project-path", default=".", help="Root directory of the project")
    parser.add_argument("--output-zip", default="argon_context.zip", help="Output zip filename")
    parser.add_argument("--task", default="", help="Task description or goal")
    parser.add_argument("--files", nargs="*", default=None, help="Specific target files/patterns")
    parser.add_argument("--max-size-mb", type=int, default=50, help="Max file size in MB (default: 50)")
    args = parser.parse_args()

    package_context(args.project_path, args.output_zip, args.task, args.files, args.max_size_mb)
