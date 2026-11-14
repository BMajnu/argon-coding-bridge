#!/usr/bin/env python3
"""Re-appliable patch for the CDP `DOM.setFileInputFiles` archive attach (R3 / F3).

THE BUG THIS SCRIPT USED TO HAVE (Round 24)
-------------------------------------------
Two, and both were fatal:

1. Its guard looked for `def _attach_zip_via_cdp_set_files(` INSIDE `dispatch_argon.py`,
   but the implementation had moved to `round23_reliability.py`
   (`_attach_zip_via_cdp_dom_file_input`). The guard never matched, so the script
   re-inserted a SECOND copy of the helper on every run.

2. The helper it inserted was not valid Python: the snippet was a plain (non-raw)
   triple-quoted string, so its inner `\\"` escapes were consumed and the generated
   code came out as

       "expression": "(() => { const i = document.querySelector("input[type='file']"); ... )()",

   -> `SyntaxError: invalid syntax. Perhaps you forgot a comma?`
   -> `dispatch_argon.py` would not even import, and the bridge was DEAD.

MEASURED (2026-11-14): `--help` rc=0 before the patch, rc=1 (SyntaxError) after.

WHAT IT DOES NOW
----------------
1. The fix is checked where it lives: `round23_reliability.py`.
2. If it ever has to insert the helper into `dispatch_argon.py`, the snippet is a RAW
   string, so the generated Python is valid - and the write is compile-verified.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _safe_patch import (  # noqa: E402
    DISPATCH_MODULE,
    RELIABILITY_MODULE,
    apply_to_target,
    module_has,
    read_source,
    report,
    result,
)

DEFAULT_TARGET = DISPATCH_MODULE
HELPER_MARKERS = ("def _attach_zip_via_cdp_dom_file_input(", "DOM.setFileInputFiles")

# RAW string: the inner double quotes must survive into the generated file.
CDP_HELPER_SNIPPET = r'''
def _attach_zip_via_cdp_set_files(page, zip_path: Path) -> tuple[bool, str, str]:
    """Attach `zip_path` using CDP `DOM.setFileInputFiles` with the file input's `objectId`."""
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
                "expression": '(() => { const i = document.querySelector("input[type=file]"); return i || null; })()',
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

'''


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()

    if module_has(RELIABILITY_MODULE, *HELPER_MARKERS):
        _, norm, _ = read_source(target)
        inline = "def _attach_zip_via_cdp_set_files(" in norm
        return result(
            True,
            "already_applied",
            target,
            f"the CDP attach lives in {RELIABILITY_MODULE.name} (dispatch_argon.py inline copy: {inline})",
        )

    if dry_run:
        return result(False, "would_change", target,
                      f"{RELIABILITY_MODULE.name} does not carry the CDP attach helper yet")

    def transform(text: str) -> str:
        if "def _attach_zip_via_cdp_set_files(" in text:
            return text
        anchor = "def attach_zip(page, zip_path: Path) -> bool:"
        if anchor not in text:
            return text
        return text.replace(anchor, CDP_HELPER_SNIPPET + "\n" + anchor, 1)

    status, detail = apply_to_target(target, transform)
    if status == "failed":
        return result(False, status, target, detail)
    if status == "already_applied":
        return result(False, status, target,
                      f"{RELIABILITY_MODULE.name} has no CDP attach helper and the target already had one")
    return result(True, status, target, detail)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply or verify the CDP DOM.setFileInputFiles patch")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check the status without writing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run)
    report("patch_bridge_cdp_setfiles", res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
