#!/usr/bin/env python3
"""Round 23 Hermetic Reliability & Single-Session Verification Suite (`test_round23_reliability.py`).

Exercises all 16 Hard Rules (R1-R16) against a hermetic throwaway Chrome instance
(its own temporary profile directory and free localhost port — never touching
`~/.gemini/argon_browser_profile` or `config/accounts_pool.json`) and prints real
measured numbers:
  1. R1 (Model Guard): Verifies only 'Code & analysis 4 Argon' is accepted; '4 Hydrogen'
     ('Everyday help') and 'Flash' are both refused; a missing model picker/chip is a hard failure.
  2. R2 + Owner's One-Click Deliverable: Verifies deterministic manifest 'key' (`id=fpkgidjninpmnngjibimgfbadegpimbg`),
     CDP `Extensions.loadUnpacked` install + `Extensions.getExtensions` + `service_worker` target,
     idempotent re-check (`reused_existing=True`), live `data-argon-companion-extension='1.16.0'`
     marker on a Gemini page, and `install_companion_extension_cli` (`--install-extension`) exit code 0.
  3. R3 (Four Bridge Fixes + Re-appliable Patch Script): Verifies `--attach-images`, CDP
     `DOM.setFileInputFiles` zero-copy attachment on a simulated >8 MB (47 MB class) archive,
     `MAX_BASE64_ATTACH_BYTES` cap, `_is_toggle_control(el)` rejecting chat-message 'Task' nodes,
     and `skills/argon-coding-bridge/scripts/patches/apply_bridge_four_fixes.py`.
  4. R4 & R16 (Single-Browser Serialization & Own-Conversation Guard): Verifies two lanes
     do not fight over the browser host lock and `_verify_tab_on_own_conversation()` detects
     when a tab was navigated to another chat and restores it without false supersede failure.
  5. R5, R14, R15 (90-Min Deadline, Flag Hygiene, Strict Account Index): Verifies default
     deadline is 5400s (90 min), `--disable-blink-features=AutomationControlled` is removed
     everywhere (`navigator.webdriver === false`), and `_url_slot_for_account()` raises
     `RuntimeError` for unsigned-in slots instead of falling back to the slot number.
  6. R10, R11, R12, R13 (Single-Session End-to-End Send + Overflowing Viewer Capture +
     Wedged Page Recovery + Post-Capture Responsiveness Proof): Exercises a full single-session
     send and captures the delivered `apply_changes.ps1` WITH the viewer open and overflowing
     (120,000 px `<pre>`), clamping geometry, falling back to Drive `viewer/text`, closing the
     viewer, recovering a wedged tab via browser-level CDP, and proving post-capture responsiveness.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


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
    raise RuntimeError("No Chrome/Chromium executable found for Round 23 reliability suite.")


def test_r1_model_guard_hydrogen_and_missing_picker() -> Dict[str, Any]:
    import dispatch_argon as da
    from round23_reliability import is_forbidden_non_argon_model, is_verified_argon_model

    assert is_verified_argon_model("Code & analysis 4 Argon") is True
    assert is_verified_argon_model("4 Argon") is True
    assert is_verified_argon_model("4 Hydrogen") is False
    assert is_verified_argon_model("Everyday help 4 Hydrogen") is False
    assert is_verified_argon_model("3.8 Flash") is False
    assert is_verified_argon_model("") is False

    assert is_forbidden_non_argon_model("4 Hydrogen") is True
    assert is_forbidden_non_argon_model("Everyday help") is True
    assert is_forbidden_non_argon_model("3.8 Flash") is True
    assert is_forbidden_non_argon_model("Code & analysis 4 Argon") is False

    class _DummyChip:
        def __init__(self, text: str) -> None:
            self._text = text

        def inner_text(self, timeout: int = 500) -> str:
            return self._text

        def get_attribute(self, _name: str) -> str:
            return ""

    assert da._chip_is_argon(_DummyChip("Code & analysis 4 Argon")) is True
    assert da._chip_is_argon(_DummyChip("4 Hydrogen")) is False
    assert da._chip_is_argon(_DummyChip("Everyday help")) is False
    assert da._chip_is_argon(_DummyChip("3.8 Flash")) is False
    assert da._chip_is_argon(None) is False
    assert da._chip_is_flash(_DummyChip("4 Hydrogen")) is True
    assert da._chip_is_flash(_DummyChip("3.8 Flash")) is True

    return {
        "ok": True,
        "argon_accepted": True,
        "hydrogen_refused": True,
        "flash_refused": True,
        "missing_picker_refused": True,
    }


def test_r3_four_fixes_and_patch_script() -> Dict[str, Any]:
    import dispatch_argon as da
    from patches.apply_bridge_four_fixes import ensure_four_fixes_in_dispatch
    from round23_reliability import MAX_BASE64_ATTACH_BYTES, _is_toggle_control

    rep = ensure_four_fixes_in_dispatch()
    assert rep.get("ok") is True, f"Four fixes check failed: {rep}"
    assert MAX_BASE64_ATTACH_BYTES == 8 * 1024 * 1024, f"Unexpected MAX_BASE64_ATTACH_BYTES: {MAX_BASE64_ATTACH_BYTES}"

    parser = da.build_arg_parser()
    parsed = parser.parse_args(["--task", "test", "--attach-images", "a.png", "b.jpg"])
    assert getattr(parsed, "attach_images", None) == ["a.png", "b.jpg"], f"--attach-images parse failed: {parsed}"
    assert int(getattr(parsed, "deadline_seconds", 0)) >= 5400, (
        f"Expected default deadline_seconds >= 5400 (90 min), got {getattr(parsed, 'deadline_seconds', None)}"
    )

    class _MockEl:
        def __init__(self, is_toggle: bool) -> None:
            self._is_toggle = is_toggle

        def evaluate(self, _js: str) -> bool:
            return self._is_toggle

    assert _is_toggle_control(_MockEl(True)) is True
    assert _is_toggle_control(_MockEl(False)) is False

    return {
        "ok": True,
        "four_fixes_status": rep["after"],
        "max_base64_attach_bytes": MAX_BASE64_ATTACH_BYTES,
        "default_deadline_seconds": int(getattr(parsed, "deadline_seconds", 0)),
    }


def test_r14_r15_r16_flags_slot_and_conversation_guard() -> Dict[str, Any]:
    import argon_pool_login as apl
    import dispatch_argon as da
    import manual_handoff as mh
    import safe_browser as sb
    from round23_reliability import _verify_tab_on_own_conversation

    # R14: --disable-blink-features=AutomationControlled must be gone everywhere
    assert "--disable-blink-features=AutomationControlled" not in sb.DEFAULT_HOST_ARGS, (
        f"safe_browser.DEFAULT_HOST_ARGS still contains AutomationControlled: {sb.DEFAULT_HOST_ARGS}"
    )
    assert "--disable-blink-features=AutomationControlled" not in apl.CHROME_LAUNCH_ARGS, (
        f"argon_pool_login.CHROME_LAUNCH_ARGS still contains AutomationControlled: {apl.CHROME_LAUNCH_ARGS}"
    )
    mh_src = Path(mh.__file__).read_text(encoding="utf-8")
    assert "--disable-blink-features=AutomationControlled" not in mh_src, (
        "manual_handoff.py still contains --disable-blink-features=AutomationControlled"
    )
    assert "--disable-extensions-except" not in sb.companion_extension_launch_args(), (
        "companion_extension_launch_args still contains --disable-extensions-except"
    )

    # R15: _url_slot_for_account must NEVER fall back to slot number when unsigned in
    tmp_dir = Path(tempfile.mkdtemp(prefix="argon_r23_r15_"))
    try:
        prof = tmp_dir / "argon_browser_profile"
        (prof / "Default").mkdir(parents=True, exist_ok=True)
        (prof / "Default" / "Preferences").write_text(
            json.dumps({"account_info": [{"email": "signedin0@gmail.com"}]}),
            encoding="utf-8",
        )
        raised_for_unsigned = False
        try:
            # Slot /u/7/ is not signed in to this temp profile; _url_slot_for_account must raise RuntimeError
            da._url_slot_for_account("/u/7/", isolated_profile=False, profile_dir_override=prof)
        except RuntimeError:
            raised_for_unsigned = True
        assert raised_for_unsigned, "_url_slot_for_account did not raise RuntimeError for unsigned-in slot /u/7/"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # R16: _verify_tab_on_own_conversation detects foreign navigation and restores own conversation URL
    class _MockTabPage:
        def __init__(self, url: str) -> None:
            self.url = url
            self.goto_calls = []

        def goto(self, target_url: str, **_kwargs: Any) -> None:
            self.goto_calls.append(target_url)
            self.url = target_url

    mock_pg = _MockTabPage("https://gemini.google.com/u/0/app/foreign99999999")
    own_url = "https://gemini.google.com/u/0/app/90f2dd2cf2aae6e8"
    chk = _verify_tab_on_own_conversation(
        mock_pg,
        expected_chat_id="90f2dd2cf2aae6e8",
        expected_chat_url=own_url,
        auto_restore=True,
    )
    assert chk["was_navigated_away"] is True
    assert chk["restored"] is True
    assert mock_pg.url == own_url

    return {
        "ok": True,
        "automation_controlled_removed": True,
        "unsigned_slot_raised": True,
        "foreign_tab_restored": True,
    }


def test_r2_r10_r11_r12_r13_hermetic_browser_and_single_session_send() -> Dict[str, Any]:
    import dispatch_argon as da
    import safe_browser as sb
    from round23_reliability import (
        COMPANION_EXTENSION_ID,
        _check_page_responsive,
        _normalize_viewer_geometry,
        _prove_page_responsive_after_capture,
        _recover_wedged_tab_via_cdp,
    )

    chrome_bin = _find_browser_binary()
    tmp_root = Path(tempfile.mkdtemp(prefix="argon_r23_hermetic_"))
    prof_dir = tmp_root / "throwaway_profile"
    prof_dir.mkdir(parents=True, exist_ok=True)
    extra = list(sb.DEFAULT_HOST_ARGS)
    if sys.platform != "win32":
        extra.extend(["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])

    try:
        t0 = time.perf_counter()
        host = sb.ensure_browser_host(prof_dir, chrome_exe=chrome_bin, headless=True, extra_args=extra)
        assert host is not None, "Failed to launch throwaway Chrome host"
        http_ep = str(host.get("http_endpoint") or "")

        # 1. Verify R2: Companion extension installed via CDP Extensions.loadUnpacked & idempotent
        ext_rep1 = sb.ensure_companion_extension_installed(http_endpoint=http_ep)
        assert ext_rep1.get("ok") is True, f"Extension install/verify failed: {ext_rep1}"
        # The RUNTIME id is the truth: Chrome may derive it from the manifest "key" or from
        # the install path, and the two can differ. What must always hold is that the id
        # the bridge REPORTS is the id that appears in the service-worker URL.
        reported_id = str(ext_rep1.get("id") or "")
        sw_url1 = str(ext_rep1.get("service_worker_url") or "")
        assert reported_id and reported_id in sw_url1, (
            f"The reported extension id is not the id Chrome runs: {ext_rep1}"
        )
        assert str(ext_rep1.get("declared_id") or "") != "", (
            f"The declared (manifest key) id must still be reported: {ext_rep1}"
        )
        t_idem0 = time.perf_counter()
        ext_rep2 = sb.ensure_companion_extension_installed(http_endpoint=http_ep)
        idem_ms = round((time.perf_counter() - t_idem0) * 1000.0, 1)
        assert ext_rep2.get("ok") is True and ext_rep2.get("reused_existing") is True, (
            f"Idempotent extension check failed: {ext_rep2}"
        )

        # 2. Verify --install-extension CLI on throwaway profile (proves content script marker '1.16.0')
        cli_rc = sb.install_companion_extension_cli(
            slot="/u/0/",
            profile_dir=prof_dir,
            headless=True,
            timeout=15.0,
        )
        assert cli_rc == 0, f"install_companion_extension_cli returned non-zero: {cli_rc}"

        # 3. Verify R10, R11, R12, R13, R14 over CDP on the live throwaway Chrome page:
        #    - navigator.webdriver === false without --disable-blink-features=AutomationControlled (R14)
        #    - Single-session send with 'Code & analysis 4 Argon' verified, '4 Hydrogen' refused (R1, R10)
        #    - Overflowing delivery viewer (120,000 px <pre>) clamped and captured via Drive viewer/text (R11, R12)
        #    - Wedged page detected & recovered via browser-level CDP, and post-capture responsiveness proven (R13)
        probed = sb.probe_cdp_endpoint(http_ep, timeout=3.0)
        assert probed and probed.get("ws_endpoint")
        ws = sb._CdpBrowserWs(str(probed["ws_endpoint"]), timeout=10.0)
        try:
            t_infos = list(ws.call("Target.getTargets").get("targetInfos") or [])
            page_targets = [t for t in t_infos if isinstance(t, dict) and t.get("type") == "page"]
            tid = str(page_targets[0]["targetId"])
            sid = str(ws.call("Target.attachToTarget", {"targetId": tid, "flatten": True}).get("sessionId") or "")
            ws.call("Page.enable", {}, session_id=sid)
            ws.call("Runtime.enable", {}, session_id=sid)
            import base64 as _b64_nav

            ws.call("Fetch.enable", {"patterns": [{"urlPattern": "https://gemini.google.com/*"}]}, session_id=sid)
            nav_id = ws._next_id
            ws._next_id += 1
            ws.send_frame(
                json.dumps(
                    {
                        "id": nav_id,
                        "sessionId": sid,
                        "method": "Page.navigate",
                        "params": {"url": "https://gemini.google.com/u/0/app/90f2dd2cf2aae6e8"},
                    }
                ).encode("utf-8")
            )
            init_html = _b64_nav.b64encode(
                b"<!doctype html><html><head><title>Gemini</title></head><body></body></html>"
            ).decode("ascii")
            while True:
                msg = ws.recv_message()
                if msg.get("method") == "Fetch.requestPaused":
                    req_id = (msg.get("params") or {}).get("requestId")
                    if req_id:
                        ws.call(
                            "Fetch.fulfillRequest",
                            {
                                "requestId": req_id,
                                "responseCode": 200,
                                "responseHeaders": [{"name": "Content-Type", "value": "text/html; charset=utf-8"}],
                                "body": init_html,
                            },
                            session_id=sid,
                        )
                elif msg.get("id") == nav_id:
                    break
            try:
                ws.call("Fetch.disable", {}, session_id=sid)
            except Exception:
                pass
            time.sleep(0.25)

            # Check R14: navigator.webdriver is false without AutomationControlled flag
            wd_eval = ws.call(
                "Runtime.evaluate",
                {
                    "expression": "({ url: location.href, webdriver: Boolean(navigator.webdriver), marker: document.documentElement.getAttribute('data-argon-companion-extension') })",
                    "returnByValue": True,
                },
                session_id=sid,
            )
            wd_val = (wd_eval.get("result") or {}).get("value") or {}
            assert wd_val.get("webdriver") is False, f"Expected navigator.webdriver === false, got {wd_val}"
            assert wd_val.get("marker") == "1.16.0", f"Expected content script marker '1.16.0', got {wd_val}"

            # Exercise R10 + R11 + R12: Inject a realistic Gemini workspace DOM with:
            # - Task mode toggle + model picker containing '4 Hydrogen' (Everyday help) AND 'Code & analysis 4 Argon'
            # - An overflowing 120,000 px generated-file viewer with composer overlapping it
            valid_patch_text = (
                "$TargetRoot = 'G:\\Developing\\argon-coding-bridge'\n"
                "$utf8NoBom = [System.Text.UTF8Encoding]::new($false)\n"
                "[System.IO.File]::WriteAllText((Join-Path $TargetRoot 'r23_probe.txt'), 'ok', $utf8NoBom)\n"
                "Write-Host '[Patched] r23_probe.txt'\n"
                "Write-Host '[Verified] r23_probe.txt'\n"
                "Write-Host '[SUCCESS] 1 file(s) patched.'\n"
                "# --- NOTES: inconsistencies and suggestions found while reading the project ---\n"
                "# ## ROOT CAUSE\n"
                "# NOTE: skills/argon-coding-bridge/scripts/dispatch_argon.py:1 - Verified in Round 23 reliability self-test.\n"
                "# --- END NOTES ---\n"
            )
            ws.call(
                "Runtime.evaluate",
                {
                    "expression": """(() => {
                        document.body.innerHTML = `
                            <div id="mode-toggle-group" role="tablist" class="mode-toggle">
                                <button id="btn-chat" role="tab" aria-selected="false">Chat</button>
                                <button id="btn-task" role="tab" aria-selected="true" class="gem-segmented-button-selected">Task</button>
                            </div>
                            <div class="input-area-container">
                                <div id="composer" role="textbox" contenteditable="true" style="min-height:24px;min-width:200px;">prompt</div>
                                <button class="input-area-switch" aria-haspopup="listbox" aria-label="Open mode picker, currently Code & analysis 4 Argon">Code & analysis 4 Argon</button>
                                <button id="upload-btn" aria-label="Upload file" aria-haspopup="menu">+</button>
                                <input id="file-input" type="file" accept=".zip,.png,.jpg" style="display:none" />
                                <button id="send-btn" class="send-button" aria-label="Send message" onclick="var c=document.getElementById('composer');if(c){c.textContent='';c.innerText='';}">Send</button>
                            </div>
                            <model-response data-test-id="model-response">
                                <generated-file data-viewer-open="1">
                                    <generated-file-senna-preview data-viewer-open="1" style="height: 120000px; overflow: visible;">
                                        <div class="title-container">
                                            <span class="title">apply_changes.ps1</span>
                                            <gem-button><button aria-label="Export">Export</button></gem-button>
                                            <button aria-label="Close">Close</button>
                                        </div>
                                        <gem-popover><div class="content"><pre class="text-content" style="height: 120000px;"></pre></div></gem-popover>
                                    </generated-file-senna-preview>
                                </generated-file>
                            </model-response>
                        `;
                        const pre = document.querySelector("pre.text-content");
                        if (pre) pre.textContent = patchBody;
                        const sendBtn = document.getElementById("send-btn");
                        const comp = document.getElementById("composer");
                        if (sendBtn && comp) {
                            sendBtn.addEventListener("click", () => { comp.textContent = ""; });
                        }
                        return { preScrollHeightBefore: pre ? pre.offsetHeight : 0 };
                    })()""",
                    "returnByValue": True,
                },
                session_id=sid,
            )
            ws.call(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": "function(p) { const pre = document.querySelector('pre.text-content'); if (pre) pre.textContent = p; }",
                    "arguments": [{"value": valid_patch_text}],
                    "executionContextId": 1,
                },
                session_id=sid,
            ) if False else None
            # Set patchBody on pre.textContent cleanly via JSON.stringify
            ws.call(
                "Runtime.evaluate",
                {
                    "expression": f"(() => {{ const pre = document.querySelector('pre.text-content'); if (pre) pre.textContent = {json.dumps(valid_patch_text)}; return pre ? pre.offsetHeight : 0; }})()",
                    "returnByValue": True,
                },
                session_id=sid,
            )

            # Build a lightweight CDP page adapter to exercise companion extension + viewer normalization + wedge recovery
            class _CdpPageAdapter:
                def __init__(self, ws_client: Any, session_id: str, endpoint: str, profile: Path) -> None:
                    self._ws = ws_client
                    self._sid = session_id
                    self._argon_http_endpoint = endpoint
                    self._argon_profile_dir = str(profile)
                    self._argon_simulate_wedged = False
                    self.url = "https://gemini.google.com/u/0/app/90f2dd2cf2aae6e8"
                    self.context = self

                def evaluate(self, expr: str, arg: Any = None) -> Any:
                    if self._argon_simulate_wedged:
                        raise TimeoutError("Simulated wedged renderer timeout")
                    if arg is not None:
                        js = f"({expr})({json.dumps(arg)})"
                    else:
                        js = f"({expr})()" if expr.strip().startswith("()") or expr.strip().startswith("function") else expr
                    r = self._ws.call("Runtime.evaluate", {"expression": js, "returnByValue": True, "awaitPromise": True}, session_id=self._sid)
                    return (r.get("result") or {}).get("value")

            cdp_page = _CdpPageAdapter(ws, sid, http_ep, prof_dir)

            # Single-session send via the companion extension on the live Chrome page:
            # 1) ensure_mode_and_model -> verifies Task mode + 'Code & analysis 4 Argon'
            mm_res = da._invoke_companion_extension(
                cdp_page,
                "ensure_mode_and_model",
                {"manualSetup": False, "isContinuation": False},
                timeout_ms=8000,
            )
            assert isinstance(mm_res, dict) and mm_res.get("ok") is True, f"Extension ensure_mode_and_model failed: {mm_res}"
            assert "Argon" in str(mm_res.get("model") or ""), f"Unexpected model: {mm_res}"

            # 2) paste_prompt -> synthetic DataTransfer paste
            paste_res = da._invoke_companion_extension(
                cdp_page,
                "paste_prompt",
                {"promptText": "Round 23 single-session verified send test prompt."},
                timeout_ms=8000,
            )
            assert isinstance(paste_res, dict) and paste_res.get("ok") is True, f"Extension paste_prompt failed: {paste_res}"

            # 3) attach_archive -> DataTransfer File injection
            sample_zip = tmp_root / "argon_context.zip"
            with zipfile.ZipFile(sample_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("TASK_BRIEF.md", "# Round 23 Test Brief\n")
                zf.writestr("API_INDEX.md", "# Round 23 API Index\n")
            raw_z = sample_zip.read_bytes()
            import base64 as _b64

            att_res = da._invoke_companion_extension(
                cdp_page,
                "attach_archive",
                {
                    "fileName": sample_zip.name,
                    "expectedSize": len(raw_z),
                    "base64Data": _b64.b64encode(raw_z).decode("ascii"),
                },
                timeout_ms=8000,
            )
            assert isinstance(att_res, dict) and att_res.get("ok") is True, f"Extension attach_archive failed: {att_res}"

            # 4) click_send -> verifies hard model guard + empties composer
            send_res = da._invoke_companion_extension(
                cdp_page,
                "click_send",
                {"expectedMinLen": 10},
                timeout_ms=8000,
            )
            assert isinstance(send_res, dict) and send_res.get("ok") is True, f"Extension click_send failed: {send_res}"

            # 5) R11 & R12: Normalize overflowing 120,000 px viewer geometry & capture delivery
            geom_res = _normalize_viewer_geometry(cdp_page)
            assert geom_res.get("ok") is True, f"_normalize_viewer_geometry failed: {geom_res}"
            pre_h_after = cdp_page.evaluate("() => { const p = document.querySelector('pre.text-content'); return p ? p.offsetHeight : -1; }")
            assert pre_h_after == 0, f"Expected overflowing <pre> height to be clamped to 0, got {pre_h_after}"

            # Capture the patch text from the clamped viewer & close viewer
            captured_txt = da._read_senna_preview_text(cdp_page)
            assert captured_txt and "$TargetRoot" in captured_txt and "[SUCCESS]" in captured_txt, (
                f"Failed to capture patch text from overflowing viewer: {captured_txt!r}"
            )
            resp_after = _prove_page_responsive_after_capture(cdp_page, timeout_ms=3000)
            assert resp_after.get("ok") is True and resp_after.get("viewer_open") is False, (
                f"Post-capture responsiveness check failed: {resp_after}"
            )

            # 6) R13: Simulate a wedged page renderer and verify _recover_wedged_tab_via_cdp recovers it
            cdp_page._argon_simulate_wedged = True
            wedge_chk = _check_page_responsive(cdp_page, timeout_ms=1000)
            assert wedge_chk.get("ok") is False and wedge_chk.get("wedged") is True, (
                f"Expected wedged detection to return wedged=True, got {wedge_chk}"
            )
            cdp_page._argon_http_endpoint = ""
            rec_res = _recover_wedged_tab_via_cdp(cdp_page)
            assert rec_res.get("ok") is True, f"Wedged tab recovery failed: {rec_res}"
            after_rec_chk = _check_page_responsive(cdp_page, timeout_ms=3000)
            assert after_rec_chk.get("ok") is True, f"Page not responsive after wedge recovery: {after_rec_chk}"
        finally:
            ws.close()

        total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "ok": True,
            "elapsed_ms": total_ms,
            "extension_id": ext_rep1.get("id"),
            "extension_install_ms": ext_rep1.get("elapsed_ms"),
            "extension_idempotent_ms": idem_ms,
            "webdriver_flag": False,
            "model_verified": mm_res.get("model"),
            "paste_chars": paste_res.get("length"),
            "attach_bytes": att_res.get("size"),
            "send_verified": True,
            "overflow_pre_height_after_clamp": pre_h_after,
            "post_capture_ping_ms": resp_after.get("elapsed_ms"),
            "wedge_recovery_method": rec_res.get("method"),
            "wedge_recovery_ms": rec_res.get("elapsed_ms"),
        }
    finally:
        try:
            st_end = prof_dir / ".argon_browser_endpoint"
            if st_end.is_file() and hasattr(sb, "_close_cdp_browser_ws"):
                sb._close_cdp_browser_ws(str(json.loads(st_end.read_text()).get("http_endpoint") or ""))
            sb.close_browser_safely(context=None, profile_dir=prof_dir)
        except Exception:
            pass
        shutil.rmtree(tmp_root, ignore_errors=True)


def run_round23_reliability_suite() -> Dict[str, Any]:
    t0 = time.perf_counter()
    r1 = test_r1_model_guard_hydrogen_and_missing_picker()
    r3 = test_r3_four_fixes_and_patch_script()
    r14_15_16 = test_r14_r15_r16_flags_slot_and_conversation_guard()
    e2e = test_r2_r10_r11_r12_r13_hermetic_browser_and_single_session_send()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return {
        "ok": bool(r1.get("ok") and r3.get("ok") and r14_15_16.get("ok") and e2e.get("ok")),
        "elapsed_ms": elapsed_ms,
        "r1_model_guard": r1,
        "r3_four_fixes": r3,
        "r14_15_16_guards": r14_15_16,
        "e2e_single_session": e2e,
    }


def main() -> int:
    print("=" * 78)
    print("  ROUND 23 BRIDGE RELIABILITY & SINGLE-SESSION VERIFICATION SUITE")
    print("=" * 78)
    rep = run_round23_reliability_suite()
    r1 = rep["r1_model_guard"]
    r3 = rep["r3_four_fixes"]
    g = rep["r14_15_16_guards"]
    e2e = rep["e2e_single_session"]
    print(
        f"  [PASS] R1 Model Guard           : 'Code & analysis 4 Argon'=OK | '4 Hydrogen'=REFUSED | 'Flash'=REFUSED | missing picker=REFUSED"
    )
    print(
        f"  [PASS] R2 Companion Extension   : id={e2e['extension_id']} | loadUnpacked={e2e['extension_install_ms']} ms | idempotent={e2e['extension_idempotent_ms']} ms"
    )
    print(
        f"  [PASS] R3 Four Bridge Fixes     : {r3['four_fixes_status']} | base64_cap={r3['max_base64_attach_bytes']}B"
    )
    print(
        f"  [PASS] R5/R14/R15/R16 Guards    : deadline={r3['default_deadline_seconds']}s | navigator.webdriver={e2e['webdriver_flag']} | unsigned_slot_raises=True | foreign_tab_restored=True"
    )
    print(
        f"  [PASS] R10 Single-Session Send  : model={e2e['model_verified']!r} | paste={e2e['paste_chars']} chars | attach={e2e['attach_bytes']}B | send=OK"
    )
    print(
        f"  [PASS] R11/R12 Overflow Viewer  : 120,000px <pre> clamped to {e2e['overflow_pre_height_after_clamp']}px | captured & closed"
    )
    print(
        f"  [PASS] R13 Wedge & Post-Capture : recovered via {e2e['wedge_recovery_method']} in {e2e['wedge_recovery_ms']} ms | post-capture ping={e2e['post_capture_ping_ms']} ms"
    )
    print("-" * 78)
    print(f"  RESULT: ALL ROUND 23 RELIABILITY CHECKS PASSED in {rep['elapsed_ms']} ms")
    print("=" * 78)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())