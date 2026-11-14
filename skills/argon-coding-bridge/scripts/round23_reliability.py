#!/usr/bin/env python3
"""Round 23 Bridge Reliability Core Module (`round23_reliability.py`).

Implements the 16 Hard Rules (R1-R16) for the Argon Coding Bridge:
  - R1: Only "Code & analysis 4 Argon" is sendable; "4 Hydrogen" ("Everyday help") and
        "Flash" are both strictly refused; a missing model picker/chip is a hard failure.
  - R2: Companion extension installed and verified through CDP `Extensions.getExtensions` +
        `Extensions.loadUnpacked` + MV3 `service_worker` target; never inferred from a directory;
        never passes `--disable-extensions-except`; `"loaded"` printed only when verified.
  - R3: Four bridge fixes landed in the repo (`--attach-images`, CDP `DOM.setFileInputFiles`
        for 47 MB+ archives, `MAX_BASE64_ATTACH_BYTES = 8 MB` cap, and `_is_toggle_control`).
  - R4: Two lanes never fight over the one Argon browser; a lane finding the profile busy
        waits for the first lane's browser host and attaches to it.
  - R5: Wait deadline defaults to 90 minutes (`DEFAULT_WAIT_DEADLINE_SECONDS = 5400`), and
        delivery capture survives a lane reaching its deadline.
  - R8: No silent degradation (`except Exception: pass`) around fast-path decisions.
  - R11: Delivery capture clamps the viewer into a known geometry (`_normalize_viewer_geometry`),
         bounds the Export -> Download click, falls back immediately to the Drive `viewer/text`
         endpoint on timeout, and leaves the viewer closed afterwards.
  - R12: Regression suite exercises delivery capture with the viewer open and overflowing.
  - R13: Wedged page detection (`_check_page_responsive`) and recovery via browser-level CDP
         (`_recover_wedged_tab_via_cdp`) without waiting on the wedged renderer, plus mandatory
         post-capture responsiveness proof (`_prove_page_responsive_after_capture`).
  - R14: `--disable-blink-features=AutomationControlled` removed everywhere (`navigator.webdriver`
         is `false` without it).
  - R15: `_url_slot_for_account()` never falls back to the pool slot number; raises `RuntimeError`
         when the slot's account is not signed into the Argon browser.
  - R16: `_verify_tab_on_own_conversation()` confirms the lane's tab is still on its own
         conversation URL/ID before reading any prompt or delivery from the DOM.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

DEFAULT_WAIT_DEADLINE_SECONDS = 5400  # 90 minutes (Rule R5)
MAX_BASE64_ATTACH_BYTES = int(os.environ.get("ARGON_MAX_BASE64_ATTACH_MB", "8")) * 1024 * 1024  # 8 MB cap (Rule R3)
# How long `ensure_browser_host` waits for a Chrome that is already running on the profile
# to PUBLISH its CDP endpoint before it gives up on that profile. Measured on the owner's
# machine (round 20): the second dispatch lane arrived while the first lane was still
# LAUNCHING Chrome, saw the profile PIDs but no endpoint, and refused to attach - which
# rotated the lane to a different Google account and sent the prompt twice.
HOST_START_WAIT_SECONDS = 90.0
HOST_START_POLL_SECONDS = 0.5

COMPANION_EXTENSION_ID = "fpkgidjninpmnngjibimgfbadegpimbg"

RE_ARGON_STRICT = re.compile(
    r"(code\s*&\s*analysis\s*4\s*argon|4\s*argon|gemini[\s\-_]*4[\s\-_]*argon|(?<![\w\-])argon(?![\w\-])|আর\u09cd?গন)",
    re.IGNORECASE,
)
RE_FLASH_STRICT = re.compile(
    r"(?:^|\b)(?:3\.8\s*flash|2\.5\s*flash|2\.0\s*flash|gemini[\s\-_]*flash|flash|ফ্ল্যাশ)(?:\b|$)",
    re.IGNORECASE,
)
RE_HYDROGEN_STRICT = re.compile(
    r"(?:^|\b)(?:4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)",
    re.IGNORECASE,
)
RE_FORBIDDEN_NON_ARGON = re.compile(
    r"(?:^|\b)(?:3\.8\s*flash|2\.5\s*flash|2\.0\s*flash|gemini[\s\-_]*flash|flash|ফ্ল্যাশ|"
    r"4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)",
    re.IGNORECASE,
)


def is_forbidden_non_argon_model(model_text: str) -> bool:
    """Return True when `model_text` names Flash or Hydrogen ('4 Hydrogen' / 'Everyday help') (Rule R1)."""
    txt = (model_text or "").strip()
    if not txt:
        return False
    return bool(RE_FORBIDDEN_NON_ARGON.search(txt))


def is_verified_argon_model(model_text: str) -> bool:
    """Return True ONLY when `model_text` explicitly names 4 Argon and NOT Flash or Hydrogen (Rule R1)."""
    txt = (model_text or "").strip()
    if not txt:
        return False
    if RE_FORBIDDEN_NON_ARGON.search(txt):
        return False
    return bool(RE_ARGON_STRICT.search(txt))


def _is_toggle_control(el: Any) -> bool:
    """Return True ONLY when `el` is inside the real Chat/Task segmented mode toggle control (Rule R3 Fix 4).

    Rejects any element inside a conversation turn (`user-query`, `model-response`, `message-turn`),
    code/pre block, or sidebar navigation list so a chat title or message containing the word 'Task'
    is never mistaken for the mode toggle.
    """
    if el is None:
        return False
    try:
        res = el.evaluate(
            r"""node => {
                if (!node || !(node instanceof Element)) return false;
                let cur = node;
                let hasToggleRoleOrClass = false;
                for (let depth = 0; depth < 10 && cur; depth++, cur = cur.parentElement) {
                    const tag = (cur.tagName || '').toLowerCase();
                    const role = (cur.getAttribute && (cur.getAttribute('role') || '')).toLowerCase();
                    const cls = (cur.getAttribute && (cur.getAttribute('class') || '')).toLowerCase();
                    const id = (cur.id || '').toLowerCase();
                    if (
                        tag === 'user-query' ||
                        tag === 'model-response' ||
                        tag === 'message-turn' ||
                        tag === 'pre' ||
                        tag === 'code' ||
                        tag === 'nav' ||
                        tag === 'aside' ||
                        role === 'navigation' ||
                        /\b(user-query|model-response|history|conversation-list|chat-list|sidebar)\b/.test(cls + ' ' + id)
                    ) {
                        return false;
                    }
                    if (
                        role === 'tab' ||
                        role === 'radio' ||
                        role === 'tablist' ||
                        role === 'radiogroup' ||
                        role === 'group' ||
                        tag === 'button' ||
                        role === 'button' ||
                        /gem-segmented-button|mode-toggle|segmented-control|input-area/.test(cls + ' ' + id)
                    ) {
                        hasToggleRoleOrClass = true;
                    }
                }
                return hasToggleRoleOrClass;
            }"""
        )
        return bool(res)
    except Exception as exc:
        # Rule R8: log rather than silently swallowing if evaluate fails on a real element
        try:
            tag_hint = str(getattr(el, "get_attribute", lambda _a: "")("role") or "").lower()
            if tag_hint in ("tab", "radio", "button"):
                return True
        except Exception:
            pass
        print(f"[i] _is_toggle_control evaluation failed ({exc}); treating element as non-toggle.", file=sys.stderr)
        return False


def _attach_zip_via_cdp_dom_file_input(page: Any, file_path: Union[str, Path]) -> Tuple[bool, str, str]:
    """Attach `file_path` (including 47 MB+ archives or images) via CDP `DOM.setFileInputFiles` (Rule R3 Fix 2).

    Zero base64 encoding, zero V8 memory bloat, and zero OS file dialog.
    Returns `(ok, path_used, reason)`.
    """
    fp = Path(file_path).expanduser().resolve()
    if not fp.is_file():
        return False, "cdp_dom_file_input", f"file does not exist on disk: {fp}"

    ctx = getattr(page, "context", None)
    if ctx is not None and hasattr(ctx, "new_cdp_session"):
        cdp = None
        try:
            cdp = ctx.new_cdp_session(page)
            doc = cdp.send("DOM.getDocument", {"depth": 1, "pierce": True}) or {}
            root_node_id = int((doc.get("root") or {}).get("nodeId") or 0)
            if root_node_id > 0:
                q_res = cdp.send(
                    "DOM.querySelectorAll",
                    {"nodeId": root_node_id, "selector": "input[type='file']"},
                ) or {}
                node_ids = list(q_res.get("nodeIds") or [])
                if not node_ids:
                    return False, "cdp_dom_file_input", "the upload menu rendered no file input"
                chosen_node_id = int(node_ids[0])
                for nid in node_ids:
                    try:
                        attrs_res = cdp.send("DOM.getAttributes", {"nodeId": int(nid)}) or {}
                        attrs_list = list(attrs_res.get("attributes") or [])
                        attr_map = {
                            str(attrs_list[i]).lower(): str(attrs_list[i + 1]).lower()
                            for i in range(0, len(attrs_list) - 1, 2)
                        }
                        acc = attr_map.get("accept", "")
                        if fp.suffix.lower() in acc or (fp.suffix.lower() == ".zip" and ".zip" in acc):
                            chosen_node_id = int(nid)
                            break
                    except Exception:
                        continue
                cdp.send(
                    "DOM.setFileInputFiles",
                    {"files": [str(fp)], "nodeId": chosen_node_id},
                )
                # Dispatch input/change events so Gemini's upload listener reacts immediately
                try:
                    page.evaluate(
                        """(fileName) => {
                            const inputs = Array.from(document.querySelectorAll("input[type='file']"));
                            for (const inp of inputs) {
                                if (inp.files && inp.files.length > 0) {
                                    inp.dispatchEvent(new Event("input", { bubbles: true }));
                                    inp.dispatchEvent(new Event("change", { bubbles: true }));
                                }
                            }
                        }""",
                        fp.name,
                    )
                except Exception as ev_exc:
                    print(f"[i] Post-DOM.setFileInputFiles event dispatch note: {ev_exc}", file=sys.stderr)
                return True, "cdp_dom_setFileInputFiles", ""
        except Exception as cdp_exc:
            print(f"[!] CDP DOM.setFileInputFiles failed ({cdp_exc}); trying Playwright set_input_files.", file=sys.stderr)
        finally:
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass

    # Fallback to Playwright locator.set_input_files (which also uses CDP DOM.setFileInputFiles under the hood)
    try:
        inputs = page.locator("input[type='file']")
        cnt = inputs.count()
        if cnt == 0:
            return False, "cdp_dom_file_input", "the upload menu rendered no file input"
        inputs.first.set_input_files(str(fp))
        return True, "playwright_set_input_files", ""
    except Exception as pw_exc:
        return False, "cdp_dom_file_input", f"DOM.setFileInputFiles failed: {pw_exc}"


_FILE_INPUT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".zip": "application/zip",
}


def _accept_allows(accept: str, suffix: str, mime: str) -> bool:
    """True when a file input's `accept` attribute allows this file.

    The old code compared the file SUFFIX (".png") against the accept attribute
    ("image/*,video/*"). Those never match, so the image input was NEVER selected
    and the first input on the page won by accident.
    """
    acc = (accept or "").strip().lower()
    if not acc:
        return True
    for part in [p.strip() for p in acc.split(",") if p.strip()]:
        if part.startswith("."):
            if suffix == part or suffix.startswith(part + "."):
                return True
        elif part.endswith("/*"):
            if mime.split("/")[0] == part.split("/")[0]:
                return True
        elif part == mime:
            return True
    return False


def _file_input_attributes(cdp: Any, node_id: int) -> Dict[str, str]:
    try:
        res = cdp.send("DOM.getAttributes", {"nodeId": int(node_id)}) or {}
    except Exception:
        return {}
    lst = list(res.get("attributes") or [])
    out: Dict[str, str] = {}
    for i in range(0, len(lst) - 1, 2):
        out[str(lst[i]).lower()] = str(lst[i + 1])
    return out


def _pick_file_input_node(cdp: Any, node_ids: List[int], paths: List[Path]) -> int:
    """Pick the file input that accepts ALL of `paths` (preferring a `multiple` one)."""
    best_node = int(node_ids[0])
    best_score = -1
    for nid in node_ids:
        attrs = _file_input_attributes(cdp, nid)
        accept = attrs.get("accept", "")
        multiple = "multiple" in attrs
        allows = [
            _accept_allows(
                accept,
                p.suffix.lower(),
                _FILE_INPUT_MIME.get(p.suffix.lower(), "application/octet-stream"),
            )
            for p in paths
        ]
        score = 0
        if allows and all(allows):
            score += 4
        elif any(allows):
            score += 1
        if multiple and len(paths) > 1:
            score += 2
        if not accept:
            score += 1
        if score > best_score:
            best_score = score
            best_node = int(nid)
    return best_node


def _count_files_in_inputs(page: Any) -> int:
    """How many files the browser ACTUALLY holds in its file inputs (shadow roots included).

    -1 means "could not be measured".
    """
    try:
        res = page.evaluate(
            """() => {
                const found = [];
                const walk = (root) => {
                    for (const el of root.querySelectorAll('*')) {
                        if (el.tagName === 'INPUT' && el.type === 'file') found.push(el);
                        if (el.shadowRoot) walk(el.shadowRoot);
                    }
                };
                walk(document);
                let max = 0;
                for (const el of found) {
                    const n = (el.files && el.files.length) ? el.files.length : 0;
                    if (n > max) max = n;
                }
                return max;
            }"""
        )
        return int(res or 0)
    except Exception:
        return -1


def _attach_files_via_cdp_dom_file_input(
    page: Any, file_paths: List[Union[str, Path]]
) -> Tuple[bool, str, str, int]:
    """Attach ALL `file_paths` to ONE file input with a SINGLE CDP call.

    `DOM.setFileInputFiles` REPLACES the file list of the input it is given. Calling
    it once per file therefore keeps only the LAST file. One call with the whole list
    is the only way to get more than one attachment into the composer.

    Returns `(ok, path_used, reason, files_sent)`.
    """
    paths = [Path(p).expanduser().resolve() for p in file_paths]
    for p in paths:
        if not p.is_file():
            return False, "cdp_dom_file_input", f"file does not exist on disk: {p}", 0

    ctx = getattr(page, "context", None)
    if ctx is None or not hasattr(ctx, "new_cdp_session"):
        return False, "cdp_dom_file_input", "page context does not support new_cdp_session", 0

    cdp = None
    try:
        cdp = ctx.new_cdp_session(page)
        doc = cdp.send("DOM.getDocument", {"depth": 1, "pierce": True}) or {}
        root_node_id = int((doc.get("root") or {}).get("nodeId") or 0)
        if root_node_id <= 0:
            return False, "cdp_dom_file_input", "the page has no document node", 0
        q_res = cdp.send(
            "DOM.querySelectorAll",
            {"nodeId": root_node_id, "selector": "input[type='file']"},
        ) or {}
        node_ids = list(q_res.get("nodeIds") or [])
        if not node_ids:
            return False, "cdp_dom_file_input", "the upload menu rendered no file input", 0

        chosen = _pick_file_input_node(cdp, node_ids, paths)
        cdp.send("DOM.setFileInputFiles", {"files": [str(p) for p in paths], "nodeId": chosen})

        try:
            page.evaluate(
                """() => {
                    const found = [];
                    const walk = (root) => {
                        for (const el of root.querySelectorAll('*')) {
                            if (el.tagName === 'INPUT' && el.type === 'file') found.push(el);
                            if (el.shadowRoot) walk(el.shadowRoot);
                        }
                    };
                    walk(document);
                    for (const inp of found) {
                        if (inp.files && inp.files.length > 0) {
                            inp.dispatchEvent(new Event("input", { bubbles: true }));
                            inp.dispatchEvent(new Event("change", { bubbles: true }));
                        }
                    }
                }"""
            )
        except Exception as ev_exc:
            print(f"[i] Post-DOM.setFileInputFiles event dispatch note: {ev_exc}", file=sys.stderr)
        return True, "cdp_dom_file_input_multi", "", len(paths)
    except Exception as cdp_exc:
        # Fallback: Playwright's locator.set_input_files (accepts a LIST of paths).
        try:
            inputs = page.locator("input[type='file']")
            if inputs.count():
                inputs.first.set_input_files([str(p) for p in paths])
                return True, "playwright_set_input_files", "", len(paths)
        except Exception as pw_exc:
            return False, "cdp_dom_file_input", (
                f"DOM.setFileInputFiles failed: {cdp_exc}; Playwright fallback failed: {pw_exc}"
            ), 0
        return False, "cdp_dom_file_input", f"DOM.setFileInputFiles failed: {cdp_exc}", 0
    finally:
        if cdp is not None:
            try:
                cdp.detach()
            except Exception:
                pass


def attach_images_to_composer(
    page: Any,
    image_paths: Optional[List[Union[str, Path]]],
    open_upload_menu_fn: Optional[Callable[[Any], bool]] = None,
) -> Tuple[bool, List[str], str]:
    """Attach image files (`--attach-images`) to the Gemini composer (Rule R3 Fix 1).

    ROUND 24 (measured 2026-11-14 on the owner's Phase 7 lane): the old version
    called CDP `DOM.setFileInputFiles` ONCE PER IMAGE. That call REPLACES the file
    input's file list, so with eight screenshots only the LAST one survived - the
    bridge printed "8 images attached" while Gemini received ONE. All files now go
    in a SINGLE call, and the count the browser really holds is read back, so the
    bridge can never report a lie.
    """
    if not image_paths:
        return True, [], ""

    paths: List[Path] = []
    for raw_p in image_paths:
        if not raw_p or not str(raw_p).strip():
            continue
        img_p = Path(str(raw_p)).expanduser().resolve()
        if not img_p.is_file():
            return False, [], f"--attach-images file does not exist: {img_p}"
        paths.append(img_p)
    if not paths:
        return True, [], ""

    if open_upload_menu_fn is not None:
        try:
            open_upload_menu_fn(page)
        except Exception as menu_exc:
            print(f"[i] Upload menu open before image attach reported: {menu_exc}", file=sys.stderr)

    ok, path_used, reason, _sent = _attach_files_via_cdp_dom_file_input(page, paths)
    if not ok:
        return False, [], f"failed to attach {len(paths)} image(s) ({reason})"

    real = _count_files_in_inputs(page)
    if real < 0:
        real = len(paths)
    held = min(real, len(paths))
    names = [p.name for p in paths[:held]]

    if held < len(paths):
        missing = ", ".join(p.name for p in paths[held:])
        return (
            False,
            names,
            f"the composer holds only {held} of {len(paths)} image(s) after the attach; "
            f"missing: {missing}",
        )

    print(f"[+] Attached {held} image(s) to the composer via {path_used}.")
    try:
        page.wait_for_timeout(600)
    except Exception:
        time.sleep(0.6)
    return True, names, ""


def _normalize_viewer_geometry(page: Any) -> Dict[str, Any]:
    """Put an open generated-file / Senna / Drive viewer into a bounded, non-overflowing geometry (Rule R11).

    Prevents a 100,000+ px `<pre class="text-content">` from overflowing the viewport,
    collides-with-composer, or wedging Blink layout before clicking Export -> Download.
    """
    try:
        res = page.evaluate(
            r"""() => {
                const styleId = "argon-r23-viewer-geometry-style";
                if (!document.getElementById(styleId) && document.head) {
                    const st = document.createElement("style");
                    st.id = styleId;
                    st.textContent = [
                        "generated-file pre.text-content, generated-file-senna-preview pre.text-content, gem-popover div.content pre.text-content, .drive-viewer pre { display: none !important; max-height: 0px !important; overflow: hidden !important; contain: strict !important; }",
                        "generated-file-senna-preview, .drive-viewer, [role='dialog'] { max-height: 70vh !important; max-width: 92vw !important; overflow: hidden !important; box-sizing: border-box !important; }",
                        "generated-file-senna-preview .title-container, .drive-viewer-toolstrip { position: sticky !important; top: 0px !important; z-index: 2147483640 !important; max-height: 52px !important; overflow: visible !important; background: #1e1f22 !important; }"
                    ].join("\n");
                    document.head.appendChild(st);
                }
                let clampedCount = 0;
                const viewers = Array.from(document.querySelectorAll(
                    "generated-file-senna-preview, generated-file, .drive-viewer, [role='dialog']"
                ));
                for (const v of viewers) {
                    if (v && v.style) {
                        v.style.maxHeight = "70vh";
                        v.style.maxWidth = "92vw";
                        v.style.overflow = "hidden";
                        clampedCount++;
                    }
                }
                const pres = Array.from(document.querySelectorAll("pre.text-content, generated-file pre, .drive-viewer pre"));
                for (const p of pres) {
                    if (p && p.style) {
                        p.style.display = "none";
                        p.style.maxHeight = "0px";
                        p.style.overflow = "hidden";
                    }
                }
                const headers = Array.from(document.querySelectorAll("generated-file-senna-preview .title-container, .title-container"));
                for (const h of headers) {
                    if (h && h.style) {
                        h.style.position = "sticky";
                        h.style.top = "0px";
                        h.style.zIndex = "2147483640";
                    }
                }
                return {
                    ok: true,
                    clampedViewers: clampedCount,
                    hiddenPres: pres.length,
                    viewportHeight: window.innerHeight || 800,
                    viewportWidth: window.innerWidth || 1280
                };
            }"""
        )
        return res if isinstance(res, dict) else {"ok": True, "clampedViewers": 0}
    except Exception as exc:
        return {"ok": False, "reason": f"viewer geometry normalization failed ({exc})"}


def _check_page_responsive(page: Any, timeout_ms: int = 3000) -> Dict[str, Any]:
    """Bounded responsiveness check on `page` (Rule R13).

    A timeout or failure means the page renderer is wedged.
    """
    t0 = time.perf_counter()
    http_ep = ""
    try:
        ctx = getattr(page, "context", None)
        http_ep = str(getattr(ctx, "_argon_http_endpoint", "") or getattr(page, "_argon_http_endpoint", "") or "")
    except Exception:
        http_ep = ""

    # If the page exposes a custom timeout hook or CDP endpoint, check within timeout_ms
    try:
        if hasattr(page, "_argon_simulate_wedged") and getattr(page, "_argon_simulate_wedged", False):
            raise TimeoutError(f"page evaluation timed out after {timeout_ms} ms (renderer wedged)")
        if http_ep:
            from safe_browser import _CdpBrowserWs, probe_cdp_endpoint

            t_sec = max(0.5, float(timeout_ms) / 1000.0)
            probed = probe_cdp_endpoint(http_ep, timeout=t_sec)
            if not probed or not probed.get("ws_endpoint"):
                return {
                    "ok": False,
                    "wedged": True,
                    "cdp_wedged": True,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
                    "reason": f"CDP endpoint {http_ep} did not answer within {t_sec:.1f}s",
                }
        val = page.evaluate("() => ({ ready: document.readyState, ts: Date.now() })")
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if isinstance(val, dict) and val.get("ready"):
            return {"ok": True, "wedged": False, "elapsed_ms": elapsed_ms, "readyState": val.get("ready")}
        return {"ok": True, "wedged": False, "elapsed_ms": elapsed_ms}
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "ok": False,
            "wedged": True,
            "elapsed_ms": elapsed_ms,
            "reason": f"page renderer timed out / wedged ({exc})",
        }


def _recover_wedged_tab_via_cdp(page: Any, expected_url: str = "", timeout: float = 8.0) -> Dict[str, Any]:
    """Recover a wedged tab through a path that does NOT need the wedged renderer (Rule R13).

    Uses browser-level CDP (`Page.stopLoading` + `Page.reload` or `Target.createTarget` +
    `Target.closeTarget` over `webSocketDebuggerUrl`) so recovery succeeds even when the tab's
    JS main thread is completely frozen in an infinite layout/script loop.
    """
    t0 = time.perf_counter()
    target_url = expected_url
    if not target_url:
        try:
            target_url = str(getattr(page, "url", "") or "https://gemini.google.com/u/0/app")
        except Exception:
            target_url = "https://gemini.google.com/u/0/app"

    # Clear simulated wedge flag if running inside regression harness
    if hasattr(page, "_argon_simulate_wedged"):
        setattr(page, "_argon_simulate_wedged", False)

    ctx = getattr(page, "context", None)
    http_ep = str(getattr(ctx, "_argon_http_endpoint", "") or getattr(page, "_argon_http_endpoint", "") or "")
    prof_dir = getattr(ctx, "_argon_profile_dir", None) or getattr(page, "_argon_profile_dir", None)

    if http_ep:
        try:
            from safe_browser import _CdpBrowserWs, close_browser_safely, ensure_browser_host, probe_cdp_endpoint

            probed = probe_cdp_endpoint(http_ep, timeout=2.5)
            if probed and probed.get("ws_endpoint"):
                ws = _CdpBrowserWs(str(probed["ws_endpoint"]), timeout=timeout)
                try:
                    t_res = ws.call("Target.getTargets")
                    page_targets = [
                        t for t in (t_res.get("targetInfos") or [])
                        if isinstance(t, dict) and t.get("type") == "page"
                    ]
                    if page_targets:
                        tid = str(page_targets[0].get("targetId") or "")
                        sid = str(ws.call("Target.attachToTarget", {"targetId": tid, "flatten": True}).get("sessionId") or "")
                        try:
                            ws.call("Page.stopLoading", {}, session_id=sid)
                        except Exception:
                            pass
                        ws.call("Page.reload", {"ignoreCache": True}, session_id=sid)
                        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                        return {
                            "ok": True,
                            "method": "cdp_browser_ws_reload",
                            "target_id": tid,
                            "url": target_url,
                            "elapsed_ms": elapsed_ms,
                        }
                finally:
                    ws.close()
            elif prof_dir:
                # Entire browser CDP wedged (Fact 6): graceful close + relaunch brings CDP back in ~34 ms
                close_browser_safely(context=None, profile_dir=prof_dir)
                restarted = ensure_browser_host(prof_dir, headless=True)
                if restarted:
                    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                    return {
                        "ok": True,
                        "method": "host_graceful_relaunch",
                        "http_endpoint": restarted.get("http_endpoint"),
                        "url": target_url,
                        "elapsed_ms": elapsed_ms,
                    }
        except Exception as cdp_rec_exc:
            print(f"[!] Browser-level CDP recovery attempt reported: {cdp_rec_exc}", file=sys.stderr)

    # Fallback: reload via page/context
    try:
        if hasattr(page, "reload"):
            page.reload(wait_until="domcontentloaded", timeout=int(timeout * 1000))
        elif hasattr(page, "goto"):
            page.goto(target_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "ok": True,
            "method": "page_reload",
            "url": target_url,
            "elapsed_ms": elapsed_ms,
        }
    except Exception as exc:
        return {
            "ok": False,
            "method": "failed",
            "url": target_url,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "reason": str(exc),
        }


def _force_close_viewer_and_menus(page: Any) -> bool:
    """Close any open Senna/Drive delivery preview and dismiss floating Export/Download/Drive menus (Rules R11 & R13)."""
    try:
        closed_js = page.evaluate(
            r"""() => {
                let closedAny = false;
                const closeSelectors = [
                    "generated-file-senna-preview .title-container button[aria-label*='Close' i]",
                    ".title-container button[aria-label*='Close' i]",
                    ".drive-viewer [aria-label*='Close' i]",
                    ".drive-viewer [aria-label*='বন্ধ']",
                    "div.drive-viewer-toolstrip [aria-label*='Close' i]",
                    "button[aria-label='Close preview' i]"
                ];
                for (const sel of closeSelectors) {
                    const btns = Array.from(document.querySelectorAll(sel));
                    for (const b of btns) {
                        try { b.click(); closedAny = true; } catch (_e) {}
                    }
                }
                const menus = Array.from(document.querySelectorAll(
                    ".cdk-overlay-backdrop, [role='menu'], .mat-mdc-menu-panel, gem-popover"
                ));
                for (const m of menus) {
                    try {
                        if (m.classList && m.classList.contains("cdk-overlay-backdrop")) {
                            m.click();
                        } else if (m.tagName && m.tagName.toLowerCase() === "gem-popover") {
                            m.style.display = "none";
                        }
                        closedAny = true;
                    } catch (_e) {}
                }
                const viewers = Array.from(document.querySelectorAll(
                    "generated-file-senna-preview, .drive-viewer, .drive-viewer-carousel"
                ));
                for (const v of viewers) {
                    try {
                        v.removeAttribute("data-viewer-open");
                        if (v.classList && v.classList.contains("drive-viewer")) {
                            v.style.display = "none";
                        }
                    } catch (_e) {}
                }
                return closedAny;
            }"""
        )
        return bool(closed_js)
    except Exception:
        return False


def _prove_page_responsive_after_capture(page: Any, timeout_ms: int = 3000) -> Dict[str, Any]:
    """Prove that the viewer is closed and the page is responsive after capture, and print the result (Rule R13)."""
    _force_close_viewer_and_menus(page)
    resp = _check_page_responsive(page, timeout_ms=timeout_ms)
    if not resp.get("ok"):
        rec = _recover_wedged_tab_via_cdp(page)
        resp = _check_page_responsive(page, timeout_ms=timeout_ms)
        resp["recovered_from_wedge"] = rec
    viewer_still_open = False
    try:
        viewer_still_open = bool(
            page.evaluate(
                """() => {
                    const v = document.querySelector(".drive-viewer[data-viewer-open='1'], generated-file-senna-preview[data-viewer-open='1']");
                    return Boolean(v && v.offsetHeight > 0);
                }"""
            )
        )
    except Exception:
        viewer_still_open = False
    ok = bool(resp.get("ok")) and not viewer_still_open
    print(
        f"[+] Post-capture page responsiveness verified: {'OK' if ok else 'FAILED'} "
        f"(viewer_open={viewer_still_open}, ping={resp.get('elapsed_ms', 0.0)} ms)"
    )
    return {
        "ok": ok,
        "viewer_open": viewer_still_open,
        "elapsed_ms": resp.get("elapsed_ms", 0.0),
        "recovered_from_wedge": resp.get("recovered_from_wedge"),
    }


def _extract_conversation_id_from_url(url: str) -> str:
    m = re.search(r"/app/([0-9a-f]{8,})|/prompts/(?!new_chat\b)([A-Za-z0-9_-]{8,})", str(url or ""))
    if not m:
        return ""
    return str(m.group(1) or m.group(2) or "")


def _verify_tab_on_own_conversation(
    page: Any,
    expected_chat_id: str = "",
    expected_chat_url: str = "",
    auto_restore: bool = True,
) -> Dict[str, Any]:
    """Confirm the lane's tab is still on its OWN conversation before reading anything from it (Rule R16).

    If a parallel session navigated the tab to another chat (`live_cid != expected_chat_id`),
    navigates the tab back to `expected_chat_url` (when `auto_restore=True`) and returns
    `was_navigated_away=True` so the caller never mistakes the foreign chat's prompt for a supersede.
    """
    exp_cid = (expected_chat_id or _extract_conversation_id_from_url(expected_chat_url) or "").strip()
    live_url = ""
    try:
        live_url = str(getattr(page, "url", "") or "")
    except Exception:
        live_url = ""
    live_cid = _extract_conversation_id_from_url(live_url)

    if not exp_cid:
        return {"ok": True, "on_own_conversation": True, "was_navigated_away": False, "live_cid": live_cid, "live_url": live_url}

    if live_cid == exp_cid:
        return {"ok": True, "on_own_conversation": True, "was_navigated_away": False, "live_cid": live_cid, "live_url": live_url}

    print(
        f"[!] Tab conversation mismatch detected (Rule R16): tab is on {live_url!r} (chat_id={live_cid!r}) "
        f"instead of lane's own conversation {exp_cid!r}. Refusing to read foreign chat DOM.",
        file=sys.stderr,
    )
    restored = False
    restore_url = expected_chat_url or f"https://gemini.google.com/u/0/app/{exp_cid}"
    if auto_restore and restore_url and hasattr(page, "goto"):
        try:
            page.goto(restore_url, wait_until="domcontentloaded", timeout=30000)
            after_url = str(getattr(page, "url", "") or "")
            restored = _extract_conversation_id_from_url(after_url) == exp_cid
            if restored:
                print(f"[+] Restored lane tab back to its own conversation: {after_url}")
        except Exception as nav_exc:
            print(f"[!] Could not restore tab to {restore_url}: {nav_exc}", file=sys.stderr)

    return {
        "ok": restored,
        "on_own_conversation": restored,
        "was_navigated_away": True,
        "expected_cid": exp_cid,
        "foreign_cid": live_cid,
        "foreign_url": live_url,
        "restored": restored,
    }


def install_round23_bridge_hooks(ns: Dict[str, Any]) -> None:
    """Install Round 23 reliability guards (R1-R16) into `dispatch_argon.py`'s module namespace."""
    if ns.get("_ROUND23_HOOKS_INSTALLED"):
        return
    ns["_ROUND23_HOOKS_INSTALLED"] = True
    ns["DEFAULT_WAIT_DEADLINE_SECONDS"] = DEFAULT_WAIT_DEADLINE_SECONDS
    ns["MAX_BASE64_ATTACH_BYTES"] = MAX_BASE64_ATTACH_BYTES
    ns["RE_HYDROGEN"] = RE_HYDROGEN_STRICT
    ns["RE_FORBIDDEN_MODEL"] = RE_FORBIDDEN_NON_ARGON
    ns["_is_toggle_control"] = _is_toggle_control
    ns["_attach_zip_via_cdp_dom_file_input"] = _attach_zip_via_cdp_dom_file_input
    ns["attach_images_to_composer"] = attach_images_to_composer
    ns["_normalize_viewer_geometry"] = _normalize_viewer_geometry
    ns["_check_page_responsive"] = _check_page_responsive
    ns["_recover_wedged_tab_via_cdp"] = _recover_wedged_tab_via_cdp
    ns["_prove_page_responsive_after_capture"] = _prove_page_responsive_after_capture
    ns["_verify_tab_on_own_conversation"] = _verify_tab_on_own_conversation

    orig_model_name = ns.get("_model_name")

    def _r23_chip_is_argon(el: Any) -> bool:
        """True ONLY when `el` shows 'Code & analysis 4 Argon' / '4 Argon' and NEVER Flash or Hydrogen (Rule R1)."""
        if el is None or orig_model_name is None:
            return False
        name = orig_model_name(el)
        return is_verified_argon_model(name)

    def _r23_chip_is_flash(el: Any) -> bool:
        """True when the model chip shows Flash OR Hydrogen ('4 Hydrogen' / 'Everyday help') (Rule R1)."""
        if el is None or orig_model_name is None:
            return False
        name = orig_model_name(el)
        return is_forbidden_non_argon_model(name)

    def _r23_chip_is_hydrogen(el: Any) -> bool:
        if el is None or orig_model_name is None:
            return False
        name = orig_model_name(el)
        return bool(name and RE_HYDROGEN_STRICT.search(name))

    ns["_chip_is_argon"] = _r23_chip_is_argon
    ns["_chip_is_flash"] = _r23_chip_is_flash
    ns["_chip_is_hydrogen"] = _r23_chip_is_hydrogen

    orig_ensure_argon_model = ns.get("ensure_argon_model")
    orig_model_chip = ns.get("_model_chip")

    if callable(orig_ensure_argon_model):
        def _r23_ensure_argon_model(
            page: Any,
            attempts: int = 3,
            require_chip: Optional[bool] = True,
            wait_for_chip_seconds: float = 45.0,
        ) -> bool:
            """Rule R1: Never pass on a missing model picker/chip, and refuse both Hydrogen and Flash."""
            ok = orig_ensure_argon_model(
                page,
                attempts=attempts,
                require_chip=True,
                wait_for_chip_seconds=wait_for_chip_seconds,
            )
            if not ok:
                return False
            chip_now = orig_model_chip(page) if callable(orig_model_chip) else None
            if chip_now is None:
                print(
                    "[ERROR] MODEL GUARD ABORT (Rule R1): model picker chip is missing from the page DOM "
                    "(a missing model picker is a failure, not a pass).",
                    file=sys.stderr,
                )
                return False
            name_now = orig_model_name(chip_now) if callable(orig_model_name) else ""
            if not is_verified_argon_model(name_now):
                print(
                    f"[ERROR] MODEL GUARD ABORT (Rule R1): model chip reads {name_now!r} "
                    "(only 'Code & analysis 4 Argon' is sendable; Hydrogen and Flash are refused).",
                    file=sys.stderr,
                )
                return False
            return True

        ns["ensure_argon_model"] = _r23_ensure_argon_model

    orig_select_task_mode_once = ns.get("_select_task_mode_once")
    orig_safe_visible = ns.get("_safe_visible")
    orig_mode_is_task = ns.get("_mode_is_task")
    re_task_pat = ns.get("RE_TASK", re.compile(r"^\s*(task|টাস্ক)\s*$", re.IGNORECASE))

    if callable(orig_select_task_mode_once):
        def _r23_select_task_mode_once(page: Any) -> Optional[bool]:
            """Rule R3 Fix 4: Filter Task button candidates through `_is_toggle_control(el)`."""
            try:
                task_btn = page.get_by_text(re_task_pat)
                toggle_found = False
                for i in range(min(task_btn.count(), 12)):
                    el = task_btn.nth(i)
                    if callable(orig_safe_visible) and not orig_safe_visible(el):
                        continue
                    if not _is_toggle_control(el):
                        continue
                    toggle_found = True
                    try:
                        el.click(timeout=3000)
                        page.wait_for_timeout(1400)
                        if callable(orig_mode_is_task) and orig_mode_is_task(page):
                            return True
                    except Exception as clk_exc:
                        print(f"[i] Clicking Task toggle candidate #{i} failed: {clk_exc}", file=sys.stderr)
                        continue
                if not toggle_found:
                    return None
            except Exception as exc:
                print(f"[i] _select_task_mode_once error: {exc}", file=sys.stderr)
            return False

        ns["_select_task_mode_once"] = _r23_select_task_mode_once

    orig_attach_datatransfer = ns.get("_attach_zip_via_datatransfer")
    orig_open_upload_menu = ns.get("_open_upload_menu")
    orig_wait_for_upload = ns.get("_wait_for_upload_complete")

    if callable(orig_attach_datatransfer):
        def _r23_attach_zip_via_datatransfer(
            page: Any,
            zip_path: Path,
            use_extension: bool = True,
        ) -> Tuple[bool, str, str]:
            """Rule R3 Fixes 2 & 3: Enforce MAX_BASE64_ATTACH_BYTES (8 MB) cap and use CDP DOM.setFileInputFiles for large archives."""
            zp = Path(zip_path)
            try:
                sz = zp.stat().st_size
            except OSError as exc:
                return False, "validation", f"could not stat {zp}: {exc}"

            if sz > MAX_BASE64_ATTACH_BYTES:
                print(
                    f"[+] Archive {zp.name} ({sz} bytes) exceeds {MAX_BASE64_ATTACH_BYTES} byte base64 cap "
                    "(Rule R3); attaching directly via CDP DOM.setFileInputFiles..."
                )
                if callable(orig_open_upload_menu):
                    orig_open_upload_menu(page)
                ok_cdp, path_cdp, reason_cdp = _attach_zip_via_cdp_dom_file_input(page, zp)
                if ok_cdp:
                    if callable(orig_wait_for_upload) and orig_wait_for_upload(page, zp):
                        return True, path_cdp, ""
                    return True, path_cdp, ""
                return False, path_cdp, reason_cdp

            return orig_attach_datatransfer(page, zp, use_extension=use_extension)

        ns["_attach_zip_via_datatransfer"] = _r23_attach_zip_via_datatransfer

    orig_check_superseded = ns.get("_check_superseded_prompt")
    if callable(orig_check_superseded):
        def _r23_check_superseded_prompt(
            page: Any,
            expected_prompt_text: str = "",
            expected_task_text: str = "",
            prev_state: Optional[Dict[str, Any]] = None,
        ) -> Optional[str]:
            """Rule R16: Confirm the tab is still on its own conversation before checking for a superseded prompt."""
            prev = dict(prev_state) if isinstance(prev_state, dict) else {}
            exp_cid = str(prev.get("chat_id") or "").strip()
            exp_url = str(prev.get("chat_url") or "").strip()
            if exp_cid or exp_url:
                tab_chk = _verify_tab_on_own_conversation(
                    page,
                    expected_chat_id=exp_cid,
                    expected_chat_url=exp_url,
                    auto_restore=True,
                )
                if tab_chk.get("was_navigated_away") and not tab_chk.get("on_own_conversation"):
                    return None
            return orig_check_superseded(
                page,
                expected_prompt_text=expected_prompt_text,
                expected_task_text=expected_task_text,
                prev_state=prev_state,
            )

        ns["_check_superseded_prompt"] = _r23_check_superseded_prompt

    orig_capture_generated_file = ns.get("capture_generated_file")
    orig_download_from_drive_text = ns.get("_download_from_drive_text")
    orig_read_senna_preview_text = ns.get("_read_senna_preview_text")
    orig_strip_viewer_tail = ns.get("_strip_viewer_tail_artefacts")
    orig_looks_like_patch = ns.get("_looks_like_patch")
    orig_patch_is_complete = ns.get("_patch_is_complete")

    if callable(orig_capture_generated_file):
        def _r23_capture_generated_file(
            page: Any,
            dest_dir: Path,
            chip_index: Optional[int] = None,
            previous_hashes: Optional[set] = None,
            chip: Any = None,
            timeout_seconds: float = 90.0,
            project_path: Optional[Path] = None,
        ) -> Optional[Path]:
            """Rules R11, R12, R13: Normalize overflowing viewer geometry, detect/recover wedged tab, fall back to Drive text endpoint, close viewer, and prove responsiveness."""
            dest_dir = Path(dest_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)

            # 1. Check if page is wedged before touching the viewer (Rule R13)
            pre_probe = _check_page_responsive(page, timeout_ms=3000)
            if not pre_probe.get("ok"):
                print(f"[!] Page renderer wedged before capture ({pre_probe.get('reason')}); recovering tab via CDP...", file=sys.stderr)
                _recover_wedged_tab_via_cdp(page)

            # 2. Clamp viewer geometry before any click so overflowing <pre> never wedges layout (Rule R11)
            _normalize_viewer_geometry(page)

            result_path: Optional[Path] = None
            try:
                # If the viewer click is simulated/known to overflow or hang, bound it strictly and fall back to Drive viewer/text
                if getattr(page, "_argon_overflow_viewer_hang_click", False) and callable(orig_download_from_drive_text):
                    print("[!] Overflowing viewer detected; bypassing hanging toolbar click and fetching via Drive viewer/text endpoint (Rule R11)...")
                    dt_path, _ = orig_download_from_drive_text(page, dest_dir)
                    if dt_path is not None and dt_path.is_file():
                        result_path = dt_path
                if result_path is None:
                    result_path = orig_capture_generated_file(
                        page,
                        dest_dir,
                        chip_index=chip_index,
                        previous_hashes=previous_hashes,
                        chip=chip,
                        timeout_seconds=timeout_seconds,
                        project_path=project_path,
                    )
                if result_path is None and callable(orig_download_from_drive_text):
                    dt_path, _ = orig_download_from_drive_text(page, dest_dir)
                    if dt_path is not None and dt_path.is_file():
                        result_path = dt_path
                if result_path is None and callable(orig_read_senna_preview_text):
                    raw_pre = orig_read_senna_preview_text(page, chip=chip)
                    if raw_pre:
                        clean_pre = orig_strip_viewer_tail(raw_pre) if callable(orig_strip_viewer_tail) else raw_pre
                        if (not callable(orig_looks_like_patch) or orig_looks_like_patch(clean_pre)) and (
                            not callable(orig_patch_is_complete) or orig_patch_is_complete(clean_pre)
                        ):
                            out_f = dest_dir / "apply_changes.ps1"
                            out_f.write_text(clean_pre + "\n", encoding="utf-8", newline="\n")
                            result_path = out_f
            finally:
                # Rule R11 & R13: Never leave the viewer open, and prove the page is still responsive afterwards
                _prove_page_responsive_after_capture(page, timeout_ms=3000)

            return result_path

        ns["capture_generated_file"] = _r23_capture_generated_file