#!/usr/bin/env python3
"""Round 26 Hermetic Regression Suite (`test_round26_composer_clear.py`).

Exercises and proves against the real production bridge and companion extension:
  1. DOM composer clear (`_clear_leftover_prompt_after_send` / `_clear_composer_via_dom`)
     empties a synthetic Gemini rich-textarea/Quill composer that ignores `Delete` and
     `Backspace` keypresses, and dispatches `input` + `change` events (R1.1, R3.1).
  2. Foreign text typed into the composer is left untouched (R1.3, R3.1).
  3. The clear helper ALWAYS logs an explicit outcome (`composer cleared (N chars removed)`
     or `composer left as is: <reason>`) across every path, and `_execute_composer_send_sequence`
     reports `composer after send : NOT EMPTY (N chars)` if a verified send leaves our prompt
     in the composer (R1.2, R1.4, R1.5, R3.1).
  4. A conversation containing a `user-query` inside a parent carrying `queued-steering-turn`
     (with upload chip and 'You said' header stripped) is recognised as 'already sent',
     reports `a queued steering turn with this prompt already exists`, and skips re-sending
     (W2: R2.1, R2.2, R2.3, R3.1).
  5. Companion extension (`skills/argon-coding-bridge/extension/content_script.js`) loaded
     and reloaded via CDP `Extensions.loadUnpacked`:
       - `paste_prompt` arms `keepPromptAlive = true` and `cachedPromptText`;
       - MutationObserver re-pastes when archive upload clears the composer BEFORE send;
       - `click_send` turns `keepPromptAlive = false`, clears `cachedPromptText = ""`, and
         returns `ok: true` once it performs the send (even if the composer is not yet empty),
         so the pure-Python fallback never fires a second turn;
       - MutationObserver NEVER re-pastes after the conversation has gained a turn.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import dispatch_argon as D  # noqa: E402
import safe_browser as SB  # noqa: E402

EXTENSION_JS = SCRIPTS_DIR.parent / "extension" / "content_script.js"


def _chrome():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _launch(pw):
    for kwargs in ({"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch(headless=True, args=["--no-first-run"], **kwargs)
        except Exception:
            continue
    raise RuntimeError("no usable Chrome/Chromium for the Round 26 suite")


def case_1_dom_clear_empties_composer_that_ignores_delete_key() -> Dict[str, Any]:
    """A composer that ignores Delete/Backspace (like Gemini's Quill editor) must still be emptied via DOM."""
    prompt = (
        "I have uploaded the relevant codebase context as a zip archive (argon_context.zip). "
        "Inside the archive, you will find TASK_BRIEF.md containing the project overview."
    )
    probe = prompt[:60]

    html = """<!doctype html><html><head><meta charset="utf-8"><style>
      .input-area-container { padding: 12px; }
      #composer { min-height: 48px; padding: 8px; border: 1px solid #555; }
    </style></head><body>
    <div id="transcript"></div>
    <div class="input-area-container">
      <rich-textarea>
        <div id="composer" contenteditable="true" role="textbox">
          <div class="ql-editor" contenteditable="true"></div>
        </div>
      </rich-textarea>
      <button id="send" class="send-button" aria-label="Send message">Send</button>
    </div>
    <script>
      window.__r26Events = { input: 0, change: 0, deleteBlocked: 0 };
      const comp = document.getElementById('composer');
      const ql = comp.querySelector('.ql-editor');
      const blockDelete = (e) => {
        if (e.key === 'Delete' || e.key === 'Backspace') {
          window.__r26Events.deleteBlocked += 1;
          e.preventDefault();
          e.stopImmediatePropagation();
          return false;
        }
      };
      comp.addEventListener('keydown', blockDelete, true);
      ql.addEventListener('keydown', blockDelete, true);
      document.addEventListener('keydown', blockDelete, true);
      comp.addEventListener('input', () => { window.__r26Events.input += 1; });
      comp.addEventListener('change', () => { window.__r26Events.change += 1; });
    </script></body></html>"""

    work = Path(tempfile.mkdtemp(prefix="argon_r26_domclear_"))
    try:
        page_file = work / "page.html"
        page_file.write_text(html, encoding="utf-8")
        with _chrome() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto(page_file.as_uri())
                page.evaluate(
                    "(txt) => { const q = document.querySelector('.ql-editor'); q.textContent = txt; }",
                    prompt,
                )
                composer = D._find_composer(page)
                assert composer is not None, "composer was not found on synthetic page"

                # First verify that raw Control+A + Delete really leaves the text in place
                D._focus_composer_dom(composer)
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                still_there = D._composer_text(composer)

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cleared = D._clear_leftover_prompt_after_send(
                        page, composer, probe_snippet=probe, expected_min_len=10
                    )
                logged = buf.getvalue().strip()
                after_clear = D._composer_text(composer)
                ev_counts = page.evaluate("() => window.__r26Events") or {}

                ok = (
                    len(still_there) >= len(prompt) - 5
                    and bool(cleared)
                    and after_clear == ""
                    and "composer cleared (" in logged
                    and "chars removed)" in logged
                    and int(ev_counts.get("input") or 0) >= 1
                    and int(ev_counts.get("change") or 0) >= 1
                )
                return {
                    "ok": ok,
                    "len_after_raw_delete": len(still_there),
                    "cleared": bool(cleared),
                    "composer_after": after_clear,
                    "logged": logged,
                    "events": ev_counts,
                }
            finally:
                browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def case_2_foreign_text_is_preserved_untouched() -> Dict[str, Any]:
    """A composer holding foreign human-typed text must be left untouched and logged."""
    sent_prompt = "Primary Round 26 task prompt with TASK_BRIEF.md and API_INDEX.md."
    probe = sent_prompt[:50]
    foreign_text = "Human note: do not delete this draft while I inspect the logs."

    html = """<!doctype html><html><head><meta charset="utf-8"><style>
      .input-area-container { padding: 12px; }
      #composer { min-height: 48px; padding: 8px; border: 1px solid #555; }
    </style></head><body>
    <div class="input-area-container">
      <rich-textarea>
        <div id="composer" contenteditable="true" role="textbox"></div>
      </rich-textarea>
    </div></body></html>"""

    work = Path(tempfile.mkdtemp(prefix="argon_r26_foreign_"))
    try:
        page_file = work / "page.html"
        page_file.write_text(html, encoding="utf-8")
        with _chrome() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto(page_file.as_uri())
                page.evaluate(
                    "(txt) => { document.getElementById('composer').textContent = txt; }",
                    foreign_text,
                )
                composer = D._find_composer(page)
                assert composer is not None, "composer not found"

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cleared = D._clear_leftover_prompt_after_send(
                        page, composer, probe_snippet=probe, expected_min_len=10
                    )
                logged = buf.getvalue().strip()
                after_text = D._composer_text(composer)

                ok = (
                    cleared is False
                    and after_text == foreign_text
                    and "composer left as is:" in logged
                )
                return {
                    "ok": ok,
                    "cleared": cleared,
                    "after_text": after_text,
                    "logged": logged,
                }
            finally:
                browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def case_3_clear_always_logs_outcome_and_preflight_reports_leftover() -> Dict[str, Any]:
    """Every call to `_clear_leftover_prompt_after_send` must log an outcome, and R1.5 reports NOT EMPTY if stuck."""
    prompt = "Round 26 verified send prompt payload for pre-flight summary check."
    probe = prompt[:50]

    class _StubComposer:
        def __init__(self, text: str, allow_clear: bool = True) -> None:
            self.text = text
            self.allow_clear = allow_clear

        def inner_text(self, **_kw: Any) -> str:
            return self.text

        def evaluate(self, _js: str, _arg: Any = None) -> Any:
            if self.allow_clear:
                self.text = ""
            return True

    class _StubKeyboard:
        def press(self, _k: str, **_kw: Any) -> None:
            return None

    class _StubPage:
        def __init__(self) -> None:
            self.keyboard = _StubKeyboard()
            self.url = "https://gemini.google.com/u/0/app/r26stub"

        def wait_for_timeout(self, _ms: float) -> None:
            return None

    orig_ext = D._extension_is_available
    orig_find = D._find_composer
    orig_click_send = D._click_send_immediately
    orig_verify_chip = D._verify_model_chip_is_argon
    orig_select_argon = D.select_argon_model
    orig_dup = D._check_duplicate_prompt
    orig_paste = D._paste_into_composer
    try:
        D._extension_is_available = lambda _p: False
        page = _StubPage()

        # 1. Empty composer -> must log `composer left as is: already empty`
        comp_empty = _StubComposer("")
        D._find_composer = lambda _p, wait_seconds=0.0: comp_empty
        buf_empty = io.StringIO()
        with contextlib.redirect_stdout(buf_empty):
            res_empty = D._clear_leftover_prompt_after_send(page, comp_empty, probe, 10)
        log_empty = buf_empty.getvalue().strip()

        # 2. Matching prompt -> must log `composer cleared (N chars removed)`
        comp_match = _StubComposer(prompt, allow_clear=True)
        buf_match = io.StringIO()
        with contextlib.redirect_stdout(buf_match):
            res_match = D._clear_leftover_prompt_after_send(page, comp_match, probe, 10)
        log_match = buf_match.getvalue().strip()

        # 3. Verified send that still leaves our prompt in composer (R1.5) -> preflight_log records NOT EMPTY
        comp_stuck = _StubComposer(prompt, allow_clear=False)
        D._find_composer = lambda _p, wait_seconds=0.0: comp_stuck
        D.select_argon_model = lambda _p, require_chip=True: True
        D._verify_model_chip_is_argon = lambda _p, stage="": (True, "4 Argon")
        D._check_duplicate_prompt = lambda *_a, **_kw: None
        D._paste_into_composer = lambda _p, _c, t: t
        D._click_send_immediately = lambda *_a, **_kw: True

        pf_log: List[str] = []
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err):
            mode_ok, model_ok, sent_ok = D._execute_composer_send_sequence(
                page=page,
                zip_path=None,
                prompt_text=prompt,
                manual_setup=True,
                preflight_log=pf_log,
            )

        not_empty_logged = any(
            f"composer after send : NOT EMPTY ({len(prompt)} chars)" in ln for ln in pf_log
        )
        ok = (
            res_empty is False
            and "composer left as is: already empty" in log_empty
            and res_match is True
            and f"composer cleared ({len(prompt)} chars removed)" in log_match
            and bool(mode_ok and model_ok and sent_ok)
            and not_empty_logged
        )
        return {
            "ok": ok,
            "log_empty": log_empty,
            "log_match": log_match,
            "not_empty_logged": not_empty_logged,
            "preflight_tail": pf_log[-2:] if pf_log else [],
        }
    finally:
        D._extension_is_available = orig_ext
        D._find_composer = orig_find
        D._click_send_immediately = orig_click_send
        D._verify_model_chip_is_argon = orig_verify_chip
        D.select_argon_model = orig_select_argon
        D._check_duplicate_prompt = orig_dup
        D._paste_into_composer = orig_paste


