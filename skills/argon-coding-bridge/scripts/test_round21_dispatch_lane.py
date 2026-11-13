#!/usr/bin/env python3
"""Round 21 regression suite - the dispatch-lane failures measured on the owner's machine.

Round 20 fixed the browser lifecycle and the live `/u/<index>/` account sync. Round 21
fixes the LANE failures that the round-20 dual dispatch exposed, live:

```
[+] Lane NEW       : slot=/u/6/  | url=https://gemini.google.com/u/6/app
[NEW /u/6/] [!] Profile ...argon_browser_profile for slot /u/6/ is busy. Refusing to hijack the running browser.
```

The NEW lane was supposed to run on slot `/u/6/`; it ended up on slot `/u/7/` (an account
that is not signed in), opened the WRONG Google account, sent the prompt TWICE and left
the model chip on **Hydrogen** instead of Argon. It produced no delivery at all.

Each case below proves one link in that chain. The suite is hermetic: it never touches
`~/.gemini/argon_browser_profile`, never signs in to anything and never uses the owner's
accounts. The one case that needs a browser uses its own throwaway profile, its own
free port and its own temp directory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch_argon as da  # noqa: E402
import safe_browser as sb  # noqa: E402


# ----------------------------------------------------------------------------- helpers
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _find_chrome() -> Optional[str]:
    for cand in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(cand).is_file():
            return cand
    try:
        return da.find_chrome_executable()
    except Exception:
        return None


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ----------------------------------------------------------------------------- cases
def case_a_endpoint_file_is_actually_written() -> Dict[str, Any]:
    """`write_host_state` must REALLY persist `.argon_browser_endpoint`.

    Measured on the owner's machine: Argon's round-20 patch made `write_host_state`
    build its temp-file name with `threading.get_ident()` but never imported `threading`
    at module scope, so the call raised `NameError` inside `except Exception: pass` and
    the endpoint file was SILENTLY never written. Every later reader therefore saw
    "no host" and the lanes could not find each other.
    """
    tmp = Path(tempfile.mkdtemp(prefix="argon_r21_state_"))
    try:
        payload = {
            "profile_dir": str(tmp),
            "port": 1234,
            "http_endpoint": "http://127.0.0.1:1234",
            "tabs": {"tab-x": {"pid": 1}},
        }
        sb.write_host_state(tmp, payload)
        ep = sb.endpoint_file_for_profile(tmp)
        written = ep.is_file()
        back = sb.read_host_state(tmp) or {}
        return {
            "ok": bool(written and back.get("port") == 1234),
            "endpoint_file_written": written,
            "read_back_port": back.get("port"),
            "tabs_preserved": bool(back.get("tabs")),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_b_busy_slot_is_not_rotatable() -> Dict[str, Any]:
    """A busy slot must be FATAL for the requested slot, never rotated away.

    Rotation is how the NEW lane ended up running under a DIFFERENT Google account than
    the one that was asked for. `EXIT_SLOT_BUSY` must not be in the rotatable set.
    """
    src = _read_source(SCRIPTS_DIR / "dispatch_argon.py")
    m = re.search(r"rotatable_codes\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        return {"ok": False, "reason": "no rotatable_codes set found in dispatch_argon.py"}
    body = m.group(1)
    has_busy = "EXIT_SLOT_BUSY" in body
    others = sorted(set(re.findall(r"EXIT_[A-Z_]+", body)))
    return {
        "ok": not has_busy,
        "rotatable_codes": others,
        "exit_slot_busy_rotatable": has_busy,
    }


def case_c_non_argon_model_chip_blocks_the_send() -> Dict[str, Any]:
    """The model chip must be READ BACK before the prompt goes out.

    The chip can fall back to another model (`Hydrogen`) even after the model step
    reported success. A non-Argon chip must abort the send.
    """
    verify = getattr(da, "_verify_model_chip_is_argon", None)
    if verify is None:
        return {"ok": False, "reason": "_verify_model_chip_is_argon is missing from dispatch_argon.py"}

    orig_chip = da._model_chip
    orig_name = da._model_name
    orig_argon = da._chip_is_argon
    try:
        # 1. a chip that reads "Hydrogen" -> must refuse
        da._model_chip = lambda page: object()
        da._model_name = lambda chip: "Hydrogen"
        da._chip_is_argon = lambda chip: False
        ok_h, name_h = verify(None, stage="unit test")
        # 2. a chip that reads "4 Argon" -> must pass
        da._model_name = lambda chip: "4 Argon"
        da._chip_is_argon = lambda chip: True
        ok_a, name_a = verify(None, stage="unit test")
        # 3. no chip at all -> must NOT block (the chip may not be rendered yet)
        da._model_chip = lambda page: None
        ok_none, name_none = verify(None, stage="unit test")
    finally:
        da._model_chip = orig_chip
        da._model_name = orig_name
        da._chip_is_argon = orig_argon

    return {
        "ok": (ok_h is False and name_h == "Hydrogen") and (ok_a is True) and (ok_none is True),
        "hydrogen_refused": ok_h is False,
        "argon_allowed": ok_a is True,
        "no_chip_allowed": ok_none is True,
        "chip_name_seen": name_h,
    }


def case_d_duplicate_prompt_is_detected_before_a_resend() -> Dict[str, Any]:
    """The retry ladder must not send a prompt that is already in the conversation.

    The double send happened because the retry ladder re-sent without asking the chat
    whether the prompt was already there. `_check_duplicate_prompt` is that ask; it must
    exist AND the send path must call it.
    """
    if not hasattr(da, "_check_duplicate_prompt"):
        return {"ok": False, "reason": "_check_duplicate_prompt is missing"}
    src = _read_source(SCRIPTS_DIR / "dispatch_argon.py")
    # the send region: from the duplicate helper's definition to the end of the file
    helper_at = src.find("def _check_duplicate_prompt")
    uses = [m.start() for m in re.finditer(r"_check_duplicate_prompt\s*\(", src)]
    # one of the call sites must be AFTER the helper's definition (a real call, not the def)
    calls_after_def = [u for u in uses if helper_at >= 0 and u > helper_at]
    return {
        "ok": len(calls_after_def) >= 2,
        "call_sites_after_the_helper": len(calls_after_def),
    }


def case_e_second_lane_waits_for_the_starting_host() -> Dict[str, Any]:
    """A lane that arrives while the host is still starting must WAIT and ATTACH.

    This is the exact window that broke the round-20 dual dispatch. The fixture starts a
    real Chrome on a throwaway profile, hides the endpoint from the discoverer (the
    endpoint file AND `DevToolsActivePort` are removed), then publishes the endpoint
    file 3 seconds later - exactly what the other lane does while it is launching.
    `ensure_browser_host` must WAIT and attach to that host instead of returning None.
    """
    chrome = _find_chrome()
    if chrome is None:
        return {"ok": True, "skipped": "no Chrome binary found"}

    tmp = Path(tempfile.mkdtemp(prefix="argon_r21_wait_"))
    prof = tmp / "profile"
    port = _free_port()
    proc = None
    published: Dict[str, Any] = {}
    try:
        prof.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [chrome, f"--user-data-dir={prof}", f"--remote-debugging-port={port}",
             "--remote-debugging-address=127.0.0.1", "--no-first-run",
             "--no-default-browser-check", "--headless=new", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # wait for Chrome to come up
        deadline = time.monotonic() + 30.0
        up = False
        while time.monotonic() < deadline:
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.4)
        if not up:
            return {"ok": False, "reason": "the throwaway Chrome never published its endpoint"}

        # Hide the endpoint from the discoverer: remove the endpoint file AND
        # DevToolsActivePort, so `ensure_browser_host` sees "Chrome is running but no
        # endpoint" - the starting-up window.
        for name in (".argon_browser_endpoint", "DevToolsActivePort"):
            p = prof / name
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        def _publish_later() -> None:
            time.sleep(3.0)
            published["state"] = {
                "profile_dir": str(prof),
                "pid": proc.pid,
                "port": port,
                "http_endpoint": f"http://127.0.0.1:{port}",
                "ws_endpoint": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tabs": {},
            }
            # The other lane publishes the endpoint file - that is exactly what the
            # waiting lane re-reads on every poll.
            sb.write_host_state(prof, published["state"])

        threading.Thread(target=_publish_later, daemon=True).start()

        t0 = time.time()
        host = sb.ensure_browser_host(prof, chrome_exe=chrome, headless=False)
        waited = time.time() - t0
        ok = bool(host) and int((host or {}).get("port") or 0) == port
        return {
            "ok": ok,
            "waited_s": round(waited, 2),
            "attached_port": (host or {}).get("port"),
            "expected_port": port,
            "published_by_the_other_lane": bool(published),
        }
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- runner
def run_round21_regression_suite() -> Dict[str, Any]:
    started = time.monotonic()
    cases: List[Tuple[str, Any]] = [
        ("case_a_endpoint_file_is_actually_written", case_a_endpoint_file_is_actually_written),
        ("case_b_busy_slot_is_not_rotatable", case_b_busy_slot_is_not_rotatable),
        ("case_c_non_argon_model_chip_blocks_the_send", case_c_non_argon_model_chip_blocks_the_send),
        ("case_d_duplicate_prompt_is_detected_before_a_resend", case_d_duplicate_prompt_is_detected_before_a_resend),
        ("case_e_second_lane_waits_for_the_starting_host", case_e_second_lane_waits_for_the_starting_host),
    ]
    results: Dict[str, Any] = {}
    failures: List[str] = []
    for name, fn in cases:
        try:
            res = fn()
        except Exception as exc:
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        results[name] = res
        if not res.get("ok"):
            failures.append(name)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": not failures,
        "failed_cases": failures,
        "cases": results,
        "elapsed_ms": elapsed_ms,
    }


def main() -> int:
    print("=" * 78)
    print("  ARGON ROUND 21 DISPATCH-LANE REGRESSION SUITE")
    print("=" * 78)
    rep = run_round21_regression_suite()
    for name, res in rep["cases"].items():
        tag = "PASS" if res.get("ok") else "FAIL"
        print(f"  [{tag}] {name}: {json.dumps(res, ensure_ascii=False)}")
    print("-" * 78)
    print(f"  Overall status : {'PASS' if rep['ok'] else 'FAIL'} ({rep['elapsed_ms']} ms)")
    if rep["failed_cases"]:
        print(f"  Failed cases   : {', '.join(rep['failed_cases'])}")
    print("=" * 78)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
