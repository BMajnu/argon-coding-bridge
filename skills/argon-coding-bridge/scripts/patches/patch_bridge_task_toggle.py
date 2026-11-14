#!/usr/bin/env python3
"""Idempotent re-appliable patch for the Chat/Task toggle detection in `dispatch_argon.py` (R3 / F3).

THE BUG
--------
`_task_toggle_present()` looked for the text "Task" ANYWHERE on the page and accepted the
first visible match. The chat transcript, a code block or a sidebar entry that merely
contains the word "Task" was therefore read as the mode toggle, so the dispatcher could
believe it was in Task mode when it was not - and refuse to send with
`Mode = Task : FAILED (observed='chat')`.

THE FIX
-------
A match only counts when it is inside a real segmented toggle control. `_is_toggle_control()`
answers that, and it already ships with the bridge (defined in `round23_reliability.py` and
imported by `dispatch_argon.py`), so this script only has to USE it - in the mode detection
and NOWHERE ELSE.

    Applying the toggle filter to the chat-title lookup or to the guest-page detection is
    wrong: the first would blank the conversation title, and the second would stop the
    bridge from recognising a guest page (Gemini would then answer "argon_context.zip did
    not come through").

Running this script is idempotent: it verifies the fix is in place, and applies it only if
it is missing.
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

# The ONLY place the toggle filter belongs: the mode detection.
OLD_TOGGLE_BODY = """        task_btn = page.get_by_text(RE_TASK)
        for i in range(min(task_btn.count(), 6)):
            if _safe_visible(task_btn.nth(i)):
                return True"""
NEW_TOGGLE_BODY = """        task_btn = page.get_by_text(RE_TASK)
        for i in range(min(task_btn.count(), 6)):
            el = task_btn.nth(i)
            # Round 23 (R3/F3): the word "Task" also appears in the chat transcript,
            # in code blocks and in the sidebar. Only a real segmented toggle control
            # counts, otherwise the dispatcher reads "chat" where it should read "task".
            if _safe_visible(el) and _is_toggle_control(el):
                return True"""

HELPER_SNIPPET = '''def _is_toggle_control(el) -> bool:
    """Return True ONLY when `el` is inside an interactive Chat/Task segmented toggle control (R3/F3: patch_bridge_task_toggle)."""
    if el is None:
        return False
    if _inside_navigation(el):
        return False
    if not hasattr(el, "evaluate"):
        return True
    try:
        return bool(
            el.evaluate(
                """e => {
                    let n = e;
                    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
                        const tag = (n.tagName || '').toLowerCase();
                        const role = (n.getAttribute && (n.getAttribute('role') || '')).toLowerCase();
                        const cls = (n.getAttribute && (n.getAttribute('class') || '')).toLowerCase();
                        const id = (n.id || '').toLowerCase();
                        const ap = n.getAttribute && n.getAttribute('aria-pressed');
                        const asel = n.getAttribute && n.getAttribute('aria-selected');
                        if (
                            tag === 'button' ||
                            tag === 'mat-button-toggle' ||
                            role === 'tab' ||
                            role === 'radio' ||
                            role === 'button' ||
                            role === 'group' ||
                            ap !== null ||
                            asel !== null ||
                            /segmented|mode-pill|mode-toggle|button-toggle|input-area/i.test(cls + ' ' + id)
                        ) {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return True


'''


def apply_patch(target_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    target = Path(target_path or DEFAULT_TARGET).resolve()
    if not target.is_file():
        return result(False, "missing_target", target)
    _, raw, _ = read_source(target)

    already = (OLD_TOGGLE_BODY not in raw) and (NEW_TOGGLE_BODY in raw)
    helper_in_module = module_has(RELIABILITY_MODULE, "def _is_toggle_control(")
    helper_present = (
        ("def _is_toggle_control(" in raw)
        or ("_is_toggle_control," in raw)
        or helper_in_module
    )
    if already and helper_present:
        return result(
            True,
            "already_applied",
            target,
            f"_task_toggle_present() already uses the toggle filter; "
            f"helper in {RELIABILITY_MODULE.name}: {helper_in_module}",
        )
    if OLD_TOGGLE_BODY not in raw and NEW_TOGGLE_BODY not in raw:
        return result(False, "anchor_not_found", target,
                      "neither the old nor the new _task_toggle_present() body was found")

    def transform(text: str) -> str:
        updated = text
        if OLD_TOGGLE_BODY in updated:
            updated = updated.replace(OLD_TOGGLE_BODY, NEW_TOGGLE_BODY, 1)
        if not helper_present:
            anchor = "def _mode_is_task(page) -> bool:"
            if anchor in updated:
                updated = updated.replace(anchor, HELPER_SNIPPET + anchor, 1)
        return updated

    if dry_run:
        return result(False, "would_change", target, "the toggle filter is not applied yet")

    status, detail = apply_to_target(target, transform)
    if status == "failed":
        return result(False, status, target, detail)
    return result(True, status, target, detail)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply or verify the Chat/Task toggle-detection patch on dispatch_argon.py")
    parser.add_argument("--target", default=str(DEFAULT_TARGET), help="Path to dispatch_argon.py")
    parser.add_argument("--dry-run", action="store_true", help="Check the status without writing")
    args = parser.parse_args()
    res = apply_patch(Path(args.target), dry_run=args.dry_run)
    print(f"[patch_bridge_task_toggle] status={res.get('status')} ok={res.get('ok')} target={res.get('target')}")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
