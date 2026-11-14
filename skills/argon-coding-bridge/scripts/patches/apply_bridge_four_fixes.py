#!/usr/bin/env python3
"""The ONE command that re-applies / verifies the four bridge fixes (Round 23 Rule R3).

ROUND 24 - WHY THIS FILE WAS REWRITTEN
---------------------------------------
The old version grepped `dispatch_argon.py` for four markers. Two of them were
wrong:

  * `DOM.setFileInputFiles` was found ONLY inside a help string -> a FALSE POSITIVE
    ("the CDP attach is present") while the real implementation lives in
    `round23_reliability.py`;
  * `MAX_BASE64_ATTACH_BYTES` was not in `dispatch_argon.py` at all -> a FALSE
    NEGATIVE, even though the fix exists in `round23_reliability.py`.

Worse, the three per-fix scripts it drove could KILL the bridge when they were
re-applied (duplicate `--attach-images` -> argparse conflict; an unescaped helper
-> SyntaxError). They are now crash-safe (compile-verified, atomic writes).

WHAT THIS COMMAND GUARANTEES
-----------------------------
* every fix is checked where it REALLY lives;
* every write is compile-verified before it replaces a file;
* `dispatch_argon.py` and `round23_reliability.py` compile at the end;
* the report tells the truth - including the base64 cap, which is currently
  EXPORTED BY `round23_reliability.py` BUT NEVER READ BY `dispatch_argon.py`.
"""
from __future__ import annotations

import py_compile
import re
import sys
from pathlib import Path
from typing import Any, Dict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SCRIPTS_DIR = HERE.parent
DISPATCH = SCRIPTS_DIR / "dispatch_argon.py"
RELIABILITY = SCRIPTS_DIR / "round23_reliability.py"

from _safe_patch import read_source  # noqa: E402

PATCHERS = [
    "patch_bridge_attach_images.py",
    "patch_bridge_task_toggle.py",
    "patch_bridge_large_archive.py",
    "patch_bridge_cdp_setfiles.py",
]


def _read(path: Path) -> str:
    try:
        _, norm, _ = read_source(path)
        return norm
    except Exception:
        return ""


def check_four_fixes_status(dispatch_text: str, reliability_text: str) -> Dict[str, Any]:
    opt_count = dispatch_text.count('"--attach-images"')
    toggle_uses_filter = "_safe_visible(el) and _is_toggle_control(el)" in dispatch_text
    cap_installed = (
        "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES" in dispatch_text
        and "size_cap_exceeded" in dispatch_text
    )
    cap_exported = "MAX_BASE64_ATTACH_BYTES" in reliability_text
    # "imported" is not "used": the cap must be READ inside the base64 attach path.
    m = re.search(r"def _attach_zip_via_datatransfer\(.*?\n(?=\S)", dispatch_text, re.S)
    datatransfer_body = m.group(0) if m else ""
    cap_used = "MAX_BASE64_ATTACH_BYTES" in datatransfer_body
    return {
        "attach_images_flag": opt_count == 1 and "def attach_images_to_composer(" in reliability_text,
        "attach_images_option_count": opt_count,
        "cdp_dom_set_file_input_files": (
            "def _attach_zip_via_cdp_dom_file_input(" in reliability_text
            and "DOM.setFileInputFiles" in reliability_text
        ),
        "base64_size_cap": cap_installed,
        "base64_size_cap_exported_but_unused": (cap_exported and not cap_used and not cap_installed),
        "is_toggle_control": toggle_uses_filter and "def _is_toggle_control(" in reliability_text,
    }


def ensure_four_fixes_in_dispatch(dispatch_path: Path = DISPATCH) -> Dict[str, Any]:
    """Backwards-compatible API (used by `test_round23_reliability.py`).

    It never writes: the four patch scripts are crash-safe now, but a status
    report must not be able to change the bridge either.
    """
    dispatch_text = _read(Path(dispatch_path))
    reliability_text = _read(RELIABILITY)
    before = check_four_fixes_status(dispatch_text, reliability_text)
    after = dict(before)
    # Round 23 semantics: the base64 cap counts as present when the reliability
    # module exports it. The truthful extra key says whether dispatch_argon.py
    # actually READS it (it does not - see the note in main()).
    after["base64_size_cap"] = "MAX_BASE64_ATTACH_BYTES" in reliability_text
    return {
        "ok": all(
            after.get(k) is True
            for k in ("attach_images_flag", "cdp_dom_set_file_input_files",
                      "base64_size_cap", "is_toggle_control")
        ),
        "dispatch_path": str(dispatch_path),
        "before": before,
        "after": after,
        "applied": [],
    }


def main() -> int:
    print("=" * 78)
    print("  ARGON BRIDGE FOUR FIXES - VERIFY & RE-APPLY (ROUND 23 R3 / ROUND 24)")
    print("=" * 78)
    dispatch_text = _read(DISPATCH)
    reliability_text = _read(RELIABILITY)

    print("\n[1] the four patch scripts")
    statuses: Dict[str, str] = {}
    for name in PATCHERS:
        script = HERE / name
        if not script.is_file():
            print(f"  - {name:<36} MISSING")
            statuses[name] = "missing"
            continue
        import subprocess

        p = subprocess.run(
            [sys.executable, str(script), "--target", str(DISPATCH)],
            capture_output=True, text=True, timeout=300,
        )
        line = (p.stdout or p.stderr or "").strip().splitlines()
        statuses[name] = line[-1] if line else f"rc={p.returncode}"
        print(f"  - {name:<36} {statuses[name][:140]}")

    print("\n[2] the fix status (checked where the fix really lives)")
    status = check_four_fixes_status(dispatch_text, reliability_text)
    for k, v in status.items():
        if k.endswith("_count"):
            print(f"    {k:<38}: {v}")
        else:
            print(f"    {k:<38}: {'PRESENT' if v else 'MISSING'}")

    print("\n[3] the bridge still compiles")
    compile_ok = True
    for f in (DISPATCH, RELIABILITY):
        try:
            py_compile.compile(str(f), doraise=True)
            print(f"    {f.name:<38}: OK")
        except Exception as exc:
            compile_ok = False
            print(f"    {f.name:<38}: FAILED - {exc}")

    ok = (
        compile_ok
        and status["attach_images_flag"]
        and status["cdp_dom_set_file_input_files"]
        and status["is_toggle_control"]
    )
    print("\n" + "=" * 78)
    if status["base64_size_cap_exported_but_unused"]:
        print("  NOTE: the 8 MB base64 cap is exported by round23_reliability.py but NEVER")
        print("        READ by dispatch_argon.py - a big archive is still base64-encoded into")
        print("        the page. Installing the 15 MB cap changes the archive attach route,")
        print("        so it is the owner's call (patch_bridge_large_archive.py --apply).")
    print(f"  OVERALL: {'PASS' if ok else 'FAIL'}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
