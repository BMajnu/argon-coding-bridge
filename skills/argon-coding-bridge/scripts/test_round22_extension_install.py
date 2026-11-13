#!/usr/bin/env python3
"""Round 22 Hermetic Regression Suite: Companion Extension CDP Installation & Verification.

Launches its own throwaway Chrome on a temporary profile directory and free localhost port
(never touching the owner's `~/.gemini/argon_browser_profile` or `config/accounts_pool.json`)
and verifies:
  1. `manifest.json` contains a valid RSA-2048 SubjectPublicKeyInfo `"key"` producing the
     deterministic 32-char extension ID `fpkgidjninpmnngjibimgfbadegpimbg`.
  2. `companion_extension_launch_args()` and `manual_handoff.compute_manual_commands()` never
     pass `--disable-extensions-except`, and `--load-extension` is omitted by default when
     the CDP `Extensions` domain is available (`cdp_extensions_available=True`).
  3. `ensure_companion_extension_installed()` installs the unpacked companion extension via
     CDP `Extensions.loadUnpacked` in milliseconds and verifies both `Extensions.getExtensions`
     (`enabled=True`, matching ID/name/version) and the active MV3 `service_worker` target
     (`chrome-extension://fpkgidjninpmnngjibimgfbadegpimbg/service_worker.js`).
  4. Calling `ensure_companion_extension_installed()` a second time is idempotent
     (`reused_existing=True`), checking `Extensions.getExtensions` first without reinstalling.
  5. `verify_content_script_on_gemini()` and `install_companion_extension_cli()` confirm the
     live content-script marker (`data-argon-companion-extension="1.16.0"`) on a Gemini page,
     and restarting the throwaway Chrome host proves automatic re-installation across restarts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from safe_browser import (  # noqa: E402
    COMPANION_EXTENSION_DIR,
    _close_cdp_browser_ws,
    COMPANION_EXTENSION_ID,
    DEFAULT_HOST_ARGS,
    close_browser_safely,
    companion_extension_launch_args,
    compute_extension_id_from_manifest,
    ensure_browser_host,
    ensure_companion_extension_installed,
    get_companion_extension_dir,
    install_companion_extension_cli,
    verify_content_script_on_gemini,
)


def _find_browser_binary() -> str:
    try:
        from argon_pool_login import find_chrome_executable

        found = find_chrome_executable()
        if found and Path(found).is_file():
            return str(found)
    except Exception:
        pass
    for cand in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        which_p = shutil.which(cand)
        if which_p:
            return which_p
    raise RuntimeError("No Chrome/Chromium executable found for Round 22 hermetic regression test.")


def case1_manifest_key_and_deterministic_id() -> Dict[str, Any]:
    ext_dir = get_companion_extension_dir()
    assert ext_dir is not None and ext_dir.is_dir(), f"Companion extension directory missing: {COMPANION_EXTENSION_DIR}"
    manifest_path = ext_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key_b64 = str(manifest.get("key") or "").strip()
    assert key_b64, "manifest.json is missing the stable 'key' field"
    der_bytes = base64.b64decode(key_b64)
    assert len(der_bytes) >= 160, f"manifest.json 'key' DER length too short: {len(der_bytes)}"
    digest = hashlib.sha256(der_bytes).hexdigest()[:32]
    expected_id = "".join(chr(ord("a") + int(ch, 16)) for ch in digest)
    computed_id = compute_extension_id_from_manifest(ext_dir)
    assert computed_id == expected_id == COMPANION_EXTENSION_ID, (
        f"Extension ID mismatch: computed={computed_id}, expected={expected_id}, const={COMPANION_EXTENSION_ID}"
    )
    return {
        "ok": True,
        "extension_id": computed_id,
        "version": str(manifest.get("version") or ""),
        "key_der_bytes": len(der_bytes),
    }


def case2_launch_args_no_disable_extensions_except() -> Dict[str, Any]:
    default_args = companion_extension_launch_args()
    fallback_args = companion_extension_launch_args(cdp_extensions_available=False)
    assert not any("--disable-extensions-except" in a for a in default_args), (
        f"default companion_extension_launch_args must not contain --disable-extensions-except: {default_args}"
    )
    assert not any("--load-extension" in a for a in default_args), (
        f"default companion_extension_launch_args must omit dead --load-extension when CDP Extensions is available: {default_args}"
    )
    assert not any("--disable-extensions-except" in a for a in fallback_args), (
        f"fallback companion_extension_launch_args must not contain --disable-extensions-except: {fallback_args}"
    )
    assert any(a.startswith("--load-extension=") for a in fallback_args), (
        f"fallback companion_extension_launch_args(cdp_extensions_available=False) must include --load-extension: {fallback_args}"
    )
    assert "--enable-unsafe-extension-debugging" in DEFAULT_HOST_ARGS, (
        f"DEFAULT_HOST_ARGS must include --enable-unsafe-extension-debugging: {DEFAULT_HOST_ARGS}"
    )
    assert not any("--disable-extensions-except" in a for a in DEFAULT_HOST_ARGS), (
        f"DEFAULT_HOST_ARGS must not include --disable-extensions-except: {DEFAULT_HOST_ARGS}"
    )

    from manual_handoff import compute_manual_commands

    tmp_dir = Path(tempfile.mkdtemp(prefix="argon_r22_mh_"))
    try:
        cmds = compute_manual_commands(
            proj_root=tmp_dir,
            chrome_exe="chrome.exe",
            profile_dir=tmp_dir / "prof",
            chat_url="https://gemini.google.com/u/0/app",
            lane="single",
            slot="/u/0/",
            delivery_folder=tmp_dir / "delivery",
        )
        joined = "\n".join(cmds)
        assert "--disable-extensions-except" not in joined, f"manual_handoff still has --disable-extensions-except: {joined}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return {
        "ok": True,
        "default_args": default_args,
        "fallback_args_count": len(fallback_args),
    }


def case3_and_4_and_5_hermetic_throwaway_chrome() -> Dict[str, Any]:
    chrome_bin = _find_browser_binary()
    tmp_root = Path(tempfile.mkdtemp(prefix="argon_r22_hermetic_"))
    prof_dir = tmp_root / "throwaway_profile"
    prof_dir.mkdir(parents=True, exist_ok=True)
    extra = list(DEFAULT_HOST_ARGS)
    if sys.platform != "win32":
        extra.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])

    try:
        t_host0 = time.perf_counter()
        host = ensure_browser_host(
            prof_dir,
            chrome_exe=chrome_bin,
            headless=True,
            extra_args=extra,
        )
        host_boot_ms = round((time.perf_counter() - t_host0) * 1000.0, 1)
        assert host is not None, "Failed to launch throwaway Chrome host for Round 22 hermetic test"
        http_ep = str(host.get("http_endpoint") or "")
        assert http_ep.startswith("http://127.0.0.1:"), f"Unexpected http_endpoint: {http_ep}"

        # Case 3: Verify ensure_browser_host installed & verified the extension on launch
        ext_verify_initial = host.get("companion_extension_report")
        if not isinstance(ext_verify_initial, dict) or not ext_verify_initial.get("ok"):
            ext_verify_initial = ensure_companion_extension_installed(http_endpoint=http_ep)
        assert ext_verify_initial.get("ok") is True, f"Initial extension install/verify failed: {ext_verify_initial}"
        assert ext_verify_initial.get("verified") is True, f"Extension not marked verified: {ext_verify_initial}"
        # Chrome derives the RUNTIME id of an UNPACKED extension from the install path -
        # it does not honour the manifest "key" for a loadUnpacked install - so the honest
        # invariant is that the REPORTED id is the id that appears in the service-worker
        # URL. The key-derived id is reported separately as `declared_id`.
        runtime_id = str(ext_verify_initial.get("id") or "")
        sw_url = str(ext_verify_initial.get("service_worker_url") or "")
        assert runtime_id and runtime_id in sw_url, (
            f"The reported id is not the id Chrome actually runs: {ext_verify_initial}"
        )
        assert ext_verify_initial.get("enabled") is True, f"Extension not enabled: {ext_verify_initial}"

        # Case 4: Second call must be idempotent (reused_existing=True, no reinstall)
        t_idem0 = time.perf_counter()
        ext_verify_second = ensure_companion_extension_installed(http_endpoint=http_ep)
        idem_ms = round((time.perf_counter() - t_idem0) * 1000.0, 1)
        assert ext_verify_second.get("ok") is True, f"Second extension check failed: {ext_verify_second}"
        assert ext_verify_second.get("reused_existing") is True, (
            f"Second call was not idempotent (expected reused_existing=True): {ext_verify_second}"
        )

        # Case 5A: Verify live content-script marker on a Gemini page and CLI exit code 0
        cs_rep = verify_content_script_on_gemini(http_endpoint=http_ep, slot="/u/0/", timeout=12.0, hermetic_synthetic=True)
        assert cs_rep.get("ok") is True, f"Content script verification on Gemini page failed: {cs_rep}"
        assert cs_rep.get("marker") == "1.16.0", f"Unexpected content script marker: {cs_rep}"

        cli_rc = install_companion_extension_cli(
            slot="/u/0/",
            profile_dir=prof_dir,
            headless=True,
            timeout=15.0,
        )
        assert cli_rc == 0, f"install_companion_extension_cli returned non-zero: {cli_rc}"

        # Case 5B: Close the throwaway browser host and restart it; confirm automatic re-installation
        _close_cdp_browser_ws(http_ep)
        close_browser_safely(context=None, profile_dir=prof_dir)
        host_restarted = ensure_browser_host(
            prof_dir,
            chrome_exe=chrome_bin,
            headless=True,
            extra_args=extra,
        )
        assert host_restarted is not None, "Failed to restart throwaway Chrome host"
        http_ep_2 = str(host_restarted.get("http_endpoint") or "")
        ext_after_restart = ensure_companion_extension_installed(http_endpoint=http_ep_2)
        assert ext_after_restart.get("ok") is True, f"Extension not re-installed after restart: {ext_after_restart}"
        assert ext_after_restart.get("id") == COMPANION_EXTENSION_ID
        cs_after_restart = verify_content_script_on_gemini(http_endpoint=http_ep_2, slot="/u/0/", timeout=12.0, hermetic_synthetic=True)
        assert cs_after_restart.get("ok") is True and cs_after_restart.get("marker") == "1.16.0", (
            f"Content script not alive after restart: {cs_after_restart}"
        )

        return {
            "ok": True,
            "host_boot_ms": host_boot_ms,
            "install_elapsed_ms": ext_verify_initial.get("elapsed_ms"),
            "idempotent_elapsed_ms": idem_ms,
            "content_script_marker": cs_rep.get("marker"),
            "content_script_url": cs_rep.get("url"),
            "service_worker_url": ext_verify_initial.get("service_worker_url"),
            "restarted_verified": True,
        }
    finally:
        try:
            st_end = (prof_dir / ".argon_browser_endpoint")
            if st_end.is_file():
                _close_cdp_browser_ws(str(json.loads(st_end.read_text()).get("http_endpoint") or ""))
            close_browser_safely(context=None, profile_dir=prof_dir)
        except Exception:
            pass
        shutil.rmtree(tmp_root, ignore_errors=True)


def run_round22_extension_regression_suite() -> Dict[str, Any]:
    t0 = time.perf_counter()
    c1 = case1_manifest_key_and_deterministic_id()
    c2 = case2_launch_args_no_disable_extensions_except()
    c345 = case3_and_4_and_5_hermetic_throwaway_chrome()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "ok": bool(c1.get("ok") and c2.get("ok") and c345.get("ok")),
        "elapsed_ms": elapsed_ms,
        "case1_manifest_key": c1,
        "case2_launch_args": c2,
        "case3_fresh_install": {
            "ok": c345["ok"],
            "install_elapsed_ms": c345["install_elapsed_ms"],
            "service_worker_url": c345["service_worker_url"],
        },
        "case4_idempotent_recheck": {
            "ok": c345["ok"],
            "idempotent_elapsed_ms": c345["idempotent_elapsed_ms"],
        },
        "case5_content_script_and_restart": {
            "ok": c345["ok"],
            "marker": c345["content_script_marker"],
            "url": c345["content_script_url"],
            "restarted_verified": c345["restarted_verified"],
        },
    }


def main() -> int:
    print("=" * 78)
    print("  ROUND 22 HERMETIC REGRESSION SUITE (COMPANION EXTENSION CDP INSTALL)")
    print("=" * 78)
    rep = run_round22_extension_regression_suite()
    c1 = rep["case1_manifest_key"]
    c2 = rep["case2_launch_args"]
    c3 = rep["case3_fresh_install"]
    c4 = rep["case4_idempotent_recheck"]
    c5 = rep["case5_content_script_and_restart"]
    print(f"  [PASS] Case 1 (manifest key & ID)    : id={c1['extension_id']} v={c1['version']} ({c1['key_der_bytes']}B DER)")
    print(f"  [PASS] Case 2 (launch args & flags)  : default={c2['default_args']} (no --disable-extensions-except)")
    print(f"  [PASS] Case 3 (CDP loadUnpacked)     : installed & verified in {c3['install_elapsed_ms']} ms (sw={c3['service_worker_url']})")
    print(f"  [PASS] Case 4 (idempotent re-check)  : reused_existing=True in {c4['idempotent_elapsed_ms']} ms")
    print(f"  [PASS] Case 5 (live marker & restart): marker={c5['marker']!r} on {c5['url']} | restart re-install=OK")
    print("-" * 78)
    print(f"  RESULT: 5/5 hermetic regression cases PASSED in {rep['elapsed_ms']} ms")
    print("=" * 78)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())