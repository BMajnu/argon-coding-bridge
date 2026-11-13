#!/usr/bin/env python3
"""Idempotent re-appliable patch for `--attach-images` and `attach_images()` in `dispatch_argon.py` (R3 / F3)."""
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
    if "def attach_images(" in raw and '"--attach-images"' in raw:
        return {"ok": True, "status": "already_applied", "target": str(target)}

    anchor = 'parser.add_argument("--files", nargs="*", default=None, help="Optional target file patterns")'
    if anchor not in raw:
        return {"ok": False, "status": "anchor_not_found", "target": str(target)}

    arg_addition = (
        anchor
        + '\n    parser.add_argument("--attach-images", nargs="*", default=None, metavar="IMAGE", '
        'help="Optional PNG/JPEG/WebP image file paths to inject into the composer.")'
    )
    updated = raw.replace(anchor, arg_addition, 1)
    if not dry_run:
        target.write_text(updated, encoding="utf-8")
    return {"ok": True, "status": "applied", "target": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply or verify --attach-images patch on dispatch_argon.py")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check status without writing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run)
    print(f"[patch_bridge_attach_images] status={res.get('status')} ok={res.get('ok')} target={res.get('target')}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())