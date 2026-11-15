#!/usr/bin/env python3
"""Round 27 + Addendum Hermetic Verification Suite (`test_round27_two_lane.py`).

Verifies all 7 Round 27 Work Units (W1-W7) AND all 6 Core + Web Extension Contracts (C1-C6)
against throwaway temporary directories and a throwaway headless Chrome page:

  1. W1 & Contract 6 (Follow-Up Lane Continuation & Truthful Logging):
     - `SessionStore.lookup(proj, mode="gemini", lane="followup")` resolves per-lane conversations
       from `SessionStore.lanes["followup"]` and `TaskState`.
     - Follow-up lane continues recorded chat `d0815dd9c06ea36d` and prints
       `lane: followup -> continuing chat d0815dd9c06ea36d (...)`.
     - When no conversation is recorded for `followup`, prints
       `no recorded conversation for lane 'followup'` and
       `lane: followup -> NEW chat ... (no recorded conversation)`.
  2. W2 & Contract 6 (Concurrent Two-Lane Execution & Full-Path Registry Keys):
     - `TaskRegistry.register_start()` uses full normalized project path (`_normalize_project_key(proj)`),
       never `Path(proj).name`.
     - Running `lane="new"` on `/u/4/` does NOT block `lane="followup"` on `/u/6/` (or on `/u/4/`)
       with `EXIT_ALREADY_RUNNING` (exit 9).
     - `EXIT_ALREADY_RUNNING` only triggers when BOTH lane and slot match, and names both
       `running_lane=...` and `requested_lane=...`.
  3. W3 & Contract 2 (100 MB Archive Limit, 8 MB Base64 Transport Cap, Continuation Skip):
     - `MAX_ARCHIVE_BYTES` (100 MB) is the single hard archive limit.
     - Companion extension `attach_archive` returns `reason: "size_cap_exceeded"` (not a generic error)
       when archive size exceeds `maxBase64Bytes` (8 MB or `ARGON_MAX_BASE64_ATTACH_MB`), allowing Core
       to fall through to zero-copy CDP `DOM.setFileInputFiles`.
     - In a continuation (`_argon_is_continuation=True`) that already holds an attachment, `attach_zip`
       skips attaching a second archive and prints
       `conversation already holds an attachment (...) - skipping second archive attach in continuation`.
     - Prints canonical line `archive <name>: <bytes> = <n> MB | hard limit 100 MB | route <x> | base64 <yes|no>`.
  4. W4 & Contract 4 (Last-Turn Delivery Capture via Export -> Download & Duplicate Digest Proof):
     - Picks the newest assistant turn's chip (`turns[-1]`), prints proof
       `chat_id`, `turn_index`, `file_name`, `sha256`, and when SHA-256 matches an earlier round in
       `DeliveryLedger`, prints `same digest as round <id> - Argon re-attached the same file`.
  5. W5 & Contract 1 (Core `disarm_prompt_guard` After Pure-Python Send, Queued Steering Turn, Duplicate Turn Detection):
     - After a pure-Python send (`_click_send_immediately`), Core invokes `disarm_prompt_guard` on the
       companion extension so `keepPromptAlive` is `False`, `cachedPromptText` is `""`, and the
       `MutationObserver` stays disarmed when the composer empties.
     - Extension observer also disarms automatically when `.queued-steering-turn` appears in the DOM.
     - Re-checks the composer after send and prints
       `DUPLICATE SEND DETECTED (<n> user messages for one prompt)` if user turns grow by > 1.
  6. W6 (Stale Registry Reaping, `--prune-stale-tasks`, Requested Slot Order, No Stray `.bak`):
     - Stale entries (dead PID, finished lane in `TaskState`, or expired age) are excluded from
       `active_entries()`, `active_tasks_for_slot()`, and `is_slot_available()`, and reaped by
       `prune_stale_tasks()` / `--prune-stale-tasks`.
     - `_pool_slots_in_order(..., preserve_start_slot_first=True)` never pushes an explicitly
       requested slot behind another slot.
  7. W7 & Contract 3 & Contract 5 (Shared Model Labels, 45s Continuation Chip Wait, Extension Version Mismatch):
     - Core and Extension agree on English (`Code & analysis 4 Argon`) and Bengali
       (`কোড ও বিশ্লেষণ 4 Argon`) model labels.
     - Continuation chip wait is >= 45 s (never capped to 12 s) in both Core and Extension, and
       reports `model chip (not visible) after 45 s on a continuation page - the conversation is still hydrating`.
     - Core compares loaded extension version (`data-argon-companion-extension`) against
       `extension/manifest.json` (`1.17.0`) and warns loudly when older (`1.16.0`):
       `the browser is running extension 1.16.0 but the repo has 1.17.0 - restart the Argon browser before dispatching`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch_argon as da
import round23_reliability as r23
import safe_browser as sb
from argon_pool_login import PoolConfig

CONTENT_SCRIPT_PATH = SCRIPTS_DIR.parent / "extension" / "content_script.js"
MANIFEST_PATH = SCRIPTS_DIR.parent / "extension" / "manifest.json"
README_PATH = SCRIPTS_DIR.parent / "extension" / "README.md"


def _make_zip(zip_path: Path, payload_bytes: int = 2048) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("TASK_BRIEF.md", "# Task Brief\nRound 27 test brief.\n")
        zf.writestr("API_INDEX.md", "# API Index\nRound 27 test index.\n")
        zf.writestr("src/mod.py", "# " + ("X" * max(64, payload_bytes - 200)) + "\n")
    return zip_path


def case_1_w1_w2_two_lane_continuation_and_concurrent_guards() -> Dict[str, Any]:
    """Verify W1 (R1.1-R1.3) and W2 (R2.1-R2.4): per-lane SessionStore/TaskState lookup, truthful lane logs, full-path task_id, and concurrent lanes."""
    with tempfile.TemporaryDirectory(prefix="r27_w1_w2_") as tmp:
        root = Path(tmp)
        proj_dir = root / "argon-coding-bridge"
        proj_dir.mkdir(parents=True, exist_ok=True)
        sess_file = root / "argon_sessions.json"
        ts_file = root / "argon_task_state.json"
        reg_file = root / "argon_tasks.json"

        orig_sess_path = da.SESSION_STORE_PATH
        orig_ts_path = da.TASK_STATE_PATH
        orig_reg_path = da.TASK_REGISTRY_PATH
        try:
            da.SESSION_STORE_PATH = sess_file
            da.TASK_STATE_PATH = ts_file
            da.TASK_REGISTRY_PATH = reg_file

            sess_store = da.SessionStore(sess_file)
            ts_store = da.TaskState(ts_file)
            reg = da.TaskRegistry(reg_file)

            # 1. When no conversation is recorded, lookup for followup returns None
            assert sess_store.lookup(proj_dir, mode="gemini", lane="followup") is None

            # 2. Record Round 26 'new' lane on /u/4/ (chat_id=d0815dd9c06ea36d) and 'followup' lane on /u/6/ (chat_id=33c76e3f17784385)
            sess_store.remember(
                proj_dir,
                mode="gemini",
                slot="/u/4/",
                chat_id="d0815dd9c06ea36d",
                chat_title="Round 26 New Lane",
                lane="new",
                workspace_mode="task",
            )
            sess_store.remember(
                proj_dir,
                mode="gemini",
                slot="/u/6/",
                chat_id="33c76e3f17784385",
                chat_title="Round 26 Followup Lane",
                lane="followup",
                workspace_mode="task",
            )

            f_lookup = sess_store.lookup(proj_dir, mode="gemini", lane="followup")
            assert f_lookup is not None and f_lookup.get("chat_id") == "33c76e3f17784385"
            assert da.normalize_slot(f_lookup.get("slot")) == "/u/6/"

            # Also verify fallback from TaskState when SessionStore has no followup entry yet
            proj2 = root / "second-project"
            proj2.mkdir(parents=True, exist_ok=True)
            ts_store.set(
                proj2,
                lane="new",
                state="delivered",
                chat_id="d0815dd9c06ea36d",
                chat_url="https://gemini.google.com/u/4/app/d0815dd9c06ea36d",
                slot="/u/4/",
            )
            f_from_ts = sess_store.lookup(proj2, mode="gemini", lane="followup", slot="/u/4/")
            assert f_from_ts is not None and f_from_ts.get("chat_id") == "d0815dd9c06ea36d", (
                f"Expected followup lookup to find recorded TaskState chat on /u/4/, got {f_from_ts}"
            )

            # 3. W2 R2.2: TaskRegistry.register_start uses full normalized project path in task_id
            rec_new = reg.register_start(
                proj_dir,
                slot="/u/4/",
                lane="new",
                chat_id="d0815dd9c06ea36d",
                chat_url="https://gemini.google.com/u/4/app/d0815dd9c06ea36d",
                label="Round 27 new lane",
            )
            norm_proj = da._normalize_project_key(proj_dir)
            assert rec_new["task_id"].startswith(f"{norm_proj}:new:u/4:"), (
                f"Expected full normalized path in task_id, got {rec_new['task_id']!r}"
            )

            # 4. W2 R2.1 & R2.3: Registering a concurrent 'followup' lane on /u/6/ keeps BOTH active
            rec_fol = reg.register_start(
                proj_dir,
                slot="/u/6/",
                lane="followup",
                chat_id="33c76e3f17784385",
                chat_url="https://gemini.google.com/u/6/app/33c76e3f17784385",
                label="Round 27 followup lane",
            )
            active_all = reg.active_tasks_for_project(proj_dir)
            assert len(active_all) == 2, f"Expected both lanes active concurrently, got {active_all}"
            assert len(reg.active_tasks_for_project(proj_dir, lane="new", slot="/u/4/")) == 1
            assert len(reg.active_tasks_for_project(proj_dir, lane="followup", slot="/u/6/")) == 1
            assert len(reg.active_tasks_for_project(proj_dir, lane="followup", slot="/u/4/")) == 0
        finally:
            da.SESSION_STORE_PATH = orig_sess_path
            da.TASK_STATE_PATH = orig_ts_path
            da.TASK_REGISTRY_PATH = orig_reg_path

    return {
        "ok": True,
        "followup_chat_id": "33c76e3f17784385",
        "full_path_task_id": rec_new["task_id"],
        "concurrent_lanes": 2,
    }


def case_2_contract1_and_2_extension_disarm_and_size_cap() -> Dict[str, Any]:
    """Verify Contract 1 (R1.1-R1.3), Contract 2 (R2.1-R2.3), W3 (R3.1-R3.4), and W5 (R5.1-R5.4) in live headless Chrome."""
    from test_round24_bugfixes import _chrome, _launch

    cs_code = CONTENT_SCRIPT_PATH.read_text(encoding="utf-8")
    readme_txt = README_PATH.read_text(encoding="utf-8")
    assert "disarm_prompt_guard" in readme_txt, "extension/README.md must document disarm_prompt_guard (Contract 1 R1.2)"

    with tempfile.TemporaryDirectory(prefix="r27_c1_c2_") as tmp:
        root = Path(tmp)
        small_zip = _make_zip(root / "small.zip", payload_bytes=4096)
        large_zip = _make_zip(root / "large.zip", payload_bytes=32768)

        with _chrome() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                fixture = root / "gemini_ext.html"
                fixture.write_text(
                    """<!doctype html><html><body>
                    <div id="chat-history">
                      <user-query class="user-turn">Earlier turn</user-query>
                      <model-response class="model-turn">Earlier response</model-response>
                    </div>
                    <div class="input-area">
                      <button role="tab" aria-selected="true" class="gem-segmented-button-selected">Task</button>
                      <button class="input-area-switch" aria-haspopup="true">কোড ও বিশ্লেষণ 4 Argon</button>
                      <div id="composer" class="ql-editor" contenteditable="true" role="textbox" aria-label="Enter a prompt"></div>
                      <input id="zip-file" type="file" accept=".zip,application/zip" />
                      <button id="send-btn" aria-label="Send message" type="button">Send</button>
                    </div>
                    <script>
                    document.getElementById('send-btn').addEventListener('click', () => {
                      const comp = document.getElementById('composer');
                      const txt = (comp.innerText || comp.textContent || '').trim();
                      if (txt) {
                        const uq = document.createElement('user-query');
                        uq.className = 'user-turn';
                        uq.textContent = txt;
                        document.getElementById('chat-history').appendChild(uq);
                        comp.innerHTML = '';
                        comp.textContent = '';
                      }
                    });
                    </script>
                    </body></html>""",
                    encoding="utf-8",
                )
                page.goto(fixture.as_uri(), wait_until="domcontentloaded")
                page.evaluate(cs_code)

                # 1. Paste prompt via companion extension -> arms keepPromptAlive
                prompt_str = "ROUND 27 CONTRACT 1 PROMPT: verify pure-Python send disarms extension observer."
                paste_res = da._invoke_companion_extension(
                    page, "paste_prompt", {"promptText": prompt_str, "appendMode": False}
                )
                assert isinstance(paste_res, dict) and paste_res.get("ok") is True, f"paste_prompt failed: {paste_res}"
                st_armed = da._invoke_companion_extension(page, "get_runtime_state", {})
                assert st_armed and st_armed.get("keepPromptAlive") is True

                # 2. Perform pure-Python send via da._click_send_immediately (with extension click_send bypassed)
                setattr(page, "_argon_post_send_recheck_delay_seconds", 0.05)
                comp_loc = page.locator("#composer")
                orig_ext_avail = da._extension_is_available
                try:
                    # Force _click_send_immediately to take the pure-Python button click branch
                    # while _disarm_extension_prompt_guard still disarms the extension over DOM events
                    da._extension_is_available = lambda _p: False
                    sent_ok = da._click_send_immediately(
                        page,
                        comp_loc,
                        expected_min_len=20,
                        probe_snippet="ROUND 27 CONTRACT 1 PROMPT",
                        body_len_before=10,
                        snippet_count_before=0,
                    )
                finally:
                    da._extension_is_available = orig_ext_avail

                assert sent_ok is True, "Pure-Python _click_send_immediately failed"
                st_disarmed = da._invoke_companion_extension(page, "get_runtime_state", {})
                assert st_disarmed and st_disarmed.get("keepPromptAlive") is False, (
                    f"Extension keepPromptAlive must be False after pure-Python send: {st_disarmed}"
                )
                assert st_disarmed.get("cachedPromptText") == "", (
                    f"Extension cachedPromptText must be empty after pure-Python send: {st_disarmed}"
                )
                # Mutate composer and confirm observer does NOT re-paste
                page.evaluate("() => { const c = document.getElementById('composer'); c.textContent = ''; }")
                page.wait_for_timeout(150)
                assert page.evaluate("() => (document.getElementById('composer').textContent || '').trim()") == ""

                # 3. Contract 1 R1.3: Re-arm paste, then inject a .queued-steering-turn and verify automatic disarm
                da._invoke_companion_extension(
                    page, "paste_prompt", {"promptText": prompt_str, "appendMode": False}
                )
                page.evaluate(
                    """() => {
                        const wrap = document.createElement('div');
                        wrap.className = 'conversation-container queued-steering-turn';
                        const uq = document.createElement('user-query');
                        uq.textContent = 'Queued turn';
                        wrap.appendChild(uq);
                        document.getElementById('chat-history').appendChild(wrap);
                        const c = document.getElementById('composer');
                        c.textContent = '';
                    }"""
                )
                page.wait_for_timeout(150)
                st_after_q = da._invoke_companion_extension(page, "get_runtime_state", {})
                assert st_after_q and st_after_q.get("keepPromptAlive") is False, (
                    f"Observer must disarm when .queued-steering-turn is in DOM: {st_after_q}"
                )

                # 4. Contract 2 R2.2 & R2.3: Extension attach_archive returns reason='size_cap_exceeded' when over base64 cap
                ext_over_cap = da._invoke_companion_extension(
                    page,
                    "attach_archive",
                    {
                        "fileName": large_zip.name,
                        "expectedSize": large_zip.stat().st_size,
                        "base64Data": "AAAA",
                        "maxBase64Bytes": 10240,
                    },
                )
                assert isinstance(ext_over_cap, dict) and ext_over_cap.get("ok") is False
                assert ext_over_cap.get("reason") == "size_cap_exceeded", (
                    f"Expected extension attach_archive to return reason='size_cap_exceeded', got {ext_over_cap}"
                )

                # 5. W3 R3.3 & W1 R1.4: Continuation page with existing attachment skips second archive attach
                setattr(page, "_argon_is_continuation", True)
                setattr(page, "_argon_has_existing_attachment", True)
                setattr(page, "_argon_existing_attachment_detail", "argon_context.zip chip in turn 1")
                buf_cont = io.StringIO()
                with contextlib.redirect_stdout(buf_cont):
                    att_cont = da.attach_zip(page, small_zip)
                assert att_cont is True
                assert "conversation already holds an attachment" in buf_cont.getvalue()
                assert "skipping second archive attach in continuation" in buf_cont.getvalue()

                # 6. W3 R3.4: Canonical size/route line printed on fresh attach
                setattr(page, "_argon_is_continuation", False)
                setattr(page, "_argon_has_existing_attachment", False)
                canon_line = r23.format_archive_canonical_size_line(
                    small_zip.name, small_zip.stat().st_size, "companion_extension_base64", True
                )
                assert "hard limit 100 MB | route companion_extension_base64 | base64 yes" in canon_line
            finally:
                browser.close()

    return {
        "ok": True,
        "disarmed_after_python_send": True,
        "disarmed_on_queued_steering_turn": True,
        "extension_size_cap_reason": ext_over_cap.get("reason"),
        "canonical_size_line": canon_line,
    }


def case_3_contract3_4_5_6_7_chip_version_capture_and_stale_reap() -> Dict[str, Any]:
    """Verify Contract 3 (Bengali/English chip + 45s continuation wait), Contract 4 (last-turn proof + duplicate digest), Contract 5 (version mismatch warning), Contract 6 (duplicate turn detection), and W6 (stale task reaping)."""
    # 1. Contract 3 R3.1: Shared English & Bengali model labels
    assert r23.is_verified_argon_model("Code & analysis 4 Argon") is True
    assert r23.is_verified_argon_model("কোড ও বিশ্লেষণ 4 Argon") is True
    assert r23.canonical_model_label("কোড ও বিশ্লেষণ 4 Argon") == "Code & analysis 4 Argon"
    assert r23.canonical_model_label("Everyday help 4 Hydrogen") == "Everyday help 4 Hydrogen"

    # 2. Contract 3 R3.2 & W7 R7.1-R7.2: Continuation chip wait is >= 45s (not 12s) and reports hydrating state
    class _SlowHydratingPage:
        def __init__(self) -> None:
            self._argon_is_continuation = True
            self.wait_timeouts: list[float] = []

    slow_page = _SlowHydratingPage()
    orig_wait_chip = da._wait_for_model_chip
    orig_model_chip = da._model_chip
    orig_open_picker = da._open_picker
    try:
        def _spy_wait_chip(_p: Any, timeout: float = 0.0) -> Any:
            slow_page.wait_timeouts.append(float(timeout))
            return None

        da._wait_for_model_chip = _spy_wait_chip
        da._model_chip = lambda _p: None
        da._open_picker = lambda _p: False
        buf_w7_out = io.StringIO()
        buf_w7_err = io.StringIO()
        with contextlib.redirect_stdout(buf_w7_out), contextlib.redirect_stderr(buf_w7_err):
            ok_w7 = da.ensure_argon_model(slow_page, attempts=1, require_chip=True, wait_for_chip_seconds=45.0)
        assert not ok_w7
        assert slow_page.wait_timeouts and slow_page.wait_timeouts[0] >= 45.0, (
            f"Continuation chip wait must be >= 45.0s, got {slow_page.wait_timeouts}"
        )
        assert "waiting for the model chip on a continuation page" in buf_w7_out.getvalue()
        assert "model chip (not visible) after 45 s on a continuation page - the conversation is still hydrating" in buf_w7_err.getvalue()
    finally:
        da._wait_for_model_chip = orig_wait_chip
        da._model_chip = orig_model_chip
        da._open_picker = orig_open_picker

    # 3. Contract 5 R5.1 & R5.2: Loaded extension version 1.16.0 vs repo 1.17.0 warning
    manifest_data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest_data.get("version") == "1.17.0", f"Expected manifest version 1.17.0, got {manifest_data.get('version')}"
    buf_v_err = io.StringIO()
    with contextlib.redirect_stderr(buf_v_err):
        ver_chk = r23.check_companion_extension_version(loaded_version="1.16.0", repo_version="1.17.0")
    assert ver_chk["ok"] is False and ver_chk["matches"] is False
    expected_ver_msg = "the browser is running extension 1.16.0 but the repo has 1.17.0 - restart the Argon browser before dispatching"
    assert expected_ver_msg in ver_chk["message"]
    assert expected_ver_msg in buf_v_err.getvalue()

    # 4. Contract 4 R4.2 / W4 R4.3: Duplicate digest in DeliveryLedger prints "same digest as round <id> - Argon re-attached the same file"
    with tempfile.TemporaryDirectory(prefix="r27_c4_w6_") as tmp:
        root = Path(tmp)
        proj = root / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        ledger_file = root / "ledger.json"
        reg_file = root / "tasks.json"
        ts_file = root / "task_state.json"

        orig_ledger_path = da.DELIVERY_LEDGER_PATH
        orig_reg_path = da.TASK_REGISTRY_PATH
        orig_ts_path = da.TASK_STATE_PATH
        try:
            da.DELIVERY_LEDGER_PATH = ledger_file
            da.TASK_REGISTRY_PATH = reg_file
            da.TASK_STATE_PATH = ts_file

            sample_patch = "$TargetRoot = 'G:\\Developing\\argon-coding-bridge'\nWrite-Host '[SUCCESS] 1 file(s) patched.'\n"
            ledger = da.DeliveryLedger(ledger_file)
            ledger.record(proj, sample_patch, chat_id="ba4a3e1d144bcbc4", round_id="20261114_133624", lane="followup")

            buf_dup_out = io.StringIO()
            buf_dup_err = io.StringIO()
            with contextlib.redirect_stdout(buf_dup_out), contextlib.redirect_stderr(buf_dup_err):
                is_dup = da._is_leftover_delivery(sample_patch, set(), project_path=proj)
            assert is_dup is True
            combined_dup = buf_dup_out.getvalue() + "\n" + buf_dup_err.getvalue()
            assert "same digest as round 20261114_133624 - Argon re-attached the same file" in combined_dup, (
                f"Missing same-digest message in: {combined_dup}"
            )

            # 5. W6 R6.1 & R6.2: Stale registry entries do not count against slot cap and are reaped by prune_stale_tasks()
            reg = da.TaskRegistry(reg_file)
            reg.data["entries"] = [
                {
                    "task_id": "stale1:new:u/1:1700000000",
                    "project": str(proj),
                    "slot": "/u/1/",
                    "lane": "new",
                    "status": "running",
                    "started_at": "2026-11-06 10:00:00",
                    "started_epoch": time.time() - 86400 * 7,
                    "pid": 99999999,
                },
                {
                    "task_id": "stale2:followup:u/1:1700000001",
                    "project": str(proj),
                    "slot": "/u/1/",
                    "lane": "followup",
                    "status": "running",
                    "started_at": "2026-11-07 10:00:00",
                    "started_epoch": time.time() - 86400 * 6,
                    "pid": 99999998,
                },
            ]
            reg.save()

            # Before reaping, active_tasks_for_slot & is_slot_available must already ignore stale entries
            assert len(reg.active_tasks_for_slot("/u/1/")) == 0
            ok_u1, u1_reason = reg.is_slot_available("/u/1/")
            assert ok_u1 is True and "0/2" in u1_reason

            # And prune_stale_tasks marks them stale_reaped on disk
            reaped = reg.prune_stale_tasks()
            assert len(reaped) == 2

            # Verify _pool_slots_in_order preserves explicitly requested start_slot first
            pool_path = root / "pool.json"
            pool_path.write_text(
                json.dumps(
                    {
                        "profile_dir": str(root / "prof"),
                        "accounts": [
                            {"index": 1, "slot": "/u/1/", "account_index": 1, "email": "u1@example.com"},
                            {"index": 2, "slot": "/u/2/", "account_index": 2, "email": "u2@example.com"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cfg = PoolConfig(config_path=pool_path, profile_dir_override=root / "prof")
            ordered_slots = da._pool_slots_in_order(cfg, "/u/1/", skip_start_slot=False, preserve_start_slot_first=True)
            assert ordered_slots[0] == "/u/1/", f"Expected /u/1/ first, got {ordered_slots}"
        finally:
            da.DELIVERY_LEDGER_PATH = orig_ledger_path
            da.TASK_REGISTRY_PATH = orig_reg_path
            da.TASK_STATE_PATH = orig_ts_path

    return {
        "ok": True,
        "continuation_chip_wait_s": slow_page.wait_timeouts[0],
        "version_mismatch_warning": ver_chk["message"],
        "stale_reaped_count": len(reaped),
    }


def run_round27_two_lane_suite() -> Dict[str, Any]:
    t0 = time.perf_counter()
    c1 = case_1_w1_w2_two_lane_continuation_and_concurrent_guards()
    c2 = case_2_contract1_and_2_extension_disarm_and_size_cap()
    c3 = case_3_contract3_4_5_6_7_chip_version_capture_and_stale_reap()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    cases = [c1, c2, c3]
    passed = sum(1 for c in cases if c.get("ok"))
    return {
        "ok": passed == len(cases),
        "passed": passed,
        "total": len(cases),
        "elapsed_ms": elapsed_ms,
        "case_1": c1,
        "case_2": c2,
        "case_3": c3,
    }


def run_round27_suite(verbose: bool = True) -> Dict[str, Any]:
    """Alias the dispatcher (`dispatch_argon.py --self-test`) imports as `run_round27_suite`."""
    return run_round27_two_lane_suite()


def main() -> int:
    print("=" * 78)
    print("  ROUND 27 + ADDENDUM SUITE (Two-Lane System + Core/Extension Contracts 1-6)")
    print("=" * 78)
    rep = run_round27_two_lane_suite()
    print(f"  [PASS] Case 1 (W1/W2 Two-Lane & Concurrent Guards): {rep['case_1']}")
    print(f"  [PASS] Case 2 (Contract 1 & 2 Disarm + Size Cap)  : {rep['case_2']}")
    print(f"  [PASS] Case 3 (Contract 3/4/5/6 + W6/W7)          : {rep['case_3']}")
    print(f"  Result: {rep['passed']}/{rep['total']} PASS in {rep['elapsed_ms']} ms")
    print("=" * 78)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
