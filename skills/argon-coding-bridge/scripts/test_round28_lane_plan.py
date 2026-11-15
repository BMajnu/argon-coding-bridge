#!/usr/bin/env python3
"""Round 28 Hermetic Verification Suite (`test_round28_lane_plan.py`).

Verifies all Round 28 Work Units (W1-W8) against throwaway temporary directories
and a throwaway headless Chrome page:

  1. W1 (R1.1-R1.4 — A follow-up NEVER opens a new chat):
     - `--lane followup` opens and continues the conversation recorded for that lane
       (`TaskState.lanes['followup']` / `SessionStore`) and prints
       `lane: followup -> continuing chat <id> (<url>)`.
     - When there is NO recorded conversation (or a conflicting slot / `--new-session`),
       the run STOPS with the named reason
       `followup has no recorded conversation for this project - refusing to start a new chat`,
       prints `lane: followup -> REFUSED (no recorded conversation)`, and exits non-zero.
     - `--append-followup` appends to the RUNNING task's chat and refuses by name when no task is running.
  2. W2 (R2.1-R2.4 — The two-lane plan is VERIFIED before and after send):
     - A two-lane run resolves to one `followup` lane continuing a recorded chat + one `new` lane
       opening a fresh chat on another slot, printing:
       `lane plan: followup -> continuing <chat id> on <slot> | new -> fresh chat on <slot>`.
     - Aborts BEFORE sending anything when `followup` has no recorded chat or when both lanes
       would use the same slot.
     - Post-send verification detects if `followup` opened a new chat and prints
       `lane plan VIOLATED: the follow-up opened a new chat`, refusing to accept the delivery.
     - EXIT 9 guard stays scoped to the same lane AND the same slot.
  3. W3 (R3.1-R3.3 — Delivery capture takes ONLY the newest turn's chip & refuses older digests):
     - Capture considers ONLY chips in the newest assistant turn after the last user message;
       an earlier round's chip is never opened.
     - Refuses any downloaded file whose SHA-256 matches an earlier `DeliveryLedger` entry with
       `this is round <id>'s delivery (sha256 ...), not a new one - the capture refuses it`
       (and `_r23_capture_generated_file` never bypasses that refusal via fallback routes).
     - Prints the full proof on every success: `chat_id`, `turn_index`, `file_name`,
       `byte_size`, `sha256`, and `ledger_entry`.
  4. W4 (R4.1-R4.3 — Manual hand-off pack makes the right file unmistakable):
     - `manual_handoff.md` and `handoff.json` carry the exact `chat_url`, `lane`, `account_email`,
       `prompt_path`, `zip_path`, `expected_delivery_file` (`apply_changes.ps1`), and the SHA-256
       of every earlier delivery.
     - States in one line which chip to download (the chip in the NEWEST assistant turn of THAT
       conversation, never an earlier chip and never the browser's download list).
     - States that the ZIP must be attached in the composer and the attachment chip must be visible
       BEFORE the prompt is sent.
  5. W5 (R5.1-R5.3 — Installer picks the delivery by the LEDGER, never by mtime):
     - `Run-ArgonPatch.ps1` and `select_installer_patch_by_ledger` pick the patch file recorded
       for the current task in `DeliveryLedger` / `TaskState`, even when an older re-downloaded
       patch has a newer `LastWriteTime`.
     - Prints candidates with their SHA-256 and the ledger's decision when multiple candidates
       exist, and refuses to guess when ambiguous. Explicit `-PatchFile` wins.
     - Refuses a file whose SHA-256 equals an older ledger entry with the round it belongs to,
       unless `-Force` (`force=True`) is passed.
  6. W6 & W8 (R6.1-R6.6 & R8.1-R8.4 — SKILL.md rules & non-stale focused archive):
     - `SKILL.md` states explicit `--lane followup`, the bold follow-up refusal rule, the two-lane
       recipe (`never two new lanes`), the manual download/send rules, the new troubleshooting rows,
       and reading step screenshots before sending the second lane.
     - `package_context` stamps the archive with the project commit + the archive's SHA-256.
     - Follow-up prompt and hand-off pack state the archive already in the conversation
       (file name + byte size + SHA-256 + project commit).
     - When project files changed since the base archive was built, `build_focused_archive` builds
       a small focused archive containing only the changed files + `FOCUS_MANIFEST.md`, and
       `attach_zip` allows attaching it in a continuation that already holds the full archive.
     - When even a focused archive cannot be attached, it reports so by name and flags switching
       to a FRESH chat with the full archive so Argon never works from a stale tree.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
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
import dual_dispatch as dd
import manual_handoff as mh
import package_context as pc
from argon_pool_login import PoolConfig

SKILL_MD_PATH = SCRIPTS_DIR.parent / "SKILL.md"
RUN_ARGON_PATCH_PS1 = SCRIPTS_DIR / "Run-ArgonPatch.ps1"


def _make_sample_patch(marker: str) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$TargetRoot = 'G:\\Developing\\argon-coding-bridge'\n"
        f"# Round marker: {marker}\n"
        "$relPath = 'src/app.py'\n"
        "$path = Join-Path $TargetRoot $relPath\n"
        "[System.IO.File]::WriteAllText($path, 'print(1)', [System.Text.UTF8Encoding]::new($false))\n"
        "Write-Host '[Verified] src/app.py'\n"
        "Write-Host '[Patched] src/app.py'\n"
        "Write-Host '[SUCCESS] 1 file(s) patched.'\n"
        "# --- NOTES: inconsistencies and suggestions found while reading the project ---\n"
        "# ## ROOT CAUSE\n"
        f"# NOTE: src/app.py:1 - root cause verified for {marker}\n"
        "# --- END NOTES ---\n"
    )


def case_1_w1_followup_continues_recorded_and_refuses_when_missing() -> Dict[str, Any]:
    """Verify W1 (R1.1-R1.4): followup continues recorded chat, refuses when missing (never opens a new chat), and --append-followup refuses when no task is running."""
    with tempfile.TemporaryDirectory(prefix="r28_w1_") as tmp:
        root = Path(tmp)
        proj = root / "proj_w1"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "app.py").write_text("def hello():\n    return 'v1'\n", encoding="utf-8")

        sess_file = root / "argon_sessions.json"
        ts_file = root / "argon_task_state.json"
        reg_file = root / "argon_tasks.json"

        orig_sess = da.SESSION_STORE_PATH
        orig_ts = da.TASK_STATE_PATH
        orig_reg = da.TASK_REGISTRY_PATH
        try:
            da.SESSION_STORE_PATH = sess_file
            da.TASK_STATE_PATH = ts_file
            da.TASK_REGISTRY_PATH = reg_file

            ss = da.SessionStore(sess_file)
            ts = da.TaskState(ts_file)

            # 1. R1.2 & R1.3: When NO recorded conversation exists for followup, MUST refuse and exit non-zero
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                res_missing = da.resolve_lane_conversation(
                    proj_root=proj,
                    lane="followup",
                    mode="gemini",
                    active_slot="/u/5/",
                    session_store=ss,
                    task_state_store=ts,
                )
            combined_missing = buf_out.getvalue() + "\n" + buf_err.getvalue()
            assert res_missing.get("refused") is True, f"Expected refused=True when no recorded chat: {res_missing}"
            assert res_missing.get("is_continuation") is False
            assert res_missing.get("exit_code") != 0
            assert "followup has no recorded conversation for this project - refusing to start a new chat" in combined_missing
            assert "lane: followup -> REFUSED (no recorded conversation)" in combined_missing
            assert "NEW chat" not in buf_out.getvalue()

            # Also verify dispatch_to_argon(..., lane='followup') exits non-zero before touching a browser
            buf_d_out = io.StringIO()
            buf_d_err = io.StringIO()
            with contextlib.redirect_stdout(buf_d_out), contextlib.redirect_stderr(buf_d_err):
                rc_dispatch_missing = da.dispatch_to_argon(
                    task="Follow-up task without recorded session",
                    project_path=str(proj),
                    lane="followup",
                    slot="/u/5/",
                    skip_package=True,
                )
            assert rc_dispatch_missing != 0
            assert "followup has no recorded conversation for this project - refusing to start a new chat" in (
                buf_d_out.getvalue() + "\n" + buf_d_err.getvalue()
            )

            # 2. R1.1 & R1.3: Record a followup conversation in TaskState (`lanes['followup']`) and verify continuation
            ts.set(
                proj,
                lane="followup",
                state="delivered",
                chat_id="45c57d433a71461d",
                chat_url="https://gemini.google.com/u/5/app/45c57d433a71461d",
                slot="/u/5/",
            )
            buf_cont = io.StringIO()
            with contextlib.redirect_stdout(buf_cont):
                res_cont = da.resolve_lane_conversation(
                    proj_root=proj,
                    lane="followup",
                    mode="gemini",
                    active_slot="/u/5/",
                    session_store=ss,
                    task_state_store=ts,
                )
            assert res_cont.get("refused") is False
            assert res_cont.get("is_continuation") is True
            assert res_cont.get("chat_id") == "45c57d433a71461d"
            expected_cont_line = (
                "lane: followup -> continuing chat 45c57d433a71461d "
                "(https://gemini.google.com/u/5/app/45c57d433a71461d)"
            )
            assert expected_cont_line in buf_cont.getvalue(), f"Missing continuation line in: {buf_cont.getvalue()}"

            # 3. R1.4: --append-followup refuses by name when there is no running task
            buf_app_out = io.StringIO()
            buf_app_err = io.StringIO()
            with contextlib.redirect_stdout(buf_app_out), contextlib.redirect_stderr(buf_app_err):
                rc_append = da.append_followup_to_running_task(
                    append_arg="Additional requirement for running task",
                    project_path=str(proj),
                    lane="followup",
                    slot="/u/5/",
                )
            assert rc_append != 0
            combined_app = buf_app_out.getvalue() + "\n" + buf_app_err.getvalue()
            assert "append-followup has no running task" in combined_app
            assert "lane: followup -> REFUSED (no running task for --append-followup)" in combined_app
        finally:
            da.SESSION_STORE_PATH = orig_sess
            da.TASK_STATE_PATH = orig_ts
            da.TASK_REGISTRY_PATH = orig_reg

    return {
        "ok": True,
        "continued_chat_id": "45c57d433a71461d",
        "refused_when_missing": True,
        "append_followup_refused_when_idle": True,
    }


def case_2_w2_two_lane_plan_verified_before_and_after_send() -> Dict[str, Any]:
    """Verify W2 (R2.1-R2.4): two-lane plan is one continue + one new on distinct slots, aborts before send when invalid, verifies outcome after send, and keeps EXIT 9 scoped to (lane, slot)."""
    with tempfile.TemporaryDirectory(prefix="r28_w2_") as tmp:
        root = Path(tmp)
        proj = root / "proj_w2"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "main.py").write_text("x = 1\n", encoding="utf-8")

        sess_file = root / "argon_sessions.json"
        ts_file = root / "argon_task_state.json"
        reg_file = root / "argon_tasks.json"

        orig_sess = da.SESSION_STORE_PATH
        orig_ts = da.TASK_STATE_PATH
        orig_reg = da.TASK_REGISTRY_PATH
        try:
            da.SESSION_STORE_PATH = sess_file
            da.TASK_STATE_PATH = ts_file
            da.TASK_REGISTRY_PATH = reg_file

            ss = da.SessionStore(sess_file)
            ts = da.TaskState(ts_file)
            reg = da.TaskRegistry(reg_file)

            # 1. R2.2: Plan with NO recorded followup chat must abort BEFORE sending anything
            buf_abort1_out = io.StringIO()
            buf_abort1_err = io.StringIO()
            with contextlib.redirect_stdout(buf_abort1_out), contextlib.redirect_stderr(buf_abort1_err):
                plan_missing = da.resolve_and_verify_two_lane_plan(
                    proj_root=proj,
                    mode="gemini",
                    followup_slot="/u/5/",
                    new_slot="/u/8/",
                    session_store=ss,
                    task_state_store=ts,
                )
            assert plan_missing.get("ok") is False
            assert "followup has no recorded conversation for this project - refusing to start a new chat" in (
                buf_abort1_out.getvalue() + "\n" + buf_abort1_err.getvalue()
            )

            # Also verify run_dual_dispatch aborts before sending when no recorded followup chat exists
            buf_dd_out = io.StringIO()
            buf_dd_err = io.StringIO()
            with contextlib.redirect_stdout(buf_dd_out), contextlib.redirect_stderr(buf_dd_err):
                rc_dd_missing = dd.run_dual_dispatch(
                    task="Test dual dispatch without history",
                    project_path=str(proj),
                    slot="/u/5/",
                    second_slot="/u/8/",
                    dry_run=True,
                )
            assert rc_dd_missing != 0

            # 2. Record winning followup conversation on /u/5/ (chat_id=45c57d433a71461d)
            ss.remember(
                proj,
                mode="gemini",
                slot="/u/5/",
                chat_id="45c57d433a71461d",
                chat_title="Round 27 Winner",
                profile_dir=str(root / "argon_profiles" / "slot-u5"),
                lane="followup",
                workspace_mode="task",
            )

            # 3. R2.2: Plan where followup and new would use the SAME slot (/u/5/) must abort before send
            buf_same_out = io.StringIO()
            buf_same_err = io.StringIO()
            with contextlib.redirect_stdout(buf_same_out), contextlib.redirect_stderr(buf_same_err):
                plan_same_slot = da.resolve_and_verify_two_lane_plan(
                    proj_root=proj,
                    mode="gemini",
                    followup_slot="/u/5/",
                    new_slot="/u/5/",
                    session_store=ss,
                    task_state_store=ts,
                )
            assert plan_same_slot.get("ok") is False
            assert "same slot" in plan_same_slot.get("reason", "")

            # 4. R2.1 & R2.2: Valid two-lane plan prints exact plan line before send
            buf_valid = io.StringIO()
            with contextlib.redirect_stdout(buf_valid):
                plan_ok = da.resolve_and_verify_two_lane_plan(
                    proj_root=proj,
                    mode="gemini",
                    followup_slot="/u/5/",
                    new_slot="/u/8/",
                    session_store=ss,
                    task_state_store=ts,
                )
            assert plan_ok.get("ok") is True
            expected_plan_line = "lane plan: followup -> continuing 45c57d433a71461d on /u/5/ | new -> fresh chat on /u/8/"
            assert plan_ok.get("plan_line") == expected_plan_line
            assert expected_plan_line in buf_valid.getvalue()

            # 5. R2.3: Post-send verification succeeds when followup kept recorded chat_id and new opened fresh chat_id
            outcome_ok = da.verify_two_lane_outcome(plan_ok, "45c57d433a71461d", "99aabbccddeeff00")
            assert outcome_ok.get("ok") is True and outcome_ok.get("accept_delivery") is True

            # And fails hard when followup opened a new chat!
            buf_viol_out = io.StringIO()
            buf_viol_err = io.StringIO()
            with contextlib.redirect_stdout(buf_viol_out), contextlib.redirect_stderr(buf_viol_err):
                outcome_bad = da.verify_two_lane_outcome(plan_ok, "deadbeef12345678", "99aabbccddeeff00")
            assert outcome_bad.get("ok") is False
            assert outcome_bad.get("accept_delivery") is False
            assert "lane plan VIOLATED: the follow-up opened a new chat" in (
                buf_viol_out.getvalue() + "\n" + buf_viol_err.getvalue()
            )

            # 6. R2.4: EXIT 9 guard stays scoped to same lane AND same slot
            ts.set(
                proj,
                lane="followup",
                state="waiting",
                slot="/u/5/",
                chat_id="45c57d433a71461d",
                chat_url="https://gemini.google.com/u/5/app/45c57d433a71461d",
                pid=os.getpid(),
            )
            reg.register_start(
                proj,
                slot="/u/5/",
                lane="followup",
                chat_id="45c57d433a71461d",
                chat_url="https://gemini.google.com/u/5/app/45c57d433a71461d",
            )
            guard_new = da.check_concurrent_lane_guard(
                proj, requested_lane="new", requested_slot="/u/8/", task_state_store=ts, task_registry=reg
            )
            assert guard_new.get("blocked") is False, f"New lane on /u/8/ must not be blocked by followup on /u/5/: {guard_new}"

            buf_g_err = io.StringIO()
            with contextlib.redirect_stderr(buf_g_err):
                guard_same = da.check_concurrent_lane_guard(
                    proj, requested_lane="followup", requested_slot="/u/5/", task_state_store=ts, task_registry=reg
                )
            assert guard_same.get("blocked") is True and guard_same.get("exit_code") == da.EXIT_ALREADY_RUNNING
        finally:
            da.SESSION_STORE_PATH = orig_sess
            da.TASK_STATE_PATH = orig_ts
            da.TASK_REGISTRY_PATH = orig_reg

    return {
        "ok": True,
        "plan_line": expected_plan_line,
        "aborted_on_missing_followup": True,
        "aborted_on_same_slot": True,
        "violation_detected_on_new_followup_chat": True,
        "exit9_scoped_to_lane_and_slot": True,
    }


def case_3_w3_capture_newest_turn_only_and_refuse_older_digest() -> Dict[str, Any]:
    """Verify W3 (R3.1-R3.3): capture takes ONLY the newest assistant turn's chip, refuses older DeliveryLedger digests by name, and prints proof with byte_size and ledger_entry."""
    from test_round24_bugfixes import _chrome, _launch

    with tempfile.TemporaryDirectory(prefix="r28_w3_") as tmp:
        root = Path(tmp)
        proj = root / "proj_w3"
        proj.mkdir(parents=True, exist_ok=True)
        dest_dir = proj / "downloads" / "followup"
        dest_dir.mkdir(parents=True, exist_ok=True)
        ledger_file = root / "argon_delivery_ledger.json"

        old_patch_text = _make_sample_patch("ROUND_27_OLD_DELIVERY")
        new_patch_text = _make_sample_patch("ROUND_28_NEW_DELIVERY")

        orig_ledger_path = da.DELIVERY_LEDGER_PATH
        try:
            da.DELIVERY_LEDGER_PATH = ledger_file
            ledger = da.DeliveryLedger(ledger_file)
            old_rec = ledger.record(
                proj,
                old_patch_text,
                chat_id="45c57d433a71461d",
                round_id="20261114_133624",
                lane="followup",
                file_path=str(proj / "downloads" / "old_apply_changes.ps1"),
            )
            old_sha = old_rec["sha256"]

            # 1. R3.2: Verify _is_leftover_delivery prints the exact named refusal
            buf_dup_out = io.StringIO()
            buf_dup_err = io.StringIO()
            with contextlib.redirect_stdout(buf_dup_out), contextlib.redirect_stderr(buf_dup_err):
                dup_res = da._is_leftover_delivery(old_patch_text, set(), project_path=proj)
            assert dup_res is True
            combined_dup = buf_dup_out.getvalue() + "\n" + buf_dup_err.getvalue()
            expected_refusal_msg = (
                f"this is round 20261114_133624's delivery (sha256 {old_sha}), not a new one - the capture refuses it"
            )
            assert expected_refusal_msg in combined_dup, f"Missing R3.2 refusal message in: {combined_dup}"

            # 2. R3.1 & R3.3 in live headless Chrome DOM:
            with _chrome() as pw:
                browser = _launch(pw)
                try:
                    page = browser.new_page()
                    fixture = root / "gemini_turns.html"
                    fixture.write_text(
                        """<!doctype html><html><body>
                        <div id="chat-history">
                          <user-query class="user-turn">Round 27 prompt</user-query>
                          <model-response id="turn-0" class="model-turn">
                            <p>Round 27 answer</p>
                            <generated-file id="old-chip" title="apply_changes.ps1" data-file-name="apply_changes.ps1">
                              <div class="file-name-lr">apply_changes.ps1</div>
                            </generated-file>
                          </model-response>
                          <user-query class="user-turn">Round 28 follow-up prompt</user-query>
                          <model-response id="turn-1" class="model-turn">
                            <p id="turn-1-text">Thinking...</p>
                          </model-response>
                        </div>
                        </body></html>""",
                        encoding="utf-8",
                    )
                    page.goto(fixture.as_uri(), wait_until="domcontentloaded")
                    # Playwright's `page.url` is a read-only property, so the fixture is
                    # served over HTTP under a Gemini-shaped path instead of assigning to it.
                    import http.server
                    import threading

                    html_bytes = fixture.read_bytes()

                    class _Handler(http.server.BaseHTTPRequestHandler):
                        def do_GET(self):  # noqa: N802
                            self.send_response(200)
                            self.send_header("Content-Type", "text/html; charset=utf-8")
                            self.send_header("Content-Length", str(len(html_bytes)))
                            self.end_headers()
                            self.wfile.write(html_bytes)

                        def log_message(self, *args):  # noqa: D401
                            pass

                    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
                    threading.Thread(target=httpd.serve_forever, daemon=True).start()
                    page.goto(
                        f"http://127.0.0.1:{httpd.server_port}/u/5/app/45c57d433a71461d",
                        wait_until="domcontentloaded",
                    )

                    # While turn-1 has no chip yet, _generated_file_chip and _new_delivery_chip MUST return None
                    # and NEVER fall back to #old-chip in turn-0!
                    assert da._generated_file_chip(page) is None, "Must not pick turn-0 chip when turn-1 has no chip yet"
                    assert da._new_delivery_chip(page, chips_before=0, responses_before=1) is None

                    old_chip_loc = page.locator("#old-chip")
                    assert da._element_is_after_last_user_turn(old_chip_loc) is False
                    buf_old_err = io.StringIO()
                    with contextlib.redirect_stderr(buf_old_err):
                        cap_old = da.capture_generated_file(
                            page, dest_dir, chip=old_chip_loc, project_path=proj, timeout_seconds=5.0
                        )
                    assert cap_old is None
                    assert "Refusing to open delivery chip: it belongs to an earlier turn" in buf_old_err.getvalue()

                    # Now add a chip to turn-1 (the newest assistant turn)
                    page.evaluate(
                        """() => {
                            const t1 = document.getElementById('turn-1');
                            const gf = document.createElement('generated-file');
                            gf.id = 'new-chip';
                            gf.setAttribute('title', 'apply_changes.ps1');
                            gf.setAttribute('data-file-name', 'apply_changes.ps1');
                            gf.innerHTML = '<div class="file-name-lr">apply_changes.ps1</div><generated-file-senna-preview><div class="title-container"><button aria-label="Export">Export</button></div><pre class="text-content"></pre></generated-file-senna-preview>';
                            t1.appendChild(gf);
                        }"""
                    )
                    newest_chip = da._generated_file_chip(page)
                    assert newest_chip is not None
                    assert da._element_is_after_last_user_turn(newest_chip) is True

                    # Test R3.2 in capture_generated_file: if the chip downloads old_patch_text, capture_generated_file refuses it!
                    orig_open = da._open_generated_file
                    orig_export_dl = da._click_chip_export_and_leaf_download
                    try:
                        da._open_generated_file = lambda _p, chip_index=None, chip=None: True

                        def _mock_dl_old(_p, _c, d_dir, **_kw):
                            p_out = Path(d_dir) / "apply_changes.ps1"
                            p_out.write_text(old_patch_text, encoding="utf-8")
                            return p_out, ""

                        da._click_chip_export_and_leaf_download = _mock_dl_old
                        buf_cap_old_out = io.StringIO()
                        buf_cap_old_err = io.StringIO()
                        with contextlib.redirect_stdout(buf_cap_old_out), contextlib.redirect_stderr(buf_cap_old_err):
                            res_dup_cap = da.capture_generated_file(
                                page, dest_dir, chip=newest_chip, project_path=proj, timeout_seconds=6.0
                            )
                        assert res_dup_cap is None, "capture_generated_file must refuse a duplicate ledger digest"
                        assert expected_refusal_msg in (buf_cap_old_out.getvalue() + "\n" + buf_cap_old_err.getvalue())

                        # Test R3.3: when the chip downloads new_patch_text, capture_generated_file accepts it
                        # and prints chat_id, turn_index, file_name, byte_size, sha256, and ledger_entry!
                        def _mock_dl_new(_p, _c, d_dir, **_kw):
                            p_out = Path(d_dir) / "apply_changes.ps1"
                            p_out.write_text(new_patch_text, encoding="utf-8")
                            return p_out, ""

                        da._click_chip_export_and_leaf_download = _mock_dl_new
                        buf_cap_new = io.StringIO()
                        with contextlib.redirect_stdout(buf_cap_new):
                            res_new_cap = da.capture_generated_file(
                                page, dest_dir, chip=newest_chip, project_path=proj, timeout_seconds=6.0
                            )
                        assert res_new_cap is not None and res_new_cap.is_file()
                        cap_stdout = buf_cap_new.getvalue()
                        proof = getattr(page, "_argon_last_capture_proof", None)
                        assert isinstance(proof, dict), f"Missing _argon_last_capture_proof: {proof}"
                        assert proof.get("chat_id") == "45c57d433a71461d"
                        assert proof.get("turn_index") == 1
                        assert proof.get("file_name") == "apply_changes.ps1"
                        assert int(proof.get("byte_size") or 0) > 100
                        assert proof.get("sha256") == da._sha256_full(new_patch_text)
                        assert isinstance(proof.get("ledger_entry"), dict) and proof["ledger_entry"].get("sha256") == proof.get("sha256")
                        assert "byte_size=" in cap_stdout and "ledger_entry=" in cap_stdout
                    finally:
                        da._open_generated_file = orig_open
                        da._click_chip_export_and_leaf_download = orig_export_dl
                finally:
                    browser.close()
        finally:
            da.DELIVERY_LEDGER_PATH = orig_ledger_path

    return {
        "ok": True,
        "earlier_turn_chip_refused": True,
        "older_digest_refused_with_round_id": "20261114_133624",
        "proof_fields": sorted(proof.keys()),
    }


