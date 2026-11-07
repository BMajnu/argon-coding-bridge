#!/usr/bin/env python3
"""
prepare_brief.py - Prepare task brief, package context archive, and generate brief summary.

Prepares dispatch files and writes a human-readable and machine-readable summary
to <out>/brief_summary.md before prompt dispatch, allowing review and correction.
Does NOT open a browser.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from package_context import package_context
except ImportError:
    package_context = None


def _make_console_utf8_safe() -> None:
    """Never let a print kill the tool with UnicodeEncodeError.

    Windows consoles and, more often, a redirected pipe default to cp1252. The brief
    summary contains emoji and Bengali text, so printing it raw raises
    UnicodeEncodeError and the tool dies with exit 1 AFTER the summary file was
    already written. Switch the stream to UTF-8 and fall back to replacement
    characters for anything that still cannot be encoded.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
            if enc not in ("utf8", "utf8sig", "utf16", "utf16le"):
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def _find_target_paths_in_brief(brief_text: str, project_root: Path) -> List[str]:
    """Parse out every target file path the brief mentions to calculate blast radius."""
    found: Set[str] = set()

    # Look for paths under markdown lists or tables or blocks
    # Common code path regex: relative paths with extension
    path_pattern = re.compile(
        r"(?:[\s`'\"]|^)([a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_\-]+)(?:[\s`'\":,]|$)"
    )
    for line in brief_text.splitlines():
        # Check files listed in section headers or bullet points
        for match in path_pattern.finditer(line):
            candidate = match.group(1).strip("`'\", :")
            if not candidate:
                continue
            # Filter obvious non-paths / URLs / numbers
            if candidate.startswith("http://") or candidate.startswith("https://"):
                continue
            if re.match(r"^\d+\.\d+", candidate):
                continue
            # Normalise path
            cand_path = Path(candidate.replace("\\", "/"))
            ext = cand_path.suffix.lower()
            if ext in {
                ".py", ".ps1", ".bat", ".cmd", ".md", ".json", ".yml", ".yaml",
                ".txt", ".sh", ".ts", ".js", ".tsx", ".jsx", ".html", ".css",
            }:
                # If path exists in repo or looks like project path
                if (project_root / cand_path).exists() or cand_path.parts[0] in {
                    "skills", "knowledge", ".github", "src", "lib", "scripts",
                }:
                    found.add(cand_path.as_posix())

    return sorted(list(found))


def _inspect_archive(zip_path: Path) -> Dict[str, Any]:
    """Inspect packaged archive entries and integrity."""
    if not zip_path.is_file():
        return {
            "path": str(zip_path),
            "size_bytes": 0,
            "entry_count": 0,
            "has_task_brief": False,
            "has_api_index": False,
            "entries": [],
        }

    size_bytes = zip_path.stat().st_size
    entries: List[str] = []
    has_tb = False
    has_api = False

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            entries = sorted(zf.namelist())
            has_tb = "TASK_BRIEF.md" in entries
            has_api = "API_INDEX.md" in entries
    except Exception:
        pass

    return {
        "path": str(zip_path),
        "size_bytes": size_bytes,
        "entry_count": len(entries),
        "has_task_brief": has_tb,
        "has_api_index": has_api,
        "entries": entries,
    }


def _lookup_slot_email(slot: str, config_path: Path) -> str:
    """Look up account email for slot in accounts_pool.json."""
    if not config_path.is_file():
        return ""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        norm = "/" + slot.strip("/") + "/" if slot else "/u/0/"
        for acct in data.get("accounts", []):
            if acct.get("slot") == norm:
                return str(acct.get("email") or "")
    except Exception:
        pass
    return ""


