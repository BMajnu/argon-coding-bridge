#!/usr/bin/env python3
"""
Cross-platform Codebase Packager for Gemini 4 Argon
Strictly respects .gitignore, excludes dependencies/build packages (node_modules, venv, Pods, .gradle, etc.),
strips secrets (.env*), and enforces a realistic max code file size (default 15MB).
"""
import os
import sys
import fnmatch
import hashlib
import json
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
    # Archives & packages
    ".zip", ".tar", ".gz", ".7z", ".rar", ".bz2", ".xz", ".zst", ".tgz", ".tbz2",
    ".whl", ".egg", ".jar", ".war", ".ear", ".apk", ".aab", ".ipa", ".msi", ".dmg",
    ".pkg", ".deb", ".rpm",
    # Binaries, executables, libraries & compiled objects
    ".exe", ".dll", ".so", ".dylib", ".iso", ".bin", ".pyc", ".pyo", ".pyd",
    ".class", ".o", ".obj", ".pdb", ".lib", ".a", ".wasm", ".node", ".rlib",
    ".rmeta", ".dill", ".dex", ".ko", ".elf",
    # Fonts (E1: 90 .ttf fonts poisoned the BM Typer archive)
    ".ttf", ".otf", ".woff", ".woff2", ".eot", ".fon", ".fnt",
    # Images, icons & design binaries (E1: 65 .png images poisoned the BM Typer archive)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".icns", ".tiff",
    ".tif", ".avif", ".heic", ".heif", ".psd", ".ai", ".eps", ".raw", ".cr2", ".nef",
    # Audio & video media
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".3gp",
    ".mpg", ".mpeg", ".ogv", ".mp3", ".wav", ".ogg", ".oga", ".flac", ".aac",
    ".m4a", ".wma", ".opus",
    # Databases, binary data, office docs & ML weights
    ".pdf", ".sqlite", ".sqlite3", ".db", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".onnx", ".pt", ".pth", ".pb", ".tflite", ".h5",
    ".safetensors", ".gguf", ".parquet", ".feather", ".arrow", ".npy", ".npz",
    ".pkl", ".pickle", ".dat", ".pak", ".idx", ".pack",
}

BINARY_CHECK_BYTES = 8192
# The caps are the packager's DEFAULT for every caller, including the dual dispatch
# (scripts/dual_dispatch.py calls package_context() without passing them). A large
# workspace truncated at 400 entries lost whole crates from the archive, so the defaults
# are raised: the archive stays bounded, but it now carries the whole related codebase.
DEFAULT_MAX_ARCHIVE_MB = 100
DEFAULT_MAX_ARCHIVE_ENTRIES = 6000
def is_binary_file(path: Path, sample_bytes: int = BINARY_CHECK_BYTES) -> tuple[bool, str]:
    """Return (True, reason) when `path` is binary by extension or content (E1).

    A file whose extension is in DEFAULT_EXCLUDED_EXTS, whose first 8 KB contain
    a NUL byte (0x00), or whose content is not valid UTF-8 is treated as binary
    and excluded so Gemini never ingests raw binary bytes inside the zip.
    """
    ext = path.suffix.lower()
    if ext in DEFAULT_EXCLUDED_EXTS:
        return True, f"binary/media extension: {ext}"
    try:
        with open(path, "rb") as fh:
            head = fh.read(sample_bytes)
            if b"\x00" in head:
                return True, "binary content: NUL byte in first 8 KB"
            import codecs as _codecs

            dec = _codecs.getincrementaldecoder("utf-8")("strict")
            dec.decode(head, final=len(head) < sample_bytes)
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    dec.decode(b"", final=True)
                    break
                if b"\x00" in chunk:
                    return True, "binary content: NUL byte in file"
                dec.decode(chunk, final=False)
    except UnicodeDecodeError:
        return True, "binary content: not valid UTF-8"
    except OSError as exc:
        return True, f"unreadable file: {exc}"
    return False, ""


