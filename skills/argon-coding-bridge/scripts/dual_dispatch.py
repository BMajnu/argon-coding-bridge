#!/usr/bin/env python3
"""
Dual-Session Dispatch Orchestrator for the Gemini 4 Argon Coding Bridge.

Rules implemented (R1-R7, D1-D5, Section 3.2):
1. First round stays SINGLE: if a project has no session history in
   ~/.gemini/argon_sessions.json, run a single dispatch (today's behaviour).
2. From the second round onward, require an explicit recorded winner from the
   previous round (`--record-winner followup|new`). Never guess when no winner
   is recorded.
3. Build TWO genuinely different prompt texts:
   - FOLLOW-UP prompt (`followup_prompt.md`), continuing in the previous round's
     winning conversation on the winning slot;
   - NEW-SESSION prompt (`new_prompt.md`), opening a brand-new conversation on a
     DIFFERENT free pool slot (`--new-session`).
4. Package `argon_context.zip` ONCE for both lanes, launch both lanes as separate
   OS processes concurrently so each runs in its own isolated slot browser
   profile, and save deliveries into:
   - `downloads/dual/<round-id>/followup/apply_changes.ps1`
   - `downloads/dual/<round-id>/new/apply_changes.ps1`
5. Record both lanes in `~/.gemini/argon_sessions.json` and run
   `compare_argon_patches.py` to generate `downloads/dual/<round-id>/comparison.html`.
   Never auto-apply either patch during a dual run.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from argon_pool_login import PoolConfig, normalize_slot
from compare_argon_patches import compare_patches, render_comparison_html
from dispatch_argon import (
    EXIT_FAILURE,
    EXIT_SLOT_BUSY,
    EXIT_SUCCESS,
    SCRIPTS_DIR,
    SessionStore,
    SlotLock,
    _pool_slots_in_order,
    _profile_dir_for_slot,
    _profile_is_busy,
    build_followup_prompt,
    build_new_session_prompt,
    build_surgical_prompt,
    dispatch_to_argon,
    find_delivered_patch,
)
from package_context import package_context

DUAL_DOWNLOADS_SUBDIR = "dual"


def has_session_history(session_entry: Optional[Dict[str, Any]]) -> bool:
    """Return True if the project has any recorded session history."""
    if not isinstance(session_entry, dict):
        return False
    if str(session_entry.get("chat_id") or "").strip():
        return True
    if str(session_entry.get("chat_url") or "").strip():
        return True
    lanes = session_entry.get("lanes")
    if isinstance(lanes, dict):
        for lane_data in lanes.values():
            if isinstance(lane_data, dict) and (
                str(lane_data.get("chat_id") or "").strip()
                or str(lane_data.get("chat_url") or "").strip()
            ):
                return True
    return False


def _is_slot_free(base_profile: Path, slot: str) -> bool:
    norm = normalize_slot(slot)
    slot_prof = _profile_dir_for_slot(base_profile, norm)
    if _profile_is_busy(slot_prof):
        return False
    lock = SlotLock(norm, profile_dir=slot_prof)
    if not lock.acquire(slot_prof):
        return False
    lock.release()
    return True


def pick_new_session_slot(
    pool_cfg: Optional[PoolConfig],
    followup_slot: str,
    preferred_slot: Optional[str] = None,
) -> Optional[str]:
    """Pick a free pool slot for the NEW-SESSION lane that differs from `followup_slot`."""
    norm_followup = normalize_slot(followup_slot)
    base_profile = pool_cfg.profile_dir if pool_cfg is not None else (Path.home() / ".gemini" / "argon_browser_profile")

    if preferred_slot:
        norm_pref = normalize_slot(preferred_slot)
        if norm_pref == norm_followup:
            return None
        if _is_slot_free(base_profile, norm_pref):
            return norm_pref
        return None

    candidates: List[str] = []
    if pool_cfg is not None:
        for s in _pool_slots_in_order(pool_cfg, norm_followup):
            ns = normalize_slot(s)
            if ns != norm_followup and ns not in candidates:
                candidates.append(ns)
        for acc in (pool_cfg.accounts or []):
            raw_s = acc.get("slot")
            if raw_s:
                ns = normalize_slot(raw_s)
                if ns != norm_followup and ns not in candidates:
                    candidates.append(ns)

    if not candidates:
        fallback_slots = ["/u/0/", "/u/1/", "/u/2/", "/u/3/", "/u/4/", "/u/5/", "/u/6/", "/u/7/"]
        candidates = [s for s in fallback_slots if s != norm_followup]

    for cand in candidates:
        if _is_slot_free(base_profile, cand):
            return cand
    return None


def prepare_dual_round(
    task: str,
    project_path: Union[str, Path],
    round_id: Optional[str] = None,
    winning_chat_id: str = "",
    winning_slot: str = "",
) -> Dict[str, Any]:
    """Create the per-round directory layout and write both prompts to disk (D4, D5, R1)."""
    proj_root = Path(project_path).resolve()
    rid = (round_id or "").strip() or time.strftime("%Y%m%d_%H%M%S")
    round_dir = proj_root / "downloads" / DUAL_DOWNLOADS_SUBDIR / rid
    followup_dir = round_dir / "followup"
    new_dir = round_dir / "new"
    followup_dir.mkdir(parents=True, exist_ok=True)
    new_dir.mkdir(parents=True, exist_ok=True)

    followup_prompt = build_followup_prompt(
        task_description=task,
        target_directory=str(proj_root),
        winning_chat_id=winning_chat_id,
        winning_slot=winning_slot,
    )
    new_prompt = build_new_session_prompt(
        task_description=task,
        target_directory=str(proj_root),
    )

    followup_prompt_path = round_dir / "followup_prompt.md"
    new_prompt_path = round_dir / "new_prompt.md"
    followup_prompt_path.write_text(followup_prompt, encoding="utf-8")
    new_prompt_path.write_text(new_prompt, encoding="utf-8")

    f_first = followup_prompt.splitlines()[0] if followup_prompt.splitlines() else ""
    n_first = new_prompt.splitlines()[0] if new_prompt.splitlines() else ""

    print("=" * 78)
    print(f"  DUAL-SESSION DISPATCH - ROUND {rid}")
    print("=" * 78)
    print(f"  [+] Follow-up prompt  : {len(followup_prompt)} chars")
    print(f"      first line        : {f_first}")
    print(f"      saved to          : {followup_prompt_path}")
    print(f"  [+] New-session prompt: {len(new_prompt)} chars")
    print(f"      first line        : {n_first}")
    print(f"      saved to          : {new_prompt_path}")
    print("=" * 78)

    return {
        "round_id": rid,
        "round_dir": round_dir,
        "followup_dir": followup_dir,
        "new_dir": new_dir,
        "followup_prompt_path": followup_prompt_path,
        "new_prompt_path": new_prompt_path,
        "followup_prompt": followup_prompt,
        "new_prompt": new_prompt,
    }


def _stream_pipe(pipe: Any, prefix: str, sink: List[str]) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            clean = line.rstrip("\r\n")
            sink.append(clean)
            print(f"{prefix} {clean}")
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _ensure_canonical_patch_path(lane_dir: Path) -> Optional[Path]:
    canonical = lane_dir / "apply_changes.ps1"
    if canonical.is_file() and canonical.stat().st_size > 0:
        return canonical
    found = find_delivered_patch([lane_dir])
    if found is not None and found.is_file():
        if found.resolve() != canonical.resolve():
            canonical.write_bytes(found.read_bytes())
        return canonical
    return None


def run_dual_dispatch(
    task: str,
    project_path: str = ".",
    mode: str = "gemini",
    slot: Optional[str] = None,
    second_slot: Optional[str] = None,
    headless: bool = False,
    manual_setup: bool = False,
    target_files: Optional[List[str]] = None,
    output_zip: str = "argon_context.zip",
    verify_timeout: int = 900,
    verify_cmd: Optional[str] = None,
    skip_verify: bool = False,
    round_id: Optional[str] = None,
    open_comparison: bool = False,
    dry_run: bool = False,
) -> int:
    """Orchestrate single-on-first-round vs concurrent two-lane dual dispatch."""
    proj_root = Path(project_path).resolve()
    session_store = SessionStore()
    session_entry = session_store.lookup(proj_root, mode=mode)

    # R3: If the project has no session history at all, send ONE normal API call.
    if not has_session_history(session_entry):
        print(
            f"[+] No session history found for {proj_root} - taking the SINGLE dispatch path "
            "(1 prompt, 1 chat). Dual dispatch applies from the next round onward."
        )
        if dry_run:
            single_prompt = build_surgical_prompt(task, target_directory=str(proj_root), is_continuation=False)
            first_ln = single_prompt.splitlines()[0] if single_prompt.splitlines() else ""
            print(f"[+] Single prompt length: {len(single_prompt)} chars | first line: {first_ln}")
            return EXIT_SUCCESS
        return dispatch_to_argon(
            task=task,
            project_path=str(proj_root),
            mode=mode,
            slot=slot,
            headless=headless,
            auto_apply=False,
            manual_setup=manual_setup,
            target_files=target_files,
            output_zip=output_zip,
            new_session=False,
            resume=False,
            carry_over=True,
            verify_timeout=verify_timeout,
            verify_cmd=verify_cmd,
            skip_verify=skip_verify,
        )

    # R5 & D3: With session history present, require an explicitly recorded winner.
    winner_info = session_store.get_winner(proj_root, mode=mode)
    if winner_info is None:
        print(
            f"[ERROR] Session history exists for {proj_root}, but NO WINNER has been recorded "
            "for the previous round.",
            file=sys.stderr,
        )
        print(
            "        Refusing to guess which session won. Record the winner explicitly first:",
            file=sys.stderr,
        )
        print(
            f'          python "{SCRIPTS_DIR / "dispatch_argon.py"}" --project-path "{proj_root}" --record-winner followup',
            file=sys.stderr,
        )
        print(
            f'          python "{SCRIPTS_DIR / "dispatch_argon.py"}" --project-path "{proj_root}" --record-winner new',
            file=sys.stderr,
        )
        return EXIT_FAILURE

    followup_slot = normalize_slot(winner_info["slot"])
    followup_chat_id = str(winner_info["chat_id"])
    followup_chat_url = str(winner_info["chat_url"])

    try:
        pool_cfg = PoolConfig()
    except Exception:
        pool_cfg = None

    new_slot = pick_new_session_slot(pool_cfg, followup_slot, preferred_slot=second_slot)
    if not new_slot or normalize_slot(new_slot) == followup_slot:
        print(
            f"[ERROR] Dual dispatch requires two DIFFERENT free pool slots. "
            f"Follow-up lane uses {followup_slot}, and no distinct free slot was available for the NEW lane.",
            file=sys.stderr,
        )
        return EXIT_SLOT_BUSY

    round_meta = prepare_dual_round(
        task=task,
        project_path=proj_root,
        round_id=round_id,
        winning_chat_id=followup_chat_id,
        winning_slot=followup_slot,
    )
    rid = round_meta["round_id"]
    round_dir: Path = round_meta["round_dir"]
    followup_dir: Path = round_meta["followup_dir"]
    new_dir: Path = round_meta["new_dir"]
    followup_prompt_path: Path = round_meta["followup_prompt_path"]
    new_prompt_path: Path = round_meta["new_prompt_path"]

    print(
        f"[+] Lane FOLLOW-UP : slot {followup_slot} -> continuing winning chat {followup_chat_id} ({followup_chat_url})"
    )
    print(
        f"[+] Lane NEW       : slot {new_slot} -> opening a brand-new chat (--new-session) on a distinct pool slot"
    )

    zip_path = proj_root / output_zip if not Path(output_zip).is_absolute() else Path(output_zip)
    dispatch_script = SCRIPTS_DIR / "dispatch_argon.py"

    followup_cmd = [
        sys.executable,
        str(dispatch_script),
        "--task",
        task,
        "--project-path",
        str(proj_root),
        "--mode",
        mode,
        "--slot",
        followup_slot,
        "--prompt-file",
        str(followup_prompt_path),
        "--delivery-dir",
        str(followup_dir),
        "--lane",
        "followup",
        "--skip-package",
        "--no-carry-over",
        "--output-zip",
        str(zip_path),
    ]
    new_cmd = [
        sys.executable,
        str(dispatch_script),
        "--task",
        task,
        "--project-path",
        str(proj_root),
        "--mode",
        mode,
        "--slot",
        new_slot,
        "--new-session",
        "--prompt-file",
        str(new_prompt_path),
        "--delivery-dir",
        str(new_dir),
        "--lane",
        "new",
        "--skip-package",
        "--no-carry-over",
        "--output-zip",
        str(zip_path),
    ]
    if headless:
        followup_cmd.append("--headless")
        new_cmd.append("--headless")
    if manual_setup:
        followup_cmd.append("--manual-setup")
        new_cmd.append("--manual-setup")
    if skip_verify:
        followup_cmd.append("--skip-verify")
        new_cmd.append("--skip-verify")
    if verify_cmd:
        followup_cmd.extend(["--verify-cmd", verify_cmd])

    if dry_run:
        print(f"[DRY-RUN] Follow-up command : {' '.join(followup_cmd)}")
        print(f"[DRY-RUN] New-session command: {' '.join(new_cmd)}")
        return EXIT_SUCCESS

    # 3.2(3): Package the project ONCE for both lanes.
    package_context(
        project_path=str(proj_root),
        output_zip=str(zip_path),
        task_description=task,
        target_files=target_files,
    )

    # Ensure top-level session entry points at the winning chat before follow-up starts.
    recorded = session_store.record_winner(proj_root, winner_info["winner"])
    if not recorded:
        # Without this the follow-up lane would quietly open a NEW chat
        # instead of continuing in the winning one - and the whole point of
        # the dual dispatch would be lost.
        print(
            "[ERROR] The winner could not be promoted to the top-level session "
            "entry, so the follow-up lane would not continue in the winning "
            "chat. Aborting instead of running both lanes on the wrong chats.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    print("[+] Starting FOLLOW-UP and NEW-SESSION processes concurrently...")
    proc_followup = subprocess.Popen(
        followup_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    proc_new = subprocess.Popen(
        new_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    followup_logs: List[str] = []
    new_logs: List[str] = []
    t_f = threading.Thread(
        target=_stream_pipe,
        args=(proc_followup.stdout, f"[FOLLOWUP {followup_slot}]", followup_logs),
        daemon=True,
    )
    t_n = threading.Thread(
        target=_stream_pipe,
        args=(proc_new.stdout, f"[NEW {new_slot}]", new_logs),
        daemon=True,
    )
    t_f.start()
    t_n.start()

    rc_followup = proc_followup.wait()
    rc_new = proc_new.wait()
    t_f.join(timeout=5.0)
    t_n.join(timeout=5.0)

    followup_patch = _ensure_canonical_patch_path(followup_dir)
    new_patch = _ensure_canonical_patch_path(new_dir)

    latest_entry = session_store.lookup(proj_root, mode=mode) or {}
    lanes_now = latest_entry.get("lanes") if isinstance(latest_entry.get("lanes"), dict) else {}
    f_lane_data = dict(lanes_now.get("followup") or {})
    n_lane_data = dict(lanes_now.get("new") or {})

    f_lane_data.setdefault("slot", followup_slot)
    f_lane_data.setdefault("chat_id", followup_chat_id)
    f_lane_data.setdefault("chat_url", followup_chat_url)
    if followup_patch is not None:
        f_lane_data["patch_path"] = str(followup_patch)

    n_lane_data.setdefault("slot", new_slot)
    if new_patch is not None:
        n_lane_data["patch_path"] = str(new_patch)

    comparison_html_path = round_dir / "comparison.html"
    merged_patch_path = round_dir / "merged_apply_changes.ps1"

    session_store.remember_dual_round(
        proj_root,
        mode=mode,
        followup_lane=f_lane_data,
        new_lane=n_lane_data,
        round_id=rid,
        dual_dir=str(round_dir),
        comparison_html=str(comparison_html_path) if (followup_patch and new_patch) else "",
        merged_patch=str(merged_patch_path),
    )

    if followup_patch is not None and new_patch is not None:
        comparison = compare_patches(followup_patch, new_patch, project_path=proj_root)
        render_comparison_html(
            comparison,
            output_html=comparison_html_path,
            followup_prompt_path=followup_prompt_path,
            new_prompt_path=new_prompt_path,
        )
        print("=" * 78)
        print(f"  DUAL DISPATCH COMPLETE - ROUND {rid}")
        print("=" * 78)
        print(f"  follow-up patch   : {followup_patch}")
        print(f"  new-session patch : {new_patch}")
        print(f"  comparison report : {comparison_html_path}")
        print("  NOTE: Neither patch was auto-applied. Compare them and record the winner:")
        print(
            f'    python "{SCRIPTS_DIR / "dispatch_argon.py"}" --project-path "{proj_root}" --record-winner <followup|new>'
        )
        print("  Or compare / merge / apply with one click:")
        print(
            f'    python "{SCRIPTS_DIR / "compare_argon_patches.py"}" --round-dir "{round_dir}" --open'
        )
        print("=" * 78)
        if open_comparison:
            try:
                import webbrowser

                webbrowser.open(comparison_html_path.as_uri())
            except Exception:
                pass
        return EXIT_SUCCESS

    print(
        f"[!] Dual dispatch finished without both patches (followup_rc={rc_followup}, new_rc={rc_new}, "
        f"followup_patch={followup_patch}, new_patch={new_patch}).",
        file=sys.stderr,
    )
    return EXIT_FAILURE


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dual-session dispatcher for Gemini 4 Argon (follow-up in winner + brand-new session)."
    )
    parser.add_argument("--task", required=True, help="Task or bug description for Gemini 4 Argon")
    parser.add_argument("--project-path", default=".", help="Path to the project root")
    parser.add_argument("--mode", choices=["gemini"], default="gemini", help="Target workspace mode")
    parser.add_argument("--slot", default=None, help="Optional slot override for single-round fallback")
    parser.add_argument("--second-slot", default=None, help="Optional explicit pool slot for the new-session lane")
    parser.add_argument("--headless", action="store_true", help="Run browsers in headless mode")
    parser.add_argument("--manual-setup", action="store_true", help="Assume workspace mode/model already configured")
    parser.add_argument("--files", nargs="*", default=None, help="Optional target file patterns")
    parser.add_argument("--output-zip", default="argon_context.zip", help="Output zip path")
    parser.add_argument("--round-id", default=None, help="Optional explicit round id (default: YYYYMMDD_HHMMSS)")
    parser.add_argument("--open-comparison", action="store_true", help="Open comparison.html when finished")
    parser.add_argument("--dry-run", action="store_true", help="Build prompts and validate slots without launching browsers")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_dual_dispatch(
        task=args.task,
        project_path=args.project_path,
        mode=args.mode,
        slot=args.slot,
        second_slot=args.second_slot,
        headless=args.headless,
        manual_setup=args.manual_setup,
        target_files=args.files,
        output_zip=args.output_zip,
        round_id=args.round_id,
        open_comparison=args.open_comparison,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
