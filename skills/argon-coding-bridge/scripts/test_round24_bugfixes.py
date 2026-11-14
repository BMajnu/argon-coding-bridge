#!/usr/bin/env python3
"""Round 24 Hermetic Bug-Fix Regression Suite (`test_round24_bugfixes.py`).

It proves, against the REAL production code, that the four bugs found on
2026-11-14 are gone:

  1. The re-apply patch scripts can no longer kill the bridge.
     (a) `patch_bridge_attach_images.py` used to append a SECOND `--attach-images`
         option -> `argparse.ArgumentError: conflicting option string`.
     (b) `patch_bridge_cdp_setfiles.py` used to write a helper whose inner quotes
         were not escaped -> `SyntaxError` -> `dispatch_argon.py` would not import.
     (c) `patch_bridge_large_archive.py` used to silently change the archive route.
     Every patcher is now compile-verified and atomic, and is a NO-OP on the
     current tree.
  2. `patch_bridge_attach_images.py` REPAIRS a file that already carries the
     duplicate option instead of adding a second one.
  3. `--attach-images` sends ALL the images in ONE CDP call. `DOM.setFileInputFiles`
     REPLACES the input's file list, so the old one-call-per-image loop kept only
     the LAST image (measured: 8 promised -> 1 delivered).
  4. The number the bridge reports is the number the browser really holds.
  5. The image input is selected by MIME type, not by a suffix that never matches.

Everything runs on COPIES and on a throwaway Chrome with its own profile - the
Argon browser, the account pool and the real plugin roots are never touched.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

PATCHES_DIR = SCRIPTS_DIR / "patches"
DISPATCH = SCRIPTS_DIR / "dispatch_argon.py"
RELIABILITY = SCRIPTS_DIR / "round23_reliability.py"

PATCHERS = [
    "patch_bridge_attach_images.py",
    "patch_bridge_task_toggle.py",
    "patch_bridge_large_archive.py",
    "patch_bridge_cdp_setfiles.py",
]

HTML = """<!doctype html><html><body>
<input id="pic" type="file" accept="image/*,video/*" multiple>
<input id="zip" type="file" accept=".zip,application/zip">
<div id="out">none</div>
<script>
window.counts = () => {
  const found = [];
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.tagName === 'INPUT' && el.type === 'file') found.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  };
  walk(document);
  let max = 0;
  for (const el of found) { const n = (el.files && el.files.length) ? el.files.length : 0; if (n > max) max = n; }
  return max;
};
</script></body></html>"""

PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _option_count(text: str) -> int:
    return len(re.findall(r'parser\.add_argument\(\s*"--attach-images"', text))


def _help_ok(path: Path) -> bool:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SCRIPTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        p = subprocess.run([sys.executable, str(path), "--help"], capture_output=True,
                           text=True, timeout=240, env=env, cwd=str(SCRIPTS_DIR))
        return p.returncode == 0
    except Exception:
        return False


def _compiles(path: Path) -> bool:
    import py_compile

    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except Exception:
        return False


def _run_patcher(name: str, target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PATCHES_DIR / name), "--target", str(target)],
        capture_output=True, text=True, timeout=300,
    )


def _chrome():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _launch(pw):
    for kwargs in ({"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch(headless=True, args=["--no-first-run"], **kwargs)
        except Exception:
            continue
    raise RuntimeError("no usable Chrome/Chromium for the Round 24 suite")


# --------------------------------------------------------------------------- cases
def case_1_patchers_cannot_kill_the_bridge() -> Dict[str, Any]:
    """Every re-apply patcher must be a safe NO-OP on the current tree."""
    work = Path(tempfile.mkdtemp(prefix="argon_r24_patcher_"))
    out: Dict[str, Any] = {}
    try:
        original = DISPATCH.read_bytes()
        for name in PATCHERS:
            probe = work / f"probe_{name}"
            shutil.copy2(DISPATCH, probe)
            before = probe.read_text(encoding="utf-8")
            p = _run_patcher(name, probe)
            after = probe.read_text(encoding="utf-8")
            out[name] = {
                "rc": p.returncode,
                "changed": before != after,
                "compiles": _compiles(probe),
                "help_ok": _help_ok(probe),
                "option_count": _option_count(after),
                "say": (p.stdout or p.stderr or "").strip().splitlines()[-1][:120]
                if (p.stdout or p.stderr or "").strip() else "",
            }
            assert before == after, f"{name} mutated dispatch_argon.py"
            assert _compiles(probe), f"{name} produced a file that does not compile"
            assert _option_count(after) == 1, f"{name} changed the --attach-images count"
        assert original == DISPATCH.read_bytes(), "the real dispatch_argon.py was touched"
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "patchers": out}


def case_2_duplicate_option_is_repaired_not_repeated() -> Dict[str, Any]:
    """A file that ALREADY carries the duplicate must be repaired, not doubled."""
    work = Path(tempfile.mkdtemp(prefix="argon_r24_dup_"))
    try:
        probe = work / "dispatch_argon.py"
        shutil.copy2(DISPATCH, probe)
        text = probe.read_text(encoding="utf-8")
        anchor = '    parser.add_argument("--files", nargs="*", default=None, help="Optional target file patterns")'
        assert anchor in text, "the --files anchor moved"
        dup = (
            anchor
            + '\n    parser.add_argument("--attach-images", nargs="*", default=None, metavar="IMAGE", '
            'help="duplicate injected by the test.")'
        )
        broken = text.replace(anchor, dup, 1)
        probe.write_text(broken, encoding="utf-8")
        assert _option_count(broken) == 2
        assert not _help_ok(probe), "the damaged copy should NOT pass --help"

        p = _run_patcher("patch_bridge_attach_images.py", probe)
        fixed = probe.read_text(encoding="utf-8")
        return {
            "ok": _option_count(fixed) == 1 and _help_ok(probe) and _compiles(probe),
            "rc": p.returncode,
            "options_before": 2,
            "options_after": _option_count(fixed),
            "help_ok_after": _help_ok(probe),
            "say": (p.stdout or "").strip()[:160],
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def case_3_images_go_in_one_call_and_all_survive() -> Dict[str, Any]:
    """8 screenshots -> the composer must hold 8 files, not 1."""
    from round23_reliability import attach_images_to_composer

    work = Path(tempfile.mkdtemp(prefix="argon_r24_imgs_"))
    out: Dict[str, Any] = {}
    try:
        page_file = work / "page.html"
        page_file.write_text(HTML, encoding="utf-8")
        pngs: List[str] = []
        for i in range(1, 9):
            p = work / f"shot{i}.png"
            p.write_bytes(PNG_1x1)
            pngs.append(str(p))

        with _chrome() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto(page_file.as_uri())
                ok, names, reason = attach_images_to_composer(page, pngs)
                held = page.evaluate("window.counts()")
                out = {
                    "ok": bool(ok) and len(names) == 8 and int(held) == 8,
                    "reported": len(names),
                    "browser_holds": int(held),
                    "reason": reason[:200],
                }
            finally:
                browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def case_4_one_call_replaces_the_list() -> Dict[str, Any]:
    """Document WHY the old loop lost 7 of 8 images (the CDP semantics)."""
    work = Path(tempfile.mkdtemp(prefix="argon_r24_cdp_"))
    out: Dict[str, Any] = {}
    try:
        page_file = work / "page.html"
        page_file.write_text(HTML, encoding="utf-8")
        pngs = []
        for i in range(1, 9):
            p = work / f"shot{i}.png"
            p.write_bytes(PNG_1x1)
            pngs.append(str(p))

        with _chrome() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto(page_file.as_uri())
                cdp = page.context.new_cdp_session(page)
                doc = cdp.send("DOM.getDocument", {"depth": 1, "pierce": True}) or {}
                root = int((doc.get("root") or {}).get("nodeId") or 0)
                ids = list((cdp.send("DOM.querySelectorAll",
                                      {"nodeId": root, "selector": "input[type='file']"}) or {}).get("nodeIds") or [])
                assert ids, "no file input on the page"
                node = int(ids[0])
                for p in pngs:
                    cdp.send("DOM.setFileInputFiles", {"files": [p], "nodeId": node})
                sequential = int(page.evaluate("window.counts()"))
                cdp.send("DOM.setFileInputFiles", {"files": pngs, "nodeId": node})
                single = int(page.evaluate("window.counts()"))
                cdp.detach()
                out = {
                    "ok": sequential == 1 and single == 8,
                    "sequential_calls_hold": sequential,
                    "single_call_holds": single,
                }
            finally:
                browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


def case_5_the_image_input_is_chosen_by_mime() -> Dict[str, Any]:
    """`.png` must be matched against `image/*`, not against the literal ".png"."""
    from round23_reliability import _accept_allows, _pick_file_input_node

    out = {
        "ok": (
            _accept_allows("image/*,video/*", ".png", "image/png") is True
            and _accept_allows(".zip,application/zip", ".png", "image/png") is False
            and _accept_allows(".zip,application/zip", ".zip", "application/zip") is True
            and _accept_allows("", ".png", "image/png") is True
        ),
        "image_wildcard_accepts_png": _accept_allows("image/*,video/*", ".png", "image/png"),
        "zip_accept_rejects_png": _accept_allows(".zip,application/zip", ".png", "image/png"),
        "zip_accept_accepts_zip": _accept_allows(".zip,application/zip", ".zip", "application/zip"),
    }
    return out


CASES = [
    ("case_1_patchers_cannot_kill_the_bridge", case_1_patchers_cannot_kill_the_bridge),
    ("case_2_duplicate_option_is_repaired_not_repeated", case_2_duplicate_option_is_repaired_not_repeated),
    ("case_3_images_go_in_one_call_and_all_survive", case_3_images_go_in_one_call_and_all_survive),
    ("case_4_one_call_replaces_the_list", case_4_one_call_replaces_the_list),
    ("case_5_the_image_input_is_chosen_by_mime", case_5_the_image_input_is_chosen_by_mime),
]


def run_round24_suite(verbose: bool = True) -> Dict[str, Any]:
    """Run every Round 24 case and return a machine-readable report."""
    import time

    t0 = time.monotonic()
    failures = 0
    passed = 0
    images_reported = 0
    details: List[Dict[str, Any]] = []
    for name, fn in CASES:
        t1 = time.monotonic()
        try:
            res = fn()
            ok = bool(res.get("ok"))
        except Exception as exc:
            ok = False
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if ok:
            passed += 1
        else:
            failures += 1
        if name.startswith("case_3"):
            try:
                images_reported = int(res.get("reported") or 0)
            except Exception:
                images_reported = 0
        details.append({"case": name, "ok": ok, "detail": res})
        if verbose:
            flag = "PASS" if ok else "FAIL"
            print(f"  [{flag}] {name}: {json.dumps(res, ensure_ascii=False)[:600]}")
            print(f"           ({time.monotonic() - t1:.2f}s)")
    return {
        "ok": failures == 0,
        "passed": passed,
        "total": len(CASES),
        "failed": failures,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
        "images_reported": images_reported,
        "cases": details,
    }


def main() -> int:
    print("=" * 78)
    print("  ARGON ROUND 24 BUG-FIX REGRESSION SUITE")
    print("=" * 78)
    rep = run_round24_suite(verbose=True)
    print("-" * 78)
    status = "PASS" if rep["ok"] else "FAIL (%d case(s))" % rep["failed"]
    print(f"  Overall status : {status}  ({rep['elapsed_ms'] / 1000:.2f}s)")
    print("=" * 78)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
