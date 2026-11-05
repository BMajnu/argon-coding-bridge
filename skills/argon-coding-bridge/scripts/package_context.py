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

ROOT_CAUSE_DIRECTIVE = (
    "If something in this project is broken, do not change anything until you can\n"
    "state the root cause and the EVIDENCE for it (the exact file and line, the\n"
    "exact DOM node you measured, the exact value you read).\n"
    "\n"
    "- Every symptom must be traced to a concrete cause in THIS code base.\n"
    "- If you cannot find the cause in the archive, say exactly what you need -\n"
    "  do not invent a fix and do not guess a signature, a path or a value.\n"
    "- Put the root cause and its evidence in the NOTES block of your patch, under\n"
    "  the heading `## ROOT CAUSE`."
)


def is_secret(filename: str) -> bool:
    """Return True only when filename looks like genuine key or credential material.

    Do NOT drop a .json file merely because the word 'secret' or 'password'
    appears somewhere in its name; when in doubt, include the file.
    """
    fn = Path(filename).name.lower()
    if fn.startswith(".env") or fn in (".netrc", ".npmrc", ".pypirc"):
        return True
    if any(fn.endswith(ext) for ext in (".pem", ".key", ".keystore", ".pfx", ".p12", ".kdbx")):
        return True
    if any(k in fn for k in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
        return True
    if re.search(r"accounts?[_-]?pool", fn):
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
    "api_index.md",
    "accounts_pool.json",
    "argon_sessions.json",
    "argon_task_state.json",
}


def is_generated_artefact(rel_path: Path) -> bool:
    name = rel_path.name.lower()
    if name in GENERATED_ARTEFACTS:
        return True
    if name.startswith("apply_changes") and (name.endswith(".ps1") or ".ps1.bak-" in name):
        return True
    if re.search(r"accounts?[_-]?pool", name):
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


DECLARATION_EXTENSIONS = {
    ".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".java", ".kt", ".kts", ".cs", ".swift", ".dart", ".c", ".cpp", ".h",
    ".hpp", ".rb", ".php", ".ex", ".exs", ".ps1", ".sh",
}

EXACT_API_SECTION_TEXT = (
    "API_INDEX.md lists every public declaration with its file and line. Use THAT -\n"
    "never invent a function name, a parameter or a return type. If a symbol you need\n"
    "is not in API_INDEX.md and not in the archive, DO NOT GUESS: name the missing\n"
    "symbol and stop."
)

BYTE_EXACT_DIRECTIVE = (
    "Inside a patch, a Rust (or any) char or string literal for a newline is the TWO\n"
    "characters backslash and n (\\n) - never a real newline. The same applies to\n"
    "\\t, \\r, \\\\ and to every other escape. The patch must not contain a raw\n"
    "control character at all. A straight apostrophe ' must stay a straight\n"
    "apostrophe - never a typographic quote. Keep opening and closing braces\n"
    "balanced in every replacement block."
)


