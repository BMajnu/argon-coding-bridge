#!/usr/bin/env python3
"""Shared, crash-safe helpers for the bridge's re-apply patch scripts.

WHY THIS EXISTS (Round 24)
--------------------------
Three of the four `patch_bridge_*.py` scripts could KILL the bridge when they were
re-applied to the current tree:

  * `patch_bridge_attach_images.py` appended a SECOND `--attach-images` argparse option
    -> `argparse.ArgumentError: argument --attach-images: conflicting option string`
  * `patch_bridge_cdp_setfiles.py` wrote a helper whose inner double quotes were not
    escaped -> `SyntaxError: invalid syntax. Perhaps you forgot a comma?`
  * `patch_bridge_large_archive.py` silently changed the archive attach route.

The root cause is the same in every case: the scripts were written against an OLD
inline implementation, while the fixes now live in `round23_reliability.py`, so their
"is it already applied?" markers never matched - and a broken guard means "apply again",
not "do nothing".

THE RULES THIS MODULE ENFORCES
-------------------------------
1. A patch script NEVER writes to the target directly. It writes a temp file, compiles
   it, and only then swaps it in (atomic). If the patched file does not compile, the
   target is left untouched and the script reports the failure.
2. The original line endings are preserved byte for byte (the repo files are LF).
3. Every script reports WHERE the fix is supposed to live, and checks there - not in a
   file the fix moved out of years ago.
"""
from __future__ import annotations

import os
import py_compile
import shutil
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
RELIABILITY_MODULE = SCRIPTS_DIR / "round23_reliability.py"
DISPATCH_MODULE = SCRIPTS_DIR / "dispatch_argon.py"


def read_source(path: Path) -> Tuple[str, str, bool]:
    """Return `(raw_text, normalized_text, use_crlf)` for `path`.

    `normalized_text` uses \\n only, so the anchors in the patch scripts match
    regardless of the file's line endings. `use_crlf` tells the writer which
    convention to restore.
    """
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return text, text.replace("\r\n", "\n"), crlf > lf


def write_verified(path: Path, new_text: str, use_crlf: bool, compile_check: bool = True) -> Tuple[bool, str]:
    """Write `new_text` to `path` only when the result is verified to be usable.

    The target is never touched until the patched copy compiles. Returns `(ok, detail)`.
    """
    path = Path(path)
    out_text = new_text.replace("\n", "\r\n") if use_crlf else new_text
    tmp = path.with_name(path.name + ".pathtmp")
    backup = path.with_name(path.name + ".pathbak")
    try:
        tmp.write_bytes(out_text.encode("utf-8"))
        if compile_check:
            try:
                py_compile.compile(str(tmp), doraise=True)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                return False, f"the patched file does not compile, nothing was written: {exc}"
        shutil.copy2(path, backup)
        os.replace(str(tmp), str(path))
        backup.unlink(missing_ok=True)
        return True, "written"
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        return False, f"could not write {path}: {exc}"


def count_occurrences(text: str, needle: str) -> int:
    return text.count(needle)


def module_has(reliability_path: Optional[Path], *needles: str) -> bool:
    """True when EVERY needle is present in the given module."""
    p = Path(reliability_path or RELIABILITY_MODULE)
    if not p.is_file():
        return False
    text = p.read_text(encoding="utf-8")
    return all(n in text for n in needles)


def result(
    ok: bool,
    status: str,
    target: Path,
    detail: str = "",
    **extra: object,
) -> Dict[str, object]:
    payload: Dict[str, object] = {
        "ok": ok,
        "status": status,
        "target": str(target),
        "detail": detail,
    }
    payload.update(extra)
    return payload


def report(name: str, rep: Dict[str, object]) -> None:
    detail = rep.get("detail") or ""
    print(f"[{name}] status={rep.get('status')} ok={rep.get('ok')} target={rep.get('target')}"
          + (f" | {detail}" if detail else ""))


def apply_to_target(
    target: Path,
    transform: Callable[[str], str],
    compile_check: bool = True,
) -> Tuple[str, str]:
    """Run `transform` on the target's normalized text.

    Returns `(status, detail)` where status is one of
    `already_applied`, `applied`, `failed`.
    """
    target = Path(target)
    if not target.is_file():
        return "missing_target", f"{target} does not exist"
    raw, norm, use_crlf = read_source(target)
    new_norm = transform(norm)
    if new_norm == norm:
        return "already_applied", "the target already has this fix"
    ok, detail = write_verified(target, new_norm, use_crlf, compile_check=compile_check)
    if not ok:
        return "failed", detail
    return "applied", f"{target.name}: {len(raw):,} -> {len(new_norm.encode('utf-8')):,} bytes"