def case_4_w4_handoff_pack_unmistakable_file_and_rules() -> Dict[str, Any]:
    """Verify W4 (R4.1-R4.3): manual_handoff.md and handoff.json carry chat_url, lane, email, prompt_path, zip_path, expected_delivery_file, earlier delivery SHA-256 digests, one-line newest-turn chip rule, and composer attachment rule."""
    with tempfile.TemporaryDirectory(prefix="r28_w4_") as tmp:
        root = Path(tmp)
        proj = root / "proj_w4"
        proj.mkdir(parents=True, exist_ok=True)
        zip_p = proj / "argon_context.zip"
        zip_p.write_bytes(b"PK\x03\x04dummy")
        ledger_file = root / "argon_delivery_ledger.json"

        orig_ledger_path = da.DELIVERY_LEDGER_PATH
        try:
            da.DELIVERY_LEDGER_PATH = ledger_file
            ledger = da.DeliveryLedger(ledger_file)
            r1 = ledger.record(proj, _make_sample_patch("R26"), chat_id="chat_r26", round_id="round_26", lane="followup")
            r2 = ledger.record(proj, _make_sample_patch("R27"), chat_id="chat_r27", round_id="round_27", lane="new")

            md_path, json_path = mh.write_manual_handoff_pack(
                proj_root=proj,
                lane="followup",
                slot="/u/5/",
                email="dev5@example.com",
                chat_url="https://gemini.google.com/u/5/app/45c57d433a71461d",
                prompt_text="Follow-up prompt body for Round 28",
                zip_path=zip_p,
                conversation_archive={
                    "file_name": "argon_context.zip",
                    "byte_size": 123456,
                    "sha256": "a" * 64,
                    "git_commit": "17d7033",
                },
            )
            md_txt = md_path.read_text(encoding="utf-8")
            j_data = json.loads(json_path.read_text(encoding="utf-8"))

            # R4.1: Exact chat URL, lane, account e-mail, prompt file path, ZIP path, expected delivery file name, and earlier digests
            assert j_data["chat_url"] == "https://gemini.google.com/u/5/app/45c57d433a71461d"
            assert j_data["lane"] == "followup"
            assert j_data["account_email"] == "dev5@example.com"
            assert Path(j_data["prompt_path"]).is_file()
            assert j_data["zip_path"] == str(zip_p.resolve())
            assert j_data["expected_delivery_file"] == "apply_changes.ps1"
            assert r1["sha256"] in j_data["earlier_delivery_sha256s"]
            assert r2["sha256"] in j_data["earlier_delivery_sha256s"]
            assert r1["sha256"] in md_txt and r2["sha256"] in md_txt
            assert "round_26" in md_txt and "round_27" in md_txt

            # R4.2: One-line rule naming the chip in the NEWEST assistant turn of THAT conversation and forbidding the download list
            assert "NEWEST assistant turn" in j_data["target_chip_rule"]
            assert "the download list is not the delivery" in j_data["target_chip_rule"]
            assert "NEWEST assistant turn" in md_txt
            assert "the download list is not the delivery" in md_txt

            # R4.3: States that the ZIP has to be attached in the composer and the attachment chip must be visible BEFORE the prompt is sent
            assert "visible in the composer BEFORE the prompt is sent" in j_data["attachment_rule"]
            assert "visible in the composer BEFORE the prompt is sent" in md_txt
        finally:
            da.DELIVERY_LEDGER_PATH = orig_ledger_path

    return {
        "ok": True,
        "expected_delivery_file": j_data["expected_delivery_file"],
        "earlier_digests_count": len(j_data["earlier_delivery_sha256s"]),
    }