def detect_verification_command(proj_root: Path) -> str:
    """Inspect the project root and return the verification command, or '' when nothing matches."""
    import json as _json
    import shutil as _shutil

    proj_root = Path(proj_root).resolve()

    if (proj_root / "Cargo.toml").is_file():
        has_clippy = _shutil.which("cargo-clippy") is not None
        if not has_clippy and _shutil.which("cargo") is not None:
            try:
                cp = subprocess.run(
                    ["cargo", "clippy", "--version"],
                    cwd=str(proj_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=10,
                )
                has_clippy = cp.returncode == 0
            except Exception:
                has_clippy = False
        if has_clippy:
            return "cargo check && cargo clippy --all-targets"
        return "cargo check"

    pkg_json = proj_root / "package.json"
    if pkg_json.is_file():
        scripts = {}
        try:
            data = _json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
                scripts = data["scripts"]
        except Exception:
            scripts = {}
        lint_script = str(scripts.get("lint") or "").strip()
        cmds = ["npm run lint" if lint_script else "npx tsc --noEmit"]
        test_script = str(scripts.get("test") or "").strip()
        if test_script and "no test specified" not in test_script.lower():
            cmds.append("npm test")
        return " && ".join(cmds)

    py_markers = [proj_root / m for m in ("pyproject.toml", "setup.cfg", "tox.ini") if (proj_root / m).is_file()]
    if py_markers:
        combined = ""
        for pm in py_markers:
            try:
                combined += "\n" + pm.read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                pass
        has_ruff = (
            "[tool.ruff" in combined
            or "[ruff" in combined
            or (proj_root / "ruff.toml").is_file()
            or (proj_root / ".ruff.toml").is_file()
        )
        cmds = ["ruff check ." if has_ruff else "python -m compileall -q ."]
        has_pytest = (
            "pytest" in combined
            or (proj_root / "pytest.ini").is_file()
            or (proj_root / "conftest.py").is_file()
        )
        if has_pytest:
            cmds.append("pytest -q")
        return " && ".join(cmds)

    if (proj_root / "go.mod").is_file():
        return "go build ./... && go vet ./..."

    if (proj_root / "pom.xml").is_file():
        return "mvn -q -DskipTests compile"

    gradle_kts = proj_root / "build.gradle.kts"
    gradle_groovy = proj_root / "build.gradle"
    if gradle_kts.is_file() or gradle_groovy.is_file():
        g_text = ""
        for gf in (gradle_kts, gradle_groovy):
            if gf.is_file():
                try:
                    g_text += gf.read_text(encoding="utf-8", errors="ignore").lower()
                except Exception:
                    pass
        is_kotlin = gradle_kts.is_file() or ("kotlin" in g_text) or (proj_root / "src" / "main" / "kotlin").is_dir()
        return "./gradlew compileKotlin" if is_kotlin else "./gradlew compileJava"

    if list(proj_root.glob("*.sln")) or list(proj_root.glob("*.csproj")):
        return "dotnet build"

    makefile = proj_root / "Makefile"
    if makefile.is_file():
        try:
            mk_text = makefile.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            mk_text = ""
        targets = set(re.findall(r"(?m)^([A-Za-z0-9_.-]+)\s*:(?!=)", mk_text))
        cmds = ["make"]
        if "test" in targets:
            cmds.append("make test")
        return " && ".join(cmds)

    if (proj_root / "mix.exs").is_file():
        return "mix compile --warnings-as-errors"

    gemfile = proj_root / "Gemfile"
    if gemfile.is_file():
        try:
            gem_text = gemfile.read_text(encoding="utf-8", errors="ignore").lower()
        except Exception:
            gem_text = ""
        if "rubocop" in gem_text or (proj_root / ".rubocop.yml").is_file():
            return "bundle exec rubocop"

    return ""


def _match_declaration_line(ext: str, raw_line: str, stripped: str) -> tuple[bool, bool]:
    """Return (matched, join_multiline_parens) for a source line."""
    if not stripped:
        return False, False
    if stripped.startswith(("//", "/*", "*", "<!--")):
        return False, False
    if ext in (".py", ".ps1", ".sh", ".rb", ".ex", ".exs") and stripped.startswith("#"):
        return False, False

    if ext == ".rs":
        is_fn = bool(
            re.match(
                r"^(?:pub(?:\s*\([^)]*\))?\s+)?(?:(?:async|unsafe|const|default|extern(?:\s+\"[^\"]*\")?)\s+)*fn\s+[A-Za-z_]\w*",
                stripped,
            )
        )
        if is_fn:
            return True, True
        is_other = bool(
            re.match(
                r"^(?:pub(?:\s*\([^)]*\))?\s+)?(?:(?:async|unsafe|const|default)\s+)*"
                r"(?:struct|enum|trait|type|const|static|mod|macro_rules!)\s+[A-Za-z_]\w*|"
                r"^impl(?:\s*<[^>]*>)?\s+[A-Za-z_:<>\w\s,'&]+",
                stripped,
            )
        )
        return is_other, False

    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"):
        is_fn = bool(
            re.match(
                r"^(?:export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\b|(?:async\s+)?function\s+[A-Za-z_$][\w$]*)",
                stripped,
            )
        )
        if is_fn:
            return True, True
        is_other = bool(
            re.match(
                r"^(?:export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const|let|var|class|interface|type|enum)\b|"
                r"export\s+default\b|"
                r"class\s+[A-Za-z_$][\w$]*|"
                r"(?:interface|type|enum)\s+[A-Za-z_$][\w$]*)",
                stripped,
            )
        )
        return is_other, False

    if ext == ".py":
        if re.match(r"^(?:async\s+def|def|class)\s+[A-Za-z_]\w*", stripped):
            return True, True
        if raw_line == raw_line.lstrip() and re.match(r"^[A-Z][A-Z0-9_]{2,}\s*(?::[^=\n]+)?=", stripped):
            return True, False
        return False, False

    if ext == ".go":
        if re.match(r"^func\s+(?:\([^)]+\)\s*)?[A-Za-z_]", stripped):
            return True, True
        if re.match(r"^(?:type|const|var)\s+[A-Za-z_(]", stripped):
            return True, False
        return False, False

    if ext in (".java", ".kt", ".kts", ".cs", ".swift", ".dart", ".c", ".cpp", ".h", ".hpp", ".php", ".rb", ".ex", ".exs"):
        m = bool(
            re.match(
                r"^(?:(?:public|protected|internal|private|static|final|abstract|open|suspend|override|"
                r"data|sealed|inline|extern|virtual|async|partial|readonly)\s+)*"
                r"(?:class|interface|enum|record|struct|object|trait|protocol|extension|fun|func|def|defp|"
                r"defmodule|val|var|void|[A-Za-z_][\w<>,.\[\]?]*\s+[A-Za-z_]\w*\s*\()",
                stripped,
            )
        )
        return m, m

    if ext == ".ps1":
        if re.match(r"^(?:function|filter)\s+[A-Za-z_][\w-]*|^param\s*\(", stripped, re.IGNORECASE):
            return True, True
        return False, False

    if ext == ".sh":
        m = bool(re.match(r"^(?:function\s+[A-Za-z_]\w*|[A-Za-z_]\w*\s*\(\s*\))", stripped))
        return m, False

    return False, False


def extract_declarations_for_file(full_path: Path, rel_path: Path) -> list[tuple[int, str]]:
    """Extract all declarations (1-based line number, complete signature) from a source file."""
    ext = rel_path.suffix.lower()
    if ext not in DECLARATION_EXTENSIONS:
        return []
    try:
        raw_text = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    lines = raw_text.splitlines()
    declarations: list[tuple[int, str]] = []
    total_lines = len(lines)
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        matched, join_parens = _match_declaration_line(ext, raw_line, stripped)
        if not matched:
            continue
        sig_parts = [stripped]
        if join_parens:
            open_parens = stripped.count("(") - stripped.count(")")
            lookahead = idx + 1
            while open_parens > 0 and lookahead < min(total_lines, idx + 20):
                nxt = lines[lookahead].strip()
                if nxt and not nxt.startswith("#") and not nxt.startswith("//"):
                    sig_parts.append(nxt)
                    open_parens += nxt.count("(") - nxt.count(")")
                lookahead += 1
        sig = re.sub(r"\s+", " ", " ".join(sig_parts)).strip()
        declarations.append((idx + 1, sig))
    return declarations


def build_api_index(proj_root: Path, selected_files: list[Path]) -> str:
    """Build the complete API_INDEX.md content for every source file in the archive."""
    h1 = "#"
    h2 = "#" * 2
    bullet = chr(45)
    nl = chr(10)
    per_file: list[tuple[str, list[tuple[int, str]]]] = []
    total_decls = 0
    scanned_files = 0

    for rel in sorted(selected_files, key=lambda p: p.as_posix().lower()):
        if rel.suffix.lower() not in DECLARATION_EXTENSIONS:
            continue
        scanned_files += 1
        full = proj_root / rel
        decls = extract_declarations_for_file(full, rel)
        total_decls += len(decls)
        per_file.append((rel.as_posix(), decls))

    out_lines = [
        f"{h1} API_INDEX.md - Complete Public & Source Declaration Index",
        f"Project Root: {proj_root.name}",
        f"Source Files Scanned: {scanned_files}",
        f"Total Declarations: {total_decls} (complete - no truncation)",
        "",
        EXACT_API_SECTION_TEXT,
        "",
    ]
    for rel_posix, decls in per_file:
        out_lines.append(f"{h2} {rel_posix} ({len(decls)} declaration(s))")
        if not decls:
            out_lines.append(f"{bullet} (no declarations matched)")
        else:
            for line_no, sig in decls:
                out_lines.append(f"{bullet} {rel_posix}:{line_no} -> {sig}")
        out_lines.append("")
    return nl.join(out_lines).rstrip() + nl


def _compose_task_brief(
    proj_root: Path,
    selected_files: list[Path],
    excluded_files: list[tuple[str, str]],
    base_brief: str,
) -> str:
    h2 = "#" * 2
    h3 = "#" * 3
    bullet = chr(45)
    nl = chr(10)

    detected_cmd = detect_verification_command(proj_root)
    py_rels = [
        r.as_posix() for r in sorted(selected_files, key=lambda p: p.as_posix().lower())
        if r.suffix.lower() == ".py"
    ]
    if detected_cmd:
        verify_lines = [
            f"Detected command: `{detected_cmd}`",
            "If automatic detection is not sure, name the exact command that verifies this",
            f"project under `{h2} VERIFICATION COMMAND` in your reply and notes so the bridge",
            "can run it directly.",
        ]
    elif py_rels:
        fallback_py = "python -m py_compile " + " ".join(py_rels)
        verify_lines = [
            "Detected root marker command: `(none - no root build marker matched)`",
            f"Archive verification command: `{fallback_py}`",
            "Argon: confirm or name the exact command that verifies this project under",
            f"`{h2} VERIFICATION COMMAND` in your reply and notes so the bridge can read it",
            "straight from the archive when automatic root detection is not sure.",
        ]
    else:
        verify_lines = [
            "Detected command: `(none - automatic detection did not match a project root build marker)`",
            "Argon: name the exact command that verifies this project under",
            f"`{h2} VERIFICATION COMMAND` in your reply and notes so the bridge can read it",
            "straight from the archive when automatic detection is not sure.",
        ]

    top_sections = nl.join([
        f"{h2} FIND THE ROOT CAUSE FIRST",
        "",
        ROOT_CAUSE_DIRECTIVE,
        "",
        f"{h2} THE EXACT API OF THIS PROJECT",
        "",
        EXACT_API_SECTION_TEXT,
        "",
        f"{h2} VERIFICATION COMMAND",
        "",
        *verify_lines,
    ])

    crit_marker = f"{h2} CRITICAL INSTRUCTIONS FOR GEMINI 4 ARGON:"
    if crit_marker in base_brief:
        base_brief = base_brief.replace(crit_marker, top_sections + nl + nl + crit_marker, 1)
    else:
        base_brief = base_brief.rstrip() + nl + nl + top_sections

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
        f"{h3} 6. DO NOT GUESS MISSING CONTEXT & KEEP PAYLOADS BYTE-EXACT (CRITICAL)",
        NO_GUESSING_DIRECTIVE,
        "",
        BYTE_EXACT_DIRECTIVE,
        "",
    ]
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
    out_zip_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip_path = out_zip_path.with_name(f".{out_zip_path.name}.{os.getpid()}.tmp")
    print(f"[+] Total eligible codebase files: {len(selected_files)} (excluded: {len(excluded_files)})")
    print(f"[+] Creating clean zip archive atomically via temp file: {out_zip_path}")

    try:
        if tmp_zip_path.exists():
            tmp_zip_path.unlink()
    except OSError:
        pass

    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
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
   - Append a trailing comment block at the very end of `apply_changes.ps1` after the last write.
     The NOTES block of every delivered patch MUST start with a `## ROOT CAUSE` section naming the
     root cause and its evidence (even when the change is trivial):
     `# --- NOTES: inconsistencies and suggestions found while reading the project ---`
     `# ## ROOT CAUSE`
     `# NOTE: <file>:<line> - <exact root cause, measured evidence, and why this change fixes it>`
     `# NOTE: <file> - <what is inconsistent, why it matters, and what you would do instead>`
     `# --- END NOTES ---`
