#!/usr/bin/env python3
"""Round 24 Hermetic Verification Suite — Three Work Units (W1, W2, W3).

Covers:
  - Work Unit 1 (R1.1–R1.6):
      * MAX_BASE64_ATTACH_BYTES is read by dispatch_argon.py and is the single source of truth
        (MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES deleted from patch_bridge_large_archive.py).
      * _attach_zip_via_datatransfer() checks zp.stat().st_size FIRST before zp.read_bytes(),
        never base64-encodes an archive above the cap, and returns "size_cap_exceeded".
      * Archive attach route order: CDP DOM.setFileInputFiles (zero-copy) -> Playwright set_input_files
        (zero-copy) -> companion-extension / DataTransfer base64 (<= cap, or loud warning fallback
        when no zero-copy route is available).
      * Prints `archive <name>: <bytes> bytes | cap <MAX_BASE64_ATTACH_BYTES> | route <chosen> | base64 <yes|no>`.
      * Overridable via ARGON_MAX_BASE64_ATTACH_MB with effective cap printed.
  - Work Unit 2 (R2.1–R2.5):
      * Additive CLI flag `--account-index N` resolves 0-based browser account position in
        Default/Preferences (`account_info`) -> owning pool slot.
      * Refuses unregistered browser account indices (`account_index_not_registered`) and
        conflicting `--slot` + `--account-index` pairs (`slot_account_index_conflict`).
      * Prints `slot /u/9/ (majnu.charitabari@gmail.com) = browser account index /u/8/ -> https://gemini.google.com/u/8/app`
        and warns when resolved index != slot number.
      * Updates `--brief-summary` and `--manual-handoff` with the resolved browser URL and
        one-line account resolution, never a raw pool-slot URL.
      * Reuses the existing run-log line in dispatch_argon.py to include `-> {target_url}`.
  - Work Unit 3 (R3.1–R3.4):
      * Enforces the 100 MB archive cap (`100 * 1024 * 1024` bytes) with named failure
        `archive_size_cap_exceeded` stating actual size, cap, and why.
      * Refuses attaching an archive > MAX_BASE64_ATTACH_BYTES to a conversation that already
        holds an attachment (`fresh_conversation_required_for_large_archive`).
      * Distinguishes `upload_still_running` ("the upload is still running") from
        `upload_failed` ("the upload failed") with elapsed time and effective window.
      * Never clicks send before the upload has finished and been confirmed.

All tests run hermetically against throwaway fixtures and a throwaway headless Chrome tab.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch_argon as da
import round23_reliability as r23
from argon_pool_login import PoolConfig
from manual_handoff import write_manual_handoff_pack
from patches import patch_bridge_large_archive


def _make_valid_context_zip(zip_path: Path, payload_bytes: int = 2048) -> Path:
    """Create a valid context zip containing TASK_BRIEF.md and API_INDEX.md of approximately `payload_bytes`."""
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("TASK_BRIEF.md", "# Task Brief\nRound 24 test brief.\n")
        zf.writestr("API_INDEX.md", "# API Index\nRound 24 test index.\n")
        filler_len = max(64, int(payload_bytes) - 256)
        zf.writestr("src/sample_module.py", "# " + ("A" * filler_len) + "\n")
    return zip_path


def _make_f7_pool_and_preferences_fixture(root: Path) -> tuple[Path, Path, PoolConfig]:
    """Create a hermetic replica of the measured F7 pool (slots 0..10) and Default/Preferences (9 accounts, indices 0..8)."""
    prof_dir = root / "chrome-profile"
    default_dir = prof_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # 9 signed-in browser accounts (indices 0..8):
    # /u/7/ (badiuzzamanmajnu786@gmail.com) and /u/10/ (takitahamid1000@gmail.com) are NOT signed in,
    # so pool /u/8/ (bmajnu786@gmail.com) is at browser index 7, and pool /u/9/ (majnu.charitabari@gmail.com) is at browser index 8.
    pref_accounts = [
        {"email": f"user{i}@example.com", "full_name": f"User {i}", "gaia": f"gaia_{i}"}
        for i in range(7)
    ] + [
        {"email": "bmajnu786@gmail.com", "full_name": "BMajnu 786", "gaia": "gaia_7"},
        {"email": "majnu.charitabari@gmail.com", "full_name": "Majnu Charitabari", "gaia": "gaia_8"},
    ]
    prefs_path = default_dir / "Preferences"
    prefs_path.write_text(
        json.dumps({"account_info": pref_accounts}, indent=2),
        encoding="utf-8",
    )

    pool_accounts = [
        {
            "index": i,
            "slot": f"/u/{i}/",
            "account_index": i,
            "email": f"user{i}@example.com",
            "note": f"Pool slot {i}",
        }
        for i in range(7)
    ] + [
        {
            "index": 7,
            "slot": "/u/7/",
            "account_index": None,
            "email": "badiuzzamanmajnu786@gmail.com",
            "note": "Not signed in",
        },
        {
            "index": 8,
            "slot": "/u/8/",
            "account_index": 7,
            "email": "bmajnu786@gmail.com",
            "note": "Browser /u/7/",
        },
        {
            "index": 9,
            "slot": "/u/9/",
            "account_index": 8,
            "email": "majnu.charitabari@gmail.com",
            "note": "Browser /u/8/",
        },
        {
            "index": 10,
            "slot": "/u/10/",
            "account_index": None,
            "email": "takitahamid1000@gmail.com",
            "note": "Not signed in",
        },
    ]
    pool_path = root / "accounts_pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "profile_dir": str(prof_dir),
                "shared_profile": True,
                "default_slot": "/u/0/",
                "last_used_slot": "/u/0/",
                "accounts": pool_accounts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    cfg = PoolConfig(config_path=pool_path, profile_dir_override=prof_dir)
    return pool_path, prof_dir, cfg


def case_1_workunit1_base64_cap_and_routes() -> Dict[str, Any]:
    """Verify R1.1–R1.6: MAX_BASE64_ATTACH_BYTES is single source of truth, stat-before-read, route order, one-line log, and fallback."""
    # R1.1: MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES is gone from patch_bridge_large_archive
    assert not hasattr(patch_bridge_large_archive, "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES"), (
        "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES must be removed from patch_bridge_large_archive.py"
    )
    patch_src = Path(patch_bridge_large_archive.__file__).read_text(encoding="utf-8")
    assert "MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES" not in patch_src, (
        "patch_bridge_large_archive.py source must not contain MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES"
    )
    assert da.MAX_BASE64_ATTACH_BYTES == r23.MAX_BASE64_ATTACH_BYTES, (
        "dispatch_argon.MAX_BASE64_ATTACH_BYTES must match round23_reliability.MAX_BASE64_ATTACH_BYTES"
    )

    with tempfile.TemporaryDirectory(prefix="r24_w1_") as tmp:
        root = Path(tmp)
        small_zip = _make_valid_context_zip(root / "small_context.zip", payload_bytes=4096)
        large_zip = _make_valid_context_zip(root / "large_context.zip", payload_bytes=32768)

        # R1.6: Override cap via ARGON_MAX_BASE64_ATTACH_MB so `small_zip` (~4 KB) is <= cap
        # and `large_zip` (~32 KB) is > cap (0.01 MB = 10,485 bytes).
        old_env = os.environ.get("ARGON_MAX_BASE64_ATTACH_MB")
        try:
            os.environ["ARGON_MAX_BASE64_ATTACH_MB"] = "0.01"
            eff_cap = r23.get_effective_base64_attach_cap()
            assert 10000 <= eff_cap <= 11000, f"Unexpected effective cap for 0.01 MB: {eff_cap}"

            # R1.2: _attach_zip_via_datatransfer checks stat().st_size FIRST before read_bytes()
            # and returns "size_cap_exceeded" without reading or base64-encoding.
            read_bytes_calls: List[str] = []
            orig_read_bytes = Path.read_bytes
            orig_b64encode = base64.b64encode
            b64_calls: List[int] = []

            def _spy_read_bytes(self_p: Path) -> bytes:
                if self_p.name == large_zip.name:
                    read_bytes_calls.append(self_p.name)
                return orig_read_bytes(self_p)

            def _spy_b64encode(data: bytes, *a: Any, **kw: Any) -> bytes:
                b64_calls.append(len(data))
                return orig_b64encode(data, *a, **kw)

            Path.read_bytes = _spy_read_bytes  # type: ignore[method-assign]
            base64.b64encode = _spy_b64encode  # type: ignore[assignment]
            try:
                dt_ok, dt_path, dt_reason = da._attach_zip_via_datatransfer(
                    page=object(),
                    zip_path=large_zip,
                    use_extension=False,
                )
            finally:
                Path.read_bytes = orig_read_bytes  # type: ignore[method-assign]
                base64.b64encode = orig_b64encode  # type: ignore[assignment]

            assert not dt_ok and dt_reason == "size_cap_exceeded", (
                f"Expected size_cap_exceeded for over-cap archive, got ok={dt_ok}, path={dt_path}, reason={dt_reason}"
            )
            assert len(read_bytes_calls) == 0, (
                f"_attach_zip_via_datatransfer read bytes of over-cap archive before checking size: {read_bytes_calls}"
            )
            assert len(b64_calls) == 0, (
                f"_attach_zip_via_datatransfer base64-encoded over-cap archive: {b64_calls}"
            )

            # R1.3 & R1.5: Test real zero-copy CDP DOM.setFileInputFiles attach on a throwaway headless Chrome tab
            from test_round24_bugfixes import _chrome, _launch

            cdp_attached_size = 0
            small_route_line = ""
            large_route_line = ""
            with _chrome() as pw:
                browser = _launch(pw)
                try:
                    page = browser.new_page()
                    fixture_html = root / "composer.html"
                    fixture_html.write_text(
                        """<!doctype html><html><body>
                        <div class="input-area">
                          <div class="ql-editor" contenteditable="true" role="textbox" aria-label="Enter a prompt"></div>
                          <input id="zip-file" type="file" accept=".zip,application/zip" />
                          <div id="attachment-chips"></div>
                        </div>
                        <script>
                        const inp = document.getElementById('zip-file');
                        inp.addEventListener('change', () => {
                          const wrap = document.getElementById('attachment-chips');
                          wrap.innerHTML = '';
                          for (const f of Array.from(inp.files || [])) {
                            const c = document.createElement('div');
                            c.className = 'att-chip';
                            c.textContent = f.name.replace(/\\.zip$/i, '');
                            wrap.appendChild(c);
                          }
                        });
                        </script>
                        </body></html>""",
                        encoding="utf-8",
                    )
                    page.goto(fixture_html.as_uri(), wait_until="domcontentloaded")

                    # 1. Small archive (<= cap) uses base64 DataTransfer route
                    buf_small = io.StringIO()
                    with contextlib.redirect_stdout(buf_small):
                        ok_s = da.attach_zip(page, small_zip)
                    out_small = buf_small.getvalue()
                    assert ok_s, f"Small archive attach failed: {out_small}"
                    small_route_line = str(getattr(page, "_argon_last_route_decision_line", ""))
                    assert f"archive {small_zip.name}: {small_zip.stat().st_size} bytes | cap {eff_cap} | route " in small_route_line
                    assert small_route_line.endswith("| base64 yes"), f"Unexpected small route line: {small_route_line}"

                    # Clear composer attachment before testing large archive in a fresh state
                    page.evaluate(
                        """() => {
                            const inp = document.getElementById('zip-file');
                            if (inp) inp.value = '';
                            const wrap = document.getElementById('attachment-chips');
                            if (wrap) wrap.innerHTML = '';
                        }"""
                    )

                    # 2. Large archive (> cap) skips base64 and uses zero-copy CDP DOM.setFileInputFiles
                    buf_large = io.StringIO()
                    with contextlib.redirect_stdout(buf_large):
                        ok_l = da.attach_zip(page, large_zip)
                    out_large = buf_large.getvalue()
                    assert ok_l, f"Large archive zero-copy attach failed: {out_large}"
                    large_route_line = str(getattr(page, "_argon_last_route_decision_line", ""))
                    expected_large_line = (
                        f"archive {large_zip.name}: {large_zip.stat().st_size} bytes | "
                        f"cap {eff_cap} | route cdp_dom_set_file_input_files | base64 no"
                    )
                    assert large_route_line == expected_large_line, (
                        f"Expected route line {expected_large_line!r}, got {large_route_line!r}"
                    )
                    assert getattr(page, "_argon_last_attach_path", "") in ("cdp_dom_setFileInputFiles", "cdp_dom_file_input"), (
                        f"Expected cdp_dom_file_input attach path, got {getattr(page, '_argon_last_attach_path', '')!r}"
                    )
                    cdp_attached_size = int(
                        page.evaluate(
                            "() => { const i = document.getElementById('zip-file'); return (i && i.files && i.files[0]) ? i.files[0].size : 0; }"
                        )
                        or 0
                    )
                    assert cdp_attached_size == large_zip.stat().st_size

                    # 3. R1.4: If zero-copy routes are unavailable for an over-cap archive,
                    # attach_zip logs a loud [WARNING] and falls back to base64 DataTransfer so attach does not fail.
                    page.evaluate(
                        """() => {
                            const inp = document.getElementById('zip-file');
                            if (inp) inp.value = '';
                            const wrap = document.getElementById('attachment-chips');
                            if (wrap) wrap.innerHTML = '';
                        }"""
                    )
                    orig_cdp_fn = da._attach_zip_via_cdp_dom_file_input
                    orig_zip_input_fn = da._zip_input
                    orig_locator = page.locator
                    try:
                        da._attach_zip_via_cdp_dom_file_input = lambda p, z: (False, "cdp_dom_file_input", "simulated_cdp_unavailable")
                        da._zip_input = lambda p: None

                        class _EmptyLocator:
                            def count(self) -> int:
                                return 0

                        page.locator = lambda sel: _EmptyLocator() if "file" in str(sel) else orig_locator(sel)
                        buf_fb_out = io.StringIO()
                        buf_fb_err = io.StringIO()
                        with contextlib.redirect_stdout(buf_fb_out), contextlib.redirect_stderr(buf_fb_err):
                            ok_fb = da.attach_zip(page, large_zip)
                        assert ok_fb, f"Expected over-cap base64 fallback to succeed when zero-copy routes unavailable, stderr={buf_fb_err.getvalue()}"
                        assert "[WARNING]" in buf_fb_err.getvalue() and "continuing with base64 DataTransfer route" in buf_fb_err.getvalue()
                        assert "base64 yes" in buf_fb_out.getvalue()
                    finally:
                        da._attach_zip_via_cdp_dom_file_input = orig_cdp_fn
                        da._zip_input = orig_zip_input_fn
                        page.locator = orig_locator
                finally:
                    browser.close()
        finally:
            if old_env is None:
                os.environ.pop("ARGON_MAX_BASE64_ATTACH_MB", None)
            else:
                os.environ["ARGON_MAX_BASE64_ATTACH_MB"] = old_env

    return {
        "ok": True,
        "small_route_line": small_route_line,
        "large_route_line": large_route_line,
        "cdp_attached_size": cdp_attached_size,
    }


def case_2_workunit2_account_slot_semantics() -> Dict[str, Any]:
    """Verify R2.1–R2.5: --account-index N, refusal on unregistered/conflicting index, one-line resolution, warning, and brief/handoff URLs."""
    parser = da.build_arg_parser()
    parsed = parser.parse_args(["--task", "test", "--account-index", "8"])
    assert parsed.account_index == 8, f"Expected --account-index 8, got {parsed.account_index}"

    with tempfile.TemporaryDirectory(prefix="r24_w2_") as tmp:
        root = Path(tmp)
        pool_path, prof_dir, cfg = _make_f7_pool_and_preferences_fixture(root)

        # R2.1 & R2.2: Resolve --account-index 8 -> pool slot /u/9/ (majnu.charitabari@gmail.com)
        res8 = r23.resolve_account_index_to_slot(8, pool_cfg=cfg, profile_dir_override=prof_dir)
        expected_line_8 = (
            "slot /u/9/ (majnu.charitabari@gmail.com) = browser account index /u/8/ "
            "-> https://gemini.google.com/u/8/app"
        )
        assert res8["slot"] == "/u/9/"
        assert res8["email"] == "majnu.charitabari@gmail.com"
        assert res8["account_index"] == 8
        assert res8["line"] == expected_line_8, f"Expected {expected_line_8!r}, got {res8['line']!r}"
        assert res8["index_differs_from_slot"] is True
        assert "[WARNING]" in res8["warning"]

        # Resolve --account-index 7 -> pool slot /u/8/ (bmajnu786@gmail.com)
        res7 = r23.resolve_account_index_to_slot(7, pool_cfg=cfg, profile_dir_override=prof_dir)
        assert res7["slot"] == "/u/8/"
        assert res7["email"] == "bmajnu786@gmail.com"
        assert res7["account_index"] == 7

        # R2.1: Refuse when N is not a registered browser account (9 accounts -> index 9 does not exist)
        unreg_err = ""
        try:
            r23.resolve_account_index_to_slot(9, pool_cfg=cfg, profile_dir_override=prof_dir)
        except RuntimeError as exc:
            unreg_err = str(exc)
        assert "account_index_not_registered" in unreg_err, (
            f"Expected account_index_not_registered for index 9, got {unreg_err!r}"
        )

        # R2.5: Refuse when both --account-index and --slot are given and disagree
        conflict_err = ""
        try:
            r23.resolve_account_index_to_slot(
                8,
                pool_cfg=cfg,
                profile_dir_override=prof_dir,
                explicit_slot="/u/5/",
            )
        except RuntimeError as exc:
            conflict_err = str(exc)
        assert "slot_account_index_conflict" in conflict_err, (
            f"Expected slot_account_index_conflict, got {conflict_err!r}"
        )

        # Agreeing --slot /u/9/ and --account-index 8 succeeds
        agree_res = r23.resolve_account_index_to_slot(
            8,
            pool_cfg=cfg,
            profile_dir_override=prof_dir,
            explicit_slot="/u/9/",
        )
        assert agree_res["slot"] == "/u/9/" and agree_res["account_index"] == 8

        # R2.3: --brief-summary and --manual-handoff must carry the resolved browser URL, never a raw pool-slot URL
        brief_summary_path = root / "brief_summary.md"
        brief_summary_path.write_text(
            "# Brief Summary — test\n\n"
            "- **Project:** `G:\\Developing\\argon-coding-bridge`\n"
            "- **Slot:** `/u/9/`\n"
            "- **Account Email:** `(from pool config)`\n",
            encoding="utf-8",
        )
        r23.update_brief_summary_with_resolved_slot(
            brief_summary_path,
            slot="/u/9/",
            email="majnu.charitabari@gmail.com",
            account_index=8,
            resolved_url="https://gemini.google.com/u/8/app",
        )
        bs_text = brief_summary_path.read_text(encoding="utf-8")
        assert "https://gemini.google.com/u/8/app" in bs_text
        assert expected_line_8 in bs_text
        assert "- **Slot:** `/u/9/`" not in bs_text
        assert "https://gemini.google.com/u/9/app" not in bs_text

        proj_dir = root / "proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        zip_p = _make_valid_context_zip(proj_dir / "argon_context.zip", payload_bytes=1024)
        md_p, json_p = write_manual_handoff_pack(
            proj_root=proj_dir,
            lane="new",
            slot="/u/9/",
            email="majnu.charitabari@gmail.com",
            chat_url="https://gemini.google.com/u/9/app",
            prompt_text="Test prompt",
            zip_path=zip_p,
        )
        r23.update_manual_handoff_with_resolved_slot(
            md_p,
            json_p,
            slot="/u/9/",
            email="majnu.charitabari@gmail.com",
            account_index=8,
            resolved_url="https://gemini.google.com/u/8/app",
        )
        md_txt = md_p.read_text(encoding="utf-8")
        json_data = json.loads(json_p.read_text(encoding="utf-8"))
        assert "https://gemini.google.com/u/8/app" in md_txt
        assert "https://gemini.google.com/u/9/app" not in md_txt
        assert expected_line_8 in md_txt
        assert json_data["chat_url"] == "https://gemini.google.com/u/8/app"
        assert json_data["resolved_browser_url"] == "https://gemini.google.com/u/8/app"
        assert json_data["account_resolution_line"] == expected_line_8

    return {
        "ok": True,
        "resolved_line": expected_line_8,
        "unregistered_reason": "account_index_not_registered",
        "conflict_reason": "slot_account_index_conflict",
    }


def case_3_workunit3_archive_cap_fresh_chat_and_upload_state() -> Dict[str, Any]:
    """Verify R3.1–R3.4: 100 MB cap, fresh conversation required for large archive, upload_still_running vs upload_failed, and no premature send."""
    with tempfile.TemporaryDirectory(prefix="r24_w3_") as tmp:
        root = Path(tmp)

        # R3.1: 100 MB archive cap enforced with named failure `archive_size_cap_exceeded`
        over_100mb_zip = root / "over_100mb.zip"
        with open(over_100mb_zip, "wb") as f:
            f.seek((100 * 1024 * 1024) + 4096 - 1)
            f.write(b"\x00")
        ok_100, sz_100, _, err_100 = da._inspect_zip_archive(over_100mb_zip)
        assert not ok_100, "Expected _inspect_zip_archive to reject >100 MB archive"
        assert "archive_size_cap_exceeded" in err_100, f"Expected archive_size_cap_exceeded, got {err_100!r}"
        assert str(100 * 1024 * 1024) in err_100 and str(sz_100) in err_100

        class _DummyPage:
            def __init__(self) -> None:
                self._argon_last_operation_error = None
                self._argon_has_existing_attachment = False
                self._argon_existing_attachment_detail = ""
                self._argon_upload_wait_cap_seconds = 0.25

        dummy_p = _DummyPage()
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err):
            att_100 = da.attach_zip(dummy_p, over_100mb_zip)
        assert not att_100
        op_err_100 = str(getattr(dummy_p._argon_last_operation_error, "reason", ""))
        assert "archive_size_cap_exceeded" in op_err_100

        # R3.2: Archive > MAX_BASE64_ATTACH_BYTES requires a FRESH conversation; refuses if chat already holds an attachment
        large_valid_zip = _make_valid_context_zip(root / "large_valid.zip", payload_bytes=24576)
        old_env = os.environ.get("ARGON_MAX_BASE64_ATTACH_MB")
        try:
            os.environ["ARGON_MAX_BASE64_ATTACH_MB"] = "0.01"
            non_fresh_page = _DummyPage()
            non_fresh_page._argon_has_existing_attachment = True
            non_fresh_page._argon_existing_attachment_detail = "existing 49 MB attachment chip in conversation"
            buf_nf = io.StringIO()
            with contextlib.redirect_stderr(buf_nf):
                att_nf = da.attach_zip(non_fresh_page, large_valid_zip)
            assert not att_nf, "attach_zip must refuse attaching large archive to non-fresh chat with existing attachment"
            nf_reason = str(getattr(non_fresh_page._argon_last_operation_error, "reason", ""))
            assert "fresh_conversation_required_for_large_archive" in nf_reason, (
                f"Expected fresh_conversation_required_for_large_archive, got {nf_reason!r}"
            )
        finally:
            if old_env is None:
                os.environ.pop("ARGON_MAX_BASE64_ATTACH_MB", None)
            else:
                os.environ["ARGON_MAX_BASE64_ATTACH_MB"] = old_env

        # R3.3: Distinguish `upload_still_running` from `upload_failed`, reporting elapsed time and effective window
        valid_small_zip = _make_valid_context_zip(root / "valid_small.zip", payload_bytes=2048)
        orig_present = da._attachment_present
        orig_in_prog = da._composer_upload_in_progress
        try:
            # Case A: spinner still active -> upload_still_running
            p_running = _DummyPage()
            da._attachment_present = lambda p, z: False
            da._composer_upload_in_progress = lambda p: True
            buf_run = io.StringIO()
            with contextlib.redirect_stderr(buf_run):
                ok_run = da._wait_for_upload_complete(p_running, valid_small_zip, timeout=600.0)
            assert not ok_run
            assert getattr(p_running, "_argon_last_upload_status", "") == "upload_still_running"
            assert "the upload is still running" in getattr(p_running, "_argon_last_upload_error", "")
            assert getattr(p_running, "_argon_last_upload_window", 0) >= 600.0

            # Case B: spinner stopped and attachment missing -> upload_failed
            p_failed = _DummyPage()
            da._attachment_present = lambda p, z: False
            da._composer_upload_in_progress = lambda p: False
            buf_fail = io.StringIO()
            with contextlib.redirect_stderr(buf_fail):
                ok_fail = da._wait_for_upload_complete(p_failed, valid_small_zip, timeout=600.0)
            assert not ok_fail
            assert getattr(p_failed, "_argon_last_upload_status", "") == "upload_failed"
            assert "the upload failed" in getattr(p_failed, "_argon_last_upload_error", "")
            assert getattr(p_failed, "_argon_last_upload_window", 0) >= 600.0
        finally:
            da._attachment_present = orig_present
            da._composer_upload_in_progress = orig_in_prog

        # R3.4: Never send the prompt before the upload has finished.
        # Verify in _execute_composer_send_sequence that if upload is not confirmed, _click_send_immediately is NEVER called.
        send_clicked = False
        orig_click_send = da._click_send_immediately
        orig_find_comp = da._find_composer
        orig_comp_text = da._composer_text
        orig_paste = da._paste_into_composer
        orig_select_argon = da.select_argon_model
        orig_verify_chip = da._verify_model_chip_is_argon
        orig_attach_zip = da.attach_zip
        try:
            def _spy_click_send(*a: Any, **kw: Any) -> bool:
                nonlocal send_clicked
                send_clicked = True
                return True

            da._click_send_immediately = _spy_click_send
            da._find_composer = lambda p, wait_seconds=0.0: object()
            da._composer_text = lambda c: "Prompt text ready in composer for Round 24 test"
            da._paste_into_composer = lambda p, c, t: t
            da.select_argon_model = lambda p, require_chip=True: True
            da._verify_model_chip_is_argon = lambda p, stage="": (True, "Code & analysis 4 Argon")
            # Simulate attach_zip returning True, but spinner still active right before send
            da.attach_zip = lambda p, z: True
            da._composer_upload_in_progress = lambda p: True
            da._attachment_present = lambda p, z: True

            p_guard = _DummyPage()
            buf_g = io.StringIO()
            with contextlib.redirect_stderr(buf_g):
                _, _, sent_ok = da._execute_composer_send_sequence(
                    page=p_guard,
                    zip_path=valid_small_zip,
                    prompt_text="Prompt text ready in composer for Round 24 test",
                    manual_setup=True,
                )
            assert not sent_ok, "Must not send prompt when upload spinner is still active"
            assert not send_clicked, "_click_send_immediately must NEVER be called when upload is not finished"
            assert "upload_still_running" in str(getattr(p_guard, "_argon_failed_reason", ""))
        finally:
            da._click_send_immediately = orig_click_send
            da._find_composer = orig_find_comp
            da._composer_text = orig_comp_text
            da._paste_into_composer = orig_paste
            da.select_argon_model = orig_select_argon
            da._verify_model_chip_is_argon = orig_verify_chip
            da.attach_zip = orig_attach_zip
            da._attachment_present = orig_present
            da._composer_upload_in_progress = orig_in_prog

    return {
        "ok": True,
        "cap_100mb_reason": "archive_size_cap_exceeded",
        "fresh_chat_reason": "fresh_conversation_required_for_large_archive",
        "upload_states_verified": ["upload_still_running", "upload_failed"],
        "send_blocked_before_upload": True,
    }


def run_round24_workunits_suite() -> Dict[str, Any]:
    """Run all three Round 24 work-unit verification cases and return a structured report."""
    t0 = time.perf_counter()
    c1 = case_1_workunit1_base64_cap_and_routes()
    c2 = case_2_workunit2_account_slot_semantics()
    c3 = case_3_workunit3_archive_cap_fresh_chat_and_upload_state()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    cases = [c1, c2, c3]
    passed = sum(1 for c in cases if c.get("ok"))
    return {
        "ok": passed == len(cases),
        "passed": passed,
        "total": len(cases),
        "elapsed_ms": elapsed_ms,
        "base64_cap_bytes": r23.get_effective_base64_attach_cap(da.MAX_BASE64_ATTACH_BYTES),
        "workunit_1": c1,
        "workunit_2": c2,
        "workunit_3": c3,
    }


def main() -> int:
    print("=" * 78)
    print("  ROUND 24 WORK UNITS SUITE (W1 base64 cap/routes, W2 slot/index, W3 100MB/fresh chat)")
    print("=" * 78)
    rep = run_round24_workunits_suite()
    print(f"  [PASS] Work Unit 1: {rep['workunit_1']['large_route_line']}")
    print(f"  [PASS] Work Unit 2: {rep['workunit_2']['resolved_line']}")
    print(
        f"  [PASS] Work Unit 3: {rep['workunit_3']['cap_100mb_reason']}, "
        f"{rep['workunit_3']['fresh_chat_reason']}, states={rep['workunit_3']['upload_states_verified']}"
    )
    print(f"  Result: {rep['passed']}/{rep['total']} PASS in {rep['elapsed_ms']} ms")
    print("=" * 78)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
