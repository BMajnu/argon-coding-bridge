#!/usr/bin/env python3
"""Re-appliable patch for the base64 archive size cap (R3 / F3).

WHAT IS ACTUALLY TRUE TODAY (measured 2026-11-14)
-------------------------------------------------
* `round23_reliability.py` exports `MAX_BASE64_ATTACH_BYTES` (8 MB) into
  `dispatch_argon.py`'s namespace, but NOTHING in `dispatch_argon.py` reads it -
  the cap is DEAD CODE. `_attach_zip_via_datatransfer()` still base64-encodes the
  WHOLE archive into `page.evaluate`, so a 49 MB archive becomes a ~65 MB string.
* The cap this script installs (`MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES`, 15 MB) is
  NOT present in the current `dispatch_argon.py`.
* Installing it is a BEHAVIOUR CHANGE on the most delicate path of the bridge: the
  cap sits in front of the companion-extension route as well, so an archive between
  15 MB and 64 MB would stop using the extension and fall through to the Playwright
  `set_input_files` route. That decision belongs to the owner, not to a patch script.

SO THIS SCRIPT IS VERIFY-ONLY BY DEFAULT. `--apply` installs the cap on purpose.
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
CAP_NAME = "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES"
CAP_BYTES = 15 * 1024 * 1024


def _install_cap(text: str) -> str:
    old_block = "def _attach_zip_via_datatransfer(page, zip_path: Path, use_extension: bool = True) -> tuple[bool, str, str]:"
    if old_block not in text:
        return text
    cap_decl = f"{CAP_NAME} = {CAP_BYTES // (1024 * 1024)} * 1024 * 1024\n\n\n"
    text = text.replace(old_block, cap_decl + old_block, 1)
    text = text.replace(
        "    zp = Path(zip_path)\n    try:\n        raw_bytes = zp.read_bytes()",
        "    zp = Path(zip_path)\n"
        f"    if zp.stat().st_size > {CAP_NAME}:\n"
        f"        return False, 'size_cap_exceeded', 'archive exceeds {CAP_NAME}'\n"
        "    try:\n        raw_bytes = zp.read_bytes()",
        1,
    )
    return text


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False,
                install: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()
    _, norm, _ = read_source(target)
    inline_cap = CAP_NAME in norm and "size_cap_exceeded" in norm
    module_cap = module_has(RELIABILITY_MODULE, "MAX_BASE64_ATTACH_BYTES")

    if inline_cap:
        return result(True, "already_applied", target,
                      f"{CAP_NAME} is installed in dispatch_argon.py")
    if not install:
        return result(
            False,
            "cap_not_installed",
            target,
            f"the base64 cap is NOT installed in dispatch_argon.py; the 8 MB cap in "
            f"{RELIABILITY_MODULE.name} is exported but never read by it. "
            f"Re-run with --apply to install the {CAP_BYTES // (1024 * 1024)} MB cap "
            f"(this changes the archive attach route for archives between "
            f"{CAP_BYTES // (1024 * 1024)} MB and 64 MB).",
            module_cap_exported=module_cap,
        )
    if dry_run:
        return result(False, "would_change", target, f"would install {CAP_NAME}")

    def transform(text: str) -> str:
        return _install_cap(text)

    status, detail = apply_to_target(target, transform)
    if status == "failed":
        return result(False, status, target, detail)
    if status == "already_applied":
        return result(False, status, target, "the anchor for the cap is gone - the code moved")
    return result(True, status, target, detail)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify (or optionally install) the base64 archive size cap")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check the status without writing")
    parser.add_argument("--apply", action="store_true",
                        help="Actually install the cap (changes the archive attach route)")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run, install=args.apply)
    report("patch_bridge_large_archive", res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
