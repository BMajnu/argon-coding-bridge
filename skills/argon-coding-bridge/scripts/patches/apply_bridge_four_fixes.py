#!/usr/bin/env python3
"""Re-appliable patch script for the four core Argon bridge fixes (Round 23 Rule R3).

Lands and preserves inside this repository the four fixes that previously lived only as
external brief scripts in `G:/Developing/Penflow/Penflow-WordCraft-Rust-Agent-Studio/.argon_backup/briefs/`:
  1. `--attach-images` CLI flag and `attach_images_to_composer()` support.
  2. CDP `DOM.setFileInputFiles` zero-copy file attachment (`_attach_zip_via_cdp_dom_file_input`)
     for large archives (such as a 47 MB archive) without OS file dialogs.
  3. Base64 `DataTransfer` size cap (`MAX_BASE64_ATTACH_BYTES = 8 * 1024 * 1024`) so archives
     over 8 MB never base64-encode tens of megabytes into `page.evaluate`.
  4. `_is_toggle_control(el)` so Chat/Task mode detection and switching only match real
     segmented mode toggle controls and never false-match chat bubbles, code blocks, or sidebar items.

Running this script is idempotent: if a plugin self-update ever overwrites `dispatch_argon.py`,
running `python skills/argon-coding-bridge/scripts/patches/apply_bridge_four_fixes.py` re-applies
any missing fix in place and verifies syntax with `py_compile`.
"""

from __future__ import annotations

import py_compile
import sys
from pathlib import Path
from typing import Dict, Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DISPATCH_PATH = SCRIPTS_DIR / "dispatch_argon.py"


def check_four_fixes_status(dispatch_text: str) -> Dict[str, bool]:
    return {
        "attach_images_flag": "--attach-images" in dispatch_text,
        "cdp_dom_set_file_input_files": "DOM.setFileInputFiles" in dispatch_text or "_attach_zip_via_cdp_dom_file_input" in dispatch_text,
        "base64_size_cap": "MAX_BASE64_ATTACH_BYTES" in dispatch_text,
        "is_toggle_control": "_is_toggle_control" in dispatch_text,
    }


def ensure_four_fixes_in_dispatch(dispatch_path: Path = DISPATCH_PATH) -> Dict[str, Any]:
    if not dispatch_path.is_file():
        raise FileNotFoundError(f"dispatch_argon.py not found at {dispatch_path}")

    raw = dispatch_path.read_bytes()
    has_crlf = b"\r\n" in raw
    text = raw.decode("utf-8").replace("\r\n", "\n")
    before_status = check_four_fixes_status(text)
    applied = []

    if "install_round23_bridge_hooks(globals())" not in text:
        anchor = "def dispatch_to_argon("
        if anchor in text:
            hook_block = (
                "try:\n"
                "    from round23_reliability import (\n"
                "        MAX_BASE64_ATTACH_BYTES,\n"
                "        _attach_zip_via_cdp_dom_file_input,\n"
                "        _is_toggle_control,\n"
                "        attach_images_to_composer,\n"
                "        install_round23_bridge_hooks,\n"
                "    )\n"
                "    install_round23_bridge_hooks(globals())\n"
                "except Exception as _r23_hook_exc:\n"
                "    print(f'[ERROR] Failed to install Round 23 bridge reliability hooks: {_r23_hook_exc}', file=sys.stderr)\n"
                "    raise\n\n\n"
            )
            text = text.replace(anchor, hook_block + anchor, 1)
            applied.append("install_round23_bridge_hooks")

    if "--attach-images" not in text:
        anchor_arg = '        "--self-test",\n'
        if anchor_arg in text:
            attach_arg_block = (
                '        "--attach-images",\n'
                '        nargs="*",\n'
                '        default=None,\n'
                '        metavar="IMAGE",\n'
                '        help="Attach one or more image files (.png, .jpg, .webp, .svg) to the composer via CDP DOM.setFileInputFiles before sending.",\n'
                '    )\n'
                '    parser.add_argument(\n'
            )
            text = text.replace(anchor_arg, attach_arg_block + anchor_arg, 1)
            applied.append("--attach-images")

    if applied:
        out_bytes = (text.replace("\n", "\r\n") if has_crlf else text).encode("utf-8")
        dispatch_path.write_bytes(out_bytes)

    py_compile.compile(str(dispatch_path), doraise=True)
    after_status = check_four_fixes_status(dispatch_path.read_text(encoding="utf-8"))
    return {
        "ok": all(after_status.values()),
        "dispatch_path": str(dispatch_path),
        "before": before_status,
        "after": after_status,
        "applied": applied,
    }


def main() -> int:
    rep = ensure_four_fixes_in_dispatch(DISPATCH_PATH)
    print("=" * 78)
    print("  ARGON BRIDGE FOUR FIXES VERIFICATION & RE-APPLICATOR (ROUND 23 R3)")
    print("=" * 78)
    print(f"  Target file                   : {rep['dispatch_path']}")
    for k, v in rep["after"].items():
        print(f"  {k:<30}: {'PRESENT' if v else 'MISSING'}")
    print(f"  Applied in this run           : {rep['applied'] or 'none (already present)'}")
    print("=" * 78)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())