def _score_candidate_for_cap(rel: Path, full_path: Path, size_bytes: int, task_tokens: set[str]) -> tuple:
    """Rank candidate files so the archive cap keeps the largest, most relevant source files (E1.2)."""
    rel_posix = rel.as_posix().lower()
    ext = rel.suffix.lower()
    is_companion_ext = 1 if "skills/argon-coding-bridge/extension/" in rel_posix else 0
    is_decl_src = 1 if (ext in DECLARATION_EXTENSIONS or is_companion_ext) else 0
    is_src = 1 if is_source_file(full_path) else 0
    task_hits = sum(1 for tok in task_tokens if tok in rel_posix)
    parts_low = {p.lower() for p in rel.parts[:-1]}
    in_low_priority = 1 if (parts_low & {"test", "tests", "__tests__", "examples", "example", "docs", "fixtures", "benchmarks"}) else 0
    return (
        is_companion_ext,
        is_decl_src,
        is_src,
        task_hits,
        -in_low_priority,
        size_bytes,
        -len(rel.parts),
    )
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
    "argon_focused_context.zip",
    "argon_package.zip",
    "argon_delivery.zip",
    "delivery_manifest.md",
    "focus_manifest.md",
    "archive_stamp.json",
    "argon_task_prompt.md",
    "argon_response.txt",
    "argon_response.md",
    "task_brief.md",
    "api_index.md",
    "accounts_pool.json",
    "argon_sessions.json",
    "argon_task_state.json",
}


