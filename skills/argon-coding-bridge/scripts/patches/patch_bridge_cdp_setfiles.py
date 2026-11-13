#!/usr/bin/env python3
"""Idempotent re-appliable patch for CDP `DOM.setFileInputFiles` (`objectId`) in `dispatch_argon.py` (R3 / F3)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_TARGET = Path(__file__).resolve().parent.parent / "dispatch_argon.py"

CDP_HELPER_SNIPPET = """def _attach_zip_via_cdp_set_files(page, zip_path: Path) -> tuple[bool, str, str]:
    \"\"\"Attach `zip_path` using CDP `DOM.setFileInputFiles` with the file input's `objectId` (R3/F3: patch_bridge_cdp_setfiles).\"\"\"
    zp = Path(zip_path).resolve()
    if not zp.is_file():
        return False, "cdp_setfiles", f"archive does not exist: {zp}"
    _open_upload_menu(page)
    ctx = getattr(page, "context", None)
    if ctx is None or not hasattr(ctx, "new_cdp_session"):
        return False, "cdp_setfiles", "page context does not support new_cdp_session"
    cdp = ctx.new_cdp_session(page)
    try:
        eval_res = cdp.send(
            "Runtime.evaluate",
            {
                "expression": "(() => { const i = document.querySelector(\"input[type='file']\"); return i || null; })()",
                "returnByValue": False,
            },
        )
        object_id = str(((eval_res or {}).get("result") or {}).get("objectId") or "").strip()
        if not object_id:
            return False, "cdp_setfiles", "the upload menu rendered no file input"
        cdp.send("DOM.setFileInputFiles", {"files": [str(zp)], "objectId": object_id})
        if _wait_for_upload_complete(page, zp):
            return True, "cdp_setfiles", ""
        return False, "cdp_setfiles", f"upload of {zp.name} did not finish within window"
    finally:
        cdp.detach()
"""


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()
    if not target.is_file():
        return {"ok": False, "status": "missing_target", "target": str(target)}
    raw = target.read_text(encoding="utf-8")
    if "def _attach_zip_via_cdp_set_files(" in raw and "DOM.setFileInputFiles" in raw:
        return {"ok": True, "status": "already_applied", "target": str(target)}

    anchor = "def attach_zip(page, zip_path: Path) -> bool:"
    if anchor not in raw:
        return {"ok": False, "status": "anchor_not_found", "target": str(target)}

    updated = raw.replace(anchor, CDP_HELPER_SNIPPET + "\n\n" + anchor, 1)
    if not dry_run:
        target.write_text(updated, encoding="utf-8")
    return {"ok": True, "status": "applied", "target": str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply or verify CDP DOM.setFileInputFiles patch on dispatch_argon.py")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check status without writing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run)
    print(f"[patch_bridge_cdp_setfiles] status={res.get('status')} ok={res.get('ok')} target={res.get('target')}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())