#!/usr/bin/env python3
"""Re-appliable patch for the `--attach-images` CLI option and the image-attach helper (R3 / F3).

THE BUG THIS SCRIPT USED TO HAVE (Round 24)
-------------------------------------------
Its guard was `if "def attach_images(" in raw and '"--attach-images"' in raw`.
The helper is really called `attach_images_to_composer` (it lives in
`round23_reliability.py`), so `def attach_images(` never matched, the guard never
fired, and the script appended a SECOND `--attach-images` option to
`dispatch_argon.py`. Re-running it after a plugin update therefore killed the
bridge with:

    argparse.ArgumentError: argument --attach-images: conflicting option string: --attach-images

MEASURED (2026-11-14): `--help` rc=0 before the patch, rc=1 after.

WHAT IT DOES NOW
----------------
1. The `--attach-images` option is added AT MOST ONCE. The script counts the real
   `parser.add_argument(...)` calls that register it and removes any duplicate.
2. The write is verified: the patched file is compiled before it replaces the target.
3. The helper is checked where it actually lives (`round23_reliability.py`).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

OPTION = "--attach-images"
HELPER_MARKERS = ("def attach_images_to_composer(", "attach_images_to_composer")


def _add_argument_calls(text: str) -> List[Tuple[int, int]]:
    """Spans of every `parser.add_argument(...)` call, found by paren balancing."""
    spans: List[Tuple[int, int]] = []
    for m in re.finditer(r"parser\.add_argument\s*\(", text):
        i = m.end()
        depth = 1
        in_str: Optional[str] = None
        while i < len(text) and depth:
            ch = text[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            elif ch in "\"'":
                in_str = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth == 0:
            spans.append((m.start(), i))
    return spans


def _option_calls(text: str) -> List[Tuple[int, int]]:
    return [s for s in _add_argument_calls(text) if f'"{OPTION}"' in text[s[0]:s[1]]]


def _remove_option_calls(text: str, spans: List[Tuple[int, int]]) -> str:
    """Remove whole `parser.add_argument(...)` calls, including their own line's indent.

    Removing only `start..end` would leave the call's leading indentation behind and
    glue it onto the NEXT call (which then fails to compile).
    """
    for start, end in reversed(spans):
        # the tail: trailing blanks and the newline that ends the call's line
        tail = end
        while tail < len(text) and text[tail] in " \t":
            tail += 1
        whole_line = tail < len(text) and text[tail] == "\n"
        if whole_line:
            tail += 1
        # the head: the indentation of the call's own line
        head = start
        while head > 0 and text[head - 1] in " \t":
            head -= 1
        own_line = whole_line and (head == 0 or text[head - 1] == "\n")
        if own_line:
            text = text[:head] + text[tail:]
        else:
            text = text[:start] + text[tail:]
    return text


def _add_option(text: str) -> str:
    anchor = '    parser.add_argument("--files", nargs="*", default=None, help="Optional target file patterns")'
    if anchor not in text:
        return text
    addition = (
        anchor
        + '\n    parser.add_argument("--attach-images", nargs="*", default=None, metavar="IMAGE", '
        'help="Optional PNG/JPEG/WebP image file paths to inject into the composer.")'
    )
    return text.replace(anchor, addition, 1)


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()
    helper_ok = module_has(RELIABILITY_MODULE, *HELPER_MARKERS)

    if dry_run:
        _, norm, _ = read_source(target)
        calls = _option_calls(norm)
        return result(
            ok=helper_ok and len(calls) == 1,
            status="already_applied" if (helper_ok and len(calls) == 1) else "would_change",
            target=target,
            detail=f"option registrations={len(calls)}, helper in round23_reliability.py={helper_ok}",
        )

    state: Dict[str, Any] = {}

    def transform(text: str) -> str:
        calls = _option_calls(text)
        state["before"] = len(calls)
        if len(calls) > 1:
            # repair: keep the first registration, drop the duplicates
            text = _remove_option_calls(text, calls[1:])
            state["repaired"] = len(calls) - 1
            return text
        if len(calls) == 1:
            state["repaired"] = 0
            return text
        return _add_option(text)

    status, detail = apply_to_target(target, transform)
    if status == "failed":
        return result(False, status, target, detail)

    _, norm, _ = read_source(target)
    after = len(_option_calls(norm))
    return result(
        ok=after == 1 and helper_ok,
        status=status if status != "already_applied" else status,
        target=target,
        detail=detail + f" | option registrations now: {after} | helper present: {helper_ok}",
        repaired=state.get("repaired", 0),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply or verify the --attach-images patch on dispatch_argon.py")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check status without writing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run)
    report("patch_bridge_attach_images", res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