def get_git_head_commit(proj_root: Path) -> str:
    """Return the current HEAD git commit of `proj_root`, or a deterministic tree tag when not in git (W8 R8.4)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(proj_root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=10,
        )
        sha = (res.stdout or "").strip()
        if sha:
            return sha
    except Exception:
        pass
    h = hashlib.sha256()
    try:
        for p in sorted(proj_root.rglob("*")):
            if p.is_file() and not is_generated_artefact(p.relative_to(proj_root)):
                rel_s = p.relative_to(proj_root).as_posix()
                if not is_dependency_or_build_path(Path(rel_s)) and not is_secret(p.name):
                    h.update(rel_s.encode("utf-8", errors="replace"))
                    try:
                        h.update(p.read_bytes())
                    except Exception:
                        pass
    except Exception:
        pass
    return "tree-" + h.hexdigest()[:16]


def compute_project_file_hashes(
    proj_root: Path,
    selected_files: list[Path] | None = None,
) -> tuple[dict[str, str], str]:
    """Compute per-file SHA-256 digests and a combined tree SHA-256 for `selected_files` (W8 R8.2, R8.4)."""
    file_hashes: dict[str, str] = {}
    tree_hasher = hashlib.sha256()
    files_to_hash: list[Path] = []
    if selected_files is not None:
        files_to_hash = sorted(selected_files, key=lambda p: p.as_posix().lower())
    else:
        for root, dirs, files in os.walk(proj_root):
            dirs[:] = sorted(
                d
                for d in dirs
                if d.lower() not in _DEFAULT_EXCLUDED_DIRS_LOWER
                and d.lower() not in {".argon_backup", "downloads", "__pycache__", "scratch"}
            )
            for fn in sorted(files):
                fp = Path(root) / fn
                if not fp.is_file():
                    continue
                rel = fp.relative_to(proj_root)
                if (
                    is_dependency_or_build_path(rel)
                    or is_secret(fn)
                    or is_generated_artefact(rel)
                    or fp.suffix.lower() in DEFAULT_EXCLUDED_EXTS
                ):
                    continue
                is_bin, _ = is_binary_file(fp)
                if is_bin:
                    continue
                files_to_hash.append(rel)
        files_to_hash.sort(key=lambda p: p.as_posix().lower())

    for rel in files_to_hash:
        full = proj_root / rel
        if not full.is_file():
            continue
        try:
            raw = full.read_bytes()
        except OSError:
            continue
        f_sha = hashlib.sha256(raw).hexdigest()
        rel_posix = rel.as_posix()
        file_hashes[rel_posix] = f_sha
        tree_hasher.update(f"{rel_posix}:{f_sha}\n".encode("utf-8"))
    return file_hashes, tree_hasher.hexdigest()


def read_archive_stamp(
    zip_path: str | Path | None = None,
    project_path: str | Path | None = None,
) -> dict:
    """Read the archive stamp (file_name, byte_size, sha256, git_commit, file_hashes) from a zip or project (W8 R8.1, R8.4)."""
    stamp: dict = {}
    proj_root = Path(project_path).resolve() if project_path else None
    zp = Path(zip_path).resolve() if zip_path else (proj_root / "argon_context.zip" if proj_root else None)

    if proj_root is not None:
        sidecar = proj_root / ".argon_backup" / "archive_stamp.json"
        if sidecar.is_file():
            try:
                parsed = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    stamp.update(parsed)
            except Exception:
                pass

    if zp is not None and zp.is_file():
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                if "ARCHIVE_STAMP.json" in zf.namelist():
                    inner = json.loads(zf.read("ARCHIVE_STAMP.json").decode("utf-8", errors="replace"))
                    if isinstance(inner, dict):
                        for k, v in inner.items():
                            if v is not None and (k not in stamp or k in ("file_hashes", "git_commit", "commit", "tree_sha256")):
                                stamp[k] = v
        except Exception:
            pass
        try:
            raw_zip = zp.read_bytes()
            outer_sha = hashlib.sha256(raw_zip).hexdigest()
            stamp["file_name"] = zp.name
            stamp["byte_size"] = len(raw_zip)
            stamp["sha256"] = outer_sha
            stamp["archive_sha256"] = outer_sha
        except Exception:
            pass

    if proj_root is not None and not stamp.get("git_commit"):
        stamp["git_commit"] = get_git_head_commit(proj_root)
    if stamp.get("git_commit") and not stamp.get("commit"):
        stamp["commit"] = stamp["git_commit"]
    if stamp.get("sha256") and not stamp.get("archive_sha256"):
        stamp["archive_sha256"] = stamp["sha256"]
    stamp.setdefault("file_name", zp.name if zp is not None else "argon_context.zip")
    stamp.setdefault("byte_size", 0)
    stamp.setdefault("sha256", stamp.get("tree_sha256") or "")
    stamp.setdefault("archive_sha256", stamp.get("sha256") or "")
    stamp.setdefault("git_commit", "")
    stamp.setdefault("commit", stamp.get("git_commit") or "")
    stamp.setdefault("file_hashes", {})
    return stamp


def build_focused_archive(
    project_path: str | Path,
    base_stamp: dict | None = None,
    output_zip: str | Path | None = None,
    changed_files: list[str] | None = None,
    base_zip_path: str | Path | None = None,
) -> dict:
    """Build a FOCUSED archive containing ONLY files changed since `base_stamp` plus `FOCUS_MANIFEST.md` (W8 R8.2)."""
    proj_root = Path(project_path).resolve()
    eff_base = dict(base_stamp) if isinstance(base_stamp, dict) and base_stamp else read_archive_stamp(base_zip_path, proj_root)
    base_hashes = eff_base.get("file_hashes") if isinstance(eff_base.get("file_hashes"), dict) else {}
    base_commit = str(eff_base.get("git_commit") or eff_base.get("commit") or "unknown")
    base_sha = str(eff_base.get("sha256") or eff_base.get("archive_sha256") or eff_base.get("tree_sha256") or "unknown")
    base_fname = str(eff_base.get("file_name") or "argon_context.zip")
    base_fsize = int(eff_base.get("byte_size") or 0)

    cur_hashes, cur_tree_sha = compute_project_file_hashes(proj_root)
    cur_commit = get_git_head_commit(proj_root)

    changed_rels: list[Path] = []
    deleted_rels: list[str] = []
    if changed_files is not None:
        for cf in changed_files:
            rel_p = Path(cf)
            if rel_p.is_absolute():
                try:
                    rel_p = rel_p.relative_to(proj_root)
                except ValueError:
                    continue
            if (proj_root / rel_p).is_file():
                changed_rels.append(rel_p)
    elif base_hashes:
        for rel_posix, f_sha in sorted(cur_hashes.items()):
            if base_hashes.get(rel_posix) != f_sha:
                changed_rels.append(Path(rel_posix))
        for old_rel in sorted(base_hashes.keys()):
            if old_rel not in cur_hashes:
                deleted_rels.append(old_rel)
    elif base_commit and base_commit != "unknown" and cur_commit != base_commit and not base_commit.startswith("tree-"):
        try:
            diff_res = subprocess.run(
                ["git", "-C", str(proj_root), "diff", "--name-only", base_commit],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            for ln in (diff_res.stdout or "").splitlines():
                cand = ln.strip()
                if cand and (proj_root / cand).is_file() and not is_generated_artefact(Path(cand)):
                    changed_rels.append(Path(cand))
        except Exception:
            pass

    dedup_changed: list[Path] = []
    seen_c: set[str] = set()
    for r in changed_rels:
        rk = r.as_posix().lower()
        if rk not in seen_c and (proj_root / r).is_file():
            seen_c.add(rk)
            dedup_changed.append(r)

    if not dedup_changed:
        return {
            "needed": False,
            "changed_files": [],
            "deleted_files": deleted_rels,
            "focused_zip_path": None,
            "file_name": "",
            "byte_size": 0,
            "sha256": "",
            "base_commit": base_commit,
            "base_archive_sha256": base_sha,
            "current_commit": cur_commit,
            "base_stamp": eff_base,
        }

    out_zip = (
        Path(output_zip).resolve()
        if output_zip
        else (proj_root / ".argon_backup" / "argon_focused_context.zip").resolve()
    )
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = out_zip.with_name(f".{out_zip.name}.{os.getpid()}.tmp")

    payload_hasher = hashlib.sha256()
    changed_entries_md: list[str] = []
    changed_hashes_map: dict[str, str] = {}
    for rel in dedup_changed:
        full_p = proj_root / rel
        raw_b = full_p.read_bytes()
        f_sha = hashlib.sha256(raw_b).hexdigest()
        rel_posix = rel.as_posix()
        changed_hashes_map[rel_posix] = f_sha
        payload_hasher.update(f"{rel_posix}:{f_sha}\n".encode("utf-8"))
        changed_entries_md.append(f"- `{rel_posix}` ({len(raw_b)} bytes, sha256=`{f_sha}`)")

    focused_payload_sha = payload_hasher.hexdigest()
    manifest_lines = [
        "# Focused Context Archive Manifest (`FOCUS_MANIFEST.md`)",
        "",
        "This focused archive carries ONLY the files that changed since the conversation's base context archive was packaged.",
        "It supplements (and replaces nothing in) the base archive already attached in this conversation.",
        "",
        "## Base Archive Already in Conversation",
        f"- **Base Archive File:** `{base_fname}`",
        f"- **Base Archive Size:** `{base_fsize}` bytes",
        f"- **Base Archive SHA-256:** `{base_sha}`",
        f"- **Base Project Commit:** `{base_commit}`",
        "",
        "## Focused Delta Summary",
        f"- **Current Project Commit:** `{cur_commit}`",
        f"- **Focused Payload SHA-256:** `{focused_payload_sha}`",
        f"- **Changed Files Count:** `{len(dedup_changed)}`",
        "",
        "## Changed Files Included in This Focused Archive",
        *changed_entries_md,
    ]
    if deleted_rels:
        manifest_lines.extend([
            "",
            "## Files Removed Since Base Archive",
            *[f"- `{d}`" for d in deleted_rels],
        ])
    manifest_text = "\n".join(manifest_lines) + "\n"

    stamp_payload = {
        "is_focused_archive": True,
        "file_name": out_zip.name,
        "base_archive_file": base_fname,
        "base_archive_byte_size": base_fsize,
        "base_archive_sha256": base_sha,
        "base_commit": base_commit,
        "git_commit": cur_commit,
        "commit": cur_commit,
        "tree_sha256": focused_payload_sha,
        "archive_sha256": focused_payload_sha,
        "sha256": focused_payload_sha,
        "changed_files": [r.as_posix() for r in dedup_changed],
        "deleted_files": deleted_rels,
        "file_hashes": changed_hashes_map,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_STORED) as zf:
        for rel in dedup_changed:
            zf.write(proj_root / rel, arcname=str(rel))
        zf.writestr("FOCUS_MANIFEST.md", manifest_text)
        zf.writestr("ARCHIVE_STAMP.json", json.dumps(stamp_payload, indent=2, ensure_ascii=False) + "\n")

    os.replace(str(tmp_zip), str(out_zip))
    raw_focused_zip = out_zip.read_bytes()
    focused_zip_sha = hashlib.sha256(raw_focused_zip).hexdigest()
    focused_zip_size = len(raw_focused_zip)

    return {
        "needed": True,
        "changed_files": [r.as_posix() for r in dedup_changed],
        "deleted_files": deleted_rels,
        "focused_zip_path": out_zip,
        "file_name": out_zip.name,
        "byte_size": focused_zip_size,
        "sha256": focused_zip_sha,
        "payload_sha256": focused_payload_sha,
        "base_commit": base_commit,
        "base_archive_sha256": base_sha,
        "current_commit": cur_commit,
        "base_stamp": eff_base,
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
    if parts & {".argon_backup", "downloads", "__pycache__", "scratch"}:
        return True
    # Stale manual backup trees inside the project (e.g. .penflow_backup_*,
    # .argon_backup/manual-*, scratch/r4cmp, scratch/pre_r4_backup). They hold
    # OUTDATED copies of the very files the patch targets, so packing them
    # wastes archive space and risks Argon patching a backup copy instead of
    # the live file.
    for part in parts:
        if part.startswith(".penflow_backup") or part.startswith(".argon_backup"):
            return True
        if part.endswith(".backup") and part.startswith("."):
            return True
        if part in {"backup", "backups", "r4cmp", "pre_r4_backup"}:
            return True
    return False


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
    cap_excluded_files: list[tuple[str, str]] | None = None,
    binary_skipped_count: int = 0,
    max_archive_mb: int = DEFAULT_MAX_ARCHIVE_MB,
    max_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
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
    if cap_excluded_files:
        excl_lines.extend([
            f"{h3} ARCHIVE IS INCOMPLETE - HARD CAP REACHED ({len(cap_excluded_files)} file(s) omitted by {max_entries}-entry / {max_archive_mb} MB cap)",
            "",
            "WARNING: This archive is INCOMPLETE because the project exceeded the hard archive cap",
            f"({max_archive_mb} MB / {max_entries} entries). The packager kept the highest-priority source",
            "files up to the 100 MB maximum and omitted every file listed below. Do NOT guess the",
            "contents of any omitted file - if your task touches one of them, name it and stop:",
            "",
        ])
        for rel_str, reason in cap_excluded_files:
            excl_lines.append(f"{bullet} {rel_str:<32} ({reason})")
        excl_lines.append("")
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
    max_size_mb: int = DEFAULT_MAX_ARCHIVE_MB,
    max_archive_mb: int = DEFAULT_MAX_ARCHIVE_MB,
    max_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
):
    proj_root = Path(project_path).resolve()
    if not proj_root.exists():
        print(f"[Error] Project root does not exist: {proj_root}", file=sys.stderr)
        sys.exit(1)

    max_bytes = max_size_mb * 1024 * 1024
    max_archive_bytes = max(1, int(max_archive_mb)) * 1024 * 1024
    max_archive_entries = max(1, int(max_entries))
    candidate_files: list[tuple[Path, int]] = []
    seen_rel: set[str] = set()
    excluded_files: list[tuple[str, str]] = []
    cap_excluded_files: list[tuple[str, str]] = []
    binary_skipped_count = 0
    cap_skipped_count = 0

    def _record_excluded(rel: Path, reason: str, is_binary: bool = False, is_cap: bool = False) -> None:
        nonlocal binary_skipped_count, cap_skipped_count
        rel_posix = rel.as_posix()
        print(f"  [-] Excluded: {rel_posix:<36} ({reason})")
        excluded_files.append((rel_posix, reason))
        if is_binary:
            binary_skipped_count += 1
        if is_cap:
            cap_skipped_count += 1
            cap_excluded_files.append((rel_posix, reason))

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
                is_bin, bin_reason = is_binary_file(p)
                if is_bin:
                    _record_excluded(rel, bin_reason, is_binary=True)
                    continue
                sz = p.stat().st_size
                if sz > max_bytes:
                    mb = max(1, int(round(sz / (1024 * 1024))))
                    _record_excluded(rel, f"oversized: {mb} MB")
                    continue
                seen_rel.add(rel_key)
                candidate_files.append((rel, sz))
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
                and d.lower() not in {".argon_backup", "downloads", "__pycache__", "scratch"}
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
                    _record_excluded(rel, f"binary/media extension: {p.suffix.lower()}", is_binary=True)
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
                is_bin, bin_reason = is_binary_file(p)
                if is_bin:
                    _record_excluded(rel, bin_reason, is_binary=True)
                    continue
                seen_rel.add(rel_key)
                candidate_files.append((rel, sz))

    # E1.2: Enforce hard archive cap on entry count (max_entries) and total bytes (max_archive_bytes).
    # Reserve 2 entries for TASK_BRIEF.md and API_INDEX.md so they are ALWAYS present.
    max_source_entries = max(1, max_archive_entries - 2)
    total_candidate_bytes = sum(sz for _, sz in candidate_files)
    if len(candidate_files) > max_source_entries or total_candidate_bytes > max_archive_bytes:
        task_tokens = {
            tok.lower()
            for tok in re.findall(r"[A-Za-z0-9_./-]{3,}", task_description or "")
        }
        ranked_candidates = sorted(
            candidate_files,
            key=lambda item: _score_candidate_for_cap(item[0], proj_root / item[0], item[1], task_tokens),
            reverse=True,
        )
        kept_files: list[Path] = []
        running_bytes = 0
        for rel, sz in ranked_candidates:
            if len(kept_files) >= max_source_entries:
                _record_excluded(
                    rel,
                    f"skipped by archive cap: entry limit ({max_archive_entries} max entries) reached; kept higher-priority source files",
                    is_cap=True,
                )
                continue
            if running_bytes + sz > max_archive_bytes:
                _record_excluded(
                    rel,
                    f"skipped by archive cap: size limit ({max_archive_mb} MB max) reached (file size {_format_size(sz)})",
                    is_cap=True,
                )
                continue
            kept_files.append(rel)
            running_bytes += sz
        selected_files = sorted(kept_files, key=lambda p: p.as_posix().lower())
    else:
        selected_files = [rel for rel, _ in candidate_files]

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

    # ZIP_STORED, not DEFLATED: Gemini reads every member of the archive as
    # plain text, so the members must not be compressed. Every archive that has
    # actually been consumed by Argon in this project uses compress_type=0.
    git_commit = get_git_head_commit(proj_root)
    file_hashes, tree_sha256 = compute_project_file_hashes(proj_root, selected_files)
    generated_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_STORED) as zf:
        for rel in selected_files:
            full = proj_root / rel
            zf.write(full, arcname=str(rel))

        brief_text = f"""# Task Brief for Gemini 4 Argon
