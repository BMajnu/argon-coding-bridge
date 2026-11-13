#!/usr/bin/env python3
"""Idempotent re-appliable patch for `MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES` in `dispatch_argon.py` (R3 / F3)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "dispatch_argon.py"


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()
    if not target.is_file():
        return {"ok": False, "status": "missing_target", "target": str(target)}
    raw = target.read_text(encoding="utf-8")
    if "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES" in raw and "size_cap_exceeded" in raw:
        return {"ok": True, "status": "already_applied", "target": str(target)}

    old_block = """def _attach_zip_via_datatransfer(page, zip_path: Path, use_extension: bool = True) -> tuple[bool, str, str]:"""
    if old_block not in raw:
        return {"ok": False, "status": "anchor_not_found", "target": str(target)}

    cap_decl = "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES = 15 * 1024 * 1024\n\n\n"
    updated = raw.replace(old_block, cap_decl + old_block, 1)
    updated = updated.replace(
        "    zp = Path(zip_path)\n    try:\n        raw_bytes = zp.read_bytes()",
        "    zp = Path(zip_path)\n    if zp.stat().st_size > MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES:\n"
        "        return False, 'size_cap_exceeded', 'archive exceeds MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES'\n"
        "    try:\n        raw_bytes = zp.read_bytes()",
        1,
    )
    if not dry_run:
        target.write_text(updated, encoding="utf-8")
    return {"ok": True, "status": "applied", "target": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply or verify large-archive base64 size cap patch on dispatch_argon.py")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check status without writing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run)
    print(f"[patch_bridge_large_archive] status={res.get('status')} ok={res.get('ok')} target={res.get('target')}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())