def prepare_brief(
    project_path: str = ".",
    brief: str = "README.md",
    lane: str = "followup",
    slot: Optional[str] = None,
    out: Optional[str] = None,
    as_json: bool = False,
) -> int:
    proj_root = Path(project_path).resolve()
    if not proj_root.exists():
        print(f"[!] Error: Project root does not exist: {proj_root}", file=sys.stderr)
        return 1

    brief_path = Path(brief)
    if not brief_path.is_absolute():
        brief_path = (proj_root / brief).resolve()
    if not brief_path.is_file():
        # Fallback to current working directory relative
        brief_path = Path(brief).resolve()
    if not brief_path.is_file():
        print(f"[!] Error: Brief file does not exist: {brief}", file=sys.stderr)
        return 1

    brief_text = brief_path.read_text(encoding="utf-8")
    brief_bytes = brief_text.encode("utf-8")
    brief_sha256 = hashlib.sha256(brief_bytes).hexdigest()
    brief_len = len(brief_bytes)

    # Determine out directory
    now_hms = datetime.now().strftime("%Y%m%d_%H%M%S")
    if out:
        expanded_out = os.path.expandvars(out)
        out_dir = Path(expanded_out).resolve()
    else:
        out_dir = proj_root / ".argon_backup" / "briefs" / now_hms

    out_dir.mkdir(parents=True, exist_ok=True)
    summary_file = out_dir / "brief_summary.md"
    zip_path = out_dir / "argon_context.zip"

    # Package context archive
    if package_context is not None:
        try:
            package_context(
                project_path=str(proj_root),
                output_zip=str(zip_path),
                task_description=brief_text,
            )
        except Exception as e:
            print(f"[!] Warning: package_context failed: {e}", file=sys.stderr)
    else:
        print("[!] Warning: package_context module not available.", file=sys.stderr)

    archive_info = _inspect_archive(zip_path)
    target_files = _find_target_paths_in_brief(brief_text, proj_root)

    # Dispatch facts
    norm_lane = (lane or "followup").strip().lower()
    norm_slot = "/" + (slot or "u/0").strip("/") + "/"
    config_file = SCRIPTS_DIR.parent / "config" / "accounts_pool.json"
    account_email = _lookup_slot_email(norm_slot, config_file)
    delivery_folder = str(proj_root / "downloads" / norm_lane)

    dispatch_facts = {
        "lane": norm_lane,
        "slot": norm_slot,
        "account_email": account_email or "(from pool config)",
        "mode": "task",
        "model": "4 Argon",
        "delivery_folder": delivery_folder,
    }

    summary_data = {
        "brief": {
            "path": str(brief_path),
            "sha256": brief_sha256,
            "bytes_length": brief_len,
            "text": brief_text,
        },
        "archive": archive_info,
        "files_to_change": target_files,
        "dispatch_facts": dispatch_facts,
        "questions_for_user": [],
    }

    # Format markdown summary
    is_overwrite = summary_file.is_file()
    entries_list_md = "\n".join(f"- `{e}`" for e in archive_info.get("entries", []))
    files_to_change_md = (
        "\n".join(f"- `{f}`" for f in target_files)
        if target_files
        else "(none parsed from brief)"
    )

    md_content = f"""# Brief Summary

**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**Summary File:** `{summary_file}`

## What will be sent

- **SHA-256:** `{brief_sha256}`
- **Length:** {brief_len} bytes
- **Source Brief:** `{brief_path}`

```markdown
{brief_text}
```

## The archive

- **Path:** `{archive_info['path']}`
- **Size:** {archive_info['size_bytes']} bytes
- **Entry Count:** {archive_info['entry_count']} entries
- **TASK_BRIEF.md Present:** {'YES' if archive_info['has_task_brief'] else 'NO'}
- **API_INDEX.md Present:** {'YES' if archive_info['has_api_index'] else 'NO'}

### Packed Files:
{entries_list_md}

## The files the brief will change

{files_to_change_md}

## The dispatch facts

- **Lane:** `{dispatch_facts['lane']}`
- **Slot:** `{dispatch_facts['slot']}`
- **Account Email:** `{dispatch_facts['account_email']}`
- **Workspace Mode:** `{dispatch_facts['mode']}`
- **Model:** `{dispatch_facts['model']}`
- **Delivery Destination:** `{dispatch_facts['delivery_folder']}`

## Questions for the user

(none)
"""

    summary_file.write_text(md_content, encoding="utf-8")

    if is_overwrite:
        print(f"[i] Overwrote existing summary file at {summary_file}")
    else:
        print(f"[+] Summary written to: {summary_file}")

    if as_json:
        print(json.dumps(summary_data, indent=2))
    else:
        print("\n" + "=" * 78)
        print(md_content)
        print("=" * 78)

    return 0


def main() -> int:
    _make_console_utf8_safe()
    parser = argparse.ArgumentParser(description="Prepare task brief and context archive summary")
    parser.add_argument("--project-path", default=".", help="Root directory of the project")
    parser.add_argument("--brief", required=True, help="Path to the brief markdown file")
    parser.add_argument("--lane", choices=["followup", "new"], default="followup", help="Dispatch lane")
    parser.add_argument("--slot", default="/u/0/", help="Account slot to target")
    parser.add_argument("--out", default=None, help="Output directory for brief summary and archive")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output summary as JSON")

    args = parser.parse_args()
    return prepare_brief(
        project_path=args.project_path,
        brief=args.brief,
        lane=args.lane,
        slot=args.slot,
        out=args.out,
        as_json=args.as_json,
    )


if __name__ == "__main__":
    sys.exit(main())