**Generated:** {generated_ts}
**Absolute Target Directory:** {proj_root}
**Project Root:** {proj_root.name}
**Project Commit:** {git_commit}
**Source Tree SHA-256:** {tree_sha256}
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

### 2. SURGICAL EDITS ONLY (ZERO UI REGRESSION & LINEAR ORDINAL REPLACEMENT):
   - Do NOT rewrite or restructure entire files unless creating a brand new file.
   - Do NOT change existing UI styling, component hierarchies, or CSS rules unless explicitly commanded above.
   - NEVER call `.NET String.Replace(String, String)` or `String.Contains(String)` on a large file or multi-kilobyte needle: on Windows PowerShell 5.1 (.NET Framework) they are O(file x needle) and hang for 25+ minutes on 500 KB files.
   - Instead, locate each target block with an ordinal index search (`$idx = $content.IndexOf($oldTarget, [System.StringComparison]::Ordinal)`) and rebuild the string with `[System.Text.StringBuilder]`.
   - Use single-quoted Here-Strings (`@' ... '@`) so JS/TS template literals (`${{var}}`) are preserved.
   - Write files using UTF-8 without BOM: `[System.Text.UTF8Encoding]::new($false)`.

### 3. TWO-PHASE PRE-FLIGHT AND WRITE (A PARTIAL APPLY MUST NEVER HAPPEN)
   - Phase 1 (Pre-flight loop): Read every target file and verify EVERY `$oldTarget` block of EVERY file with `$content.IndexOf($oldTarget, [System.StringComparison]::Ordinal) -lt 0` BEFORE any file is backed up or written. Abort with `throw` if any target block is missing so the project stays untouched.
   - Phase 2 (Write loop): Back up every file you overwrite into `.argon_backup/<yyyyMMdd_HHmmss>/<relative path>`, apply the replacements with `IndexOf` + `StringBuilder`, write UTF-8 without BOM, and print one `[Patched] <relative path>` line per changed file and a final `[SUCCESS] N file(s) patched.` summary line.

