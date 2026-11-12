"""Manual hand-off pack generator and browser launcher for Gemini 4 Argon Coding Bridge (Round 13).

Provides:
  - progress tracking constants and formatting (send_progress: none, zip_attached, prompt_pasted, prompt_sent, verified)
  - writing manual_handoff.md and handoff.json into .argon_backup/handoff/<yyyyMMdd_HHmmss>/
  - opening the target conversation in that slot's real Chrome profile and leaving it open
  - CLI handler for `dispatch_argon.py --manual-handoff`
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROGRESS_DESCRIPTIONS = {
    "none": "Manual hand-off pack prepared upfront before send (prompt and ZIP ready for manual or automated send).",
    "prompt_pasted": "The model was verified as 4 Argon and the prompt text is pasted in the composer.",
    "zip_attached": "The prompt is in the composer and the context archive (ZIP) upload has completed.",
    "prompt_sent": "The prompt and context archive were submitted to Gemini 4 Argon.",
    "verified": "The prompt was verified to have landed in the conversation.",
}

PROGRESS_ORDER = ("none", "prompt_pasted", "zip_attached", "prompt_sent", "verified")


def format_progress_in_words(progress: str) -> str:
    p = str(progress or "none").strip().lower()
    return PROGRESS_DESCRIPTIONS.get(p, f"Progress level: {p}")


def compute_missing_actions(send_progress: str, zip_path: Path, prompt_path: Path, chat_url: str) -> List[str]:
    p = str(send_progress or "none").strip().lower()
    if p == "none":
        return [
            f"1. Open the conversation link in the Argon browser ({chat_url}).",
            "2. Verify Task mode is active and '4 Argon (Complex tasks)' model is selected (never Flash).",
            f"3. Copy the full prompt from {prompt_path.resolve()} and paste into the composer.",
            f"4. Attach the context archive: {zip_path.resolve()}",
            "5. The moment the upload spinner finishes, click the Send button immediately.",
            "6. Wait for Argon to generate apply_changes.ps1 (and any image/SVG/audio artifacts) and download via the chip's Export -> Download menu item.",
        ]
    elif p == "prompt_pasted":
        return [
            f"1. Open the conversation link in the Argon browser ({chat_url}) where the prompt is already pasted.",
            "2. Confirm the model chip still reads '4 Argon' (never Flash).",
            f"3. Attach the context archive: {zip_path.resolve()}",
            "4. The moment the upload finishes, click the Send button immediately.",
            "5. Wait for Argon to generate apply_changes.ps1 and download via the chip's Export -> Download menu item.",
        ]
    elif p == "zip_attached":
        return [
            f"1. Focus the composer in the Argon browser ({chat_url}) where the prompt is pasted and the ZIP upload has finished.",
            "2. Confirm the model chip reads '4 Argon' (never Flash) and click the Send button immediately.",
            "3. Wait for Argon to generate apply_changes.ps1 and download via the chip's Export -> Download menu item.",
        ]
    elif p == "prompt_sent":
        return [
            f"1. In the Argon browser ({chat_url}), verify Argon is generating the response.",
            "2. Wait for the response to finish and look for the apply_changes.ps1 generated-file chip (and any asset chips).",
            "3. Click the chip's Export button -> leaf 'Download' item, and save to the downloads folder (or run with --resume to build argon_delivery.zip and DELIVERY_MANIFEST.md automatically).",
        ]
    else:  # verified
        return [
            f"1. Prompt was already verified in the conversation ({chat_url}).",
            "2. Wait for the generated apply_changes.ps1 (and argon_delivery.zip bundle) or run with --resume.",
        ]

def compute_manual_commands(
    proj_root: Path,
    chrome_exe: Optional[str],
    profile_dir: Optional[Path],
    chat_url: str,
    lane: str,
    slot: str,
    delivery_folder: Path,
) -> List[str]:
    cmds: List[str] = []
    if chrome_exe and profile_dir:
        ext_flags = ""
        try:
            from safe_browser import get_companion_extension_dir

            ext_dir = get_companion_extension_dir()
            if ext_dir is not None:
                ext_flags = f'--disable-extensions-except="{ext_dir}" --load-extension="{ext_dir}" '
        except Exception:
            ext_flags = ""
        cmds.append(
            f'"{chrome_exe}" --user-data-dir="{profile_dir.resolve()}" '
            '--disable-blink-features=AutomationControlled --no-default-browser-check '
            f'--no-first-run {ext_flags}"{chat_url}"'
        )
    cmds.append(
        f'python skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path "{proj_root.resolve()}" '
        f'--resume --lane {lane} --slot {slot}'
    )
    cmds.append(
        f'powershell -ExecutionPolicy Bypass -File "{(delivery_folder / "apply_changes.ps1").resolve()}"'
    )
    return cmds

def write_manual_handoff_pack(
    proj_root: Path,
    lane: str = "new",
    slot: str = "/u/0/",
    email: str = "",
    chat_url: str = "",
    prompt_text: str = "",
    prompt_path: Optional[Path] = None,
    zip_path: Optional[Path] = None,
    send_progress: str = "none",
    failed_step: str = "unknown",
    last_screenshot: Optional[Path] = None,
    chrome_exe: Optional[str] = None,
    profile_dir: Optional[Path] = None,
    session_folder: Optional[Path] = None,
    delivery_folder: Optional[Path] = None,
    timestamp: Optional[str] = None,
    phase: str = "send",
    attempt: int = 0,
    max_attempts: int = 3,
    quarantined_file: Optional[str] = None,
    failure_reason: str = "",
) -> Tuple[Path, Path]:
    """Write manual_handoff.md and handoff.json into .argon_backup/handoff/<timestamp>/."""
    proj_root = Path(proj_root).resolve()
    ts = timestamp or time.strftime("%Y%m%d_%H%M%S")
    handoff_dir = proj_root / ".argon_backup" / "handoff" / ts
    handoff_dir.mkdir(parents=True, exist_ok=True)

    eff_lane = (lane or "new").strip().lower()
    eff_slot = slot or "/u/0/"
    eff_chat_url = chat_url or "https://gemini.google.com/u/0/app"
    eff_zip = (zip_path or (proj_root / "argon_context.zip")).resolve()
    eff_prompt_path = (prompt_path or (handoff_dir / "dispatch_prompt.md")).resolve()
    eff_session_dir = (session_folder or (proj_root / ".argon_backup")).resolve()
    eff_delivery_dir = (delivery_folder or (proj_root / "downloads" / eff_lane)).resolve()
    eff_delivery_dir.mkdir(parents=True, exist_ok=True)

    if not eff_prompt_path.is_file() and prompt_text:
        try:
            eff_prompt_path.write_text(prompt_text, encoding="utf-8")
        except Exception:
            pass

    progress_words = format_progress_in_words(send_progress)
    missing_actions = compute_missing_actions(send_progress, eff_zip, eff_prompt_path, eff_chat_url)
    commands = compute_manual_commands(
        proj_root=proj_root,
        chrome_exe=chrome_exe,
        profile_dir=profile_dir,
        chat_url=eff_chat_url,
        lane=eff_lane,
        slot=eff_slot,
        delivery_folder=eff_delivery_dir,
    )

    shot_str = str(last_screenshot.resolve()) if last_screenshot and Path(last_screenshot).is_file() else (
        str((proj_root / ".argon_backup" / "shots" / "01-preflight_failure.png").resolve())
    )

    # 1. Write manual_handoff.md
    md_lines: List[str] = [
        "# Gemini 4 Argon - Manual Hand-Off Pack",
        f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Project Root:** `{proj_root}`",
        "",
        "---",
        "",
        "## 1. Lane and Slot",
        f"- **Lane:** `{eff_lane}`",
        f"- **Slot:** `{eff_slot}`",
        "",
        "## 2. Gmail Account E-mail",
        f"- **Account E-mail:** `{email or '(not detected / default slot account)'}`",
        "",
        "## 3. Chat Link",
        f"- **Conversation URL:** {eff_chat_url}",
        "",
        "## 4. Prompt Details",
        f"- **Prompt File Path:** `{eff_prompt_path}`",
        "- **Prompt Text:**",
        "```text",
        prompt_text.strip() if prompt_text.strip() else "(Prompt text was not provided or empty)",
        "```",
        "",
        "## 5. Context Archive (ZIP)",
        f"- **Absolute ZIP Path:** `{eff_zip}`",
        f"- **Exists on Disk:** {'Yes' if eff_zip.is_file() else 'No'}",
        "",
        "## 6. Dispatch Artefacts Folders",
        f"- **Backup Session Folder:** `{eff_session_dir}`",
        f"- **Delivery Folder:** `{eff_delivery_dir}`",
        "",
        "## 7. How Far the Send Got (Progress)",
        f"- **Progress Status:** `{send_progress}`",
        f"- **Description:** {progress_words}",
        "",
        "## 8. Where It Failed & Retry Ladder State",
        f"- **Ladder Phase:** `{phase}` (attempt `{attempt}` of `{max_attempts}`)",
        f"- **Failing Step:** `{failed_step}`",
        f"- **Failure Reason:** `{failure_reason or failed_step}`",
        f"- **Quarantined Patch Path:** `{quarantined_file or '(none)'}`",
        f"- **Last Step Screenshot:** `{shot_str}`",
        "",
        "## 9. What Is Still Missing",
    ]
    for action in missing_actions:
        md_lines.append(f"- {action}")

    md_lines.extend([
        "",
        "## 10. Exact Commands to Finish by Hand",
        "```powershell",
    ])
    for cmd in commands:
        md_lines.append(cmd)
    md_lines.extend([
        "```",
        "",
        "---",
        "*Pack written by Argon Coding Bridge manual hand-off ladder.*",
        "",
    ])

    md_path = handoff_dir / "manual_handoff.md"
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # 2. Write handoff.json
    json_data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "project_root": str(proj_root),
        "lane": eff_lane,
        "slot": eff_slot,
        "account_email": email or "",
        "chat_url": eff_chat_url,
        "prompt_path": str(eff_prompt_path),
        "prompt_text": prompt_text,
        "zip_path": str(eff_zip),
        "session_folder": str(eff_session_dir),
        "delivery_folder": str(eff_delivery_dir),
        "send_progress": send_progress,
        "send_progress_words": progress_words,
        "phase": phase,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "failed_step": failed_step,
        "failure_reason": failure_reason or failed_step,
        "quarantined_file": quarantined_file or "",
        "last_screenshot": shot_str,
        "missing_actions": missing_actions,
        "commands": commands,
    }
    json_path = handoff_dir / "handoff.json"
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return md_path, json_path


def open_browser_for_slot(slot: str, chat_url: str, chrome_exe: Optional[str] = None, profile_dir: Optional[Path] = None) -> bool:
    """Open Chrome in the slot's real profile and leave it open.

    It opens the slot's CDP browser HOST, not a second plain Chrome process. A plain
    Chrome takes the profile directory's lock and publishes NO debugging endpoint, so
    the bridge can no longer attach to that profile - every later browser call on it
    fails, and the prompt that should have been sent is never sent. The host keeps a
    single Chrome per profile and publishes its endpoint, so the browser stays open
    AND usable.
    """
    from argon_pool_login import find_chrome_executable

    exe = chrome_exe or find_chrome_executable()
    if not exe:
        print("[!] Cannot open browser: Google Chrome executable was not found.", file=sys.stderr)
        return False

    from argon_pool_login import PoolConfig, is_isolated_slot_profile

    cfg = PoolConfig()
    if profile_dir is None:
        profile_dir = cfg.profile_dir_for_slot(slot or "/u/0/")

    profile_dir.mkdir(parents=True, exist_ok=True)
    default_url_slot = "/u/0/" if is_isolated_slot_profile(profile_dir) else f"/u/{cfg.get_account_index(slot)}/"
    target_url = chat_url or f"https://gemini.google.com{default_url_slot}app"

    # The CDP host: one Chrome for the whole Argon Bridge browser, with a debugging endpoint.
    try:
        from safe_browser import open_browser_cli

        if open_browser_cli(slot=slot, profile_dir=profile_dir, url=target_url) == 0:
            print(f"[+] Browser opened in Argon Bridge profile for slot {slot} and left open.")
            print(f"    Chat URL: {target_url}")
            return True
        print(
            "[!] The browser host could not be started for this profile; falling back to "
            "a plain Chrome (the bridge cannot attach to it).",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[i] The browser host could not be started ({exc}); falling back to a plain Chrome.")

    args = [
        str(exe),
        f"--user-data-dir={profile_dir.resolve()}",
        "--disable-blink-features=AutomationControlled",
        "--no-default-browser-check",
        "--no-first-run",
        target_url,
    ]

    try:
        if sys.platform == "win32":
            detached_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(args, creationflags=detached_flags, close_fds=True)
        else:
            subprocess.Popen(args, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[+] Browser opened in real Chrome profile for slot {slot} and left open.")
        print(f"    Chat URL: {target_url}")
        return True
    except Exception as exc:
        print(f"[!] Failed to launch Chrome: {exc}", file=sys.stderr)
        return False


def run_manual_handoff_cli(
    project_path: str = ".",
    lane: Optional[str] = None,
    slot: Optional[str] = None,
    open_browser: bool = False,
    mode: str = "gemini",
) -> int:
    """Handle the CLI command: dispatch_argon.py --project-path . --manual-handoff."""
    proj_root = Path(project_path).resolve()
    eff_lane = (lane or "new").strip().lower()

    # Import SessionStore, TaskState, _slot_key from dispatch_argon
    from dispatch_argon import SessionStore, TaskState, _slot_key
    from argon_pool_login import PoolConfig, find_chrome_executable, normalize_slot

    sess_store = SessionStore()
    sess_store.load()
    sess_entry = sess_store.lookup(proj_root, mode=mode) or {}

    task_state_store = TaskState()
    task_state_store.load()
    task_entry = task_state_store.get(proj_root, lane=eff_lane) or task_state_store.get(proj_root) or {}

    pool_cfg = None
    try:
        pool_cfg = PoolConfig()
    except Exception:
        pool_cfg = None

    eff_slot = slot or task_entry.get("slot") or sess_entry.get("slot") or "/u/0/"
    eff_slot = normalize_slot(eff_slot)

    email = task_entry.get("email") or sess_entry.get("email") or ""
    if not email and pool_cfg:
        for acc in pool_cfg.accounts:
            if isinstance(acc, dict) and normalize_slot(acc.get("slot", "")) == eff_slot:
                email = str(acc.get("email") or "")
                break

    from argon_pool_login import is_isolated_slot_profile

    profile_dir = (
        pool_cfg.profile_dir_for_slot(eff_slot)
        if pool_cfg is not None
        else (Path.home() / ".gemini" / "argon_browser_profile")
    )
    url_slot = (
        "/u/0/"
        if is_isolated_slot_profile(profile_dir)
        else f"/u/{pool_cfg.get_account_index(eff_slot) if pool_cfg is not None else 0}/"
    )
    chat_url = task_entry.get("chat_url") or sess_entry.get("chat_url") or f"https://gemini.google.com{url_slot}app"

    prompt_text = str(task_entry.get("task") or "").strip()
    prompt_path = None
    if task_entry.get("prompt_file"):
        cand_p = Path(task_entry["prompt_file"])
        if cand_p.is_file():
            prompt_path = cand_p
            if not prompt_text:
                try:
                    prompt_text = cand_p.read_text(encoding="utf-8")
                except Exception:
                    pass

    send_progress = str(task_entry.get("send_progress") or "none")
    failed_step = str(task_entry.get("failed_step") or task_entry.get("state") or "manual_request")
    phase = str(task_entry.get("phase") or ("delivery" if send_progress in ("prompt_sent", "verified") else "send"))
    attempt = int(task_entry.get("attempt") or task_entry.get("delivery_attempt") or task_entry.get("send_attempt") or 0)
    quarantined_file = str(task_entry.get("quarantined_file") or "")
    failure_reason = str(task_entry.get("last_failure_reason") or "")

    last_screenshot = None
    if task_entry.get("last_screenshot"):
        cand_s = Path(task_entry["last_screenshot"])
        if cand_s.is_file():
            last_screenshot = cand_s
    if not last_screenshot:
        shots_dir = proj_root / ".argon_backup" / "shots"
        if shots_dir.is_dir():
            shots = sorted(shots_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            if shots:
                last_screenshot = shots[0]

    chrome_exe = find_chrome_executable()

    md_path, json_path = write_manual_handoff_pack(
        proj_root=proj_root,
        lane=eff_lane,
        slot=eff_slot,
        email=email,
        chat_url=chat_url,
        prompt_text=prompt_text,
        prompt_path=prompt_path,
        zip_path=proj_root / "argon_context.zip",
        send_progress=send_progress,
        failed_step=failed_step,
        last_screenshot=last_screenshot,
        chrome_exe=chrome_exe,
        profile_dir=profile_dir,
        phase=phase,
        attempt=attempt,
        quarantined_file=quarantined_file,
        failure_reason=failure_reason,
    )

    if open_browser:
        open_browser_for_slot(eff_slot, chat_url, chrome_exe=chrome_exe, profile_dir=profile_dir)

    shot_disp = str(last_screenshot.resolve()) if last_screenshot else "(none)"
    zip_disp = str((proj_root / "argon_context.zip").resolve())
    prompt_disp = str(prompt_path.resolve()) if prompt_path else str((md_path.parent / "dispatch_prompt.md").resolve())

    print("[i] Manual hand-off summary:")
    print(f"    Account e-mail : {email or '(none / unconfigured)'}")
    print(f"    Chat link      : {chat_url}")
    print(f"    Prompt path    : {prompt_disp}")
    print(f"    ZIP path       : {zip_disp}")
    print(f"    Last screenshot: {shot_disp}")
    print(f"[i] Manual hand-off pack: {md_path.resolve()}")

    return 0