"""
        api_index_text = build_api_index(proj_root, selected_files)
        zf.writestr("API_INDEX.md", api_index_text)
        brief_text = _compose_task_brief(proj_root, selected_files, excluded_files, brief_text)
        zf.writestr("TASK_BRIEF.md", brief_text)

    # D13.2: Write TASK_BRIEF.md atomically via a temporary file + os.replace
    try:
        local_brief = proj_root / "TASK_BRIEF.md"
        tmp_brief = local_brief.with_name(f".TASK_BRIEF.md.{os.getpid()}.tmp")
        tmp_brief.write_text(brief_text, encoding="utf-8")
        os.replace(str(tmp_brief), str(local_brief))
    except Exception:
        pass

    # D13.1: Verify temporary zip archive and move into place atomically (os.replace)
    with zipfile.ZipFile(tmp_zip_path, "r") as verify_zf:
        archive_members = verify_zf.namelist()
        if "TASK_BRIEF.md" not in archive_members:
            raise RuntimeError("Packaged archive is missing TASK_BRIEF.md")
    os.replace(str(tmp_zip_path), str(out_zip_path))

    size_bytes = out_zip_path.stat().st_size
    size_kb = round(size_bytes / 1024, 2)
    total_entries = len(archive_members)
    print(
        f"[SUCCESS] Packaged {len(selected_files)} clean files "
        f"({total_entries} total entries, {size_bytes} bytes / {size_kb} KB) to {out_zip_path}"
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Package codebase context for Gemini 4 Argon")
    parser.add_argument("--project-path", default=".", help="Root directory of the project")
    parser.add_argument("--output-zip", default="argon_context.zip", help="Output zip filename")
    parser.add_argument("--task", default="", help="Task description or goal")
    parser.add_argument("--files", nargs="*", default=None, help="Specific target files/patterns")
    parser.add_argument("--max-size-mb", type=int, default=50, help="Max file size in MB (default: 50)")
    args = parser.parse_args()

    package_context(args.project_path, args.output_zip, args.task, args.files, args.max_size_mb)