### 4. VERIFY YOUR WORK (REQUIRED)
   - After writing each file, re-read it from disk and assert with `$verify.IndexOf($newReplacement, [System.StringComparison]::Ordinal) -ge 0` that the intended new content is present.
   - Print `[Verified] <relative path>` for every file that matches and `[FAILED VERIFY] <relative path>` (exiting non-zero) if any file does not match.

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
        brief_text = _compose_task_brief(
            proj_root,
            selected_files,
            excluded_files,
            brief_text,
            cap_excluded_files=cap_excluded_files,
            binary_skipped_count=binary_skipped_count,
            max_archive_mb=max_archive_mb,
            max_entries=max_archive_entries,
        )
        zf.writestr("TASK_BRIEF.md", brief_text)
        inner_stamp = {
            "is_focused_archive": False,
            "file_name": out_zip_path.name,
            "git_commit": git_commit,
            "commit": git_commit,
            "tree_sha256": tree_sha256,
            "archive_sha256": tree_sha256,
            "sha256": tree_sha256,
            "included_files_count": len(selected_files),
            "file_hashes": file_hashes,
            "timestamp": generated_ts,
        }
        zf.writestr("ARCHIVE_STAMP.json", json.dumps(inner_stamp, indent=2, ensure_ascii=False) + "\n")

    # D13.2: Write TASK_BRIEF.md and API_INDEX.md atomically via temporary files + os.replace
    try:
        local_brief = proj_root / "TASK_BRIEF.md"
        tmp_brief = local_brief.with_name(f".TASK_BRIEF.md.{os.getpid()}.tmp")
        tmp_brief.write_text(brief_text, encoding="utf-8")
        os.replace(str(tmp_brief), str(local_brief))
    except Exception:
        pass
    try:
        local_api = proj_root / "API_INDEX.md"
        tmp_api = local_api.with_name(f".API_INDEX.md.{os.getpid()}.tmp")
        tmp_api.write_text(api_index_text, encoding="utf-8")
        os.replace(str(tmp_api), str(local_api))
    except Exception:
        pass

    # E1.3 & D13.1: Verify TASK_BRIEF.md and API_INDEX.md are both present and readable UTF-8 before os.replace
    with zipfile.ZipFile(tmp_zip_path, "r") as verify_zf:
        archive_members = verify_zf.namelist()
        for required_member in ("TASK_BRIEF.md", "API_INDEX.md"):
            if required_member not in archive_members:
                raise RuntimeError(f"Packaged archive is missing required {required_member}")
            raw_member = verify_zf.read(required_member)
            if not raw_member or b"\x00" in raw_member:
                raise RuntimeError(f"Packaged archive member {required_member} is empty or contains NUL bytes")
            decoded_member = raw_member.decode("utf-8", errors="strict")
            if not decoded_member.strip():
                raise RuntimeError(f"Packaged archive member {required_member} is blank after UTF-8 decode")
    os.replace(str(tmp_zip_path), str(out_zip_path))

    size_bytes = out_zip_path.stat().st_size
    size_kb = round(size_bytes / 1024, 2)
    total_entries = len(archive_members)
    outer_sha256 = hashlib.sha256(out_zip_path.read_bytes()).hexdigest()
    final_stamp = {
        "is_focused_archive": False,
        "file_name": out_zip_path.name,
        "byte_size": size_bytes,
        "sha256": outer_sha256,
        "archive_sha256": outer_sha256,
        "tree_sha256": tree_sha256,
        "git_commit": git_commit,
        "commit": git_commit,
        "included_files_count": len(selected_files),
        "file_hashes": file_hashes,
        "timestamp": generated_ts,
    }
    try:
        stamp_file = proj_root / ".argon_backup" / "archive_stamp.json"
        stamp_file.parent.mkdir(parents=True, exist_ok=True)
        stamp_file.write_text(json.dumps(final_stamp, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass
    print(
        f"[+] Archive stamp: file={out_zip_path.name} | bytes={size_bytes} | "
        f"sha256={outer_sha256} | commit={git_commit}"
    )
    print(
        f"[SUCCESS] Packaged {len(selected_files)} source files | "
        f"entries={total_entries}, bytes={size_bytes} ({size_kb} KB), "
        f"binary files skipped={binary_skipped_count}, files skipped by cap={cap_skipped_count} -> {out_zip_path}"
    )
    return final_stamp


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Package codebase context for Gemini 4 Argon")
    parser.add_argument("--project-path", default=".", help="Root directory of the project")
    parser.add_argument("--output-zip", default="argon_context.zip", help="Output zip filename")
    parser.add_argument("--task", default="", help="Task description or goal")
    parser.add_argument("--files", nargs="*", default=None, help="Specific target files/patterns")
    parser.add_argument("--max-size-mb", type=int, default=DEFAULT_MAX_ARCHIVE_MB, help="Max single file size in MB (default: 100)")
    parser.add_argument("--max-archive-mb", type=int, default=DEFAULT_MAX_ARCHIVE_MB, help="Max total archive size in MB (default: 100)")
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ARCHIVE_ENTRIES, help="Max total archive entries (default: 6000)")
    args = parser.parse_args()

    package_context(
        args.project_path,
        args.output_zip,
        args.task,
        args.files,
        args.max_size_mb,
        max_archive_mb=args.max_archive_mb,
        max_entries=args.max_entries,
    )
