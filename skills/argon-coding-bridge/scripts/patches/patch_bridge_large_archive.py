#!/usr/bin/env python3
"""Re-appliable patch & verifier for the base64 archive size cap (Round 24 W1 / R1.1).

WHAT IS TRUE IN ROUND 24
------------------------
* `round23_reliability.py` defines `MAX_BASE64_ATTACH_BYTES` (default 8 MB,
  overridable via `ARGON_MAX_BASE64_ATTACH_MB`) as the SINGLE source of truth.
* `dispatch_argon.py` imports and reads `MAX_BASE64_ATTACH_BYTES` inside
  `_attach_zip_via_datatransfer()` and `attach_zip()`: it checks `zp.stat().st_size`
  BEFORE `zp.read_bytes()`, returns `"size_cap_exceeded"` for archives above the cap,
  and routes them through the zero-copy CDP `DOM.setFileInputFiles` / Playwright
  `set_input_files` paths.
* This script verifies that the real `MAX_BASE64_ATTACH_BYTES` cap is exported by
  `round23_reliability.py` and read inside `_attach_zip_via_datatransfer()` in
  `dispatch_argon.py` (or installs that check if run with `--apply` against an older
  copy of `dispatch_argon.py`).
"""
from __future__ import annotations

import argparse
import re
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
CAP_NAME = "MAX_BASE64_ATTACH_BYTES"


def _datatransfer_reads_real_cap(norm_text: str) -> bool:
    m = re.search(r"def _attach_zip_via_datatransfer\b.*?(?=\ndef\s+)", norm_text, re.S)
    body = m.group(0) if m else ""
    return (
        CAP_NAME in body
        and "size_cap_exceeded" in body
        and "stat().st_size" in body
    )


def _install_cap(text: str) -> str:
    if _datatransfer_reads_real_cap(text):
        return text
    old_block = (
        "    zp = Path(zip_path)\n"
        "    try:\n"
        "        raw_bytes = zp.read_bytes()"
    )
    if old_block not in text:
        return text
    new_block = (
        "    zp = Path(zip_path)\n"
        "    try:\n"
        "        sz = zp.stat().st_size\n"
        "    except OSError as exc:\n"
        "        return False, \"python_fallback\", f\"could not stat archive {zp.name}: {exc}\"\n"
        "    eff_cap = (\n"
        "        int(float(os.environ[\"ARGON_MAX_BASE64_ATTACH_MB\"]) * 1024 * 1024)\n"
        "        if os.environ.get(\"ARGON_MAX_BASE64_ATTACH_MB\")\n"
        f"        else int({CAP_NAME})\n"
        "    )\n"
        "    if sz > eff_cap:\n"
        "        return False, \"size_cap_exceeded\", \"size_cap_exceeded\"\n"
        "    try:\n"
        "        raw_bytes = zp.read_bytes()"
    )
    return text.replace(old_block, new_block, 1)


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False,
                install: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()
    _, norm, _ = read_source(target)
    cap_read = _datatransfer_reads_real_cap(norm)
    module_cap = module_has(RELIABILITY_MODULE, CAP_NAME)

    if cap_read and module_cap:
        return result(
            True,
            "already_applied",
            target,
            f"{CAP_NAME} is exported by {RELIABILITY_MODULE.name} and read in {target.name} (size_cap_exceeded)",
            module_cap_exported=module_cap,
            cap_read_in_dispatch=cap_read,
        )
    if not install:
        return result(
            False,
            "cap_not_installed",
            target,
            f"{CAP_NAME} is not yet read in _attach_zip_via_datatransfer in {target.name} "
            f"(exported in {RELIABILITY_MODULE.name}={module_cap}). "
            f"Re-run with --apply to wire {CAP_NAME} into _attach_zip_via_datatransfer.",
            module_cap_exported=module_cap,
            cap_read_in_dispatch=cap_read,
        )
    if dry_run:
        return result(False, "would_change", target, f"would wire {CAP_NAME} into {target.name}")

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
        description="Verify (or optionally wire) the MAX_BASE64_ATTACH_BYTES archive size cap")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check the status without writing")
    parser.add_argument("--apply", action="store_true",
                        help="Wire MAX_BASE64_ATTACH_BYTES into _attach_zip_via_datatransfer if missing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run, install=args.apply)
    report("patch_bridge_large_archive", res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