def case_5_w5_installer_picks_by_ledger_never_mtime() -> Dict[str, Any]:
    """Verify W5 (R5.1-R5.3): Run-ArgonPatch.ps1 and select_installer_patch_by_ledger choose the patch by DeliveryLedger/TaskState (never by LastWriteTime), refuse older digests without -Force, and honor explicit -PatchFile."""
    ps1_source = RUN_ARGON_PATCH_PS1.read_text(encoding="utf-8")
    assert "Sort-Object -Property LastWriteTime -Descending | Select-Object -First 1" not in ps1_source, (
        "Run-ArgonPatch.ps1 must not pick the patch by LastWriteTime (F4 / R5.1)"
    )
    assert "Get-ArgonLedgerContext" in ps1_source and "[switch]$Force" in ps1_source

    with tempfile.TemporaryDirectory(prefix="r28_w5_") as tmp:
        root = Path(tmp)
        proj = root / "proj_w5"
        dl_dir = proj / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        ledger_file = root / "argon_delivery_ledger.json"
        ts_file = root / "argon_task_state.json"

        old_patch_txt = _make_sample_patch("ROUND_26_OLDER_PATCH")
        cur_patch_txt = _make_sample_patch("ROUND_28_CURRENT_PATCH")

        # Write current patch first with an OLDER mtime, and write the old patch with a NEWER mtime (simulating F3/F4 re-download)
        cur_file = dl_dir / "apply_changes.ps1"
        old_redownloaded = dl_dir / "apply_changes (1).ps1"
        cur_file.write_text(cur_patch_txt, encoding="utf-8")
        old_redownloaded.write_text(old_patch_txt, encoding="utf-8")

        now_ts = time.time()
        os.utime(cur_file, (now_ts - 3600, now_ts - 3600))
        os.utime(old_redownloaded, (now_ts, now_ts))
        assert old_redownloaded.stat().st_mtime > cur_file.stat().st_mtime

        ledger = da.DeliveryLedger(ledger_file)
        old_entry = ledger.record(
            proj, old_patch_txt, chat_id="chat_old", round_id="round_26_old", lane="followup", file_path=str(old_redownloaded)
        )
        cur_entry = ledger.record(
            proj, cur_patch_txt, chat_id="chat_cur", round_id="round_28_cur", lane="followup", file_path=str(cur_file)
        )

        ts = da.TaskState(ts_file)
        ts.set(proj, state="delivered", delivered=str(cur_file), delivery_hash=cur_entry["short_hash"])

        # 1. R5.1 & R5.2: Installer selects cur_file (from ledger) even though old_redownloaded has newer LastWriteTime!
        buf_sel = io.StringIO()
        with contextlib.redirect_stdout(buf_sel):
            sel_res = da.select_installer_patch_by_ledger(
                proj,
                ledger_path=ledger_file,
                task_state_path=ts_file,
            )
        assert sel_res.get("ok") is True, f"Expected ledger selection to succeed: {sel_res}"
        assert Path(sel_res["selected_path"]).resolve() == cur_file.resolve(), (
            f"Installer picked {sel_res['selected_path']} instead of ledger file {cur_file}"
        )
        assert "Candidate patch files evaluated against DeliveryLedger" in buf_sel.getvalue()

        # 2. R5.3: Passing the older file via -PatchFile without -Force is REFUSED with the round it belongs to
        buf_old_err = io.StringIO()
        with contextlib.redirect_stderr(buf_old_err):
            refuse_old = da.select_installer_patch_by_ledger(
                proj,
                patch_file=str(old_redownloaded),
                force=False,
                ledger_path=ledger_file,
                task_state_path=ts_file,
            )
        assert refuse_old.get("ok") is False
        assert refuse_old.get("refused_round_id") == "round_26_old"
        assert "round_26_old" in buf_old_err.getvalue()

        # 3. R5.2 & R5.3: With -Force (force=True), explicit -PatchFile wins even for an older round
        allow_forced = da.select_installer_patch_by_ledger(
            proj,
            patch_file=str(old_redownloaded),
            force=True,
            ledger_path=ledger_file,
            task_state_path=ts_file,
        )
        assert allow_forced.get("ok") is True
        assert Path(allow_forced["selected_path"]).resolve() == old_redownloaded.resolve()

        # 4. R5.2: When multiple unrecorded candidates exist, refuses to guess
        unrec_dir = root / "proj_unrec"
        (unrec_dir / "downloads").mkdir(parents=True, exist_ok=True)
        (unrec_dir / "downloads" / "apply_changes_a.ps1").write_text(_make_sample_patch("UNREC_A"), encoding="utf-8")
        (unrec_dir / "downloads" / "apply_changes_b.ps1").write_text(_make_sample_patch("UNREC_B"), encoding="utf-8")
        buf_amb_out = io.StringIO()
        buf_amb_err = io.StringIO()
        with contextlib.redirect_stdout(buf_amb_out), contextlib.redirect_stderr(buf_amb_err):
            amb_res = da.select_installer_patch_by_ledger(
                unrec_dir,
                ledger_path=ledger_file,
                task_state_path=ts_file,
            )
        assert amb_res.get("ok") is False
        assert len(amb_res.get("candidates") or []) == 2
        assert "refusing to guess" in (buf_amb_out.getvalue() + "\n" + buf_amb_err.getvalue()).lower()

        # If pwsh or powershell is installed on this machine, also run Run-ArgonPatch.ps1 -DryRun end-to-end!
        ps_exe = shutil.which("pwsh") or shutil.which("powershell")
        ps_tested = False
        if ps_exe:
            env = dict(os.environ)
            env["ARGON_DELIVERY_LEDGER_PATH"] = str(ledger_file)
            env["ARGON_TASK_STATE_PATH"] = str(ts_file)
            proc = subprocess.run(
                [ps_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(RUN_ARGON_PATCH_PS1), "-ProjectPath", str(proj), "-DryRun"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                timeout=30,
            )
            assert proc.returncode == 0, f"Run-ArgonPatch.ps1 -DryRun failed: {proc.stdout}"
            # `tempfile` hands out the 8.3 short form of the temp dir; the installer
            # prints the resolved (long) path - compare the resolved paths.
            expected_path = str(cur_file.resolve())
            assert expected_path in proc.stdout, (
                f"the installer did not name the ledger's current delivery.\n"
                f"expected: {expected_path}\n--- installer output ---\n{proc.stdout}"
            )
            ps_tested = True

    return {
        "ok": True,
        "selected_ledger_file": cur_file.name,
        "refused_older_round": "round_26_old",
        "refused_ambiguous_unrecorded": True,
        "pwsh_live_dryrun": ps_tested,
    }


def case_6_w6_w8_skill_prompt_and_focused_archive_non_stale_tree() -> Dict[str, Any]:
    """Verify W6 (R6.1-R6.6) in SKILL.md and W8 (R8.1-R8.4) archive stamp, follow-up provenance, focused archive creation & continuation attach, and fresh-chat fallback."""
    skill_text = SKILL_MD_PATH.read_text(encoding="utf-8")
    # R6.1
    assert "--lane followup" in skill_text
    assert "**a follow-up NEVER opens a new chat; if it cannot continue the recorded conversation it STOPS and reports - it never sends a new session instead.**" in skill_text
    # R6.2
    assert "never two new lanes" in skill_text
    # R6.3
    assert "the download list is not the delivery" in skill_text
    # R6.4
    assert "attachment chip is visible" in skill_text
    # R6.5
    assert "The follow-up opened a new chat" in skill_text
    assert "The old delivery was downloaded again" in skill_text
    assert "The installer applied an older patch" in skill_text
    # R6.6
    assert "reads the step screenshot of EVERY step before acting" in skill_text
    assert "A lane-plan mismatch is fixed BEFORE the second lane is sent." in skill_text

    # W8 (R8.1-R8.4):
    with tempfile.TemporaryDirectory(prefix="r28_w8_") as tmp:
        root = Path(tmp)
        proj = root / "proj_w8"
        (proj / "src").mkdir(parents=True, exist_ok=True)
        mod_a = proj / "src" / "alpha.py"
        mod_b = proj / "src" / "beta.py"
        mod_a.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        mod_b.write_text("def beta():\n    return 2\n", encoding="utf-8")

        base_zip = proj / "argon_context.zip"
        buf_pkg = io.StringIO()
        with contextlib.redirect_stdout(buf_pkg):
            stamp = pc.package_context(
                project_path=str(proj),
                output_zip=str(base_zip),
                task_description="Round 28 base archive test",
            )
        # R8.4: Archive carries a stamp (project commit + archive SHA-256)
        read_back_stamp = pc.read_archive_stamp(base_zip, proj)
        assert read_back_stamp.get("sha256") and len(read_back_stamp["sha256"]) == 64
        assert read_back_stamp.get("git_commit")
        assert "src/alpha.py" in read_back_stamp.get("file_hashes", {})
        assert "src/beta.py" in read_back_stamp.get("file_hashes", {})

        # R8.1: Follow-up prompt states the archive already in the chat (file name + byte size + SHA-256 + commit)
        f_prompt_initial = da.build_followup_prompt(
            task_description="Follow-up task",
            target_directory=str(proj),
            winning_chat_id="45c57d433a71461d",
            winning_slot="/u/5/",
            conversation_archive=read_back_stamp,
        )
        assert read_back_stamp["file_name"] in f_prompt_initial
        assert str(read_back_stamp["byte_size"]) in f_prompt_initial
        assert read_back_stamp["sha256"] in f_prompt_initial
        assert read_back_stamp["git_commit"] in f_prompt_initial

        # Modify ONLY src/beta.py after base archive was built (R8.2)
        mod_b.write_text("def beta():\n    return 42  # updated in round 28\n", encoding="utf-8")
        focused_zip = proj / "downloads" / "argon_focused_context.zip"
        focused_info = pc.build_focused_archive(
            project_path=proj,
            base_stamp=read_back_stamp,
            output_zip=focused_zip,
        )
        assert focused_info.get("needed") is True
        assert focused_info.get("changed_files") == ["src/beta.py"]
        assert focused_zip.is_file()

        with zipfile.ZipFile(focused_zip, "r") as zf_foc:
            foc_names = zf_foc.namelist()
            assert "src/beta.py" in foc_names
            assert "src/alpha.py" not in foc_names, "Unchanged files must NOT be in the focused archive (R8.2)"
            assert "FOCUS_MANIFEST.md" in foc_names
            manifest_md = zf_foc.read("FOCUS_MANIFEST.md").decode("utf-8")
            assert "src/beta.py" in manifest_md
            assert read_back_stamp["git_commit"] in manifest_md
            assert read_back_stamp["sha256"] in manifest_md

        # Verify attach_zip ALLOWS the focused archive in a continuation that already holds the full archive (R8.2)
        class _ContinuationStubPage:
            def __init__(self) -> None:
                self._argon_is_continuation = True
                self._argon_has_existing_attachment = True
                self._argon_existing_attachment_detail = "argon_context.zip in turn 1"
                self._argon_allow_focused_archive = True

        stub_page = _ContinuationStubPage()
        orig_dt = da._attach_zip_via_datatransfer
        try:
            da._attach_zip_via_datatransfer = lambda _p, _zp, **_kw: (True, "companion_extension_base64", "")
            buf_att = io.StringIO()
            with contextlib.redirect_stdout(buf_att):
                att_ok = da.attach_zip(stub_page, focused_zip)
            assert att_ok is True, "Focused archive must be allowed in a continuation holding the base archive (R8.2)"
            assert "allowing focused archive" in buf_att.getvalue()
            assert getattr(stub_page, "_argon_last_attach_path", "") == "companion_extension_base64"

            # R8.3: When even a focused archive cannot be attached (e.g. exceeds attach cap),
            # attach_zip reports by name and flags switching to a FRESH chat with the full archive!
            orig_cap_fn = da.get_effective_base64_attach_cap
            try:
                da.get_effective_base64_attach_cap = lambda *_a, **_k: 10  # 10-byte cap to force R8.3 fallback
                stub_page2 = _ContinuationStubPage()
                buf_r83_out = io.StringIO()
                buf_r83_err = io.StringIO()
                with contextlib.redirect_stdout(buf_r83_out), contextlib.redirect_stderr(buf_r83_err):
                    att_r83 = da.attach_zip(stub_page2, focused_zip)
                assert att_r83 is False
                assert getattr(stub_page2, "_argon_focused_archive_requires_fresh_chat", False) is True
                combined_r83 = buf_r83_out.getvalue() + "\n" + buf_r83_err.getvalue()
                assert "focused_archive_cannot_be_attached" in combined_r83
                assert "switching to a FRESH chat with the full archive" in combined_r83
            finally:
                da.get_effective_base64_attach_cap = orig_cap_fn
        finally:
            da._attach_zip_via_datatransfer = orig_dt

    return {
        "ok": True,
        "skill_md_verified": True,
        "base_archive_sha256": read_back_stamp["sha256"][:16],
        "focused_changed_files": focused_info["changed_files"],
        "focused_attach_allowed_in_continuation": True,
        "fresh_chat_fallback_named": True,
    }


def run_round28_lane_plan_suite() -> Dict[str, Any]:
    t0 = time.perf_counter()
    c1 = case_1_w1_followup_continues_recorded_and_refuses_when_missing()
    c2 = case_2_w2_two_lane_plan_verified_before_and_after_send()
    c3 = case_3_w3_capture_newest_turn_only_and_refuse_older_digest()
    c4 = case_4_w4_handoff_pack_unmistakable_file_and_rules()
    c5 = case_5_w5_installer_picks_by_ledger_never_mtime()
    c6 = case_6_w6_w8_skill_prompt_and_focused_archive_non_stale_tree()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    cases = [c1, c2, c3, c4, c5, c6]
    passed = sum(1 for c in cases if c.get("ok"))
    return {
        "ok": passed == len(cases),
        "passed": passed,
        "total": len(cases),
        "elapsed_ms": elapsed_ms,
        "case_1": c1,
        "case_2": c2,
        "case_3": c3,
        "case_4": c4,
        "case_5": c5,
        "case_6": c6,
    }


def main() -> int:
    print("=" * 78)
    print("  ROUND 28 SUITE (Lane Plan, Delivery Selection, & Non-Stale Focused Archive)")
    print("=" * 78)
    rep = run_round28_lane_plan_suite()
    print(f"  [PASS] Case 1 (W1 Follow-Up Continuation & Refusal) : {rep['case_1']}")
    print(f"  [PASS] Case 2 (W2 Two-Lane Plan Verification)       : {rep['case_2']}")
    print(f"  [PASS] Case 3 (W3 Newest-Turn Chip & Ledger Refusal): {rep['case_3']}")
    print(f"  [PASS] Case 4 (W4 Manual Hand-Off Pack & Digests)   : {rep['case_4']}")
    print(f"  [PASS] Case 5 (W5 Installer Picks by Ledger)        : {rep['case_5']}")
    print(f"  [PASS] Case 6 (W6 SKILL.md & W8 Focused Archive)    : {rep['case_6']}")
    print(f"  Result: {rep['passed']}/{rep['total']} PASS in {rep['elapsed_ms']} ms")
    print("=" * 78)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