def case_4_queued_steering_turn_recognized_as_already_sent() -> Dict[str, Any]:
    """A `user-query` inside a `.queued-steering-turn` parent must count as already sent (W2: R2.1-R2.3)."""
    prompt = (
        "I have uploaded the relevant codebase context as a zip archive (`argon_context.zip`). "
        "Inside the archive, you will find `TASK_BRIEF.md` containing the project overview and "
        "file structure. Please deliver ONE `apply_changes.ps1` for Round 26."
    )
    # Simulate Gemini stripping backticks in the rendered queued-steering-turn (like 30,848 -> 30,835 chars)
    rendered_in_dom = prompt.replace("`", "")

    html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
    <div id="conversation-transcript">
      <div class="conversation-container message-actions-hover-boundary">
        <user-query>
          <div class="user-query-header">You said</div>
          <div class="user-query-text">Earlier round prompt from previous task</div>
        </user-query>
        <model-response>Earlier response</model-response>
      </div>
      <div class="conversation-container message-actions-hover-boundary queued-steering-turn">
        <user-query>
          <div class="attachment-chip" data-stem="argon_context">argon_context</div>
          <div class="user-query-header">You said</div>
          <div class="user-query-text">You said\n{rendered_in_dom}</div>
        </user-query>
      </div>
    </div>
    <div class="input-area-container">
      <rich-textarea>
        <div id="composer" contenteditable="true" role="textbox" style="min-height:40px;"></div>
      </rich-textarea>
    </div></body></html>"""

    work = Path(tempfile.mkdtemp(prefix="argon_r26_queued_"))
    orig_select_argon = D.select_argon_model
    orig_verify_chip = D._verify_model_chip_is_argon
    orig_paste = D._paste_into_composer
    try:
        page_file = work / "page.html"
        page_file.write_text(html, encoding="utf-8")
        with _chrome() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto(page_file.as_uri())

                turns = D._extract_user_turns(page)
                last_txt = D._last_user_message_text(page)

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    dup_res = D._check_duplicate_prompt(
                        page,
                        prompt_text=prompt,
                        prev_state={"state": "sent", "sent_at": "12:39:04"},
                        new_session=False,
                    )
                dup_out = buf.getvalue().strip()

                # Also verify _execute_composer_send_sequence skips pasting/sending and reports queued steering turn
                paste_called = False

                def _fail_if_pasted(*_a: Any, **_kw: Any) -> str:
                    nonlocal paste_called
                    paste_called = True
                    return prompt

                D.select_argon_model = lambda _p, require_chip=True: True
                D._verify_model_chip_is_argon = lambda _p, stage="": (True, "4 Argon")
                D._paste_into_composer = _fail_if_pasted

                pf_log: List[str] = []
                buf_seq = io.StringIO()
                with contextlib.redirect_stdout(buf_seq):
                    mode_ok, model_ok, sent_ok = D._execute_composer_send_sequence(
                        page=page,
                        zip_path=None,
                        prompt_text=prompt,
                        manual_setup=True,
                        preflight_log=pf_log,
                    )
                seq_out = buf_seq.getvalue().strip()

                ok = (
                    len(turns) == 2
                    and bool(turns[-1].get("queuedSteering"))
                    and "argon_context" not in last_txt[:20]
                    and not last_txt.lower().startswith("you said")
                    and dup_res is not None
                    and "a queued steering turn with this prompt already exists" in str(dup_res)
                    and "a queued steering turn with this prompt already exists" in dup_out
                    and "a queued steering turn with this prompt already exists" in seq_out
                    and any("a queued steering turn with this prompt already exists" in ln for ln in pf_log)
                    and not paste_called
                    and bool(mode_ok and model_ok and sent_ok)
                )
                return {
                    "ok": ok,
                    "turns_found": len(turns),
                    "queued_flag": bool(turns[-1].get("queuedSteering")) if turns else False,
                    "dup_res": dup_res,
                    "paste_called": paste_called,
                    "preflight_guard": pf_log[-1] if pf_log else "",
                }
            finally:
                browser.close()
    finally:
        D.select_argon_model = orig_select_argon
        D._verify_model_chip_is_argon = orig_verify_chip
        D._paste_into_composer = orig_paste
        shutil.rmtree(work, ignore_errors=True)


def case_5_companion_extension_disarms_keep_prompt_alive_and_sends_once() -> Dict[str, Any]:
    """Prove the companion extension root-cause fix (Addendum Requirements 1, 2, 3, 4 + CDP reload)."""
    cs_code = EXTENSION_JS.read_text(encoding="utf-8")
    prompt = (
        "Round 26 Addendum end-to-end prompt via companion extension: "
        "verify keepPromptAlive is turned OFF on send, cachedPromptText is cleared, "
        "click_send returns ok=true even if Gemini keeps composer text until Python clears it, "
        "and the MutationObserver never re-pastes after a turn is added."
    )
    probe = prompt[:60]

    work = Path(tempfile.mkdtemp(prefix="argon_r26_ext_"))
    try:
        test_zip = D._create_selftest_zip(work)
        raw_z = test_zip.read_bytes()
        b64_z = base64.b64encode(raw_z).decode("ascii")
        synth_file = work / "synthetic_gemini.html"
        synth_file.write_text(D.SYNTHETIC_GEMINI_SELFTEST_HTML, encoding="utf-8")

        with _chrome() as pw:
            browser = _launch(pw)
            try:
                ext_reloaded = False
                prof_dir = getattr(browser, "_prof_dir", None)
                if prof_dir is not None:
                    st = SB.read_host_state(Path(prof_dir)) or {}
                    http_ep = str(st.get("http_endpoint") or "")
                    if http_ep:
                        rep = SB.ensure_companion_extension_installed(
                            http_endpoint=http_ep,
                            ext_dir=EXTENSION_JS.parent,
                        )
                        ext_reloaded = bool(rep.get("ok"))

                page = browser.new_page()
                page.goto(synth_file.as_uri())
                page.evaluate(cs_code)

                # Patch synthetic page's send button so it also appends a <user-query> turn
                # and simulates Gemini Task mode keeping the prompt in the composer on click,
                # proving click_send still reports ok=true and Python's DOM clear empties it.
                page.evaluate(
                    """() => {
                        const sendBtn = document.getElementById('semantic-submit-btn');
                        const composer = document.getElementById('real-bottom-composer');
                        const transcript = document.getElementById('conversation-transcript');
                        sendBtn.addEventListener('click', () => {
                            const q = document.createElement('user-query');
                            q.className = 'user-query';
                            q.textContent = (window.__selftestState && window.__selftestState.sentMessages.length)
                                ? window.__selftestState.sentMessages[window.__selftestState.sentMessages.length - 1].text
                                : 'sent';
                            transcript.appendChild(q);
                            // Simulate Gemini Task mode leaving the prompt text in the composer right after click
                            if (window.__selftestState && window.__selftestState.sentMessages.length) {
                                composer.textContent = window.__selftestState.sentMessages[window.__selftestState.sentMessages.length - 1].text;
                            }
                        });
                    }"""
                )

                mm_res = D._invoke_companion_extension(
                    page,
                    "ensure_mode_and_model",
                    {"manualSetup": False, "isContinuation": False},
                    timeout_ms=8000,
                )
                paste_res = D._invoke_companion_extension(
                    page,
                    "paste_prompt",
                    {"promptText": prompt, "appendMode": False},
                    timeout_ms=8000,
                )
                rt_after_paste = D._invoke_companion_extension(
                    page, "get_runtime_state", {}, timeout_ms=5000
                )

                # Attach archive: synthetic page clears composer on upload, extension re-pastes BEFORE send
                att_res = D._invoke_companion_extension(
                    page,
                    "attach_archive",
                    {
                        "fileName": test_zip.name,
                        "expectedSize": len(raw_z),
                        "base64Data": b64_z,
                    },
                    timeout_ms=8000,
                )
                rt_after_attach = D._invoke_companion_extension(
                    page, "get_runtime_state", {}, timeout_ms=5000
                )

                # Now run _click_send_immediately (invokes extension click_send + Python DOM clear)
                composer = D._find_composer(page)
                body_before = D._page_body_snapshot(page)
                buf_send = io.StringIO()
                with contextlib.redirect_stdout(buf_send):
                    sent_ok = D._click_send_immediately(
                        page,
                        composer,
                        expected_min_len=20,
                        probe_snippet=probe,
                        body_len_before=len(body_before),
                        snippet_count_before=body_before.count(probe),
                    )
                send_stdout = buf_send.getvalue().strip()

                rt_after_send = D._invoke_companion_extension(
                    page, "get_runtime_state", {}, timeout_ms=5000
                )

                # Trigger a DOM mutation / empty composer after the turn exists to prove observer NEVER re-pastes
                page.evaluate(
                    """() => {
                        const c = document.getElementById('real-bottom-composer');
                        c.textContent = '';
                        c.setAttribute('data-post-send-probe', '1');
                    }"""
                )
                page.wait_for_timeout(150)
                composer_after_mutation = D._composer_text(composer)
                st_final = page.evaluate("() => window.__selftestState") or {}
                sent_count = len(st_final.get("sentMessages") or [])
                send_path = getattr(page, "_argon_last_send_path", "")

                ok = (
                    bool(mm_res.get("ok"))
                    and bool(paste_res.get("ok"))
                    and bool(rt_after_paste.get("keepPromptAlive") is True)
                    and int(rt_after_paste.get("cachedPromptTextLength") or 0) == len(prompt)
                    and bool(att_res.get("ok"))
                    and bool(att_res.get("composerRestored") is True)
                    and bool(rt_after_attach.get("keepPromptAlive") is True)
                    and bool(sent_ok)
                    and send_path == "extension"
                    and bool(rt_after_send.get("keepPromptAlive") is False)
                    and rt_after_send.get("cachedPromptText") == ""
                    and int(rt_after_send.get("cachedPromptTextLength") or 0) == 0
                    and bool(rt_after_send.get("sendPerformed") is True)
                    and composer_after_mutation == ""
                    and sent_count == 1
                    and "composer cleared (" in send_stdout
                )
                return {
                    "ok": ok,
                    "ext_reloaded": ext_reloaded,
                    "keep_alive_after_paste": rt_after_paste.get("keepPromptAlive"),
                    "repasted_on_upload_before_send": att_res.get("composerRestored"),
                    "send_path": send_path,
                    "keep_alive_after_send": rt_after_send.get("keepPromptAlive"),
                    "cached_len_after_send": rt_after_send.get("cachedPromptTextLength"),
                    "composer_after_post_send_mutation": composer_after_mutation,
                    "sent_messages_count": sent_count,
                    "send_stdout": send_stdout.splitlines()[-1] if send_stdout else "",
                }
            finally:
                browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


CASES = [
    ("case_1_dom_clear_empties_composer_that_ignores_delete_key", case_1_dom_clear_empties_composer_that_ignores_delete_key),
    ("case_2_foreign_text_is_preserved_untouched", case_2_foreign_text_is_preserved_untouched),
    ("case_3_clear_always_logs_outcome_and_preflight_reports_leftover", case_3_clear_always_logs_outcome_and_preflight_reports_leftover),
    ("case_4_queued_steering_turn_recognized_as_already_sent", case_4_queued_steering_turn_recognized_as_already_sent),
    ("case_5_companion_extension_disarms_keep_prompt_alive_and_sends_once", case_5_companion_extension_disarms_keep_prompt_alive_and_sends_once),
]


def run_round26_suite(verbose: bool = True) -> Dict[str, Any]:
    """Run every Round 26 + Addendum regression case and return a machine-readable report."""
    t0 = time.monotonic()
    passed = 0
    failures = 0
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
        "cases": details,
    }


def main() -> int:
    print("=" * 78)
    print("  ARGON ROUND 26 COMPOSER CLEAR & EXTENSION GUARD SUITE")
    print("=" * 78)
    rep = run_round26_suite(verbose=True)
    print("-" * 78)
    status = "PASS" if rep["ok"] else f"FAIL ({rep['failed']} case(s))"
    print(f"  Overall status : {status}  ({rep['elapsed_ms'] / 1000:.2f}s)")
    print("=" * 78)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
