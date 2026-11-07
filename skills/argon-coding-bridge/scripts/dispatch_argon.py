#!/usr/bin/env python3
"""
Automated Dispatcher for the Gemini 4 Argon Coding Bridge.

Workflow:
1. Packages the project codebase into argon_context.zip (via package_context.py).
2. Builds the surgical zero-UI-regression prompt from templates/argon_task_prompt_template.md.
3. Launches a persistent Chrome context (using the installed Google Chrome binary when
   available and falling back to bundled Chromium) on the pool profile directory.
4. Opens the target workspace URL for the pool slot:
   - Gemini Web:       https://gemini.google.com/u/<slot>/app
   (The slot IS part of the URL: every account has its own Gemini session under /u/<n>/.)
   Gemini Web is the ONLY workspace: Google AI Studio was removed because it
   consistently fails on agentic work.
5. Verifies an account is signed in (if not, directs the user to login_pool_accounts.py),
   rotates across pool accounts if quota is exhausted, uploads argon_context.zip, submits
   the prompt, extracts the returned PowerShell patch (apply_changes.ps1), and optionally
   applies it automatically.

Pre-flight gate (the important part):
   The workspace is configured and VERIFIED before anything is sent -
   Task mode (the টাস্ক / Task toggle, NOT চ্যাট / Chat), the 4 Argon model, and
   the zip really attached to the composer. The Gemini UI renders in
   the language of the signed-in account, so every text matcher is bilingual.
   If any check fails the prompt is NOT sent: the run exits with code 3, prints a
   bilingual checklist and saves .argon_backup/preflight_failure.png.

Session continuity:
   ~/.gemini/argon_sessions.json maps a project root to the Gemini conversation it
   was dispatched in. A follow-up task on the same project reopens that SAME chat,
   so the understanding Argon built earlier (conventions, decisions, naming) is
   still there. The conversation id is read back from the URL after the prompt
   is submitted and stored for the next task. `--new-session` forces a fresh
   chat.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from argon_pool_login import (
    CHROME_LAUNCH_ARGS,
    GEMINI_WEB_URL,
    PoolConfig,
    find_chrome_executable,
    normalize_slot,
)
from package_context import package_context

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
PROMPT_TEMPLATE_PATH = SKILL_ROOT / "templates" / "argon_task_prompt_template.md"
LOGIN_ASSISTANT_SCRIPT = SCRIPTS_DIR / "login_pool_accounts.py"
APPLY_PATCH_SCRIPT = SCRIPTS_DIR / "Apply-ArgonPatch.ps1"

# Per-project session map: which Gemini conversation to continue in.
# It lives in the USER profile, next to the browser profile, so every copy of
# this plugin (the repo, the Gemini CLI plugin, the MiniMax plugin) shares the
# same map. A per-plugin copy would lose the conversation as soon as another
# agent or another installation of the plugin picks the project up.
SESSION_STORE_PATH = Path.home() / ".gemini" / "argon_sessions.json"

# Per-project task state: which conversation the current task is in and how far
# it got, so a run can be resumed after the browser was closed.
TASK_STATE_PATH = Path.home() / ".gemini" / "argon_task_state.json"
TASK_REGISTRY_PATH = Path.home() / ".gemini" / "argon_tasks.json"
DELIVERY_LEDGER_PATH = Path.home() / ".gemini" / "argon_delivery_ledger.json"
TASK_STATE_VERSION = "1.1.0"
TASK_REGISTRY_VERSION = "1.0.0"
DELIVERY_LEDGER_VERSION = "1.0.0"
SESSION_STORE_VERSION = "1.0.0"
MAX_TASKS_PER_SLOT = 2

# Exit codes:
#   0 = success
#   1 = failure
#   2 = not signed in
#   3 = pre-flight failed
#   4 = browser closed
#   5 = usage limit reached (quota_limited)
#   6 = browser launch failed
#   7 = slot busy
#   8 = build/lint verification failed after all verification rounds
#   9 = task already running for this project/lane (use --resume or --force)
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_NOT_SIGNED_IN = 2
EXIT_PREFLIGHT_FAILED = 3
EXIT_BROWSER_CLOSED = 4
EXIT_QUOTA_LIMITED = 5
EXIT_BROWSER_LAUNCH_FAILED = 6
EXIT_SLOT_BUSY = 7
EXIT_BUILD_FAILED = 8
EXIT_VERIFY_FAILED = 8
EXIT_ALREADY_RUNNING = 9
SESSION_STORE_DESCRIPTION = (
    "Per-project Gemini session map. A follow-up task on the same project continues "
    "in the SAME conversation, so the earlier codebase understanding stays available."
)


# --------------------------------------------------------------------------- #
# Session continuity notes                                                      #
#                                                                             #
# A follow-up task on the same project runs in the SAME Gemini conversation,   #
# so the note tells Argon that the archive is a fresh snapshot while the      #
# conversation already holds the earlier understanding.                        #
# --------------------------------------------------------------------------- #

CONTINUATION_NOTE = """### 🧠 CONTINUING AN EXISTING SESSION
This is a follow-up task in the SAME conversation as the earlier work on this project.
- The attached archive is a FRESH snapshot of the project's CURRENT state, taken just now.
- Earlier messages in this conversation hold the codebase understanding, the decisions and the
  ideas from the previous tasks on this project.
- Use that context: keep the same conventions, the same naming and the same architectural
  choices, and build on the earlier work instead of starting over.
- If the earlier context and the attached snapshot disagree, trust the snapshot for the current
  file contents and the conversation for the intent."""

FIRST_TASK_NOTE = """### 🧠 FIRST TASK FOR THIS PROJECT
This is the first task for this project. The attached archive is a fresh snapshot of the
project's current state and is the only context you have."""


NO_GUESSING_RULE = (
    "If something you need is not in this archive - a file, an API endpoint, a\n"
    "function, a configuration - DO NOT GUESS and DO NOT INVENT it. Say exactly\n"
    "which file or symbol is missing and stop there. A wrong guess is much worse\n"
    "than an incomplete answer."
)

EXACT_API_RULE = (
    "API_INDEX.md lists every public declaration with its file and line. Use THAT -\n"
    "never invent a function name, a parameter or a return type. If a symbol you need\n"
    "is not in API_INDEX.md and not in the archive, DO NOT GUESS: name the missing\n"
    "symbol and stop."
)

BYTE_EXACT_RULE = (
    "Inside a patch, a Rust (or any) char literal for a newline is the TWO characters\n"
    "backslash and n (\\n) - never a real newline. The same applies to \\t, \\r, \\\\ and\n"
    "to every other escape. The patch must not contain a raw control character at all.\n"
    "A straight apostrophe ' must stay a straight apostrophe - never a typographic quote.\n"
    "Never start a payload line with " + chr(64) + chr(39) + ", never use double-quoted here-strings with $,\n"
    "and keep all opening and closing braces balanced."
)

VERIFY_COMMAND_RULE = (
    "In TASK_BRIEF.md and in your NOTES block under VERIFICATION COMMAND, name the exact\n"
    "command that verifies (builds/lints/tests) this project when automatic detection is\n"
    "not sure."
)

ROOT_CAUSE_RULE = (
    "## FIND THE ROOT CAUSE FIRST\n\n"
    "If something in this project is broken, do not change anything until you can\n"
    "state the root cause and the EVIDENCE for it (the exact file and line, the\n"
    "exact DOM node you measured, the exact value you read).\n\n"
    "- Every symptom must be traced to a concrete cause in THIS code base.\n"
    "- If you cannot find the cause in the archive, say exactly what you need -\n"
    "  do not invent a fix and do not guess a signature, a path or a value.\n"
    "- Put the root cause and its evidence in the NOTES block of your patch, under\n"
    "  the heading `## ROOT CAUSE`."
)


def _ensure_no_guessing_rule(prompt_body: str) -> str:
    out = prompt_body.rstrip()
    if "FIND THE ROOT CAUSE FIRST" not in out:
        out = out + "\n\n" + ROOT_CAUSE_RULE
    if "API_INDEX.md lists every public declaration" not in out:
        out = out + "\n\n" + EXACT_API_RULE
    if "characters backslash and n" not in out:
        out = out + "\n\n" + BYTE_EXACT_RULE
    if "VERIFICATION COMMAND" not in out:
        out = out + "\n\n" + VERIFY_COMMAND_RULE
    if "DO NOT GUESS and DO NOT INVENT it" not in out:
        out = out + "\n\n" + NO_GUESSING_RULE
    return out + "\n"


DUAL_FOLLOWUP_HEADER = (
    "[FOLLOW-UP SESSION - CONTINUING WINNING CONVERSATION] I have uploaded a fresh snapshot "
    "of the project codebase as a zip archive (`argon_context.zip`) with `TASK_BRIEF.md` and `API_INDEX.md`."
)

DUAL_NEW_SESSION_HEADER = (
    "[NEW SESSION - FRESH CONVERSATION WITH ZERO PRIOR CHAT CONTEXT] I have uploaded the complete "
    "project codebase as a zip archive (`argon_context.zip`) containing `TASK_BRIEF.md` and `API_INDEX.md`."
)

DUAL_NEW_SESSION_NOTE = """### 🧠 NEW SESSION FOR THIS PROJECT (FRESH CONVERSATION - NO PRIOR CHAT HISTORY)
This is a brand-new conversation with NO earlier messages or context from previous rounds.
- The attached archive (`argon_context.zip`, `TASK_BRIEF.md`, and `API_INDEX.md`) is a complete, fresh snapshot
  of the project's current state and is the ONLY context you have.
- Read `TASK_BRIEF.md` and `API_INDEX.md` carefully before making any changes, preserve existing conventions
  and architecture, and never guess a missing file or symbol."""


def build_context_note(is_continuation: bool = False) -> str:
    """Return the continuity note that is injected into the prompt.

    A NEW session gets the "first task" note; a CONTINUATION gets the note that
    tells Argon to reuse the understanding already present in the conversation.
    """
    return CONTINUATION_NOTE if is_continuation else FIRST_TASK_NOTE


def build_surgical_prompt(
    task_description: str,
    target_directory: str = "",
    template_path: Path = PROMPT_TEMPLATE_PATH,
    is_continuation: bool = False,
    diagnostic_evidence: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the surgical prompt from templates/argon_task_prompt_template.md.

    The prompt carries the ABSOLUTE target directory, so the `apply_changes.ps1`
    Gemini delivers resolves every path against the real project and applies the
    whole change set in a single run. It also carries the session continuity
    note, so Argon knows whether it is the first task for this project or a
    follow-up in an existing conversation.
    """
    target = str(target_directory)
    note = build_context_note(is_continuation)
    if template_path.is_file():
        raw = template_path.read_text(encoding="utf-8")
        # Extract the copy-paste block only. The template also carries prose
        # before and after the fence; sending that to Argon would put the
        # template's own documentation into the prompt.
        m = re.search(r"```markdown[^\S\r\n]*\r?\n(.*?)\r?\n```", raw, flags=re.DOTALL)
        template_body = m.group(1) if m else raw
        body = template_body
        if "{{USER_TASK_OR_BUG_DESCRIPTION}}" in body:
            body = body.replace("{{USER_TASK_OR_BUG_DESCRIPTION}}", task_description.strip())
        if "{{TARGET_DIRECTORY}}" in body:
            body = body.replace("{{TARGET_DIRECTORY}}", target)
        if "{{CONTEXT_NOTE}}" in body:
            body = body.replace("{{CONTEXT_NOTE}}", note)
        rendered = _ensure_no_guessing_rule(body)
        if diagnostic_evidence:
            rendered = rendered.rstrip() + "\n\n" + _format_diagnostic_evidence_block(diagnostic_evidence) + "\n"
        return rendered
    h3 = "#" * 3
    rendered = _ensure_no_guessing_rule(
        "I have uploaded the relevant codebase context as a zip archive (`argon_context.zip`). "
        "Inside the archive, you will find `TASK_BRIEF.md` containing the project overview and file structure.\n\n"
        f"{h3} 🎯 Primary Objective:\n{task_description.strip()}\n\n"
        f"{note}\n\n"
        f"{h3} 🗂️ TARGET DIRECTORY (USE THESE EXACT PATHS)\nThe codebase lives at this absolute directory:\n\n"
        f"```\n{target}\n```\n\n"
        "Your `apply_changes.ps1` must start with `$TargetRoot = '<that path>'` and resolve every path "
        "against it with `Join-Path $TargetRoot '<relative path>'`. Never depend on the current working "
        "directory.\n\n"
        f"{h3} 📦 DELIVERY (STRICT)\nWrite the complete patch to a file named `apply_changes.ps1` and "
        "ATTACH it to your reply so it can be downloaded with one click. Do not paste the script body into "
        "the chat; keep the reply short: what changed, which files changed and why, the file reference, "
        "and how to verify. An inline code block is a fallback used only when file delivery is unavailable."
    )
    if diagnostic_evidence:
        rendered = rendered.rstrip() + "\n\n" + _format_diagnostic_evidence_block(diagnostic_evidence) + "\n"
    return rendered


def build_followup_prompt(
    task_description: str,
    target_directory: str = "",
    template_path: Path = PROMPT_TEMPLATE_PATH,
    diagnostic_evidence: Optional[Dict[str, Any]] = None,
    winning_chat_id: str = "",
    winning_slot: str = "",
) -> str:
    """Build the FOLLOW-UP prompt for a chat that already holds the earlier project work."""
    base = build_surgical_prompt(
        task_description=task_description,
        target_directory=target_directory,
        template_path=template_path,
        is_continuation=True,
        diagnostic_evidence=diagnostic_evidence,
    )
    lines = base.splitlines()
    if lines and lines[0].startswith("I have uploaded the relevant codebase context"):
        lines[0] = DUAL_FOLLOWUP_HEADER
    rebuilt = "\n".join(lines) + "\n"
    provenance_parts = []
    if winning_slot:
        provenance_parts.append(f"slot `{winning_slot}`")
    if winning_chat_id:
        provenance_parts.append(f"conversation `{winning_chat_id}`")
    if provenance_parts:
        prov_line = (
            "- Winning session provenance: this follow-up continues in "
            + ", ".join(provenance_parts)
            + " (the winning session from the previous round)."
        )
        rebuilt = rebuilt.replace(CONTINUATION_NOTE, CONTINUATION_NOTE + "\n" + prov_line, 1)
    return _ensure_no_guessing_rule(rebuilt)


def build_new_session_prompt(
    task_description: str,
    target_directory: str = "",
    template_path: Path = PROMPT_TEMPLATE_PATH,
    diagnostic_evidence: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the NEW-SESSION prompt for a fresh chat with no prior conversation history."""
    base = build_surgical_prompt(
        task_description=task_description,
        target_directory=target_directory,
        template_path=template_path,
        is_continuation=False,
        diagnostic_evidence=diagnostic_evidence,
    )
    lines = base.splitlines()
    if lines and lines[0].startswith("I have uploaded the relevant codebase context"):
        lines[0] = DUAL_NEW_SESSION_HEADER
    rebuilt = "\n".join(lines) + "\n"
    rebuilt = rebuilt.replace(FIRST_TASK_NOTE, DUAL_NEW_SESSION_NOTE, 1)
    return _ensure_no_guessing_rule(rebuilt)


def build_dual_prompts(
    task_description: str,
    target_directory: str = "",
    template_path: Path = PROMPT_TEMPLATE_PATH,
    diagnostic_evidence: Optional[Dict[str, Any]] = None,
    winning_chat_id: str = "",
    winning_slot: str = "",
) -> tuple[str, str]:
    """Build both (followup_prompt, new_session_prompt) for dual-session dispatch."""
    return (
        build_followup_prompt(
            task_description=task_description,
            target_directory=target_directory,
            template_path=template_path,
            diagnostic_evidence=diagnostic_evidence,
            winning_chat_id=winning_chat_id,
            winning_slot=winning_slot,
        ),
        build_new_session_prompt(
            task_description=task_description,
            target_directory=target_directory,
            template_path=template_path,
            diagnostic_evidence=diagnostic_evidence,
        ),
    )


def find_delivered_patch(search_dirs, target_name: str = "apply_changes",
                         newer_than: Optional[float] = None) -> Optional[Path]:
    """Find the newest delivered `apply_changes*.ps1` inside the given folders.

    `newer_than` filters by modification time so a file that was already lying
    around cannot be mistaken for a fresh delivery.
    """
    best: Optional[Path] = None
    for d in search_dirs:
        try:
            d = Path(d)
            if not d.is_dir():
                continue
            for p in d.glob(f"{target_name}*.ps1"):
                if not p.is_file():
                    continue
                if newer_than is not None:
                    try:
                        if p.stat().st_mtime < newer_than:
                            continue
                    except OSError:
                        continue
                if best is None or p.stat().st_mtime > best.stat().st_mtime:
                    best = p
        except Exception:
            continue
    return best


def _copy_with_retry(src: Path, dst: Path, attempts: int = 12, delay: float = 0.5) -> None:
    """Copy a file, retrying while the browser still holds a handle on it."""
    last = None
    for _ in range(attempts):
        try:
            if src.resolve() == dst.resolve():
                return
            shutil.copy2(str(src), str(dst))
            return
        except Exception as exc:
            last = exc
            time.sleep(delay)
    if last is not None:
        raise last


TYPOGRAPHIC_QUOTES = (chr(0x2018), chr(0x2019), chr(0x201C), chr(0x201D))
PROSE_ONLY_EXTENSIONS = {".md", ".txt", ".rst"}


def _extract_patch_payload_blocks(patch_text: str) -> List[Dict[str, Any]]:
    """Extract every here-string payload block (@'...'@ and @"..."@) with its target file and label."""
    blocks: List[Dict[str, Any]] = []
    if not patch_text:
        return blocks

    open_sq = chr(64) + chr(39)
    close_sq = chr(39) + chr(64)
    open_dq = chr(64) + chr(34)
    close_dq = chr(34) + chr(64)

    file_assign_re = re.compile(
        r"(?:\$(?:relPath|rel|relativePath|file|targetRel)\s*=\s*['\"]([^'\"]+)['\"]|"
        r"Join-Path\s+\$TargetRoot\s+['\"]([^'\"]+)['\"]|"
        r"(?:-RelPath|File\s*=)\s*['\"]([^'\"]+)['\"])",
        re.IGNORECASE,
    )
    label_assign_re = re.compile(
        r"(?:\$([A-Za-z_]\w*)\s*=\s*$|(?:-Label|Label\s*=)\s*['\"]([^'\"]+)['\"])",
        re.IGNORECASE,
    )
    hs_re = re.compile(
        r"(" + re.escape(open_sq) + r"|" + re.escape(open_dq) + r")[ \t]*\r?\n(.*?)\r?\n(" +
        re.escape(close_sq) + r"|" + re.escape(close_dq) + r")",
        re.DOTALL,
    )

    block_idx = 0
    for m in hs_re.finditer(patch_text):
        block_idx += 1
        opener = m.group(1)
        body = m.group(2)
        quote_kind = "single" if opener == open_sq else "double"
        prefix = patch_text[: m.start()]

        current_file = "<unknown>"
        for fm in file_assign_re.finditer(prefix):
            cand = fm.group(1) or fm.group(2) or fm.group(3) or ""
            if cand and ".argon_backup" not in cand:
                current_file = cand.replace("\\", "/")

        prefix_tail = prefix[-400:]
        current_label = f"block #{block_idx}"
        var_matches = list(re.finditer(r"\$([A-Za-z_]\w*)\s*=\s*$", prefix_tail))
        if var_matches:
            current_label = "$" + var_matches[-1].group(1)
        else:
            lbl_matches = list(label_assign_re.finditer(prefix_tail))
            if lbl_matches:
                lm = lbl_matches[-1]
                current_label = ("$" + lm.group(1)) if lm.group(1) else (lm.group(2) or current_label)

        blocks.append(
            {
                "index": block_idx,
                "file": current_file,
                "label": current_label,
                "quote_kind": quote_kind,
                "body": body,
                "script_offset": m.start(2),
            }
        )
    return blocks


def validate_patch_payloads(patch_text: str, project_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Validate every payload block in a delivered patch script BEFORE writing any file.

    Refuses when:
      1. A block contains a raw control character (< 0x20 other than CR 0x0D or LF 0x0A).
      2. A line of the block starts with @' (or '@), which would terminate or corrupt the here-string.
      3. A block sits inside a double-quoted here-string (@"..."@) and contains '$'.
      4. A block contains a typographic quote (U+2018, U+2019, U+201C, U+201D) where the target
         language requires a straight quote.
    """
    defects: List[Dict[str, Any]] = []
    if not patch_text:
        return defects

    open_sq = chr(64) + chr(39)
    close_sq = chr(39) + chr(64)
    open_dq = chr(64) + chr(34)
    close_dq = chr(34) + chr(64)

    if patch_text.count(open_sq) != patch_text.count(close_sq):
        defects.append(
            {
                "file": "<script>",
                "label": "here-string",
                "offset": 0,
                "reason": "unbalanced single-quoted here-string delimiters (@' vs '@)",
            }
        )
    if patch_text.count(open_dq) != patch_text.count(close_dq):
        defects.append(
            {
                "file": "<script>",
                "label": "here-string",
                "offset": 0,
                "reason": 'unbalanced double-quoted here-string delimiters (@" vs "@)',
            }
        )

    blocks = _extract_patch_payload_blocks(patch_text)
    for blk in blocks:
        file_rel = str(blk["file"])
        label = str(blk["label"])
        quote_kind = str(blk["quote_kind"])
        body = str(blk["body"])

        # 1. Raw control characters (< 0x20 other than CR or LF)
        for idx, ch in enumerate(body):
            code = ord(ch)
            if code < 0x20 and ch not in ("\r", "\n"):
                defects.append(
                    {
                        "file": file_rel,
                        "label": label,
                        "offset": idx,
                        "reason": f"raw control character 0x{code:02X} at offset {idx}",
                    }
                )
                break

        # 2. A line of the block starts with @' (or '@)
        pos = 0
        for raw_ln in body.splitlines(keepends=True):
            stripped_ln = raw_ln.rstrip("\r\n")
            if stripped_ln.startswith(open_sq) or stripped_ln.startswith(close_sq):
                defects.append(
                    {
                        "file": file_rel,
                        "label": label,
                        "offset": pos,
                        "reason": f"line starts with {open_sq} ({stripped_ln[:2]}) at offset {pos} (would end or corrupt here-string early)",
                    }
                )
                break
            pos += len(raw_ln)

        # 3. Block sits inside a double-quoted here-string (@"..."@) and contains '$'
        if quote_kind == "double" and "$" in body:
            dollar_idx = body.find("$")
            defects.append(
                {
                    "file": file_rel,
                    "label": label,
                    "offset": dollar_idx,
                    "reason": f"double-quoted here-string contains '$' at offset {dollar_idx} (PowerShell would interpolate and corrupt code)",
                }
            )

        # 4. Typographic quote where target language needs a straight quote
        ext = Path(file_rel).suffix.lower() if file_rel != "<unknown>" else ""
        needs_straight_quote = ext not in PROSE_ONLY_EXTENSIONS
        existing_file_text = ""
        if project_path is not None and file_rel != "<unknown>":
            cand_path = Path(project_path) / file_rel
            if cand_path.is_file():
                try:
                    existing_file_text = cand_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    existing_file_text = ""
        for idx, ch in enumerate(body):
            if ch in TYPOGRAPHIC_QUOTES:
                if needs_straight_quote or (existing_file_text and ch not in existing_file_text):
                    defects.append(
                        {
                            "file": file_rel,
                            "label": label,
                            "offset": idx,
                            "reason": (
                                f"typographic quote U+{ord(ch):04X} at offset {idx} "
                                "where target language requires a straight quote"
                            ),
                        }
                    )
                    break

    return defects


def refuse_invalid_patch(
    patch_text: str,
    project_path: Optional[Path] = None,
    patch_name: str = "apply_changes.ps1",
) -> bool:
    """Validate patch payloads before any file is touched. Print refusals and return False on any defect."""
    defects = validate_patch_payloads(patch_text, project_path=project_path)
    if not defects:
        return True
    print(
        f"[ERROR] Refusing to apply {patch_name} - {len(defects)} payload defect(s) detected before writing any file:",
        file=sys.stderr,
    )
    for d in defects:
        print(
            f"  [REFUSED] file={d['file']}  label={d['label']}  offset={d['offset']}  reason={d['reason']}",
            file=sys.stderr,
        )
    return False


def _snapshot_project_files(project_path: Path) -> Dict[str, bytes]:
    """Snapshot raw bytes of eligible project files before applying a patch."""
    snap: Dict[str, bytes] = {}
    project_path = Path(project_path).resolve()
    skip_dirs = {".argon_backup", "downloads", "__pycache__", "node_modules", ".git", "venv", ".venv", "target", "dist"}
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        for fn in files:
            if fn.lower().startswith("apply_changes") and ".ps1" in fn.lower():
                continue
            p = Path(root) / fn
            try:
                if not p.is_file() or p.stat().st_size > 15 * 1024 * 1024:
                    continue
                rel = p.relative_to(project_path).as_posix()
                snap[rel] = p.read_bytes()
            except Exception:
                continue
    return snap


def verify_written_files_bytes(project_path: Path, before_files: Dict[str, bytes]) -> List[Dict[str, Any]]:
    """Compare written files with their pre-patch bytes and report any newly introduced raw control character."""
    issues: List[Dict[str, Any]] = []
    after_files = _snapshot_project_files(project_path)
    for rel, new_bytes in sorted(after_files.items()):
        old_bytes = before_files.get(rel, b"")
        if new_bytes == old_bytes:
            continue
        old_ctrl_counts: Dict[int, int] = {}
        for b in old_bytes:
            if b < 0x20 and b not in (0x0D, 0x0A):
                old_ctrl_counts[b] = old_ctrl_counts.get(b, 0) + 1
        seen_ctrl_counts: Dict[int, int] = {}
        for idx, b in enumerate(new_bytes):
            if b < 0x20 and b not in (0x0D, 0x0A):
                seen_ctrl_counts[b] = seen_ctrl_counts.get(b, 0) + 1
                if seen_ctrl_counts[b] > old_ctrl_counts.get(b, 0):
                    msg = f"raw control byte 0x{b:02X} at byte offset {idx} was not in the old file"
                    print(f"[ERROR] Written file byte defect in {rel}: {msg}", file=sys.stderr)
                    issues.append({"file": rel, "offset": idx, "byte": b, "reason": msg})
    return issues


def _structure_balance(text: str) -> Dict[str, int]:
    """Brace / paren / bracket balance of a source file.

    Strings and comments are stripped first, so only real code counts. A file
    that is balanced before a patch and unbalanced after it lost a brace - the
    single most common way a delivered patch breaks the build.
    """
    out = text or ""
    out = re.sub(r"/\*.*?\*/", "", out, flags=re.DOTALL)
    out = re.sub(r"//[^\n]*", "", out)
    out = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', out)
    out = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", out)
    out = re.sub(r"`(?:\\.|[^`\\])*`", "``", out)
    return {
        "braces": out.count("{") - out.count("}"),
        "parens": out.count("(") - out.count(")"),
        "brackets": out.count("[") - out.count("]"),
    }


def verify_written_files_structure(
    project_path: Path, before_files: Dict[str, bytes]
) -> List[Dict[str, Any]]:
    """Report files that were balanced before the patch and are unbalanced after.

    That means the patch dropped or added a brace - the exact defect that made
    a delivered Rust patch fail to compile. It is reported here so the agent
    can fix it locally; it costs no quota.
    """
    issues: List[Dict[str, Any]] = []
    after_files = _snapshot_project_files(project_path)
    for rel, new_bytes in sorted(after_files.items()):
        old_bytes = before_files.get(rel)
        if old_bytes is None or new_bytes == old_bytes:
            continue
        try:
            old_bal = _structure_balance(old_bytes.decode("utf-8", errors="replace"))
            new_bal = _structure_balance(new_bytes.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if all(v == 0 for v in old_bal.values()) and any(v != 0 for v in new_bal.values()):
            broken = ", ".join(f"{k}={v}" for k, v in new_bal.items() if v != 0)
            issues.append(
                {
                    "file": rel,
                    "label": rel,
                    "reason": (
                        "the file was balanced before the patch and is unbalanced "
                        f"after it ({broken}) - a brace/paren/bracket was lost"
                    ),
                }
            )
    return issues


def apply_delivered_patch(project_path: Path, patch_path: Path) -> bool:
    """Apply a delivered apply_changes.ps1 against the project root.

    Validates every payload block BEFORE touching any file, then runs
    Run-ArgonPatch.ps1 and verifies written files for raw control bytes.
    """
    project_path = project_path.resolve()
    patch_path = Path(patch_path)
    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        print(f"[!] Could not read delivered patch {patch_path}: {exc}", file=sys.stderr)
        return False

    if not refuse_invalid_patch(patch_text, project_path=project_path, patch_name=patch_path.name):
        return False

    before_files = _snapshot_project_files(project_path)
    runner = SCRIPTS_DIR / "Run-ArgonPatch.ps1"
    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"

    if runner.is_file():
        cmd = [
            shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(runner),
            "-ProjectPath", str(project_path),
            "-PatchFile", str(patch_path),
            "-SkipVerify",
        ]
    else:
        staged = project_path / "apply_changes.ps1"
        try:
            if staged.exists():
                stamp = time.strftime("%Y%m%d_%H%M%S")
                shutil.copy2(str(staged), str(project_path / f"apply_changes.ps1.bak-{stamp}"))
            _copy_with_retry(patch_path, staged)
        except Exception as exc:
            print(f"[!] Could not stage the delivered patch: {exc}")
            return False
        cmd = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(staged)]

    print(f"[+] Applying delivered patch: {patch_path}")
    try:
        res = subprocess.run(cmd, cwd=str(project_path), timeout=300)
        if res.returncode != 0:
            return False
    except subprocess.TimeoutExpired:
        print(f"[!] Applying patch timed out after 300 s: {patch_path}", file=sys.stderr)
        return False

    byte_issues = verify_written_files_bytes(project_path, before_files)
    if byte_issues:
        for issue in byte_issues:
            print(
                f"[!] {issue.get('file', '?')}: {issue.get('reason', 'raw control character introduced')}",
                file=sys.stderr,
            )
        return False

    structure_issues = verify_written_files_structure(project_path, before_files)
    if structure_issues:
        print(
            "[!] The patch broke the structure of a file - a brace, parenthesis or "
            "bracket was lost. Nothing was rolled back; fix it locally (it costs no quota):",
            file=sys.stderr,
        )
        for issue in structure_issues:
            print(f"    [BROKEN] {issue['file']}: {issue['reason']}", file=sys.stderr)
        return False
    return True


def _chip_matches_patch_name(chip) -> bool:
    name = ""
    try:
        name = chip.locator(".file-name-lr").first.inner_text(timeout=500) or ""
        if not name:
            name = chip.get_attribute("title") or ""
        if not name:
            name = chip.inner_text(timeout=500) or ""
    except Exception:
        name = ""
    low = name.lower()
    return "apply_changes" in low or low.endswith(".ps1")


def _element_is_after_last_user_turn(el) -> bool:
    """Return True when no user message appears after `el` in document order (D1.3).

    In a follow-up conversation that already holds an earlier delivery, an
    earlier round's assistant turn (and its chip) precedes the newest user
    message in the DOM. Checking document position guarantees that an earlier
    round's chip is never mistaken for the newest assistant turn's delivery.
    When evaluation fails, returns False so an older turn's chip is never opened.
    """
    try:
        return bool(
            el.evaluate(
                """node => {
                    const selectors = [
                        'user-query',
                        '[data-test-id="user-query"]',
                        '.user-query-text',
                        '.query-text',
                        'message-turn.user-query',
                        'div.user-query'
                    ];
                    const users = document.querySelectorAll(selectors.join(','));
                    if (!users || !users.length) return true;
                    const lastUser = users[users.length - 1];
                    if (lastUser === node || lastUser.contains(node)) return false;
                    const pos = node.compareDocumentPosition(lastUser);
                    if (pos & Node.DOCUMENT_POSITION_FOLLOWING) {
                        return false;
                    }
                    return true;
                }"""
            )
        )
    except Exception:
        return False


def _generated_file_chip_indices_after_user(page) -> List[int]:
    """Return indices of visible apply_changes chips that sit after the last user message."""
    try:
        chips = page.locator("generated-file")
        found: List[int] = []
        for i in range(min(chips.count(), 40)):
            chip = chips.nth(i)
            if not _safe_visible(chip):
                continue
            if not _element_is_after_last_user_turn(chip):
                continue
            if _chip_matches_patch_name(chip):
                found.append(i)
        return found
    except Exception:
        return []


def _generated_file_chip_indices(page) -> List[int]:
    """Return the indices of every visible apply_changes delivery chip."""
    try:
        chips = page.locator("generated-file")
        found: List[int] = []
        for i in range(min(chips.count(), 40)):
            chip = chips.nth(i)
            if not _safe_visible(chip):
                continue
            if _chip_matches_patch_name(chip):
                found.append(i)
        return found
    except Exception:
        return []


def _generated_file_chip(page):
    """Argon's generated-file chip for apply_changes in the NEWEST assistant turn (D1.3).

    In a conversation that already holds an earlier delivery, ONLY a chip belonging
    to an assistant turn AFTER the last user message may be opened. When the newest
    turn has no chip yet, returns None and waits - never falls back to an older chip.
    """
    turns = _assistant_turns(page)
    if turns:
        newest_turn = turns[-1]
        if not _element_is_after_last_user_turn(newest_turn):
            return None
        try:
            chips = newest_turn.locator("generated-file")
            matched_in_turn = []
            for i in range(min(chips.count(), 10)):
                chip = chips.nth(i)
                if not _safe_visible(chip):
                    continue
                if not _element_is_after_last_user_turn(chip):
                    continue
                if _chip_matches_patch_name(chip):
                    matched_in_turn.append(chip)
            if matched_in_turn:
                return matched_in_turn[-1]
        except Exception:
            pass
        return None

    try:
        chips = page.locator("generated-file")
        matched = []
        for i in range(min(chips.count(), 40)):
            chip = chips.nth(i)
            if not _safe_visible(chip):
                continue
            if not _element_is_after_last_user_turn(chip):
                continue
            if _chip_matches_patch_name(chip):
                matched.append(chip)
        if matched:
            return matched[-1]
    except Exception:
        pass
    return None


def _slot_number(slot: Optional[str]) -> int:
    """The pool number of a slot ('/u/3/' -> 3)."""
    try:
        m = re.search(r"/u/(\d+)", str(slot or ""))
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


def _chat_title(page) -> str:
    """Return the title of the conversation the page is ACTUALLY in (D21).

    Never reads a generic selected sidebar entry: if the current URL has no
    conversation id (a new chat that has not been created yet), returns '' so
    diagnostics report that no conversation exists yet instead of naming a
    foreign sidebar conversation.
    """
    current_url = ""
    try:
        current_url = str(getattr(page, "url", "") or "")
    except Exception:
        current_url = ""
    cid = ""
    try:
        m_cid = re.search(r"/app/([0-9a-f]{8,})|/prompts/(?!new_chat\b)([A-Za-z0-9_-]{8,})", current_url)
        if m_cid:
            cid = m_cid.group(1) or m_cid.group(2) or ""
    except Exception:
        cid = ""
    if not cid:
        return ""
    try:
        title = (page.title() or "").strip()
        for suffix in (" - Google Gemini", " - Gemini", " | Google Gemini"):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
                break
        if title and title.lower() not in ("gemini", "google gemini"):
            return title
    except Exception:
        pass
    # Only inspect a link whose href explicitly contains THIS conversation's cid
    try:
        loc = page.locator(f"a[href*='{cid}']")
        for i in range(min(loc.count(), 4)):
            el = loc.nth(i)
            if not _safe_visible(el):
                continue
            t = (el.inner_text(timeout=1000) or "").strip()
            if t:
                return t.splitlines()[0].strip()
    except Exception:
        pass
    return ""


def _assistant_turns(page):
    """The assistant turns of the conversation, or None when the DOM hides them."""
    for sel in (
        "model-response",
        "[data-test-id='model-response']",
        ".model-response-text",
        "message-turn.model-response",
        "div.model-response",
    ):
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:
            continue
        if n:
            try:
                return [loc.nth(i) for i in range(n)]
            except Exception:
                continue
    return None


def _new_delivery_chip(page, chips_before: int, responses_before: int):
    """The chip of the delivery this run is waiting for - never an older one.

    In a conversation that already holds an earlier delivery, only the chip of
    the newest assistant turn may be opened. An earlier round's chip must never
    be clicked.
    """
    turns = _assistant_turns(page)
    if turns is not None:
        if len(turns) == 0 or len(turns) <= responses_before:
            return None
        newest_turn = turns[-1]
        if not _element_is_after_last_user_turn(newest_turn):
            return None
        try:
            chips = newest_turn.locator("generated-file")
            matched = []
            for j in range(min(chips.count(), 10)):
                cand = chips.nth(j)
                if not _safe_visible(cand):
                    continue
                if _chip_matches_patch_name(cand):
                    matched.append(cand)
            if matched:
                return matched[-1]
        except Exception:
            pass
        return None

    try:
        all_chips = page.locator("generated-file")
        n = all_chips.count()
        if n <= chips_before:
            return None
        for i in range(n - 1, max(chips_before, 0) - 1, -1):
            chip = all_chips.nth(i)
            if not _safe_visible(chip):
                continue
            if not _element_is_after_last_user_turn(chip):
                continue
            if _chip_matches_patch_name(chip):
                return chip
    except Exception:
        pass
    return None


def _open_generated_file(page, chip_index: Optional[int] = None, chip=None) -> bool:
    """Click the chip so Gemini opens its Drive preview.

    The chip renders a <gem-button class="open-button"> wrapper around a real
    <button>; the click has to land on the inner <button> for the handler to
    fire, so that selector is tried first.

    `chip_index` selects one specific delivery chip (the index into the
    generated-file elements) so a run can capture exactly the delivery it is
    waiting for instead of "the last one that happens to be there". `chip`
    selects it directly, which is even safer.
    """
    if chip is None and chip_index is not None:
        try:
            cand = page.locator("generated-file").nth(chip_index)
            if cand.count() > 0:
                chip = cand
        except Exception:
            chip = None
    if chip is None:
        chip = _generated_file_chip(page)
    if chip is None:
        print(
            "[!] No delivery chip found - the chat does not (yet) show a "
            "generated file for this task.",
            file=sys.stderr,
        )
        return False
    for sel in ("button", ".open-button", "gem-button"):
        try:
            btn = chip.locator(sel).first
            if btn.count() > 0:
                btn.click(timeout=6000)
                page.wait_for_timeout(3000)
                return True
        except Exception:
            continue
    try:
        chip.click(timeout=6000)
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def _strip_viewer_tail_artefacts(text: str) -> str:
    """Drop trailing blank lines and Drive viewer filename captions (such as a
    stray trailing 'apply_changes.ps1' line after '# --- END NOTES ---') while
    keeping the rest of the script byte-exact.
    """
    if not text:
        return text
    lines = text.splitlines()
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if re.match(
            r"^(?:apply_changes(?:\s*\(\d+\))?(?:\.ps1)?|[\w.\- ()]+\.ps1|ps1|txt)$",
            last,
            re.IGNORECASE,
        ):
            lines.pop()
            continue
        break
    return "\n".join(lines)


def _read_previewed_file(page) -> Optional[str]:
    """Read the file content out of Gemini's Drive viewer preview."""
    for sel in (
        ".drive-viewer-carousel",
        ".drive-viewer",
        ".drive-viewer-default",
        "[role='dialog'] pre",
        "[role='dialog'] code",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not _safe_visible(loc):
                continue
            text = loc.inner_text(timeout=15000)
            if not text:
                continue
            if "$TargetRoot" in text:
                return _strip_viewer_tail_artefacts(text)
            first_line = text.splitlines()[0][:120] if text.splitlines() else ""
            print(f"[!] Preview ({sel}) has no $TargetRoot yet - first line: {first_line!r}")
        except Exception as exc:
            print(f"[!] Could not read the preview ({sel}): {type(exc).__name__}")
    return None


# Print control marker: any candidate whose label, tooltip, title, or icon
# matches RE_PRINT is strictly skipped so the print preview is never opened.
# Note that 'save as pdf' is NOT treated as print unless 'print' is present.
RE_PRINT = re.compile(
    r"(print\s*preview|printout|(?<!blue)print|প্রিন্ট|মুদ্রণ)",
    re.IGNORECASE,
)

# Explicit download button selectors only. The blind '.drive-viewer-custom-button'
# selector is intentionally excluded because it matches the Print toolbar button
# that sits immediately to the left of the Download button.
# D5.2: ONLY the Drive viewer's own toolbar button may ever be clicked.
# Loose page-wide button[aria-label*='download'] selectors are forbidden because a
# chat bubble or conversation control containing the word "download" must never be clicked.
DOWNLOAD_BUTTON_SELECTORS = (
    "div.drive-viewer-toolstrip-custom-panel .drive-viewer-custom-button[aria-label*='ডাউনলোড']",
    "div.drive-viewer-toolstrip-custom-panel .drive-viewer-custom-button[aria-label*='download' i]",
    "div.drive-viewer-toolstrip-custom-panel .drive-viewer-custom-button[data-tooltip*='ডাউনলোড']",
    "div.drive-viewer-toolstrip-custom-panel .drive-viewer-custom-button[data-tooltip*='download' i]",
    "div.drive-viewer-toolstrip-custom-panel .drive-viewer-custom-button",
    ".drive-viewer-toolstrip .drive-viewer-custom-button[aria-label*='ডাউনলোড']",
    ".drive-viewer-toolstrip .drive-viewer-custom-button[aria-label*='download' i]",
    ".drive-viewer-toolstrip button[aria-label*='ডাউনলোড']",
    ".drive-viewer-toolstrip button[aria-label*='download' i]",
    ".drive-viewer [role='button'][aria-label*='ডাউনলোড']",
    ".drive-viewer [role='button'][aria-label*='download' i]",
)


def _candidate_label_and_icon(el) -> tuple[str, str]:
    """Extract the readable label/tooltip/title and icon identifier of `el`."""
    label_parts: List[str] = []
    icon_parts: List[str] = []
    for attr in ("aria-label", "data-tooltip", "title", "mattooltip"):
        try:
            val = str(el.get_attribute(attr) or "").strip()
            if val:
                label_parts.append(val)
        except Exception:
            continue
    try:
        vis_text = (el.inner_text(timeout=400) or "").strip()
        if vis_text:
            label_parts.append(vis_text)
    except Exception:
        pass
    try:
        meta = el.evaluate(
            r"""node => {
                const labels = [];
                const icons = [];
                for (const a of ['aria-label', 'data-tooltip', 'title', 'mattooltip']) {
                    const v = (node.getAttribute && node.getAttribute(a)) || '';
                    if (v) labels.push(v);
                }
                const descId = (node.getAttribute && node.getAttribute('aria-describedby')) || '';
                if (descId) {
                    for (const id of descId.split(/\s+/)) {
                        const tip = id ? document.getElementById(id) : null;
                        if (tip && tip.innerText) labels.push(tip.innerText.trim());
                    }
                }
                const iconNodes = node.querySelectorAll
                    ? node.querySelectorAll('mat-icon, [fonticon], [data-mat-icon-name], i, svg')
                    : [];
                iconNodes.forEach(ic => {
                    const fi = (ic.getAttribute && (ic.getAttribute('fonticon') || ic.getAttribute('data-mat-icon-name'))) || '';
                    if (fi) icons.push(fi);
                    const it = (ic.innerText || ic.textContent || '').trim();
                    if (it) icons.push(it);
                });
                return {
                    label: labels.join(' ').trim(),
                    icon: icons.join(' ').trim()
                };
            }"""
        )
        if isinstance(meta, dict):
            if meta.get("label"):
                label_parts.append(str(meta["label"]))
            if meta.get("icon"):
                icon_parts.append(str(meta["icon"]))
    except Exception:
        pass
    return " ".join(label_parts).strip(), " ".join(icon_parts).strip()


def _is_download_and_not_print(el) -> bool:
    """Return True only when `el` clearly names a download and does NOT mention print.

    Rule 1.1 & 1.3:
      1. Any candidate whose label, tooltip, title, or icon mentions printing
         (print, printout, প্রিন্ট, print preview) is rejected.
      2. 'save as pdf' is NOT treated as print unless 'print' is also present.
      3. A candidate with no readable download label or download icon is skipped.
    """
    label_text, icon_text = _candidate_label_and_icon(el)
    combined = f"{label_text} {icon_text}".strip()
    if not combined:
        return False
    # A real toolbar button has a SHORT label ("ডাউনলোড করুন"). A conversation
    # bubble carries a whole message in its label, and that message can
    # contain the word "download" - clicking that bubble is never the button.
    if len(label_text.strip()) > 60:
        return False
    if RE_PRINT.search(combined):
        return False
    # D5.2: Reject any element inside a conversation bubble/message or outside the Drive viewer
    try:
        in_chat_or_outside_viewer = bool(
            el.evaluate(
                """node => {
                    let cur = node;
                    let insideViewer = false;
                    while (cur) {
                        const tag = (cur.tagName || '').toLowerCase();
                        const cls = (cur.getAttribute && (cur.getAttribute('class') || '')) || '';
                        const tid = (cur.getAttribute && (cur.getAttribute('data-test-id') || '')) || '';
                        if (tag === 'user-query' || tag === 'model-response' || tag === 'message-turn' ||
                            tid === 'user-query' || tid === 'model-response' ||
                            /\b(user-query|model-response)\b/i.test(cls)) {
                            return true;
                        }
                        if (/drive-viewer/i.test(cls) || /drive-viewer/i.test(tag)) {
                            insideViewer = true;
                        }
                        cur = cur.parentElement;
                    }
                    return !insideViewer;
                }"""
            )
        )
        if in_chat_or_outside_viewer:
            return False
    except Exception:
        pass
    has_download_label = bool(RE_DOWNLOAD.search(label_text))
    has_download_icon = bool(
        re.search(r"\b(download|file_download)\b", icon_text, re.IGNORECASE)
    ) or bool(RE_DOWNLOAD.search(icon_text))
    return has_download_label or has_download_icon


def _find_viewer_download_button(page):
    """Return the single verified download button in the viewer's own toolbar (D5.2), or None."""
    for sel in DOWNLOAD_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                if _is_download_and_not_print(el):
                    return el
        except Exception:
            continue
    return None


def _viewer_is_open(page) -> bool:
    """Return True when Gemini's file preview / Drive viewer is open on screen."""
    if _find_viewer_download_button(page) is not None:
        return True
    for sel in (
        "div.drive-viewer-toolstrip-custom-panel",
        ".drive-viewer-carousel",
        ".drive-viewer",
        ".drive-viewer-default",
        "[role='dialog'] pre",
        "[role='dialog'] code",
    ):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                if _safe_visible(loc.nth(i)):
                    return True
        except Exception:
            continue
    return False


MIN_PATCH_FILE_BYTES = 64


def _validate_and_save_download(dl, dest_dir: Path) -> tuple[Optional[Path], str]:
    """Verify that a Playwright download is really a non-empty .ps1 patch (D5, D18.1).

    Saves to a temporary staging file first so a 0-byte or tiny/corrupt download
    is rejected immediately and NEVER overwrites an existing good file in dest_dir.
    """
    dest_dir = Path(dest_dir)
    dl_url = ""
    try:
        dl_url = str(getattr(dl, "url", "") or "")
    except Exception:
        dl_url = ""
    low_url = dl_url.lower()
    if "contribservice" in low_url or "print" in low_url:
        print(
            f"[!] Discarding print-preview download from URL: {dl_url}",
            file=sys.stderr,
        )
        try:
            dl.delete()
        except Exception:
            pass
        return None, "browser handed over a print preview URL (contribservice/print)"

    suggested = ""
    try:
        suggested = str(getattr(dl, "suggested_filename", "") or "").strip()
    except Exception:
        suggested = ""
    if not suggested or not suggested.lower().endswith(".ps1"):
        print(
            f"[!] Discarding non-.ps1 download ({suggested!r}) - refusing to rename it to apply_changes.ps1.",
            file=sys.stderr,
        )
        try:
            dl.delete()
        except Exception:
            pass
        return None, f"browser handed over non-.ps1 file ({suggested or 'unnamed'})"

    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(suggested).name
    dest = dest_dir / safe_name
    staging = dest_dir / f".staging_{os.getpid()}_{int(time.time() * 1000)}_{safe_name}"
    dl.save_as(str(staging))

    try:
        file_size = staging.stat().st_size if staging.is_file() else 0
    except OSError:
        file_size = 0

    if file_size < MIN_PATCH_FILE_BYTES:
        print(
            f"[!] Rejecting {file_size}-byte download for {safe_name} (minimum {MIN_PATCH_FILE_BYTES} bytes) - "
            f"existing file {dest} was NOT overwritten.",
            file=sys.stderr,
        )
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return None, f"browser handed over an empty or undersized file ({file_size} bytes)"

    try:
        head_bytes = staging.read_bytes()[:8]
        if head_bytes.startswith(b"%PDF"):
            print(
                f"[!] Discarding PDF file masquerading as .ps1: {staging}",
                file=sys.stderr,
            )
            staging.unlink(missing_ok=True)
            return None, "browser handed over PDF bytes instead of a PowerShell script"
    except Exception:
        pass

    os.replace(str(staging), str(dest))
    return dest, ""


def _download_from_viewer(page, dest_dir: Path, timeout_ms: int = 12000) -> Optional[Path]:
    """Find the viewer's verified toolbar download button, click ONLY that button, and validate the file."""
    dest_dir = Path(dest_dir)
    btn = _find_viewer_download_button(page)
    if btn is None:
        return None
    try:
        with page.expect_download(timeout=timeout_ms) as dl:
            btn.click(timeout=6000)
        saved, _ = _validate_and_save_download(dl.value, dest_dir)
        return saved
    except Exception:
        return None


def _close_preview(page) -> None:
    """Close the Drive viewer preview without ever cancelling an active Argon response (D10.1)."""
    try:
        if not _viewer_is_open(page):
            return
        for close_sel in (
            ".drive-viewer [aria-label*='Close' i]",
            ".drive-viewer [aria-label*='বন্ধ']",
            "div.drive-viewer-toolstrip [aria-label*='Close' i]",
            "button[aria-label='Close preview' i]",
        ):
            try:
                loc = page.locator(close_sel)
                if loc.count() > 0 and _safe_visible(loc.first):
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(400)
                    return
            except Exception:
                continue
        if _viewer_is_open(page) and not _is_argon_working(page):
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
    except Exception:
        pass


def _hash_text(text: str) -> str:
    """Stable short hash of a captured delivery."""
    try:
        return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    except Exception:
        return ""


def _sha256_full(text: str) -> str:
    """Full 64-char SHA-256 hex digest of normalized patch text."""
    try:
        norm = (text or "").replace("\r\n", "\n").strip()
        return hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return ""


class DeliveryLedger:
    """Per-project delivery ledger tracking every handled patch by SHA-256, chat_id, round_id, and timestamp (D1.2)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or DELIVERY_LEDGER_PATH)
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        try:
            if self.path.is_file():
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    self.data = parsed
        except Exception:
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.setdefault("version", DELIVERY_LEDGER_VERSION)
        if not isinstance(self.data.get("projects"), dict):
            self.data["projects"] = {}
        return self.data

    def save(self) -> None:
        lock = _acquire_session_store_lock(self.path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(self.path))
        except Exception:
            pass
        finally:
            _release_session_store_lock(lock)

    def _proj_entries(self, project_path: Any) -> List[Dict[str, Any]]:
        self.load()
        projects = self.data.setdefault("projects", {})
        wanted = _normalize_project_key(project_path)
        for k, v in projects.items():
            if _normalize_project_key(k) == wanted and isinstance(v, list):
                return v
        canonical = str(Path(str(project_path)).expanduser().resolve())
        projects[canonical] = []
        return projects[canonical]

    def lookup_digest(self, project_path: Any, text: str) -> Optional[Dict[str, Any]]:
        """Return the ledger record if `text` was already handled for `project_path`, else None."""
        if not text or not text.strip():
            return None
        short_h = _hash_text(text)
        full_h = _sha256_full(text)
        short_norm = _hash_text(text.replace("\r\n", "\n").strip())
        for entry in self._proj_entries(project_path):
            if not isinstance(entry, dict):
                continue
            eh_short = str(entry.get("short_hash") or "")
            eh_full = str(entry.get("sha256") or "")
            if (short_h and eh_short in (short_h, short_norm)) or (full_h and eh_full == full_h):
                return entry
        return None

    def known_hashes(self, project_path: Any) -> set:
        """Return all known short and full digests for `project_path`."""
        out: set = set()
        for entry in self._proj_entries(project_path):
            if isinstance(entry, dict):
                if entry.get("short_hash"):
                    out.add(str(entry["short_hash"]))
                if entry.get("sha256"):
                    out.add(str(entry["sha256"]))
        return out

    def record(
        self,
        project_path: Any,
        text: str,
        chat_id: str = "",
        round_id: str = "",
        lane: str = "",
        file_path: str = "",
    ) -> Dict[str, Any]:
        """Record a handled delivery in the ledger so it is never downloaded again."""
        existing = self.lookup_digest(project_path, text)
        if existing is not None:
            return existing
        entries = self._proj_entries(project_path)
        rec = {
            "short_hash": _hash_text(text),
            "sha256": _sha256_full(text),
            "chat_id": str(chat_id or ""),
            "round_id": str(round_id or time.strftime("%Y%m%d_%H%M%S")),
            "lane": str(lane or ""),
            "file_path": str(file_path or ""),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        entries.append(rec)
        self.save()
        try:
            proj_mirror = Path(str(project_path)).expanduser().resolve() / ".argon_backup" / "delivery_ledger.json"
            proj_mirror.parent.mkdir(parents=True, exist_ok=True)
            proj_mirror.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass
        return rec


def _infer_project_root_from_dest(dest_dir: Path) -> Optional[Path]:
    """Infer the project root from a downloads or dual lane directory."""
    try:
        d = Path(dest_dir).resolve()
        for cand in (d, d.parent, d.parent.parent, d.parent.parent.parent, d.parent.parent.parent.parent):
            if cand.name.lower() == "downloads":
                return cand.parent
    except Exception:
        pass
    return None


def _is_leftover_delivery(
    text: str,
    previous_hashes: Optional[set],
    project_path: Optional[Path] = None,
) -> bool:
    """True when the captured delivery is one that was already handled (D1.1, D1.2).

    Consults both `previous_hashes` and the persistent `DeliveryLedger`. When a
    match is found, prints the round id, chat id, sha256, and timestamp it
    belongs to and refuses the old file.
    """
    if not text or not text.strip():
        return False
    digest = _hash_text(text)
    full_sha = _sha256_full(text)
    if project_path is not None:
        try:
            ledger_hit = DeliveryLedger().lookup_digest(project_path, text)
            if ledger_hit is not None:
                print(
                    f"[!] Refusing already-handled delivery from round {ledger_hit.get('round_id') or 'previous'} "
                    f"(chat={ledger_hit.get('chat_id') or '?'}, sha256={str(ledger_hit.get('sha256') or digest)[:16]}, "
                    f"captured_at={ledger_hit.get('timestamp') or '?'}). Waiting for the new delivery chip.",
                    file=sys.stderr,
                )
                return True
        except Exception:
            pass
    if not previous_hashes:
        return False
    if (digest and digest in previous_hashes) or (full_sha and full_sha in previous_hashes):
        print(
            f"[!] Refusing already-handled delivery (digest={digest} matches an earlier round in this project). "
            "Ignoring it and waiting for the new delivery.",
            file=sys.stderr,
        )
        return True
    return False


def _patch_is_complete(text: str) -> bool:
    """True when a delivered script is COMPLETE and structurally parseable (D18.2)."""
    if not text or not text.strip():
        return False
    if len(text.encode("utf-8", errors="replace")) < MIN_PATCH_FILE_BYTES:
        return False
    # The delivery contract ends the script with the success marker.
    if "[SUCCESS]" not in text and "All changes applied and verified successfully" not in text:
        return False
    # A NOTES block that was started must be closed.
    if "# --- NOTES" in text and "# --- END NOTES" not in text:
        return False
    # A truncated script very often ends in the middle of a here-string.
    open_sq = chr(64) + chr(39)
    close_sq = chr(39) + chr(64)
    open_dq = chr(64) + chr(34)
    close_dq = chr(34) + chr(64)
    if text.count(open_sq) != text.count(close_sq):
        return False
    if text.count(open_dq) != text.count(close_dq):
        return False
    # D18.2: Verify the patch parses cleanly (every here-string payload extracts and has balanced delimiters)
    payload_blocks = _extract_patch_payload_blocks(text)
    if (open_sq in text or open_dq in text) and not payload_blocks:
        return False
    if validate_patch_payloads(text):
        return False
    return True


def _dismiss_print_preview_if_any(page) -> None:
    """Close any accidental contribservice / print preview tab if one ever opened."""
    try:
        ctx = getattr(page, "context", None)
        if ctx is not None and hasattr(ctx, "pages") and len(ctx.pages) > 1:
            for other in list(ctx.pages):
                if other is page:
                    continue
                other_url = str(getattr(other, "url", "") or "").lower()
                if "contribservice" in other_url or "print" in other_url:
                    try:
                        other.close()
                    except Exception:
                        pass
    except Exception:
        pass


def capture_generated_file(
    page,
    dest_dir: Path,
    chip_index: Optional[int] = None,
    previous_hashes: Optional[set] = None,
    chip=None,
    timeout_seconds: float = 90.0,
    project_path: Optional[Path] = None,
) -> Optional[Path]:
    """Capture Argon's generated apply_changes.ps1 into dest_dir.

    Each attempt re-opens the chip if the viewer closed itself, waits for the
    viewer's verified download button to be visible, and clicks ONLY that one
    button (never clicking a print control or random page elements). Retries
    for up to `timeout_seconds` (~90 s) before failing loudly.
    """
    dest_dir = Path(dest_dir)
    proj_for_ledger = project_path or _infer_project_root_from_dest(dest_dir)
    combined_hashes: set = set(previous_hashes or set())
    if proj_for_ledger is not None:
        try:
            combined_hashes.update(DeliveryLedger().known_hashes(proj_for_ledger))
        except Exception:
            pass

    if chip is not None and not _element_is_after_last_user_turn(chip):
        print(
            "[!] Refusing to open delivery chip: it belongs to an earlier turn before the last user message.",
            file=sys.stderr,
        )
        return None

    if not _open_generated_file(page, chip_index=chip_index, chip=chip):
        return None

    capture_started = time.time()
    deadline = time.monotonic() + max(5.0, float(timeout_seconds))
    attempt = 0
    last_downloaded: Optional[Path] = None
    saw_download_button = False
    failure_reason = "the viewer's toolbar download button was not reachable (a print control was never clicked)"

    # D5.1: One capture, then stop. As soon as the toolbar download succeeds and validates, exit immediately.
    while True:
        attempt += 1
        _dismiss_print_preview_if_any(page)
        try:
            page.wait_for_timeout(1500)
        except Exception:
            time.sleep(1.5)

        if attempt > 1 and not _viewer_is_open(page):
            break

        dl_btn = _find_viewer_download_button(page)
        downloaded: Optional[Path] = None
        if dl_btn is not None:
            saw_download_button = True
            try:
                with page.expect_download(timeout=12000) as dl_info:
                    dl_btn.click(timeout=6000)
                downloaded, discard_reason = _validate_and_save_download(dl_info.value, dest_dir)
                if discard_reason:
                    failure_reason = f"{discard_reason} (a print control was never clicked)"
            except Exception as dl_exc:
                failure_reason = f"clicking viewer toolbar download button did not trigger download ({dl_exc})"
                downloaded = None

        if downloaded is None:
            cand = find_delivered_patch([dest_dir], newer_than=capture_started - 1.0)
            if cand is not None and cand.suffix.lower() == ".ps1" and cand.stat().st_size >= MIN_PATCH_FILE_BYTES:
                downloaded = cand

        if downloaded is not None and downloaded.is_file():
            last_downloaded = downloaded
            try:
                raw_dl_text = downloaded.read_text(encoding="utf-8", errors="replace")
            except Exception:
                raw_dl_text = ""
            dl_text = _strip_viewer_tail_artefacts(raw_dl_text)

            if _is_leftover_delivery(dl_text, combined_hashes, project_path=proj_for_ledger):
                try:
                    downloaded.unlink(missing_ok=True)
                except OSError:
                    pass
                _close_preview(page)
                return None

            if _looks_like_patch(dl_text) and _patch_is_complete(dl_text):
                if dl_text != raw_dl_text.rstrip("\r\n"):
                    try:
                        downloaded.write_text(dl_text + "\n", encoding="utf-8", newline="\n")
                    except Exception:
                        pass
                if proj_for_ledger is not None:
                    try:
                        cid_now = _extract_chat_id(str(getattr(page, "url", "") or "")) or ""
                        DeliveryLedger().record(
                            proj_for_ledger,
                            dl_text,
                            chat_id=cid_now,
                            file_path=str(downloaded),
                        )
                    except Exception:
                        pass
                _close_preview(page)
                return downloaded
            # D5.1: If the toolbar button produced an invalid/truncated file on this chip, do not re-download in a loop
            break
            if not _looks_like_patch(dl_text):
                failure_reason = (
                    f"the downloaded file ({downloaded.name}) is not a valid patch script "
                    "(a print control was never clicked)"
                )
            elif not _patch_is_complete(dl_text):
                failure_reason = (
                    f"the downloaded file ({downloaded.name}) was truncated "
                    "(a print control was never clicked)"
                )

        if time.monotonic() >= deadline:
            break

        try:
            page.wait_for_timeout(2500)
        except Exception:
            time.sleep(2.5)

    _close_preview(page)
    if last_downloaded is not None and last_downloaded.is_file():
        try:
            raw_dl_text = last_downloaded.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_dl_text = ""
        dl_text = _strip_viewer_tail_artefacts(raw_dl_text)
        if not _looks_like_patch(dl_text):
            print(
                f"[!] The downloaded file is not a usable patch script: {last_downloaded}",
                file=sys.stderr,
            )
            try:
                last_downloaded.unlink()
            except OSError:
                pass
        elif not _patch_is_complete(dl_text):
            print(
                f"[!] The downloaded file looks TRUNCATED: {last_downloaded}",
                file=sys.stderr,
            )
            print(
                "    Nothing was written on purpose - a half patch would fail the "
                "apply step. Download the file again from the Gemini chat.",
                file=sys.stderr,
            )
            try:
                last_downloaded.unlink()
            except OSError:
                pass
    elif saw_download_button and "not reachable" in failure_reason:
        failure_reason = (
            "the viewer's download button did not produce a .ps1 file "
            "(a print control was never clicked)"
        )

    secs = int(round(timeout_seconds))
    print(
        f"[!] The delivery could NOT be downloaded after {secs} s - {failure_reason}.\n"
        "    Download it from the Gemini chat by hand and apply it with:\n"
        "        Open_Argon_Apply.bat -PatchFile <path-to-apply_changes.ps1>",
        file=sys.stderr,
    )
    return None


RE_COPY_RESPONSE = re.compile(
    r"(উত্তর কপি করুন|কপি করুন|copy response|copy answer|copy text)",
    re.IGNORECASE,
)
RE_COPY_CODE = re.compile(
    r"(copy code|কোড কপি|copy snippet)",
    re.IGNORECASE,
)

COPY_RESPONSE_SELECTORS = (
    "button[aria-label*='উত্তর কপি করুন']",
    "button[data-tooltip*='উত্তর কপি করুন']",
    "button[mattooltip*='উত্তর কপি করুন']",
    "button[title*='উত্তর কপি করুন']",
    "button[aria-label*='Copy response' i]",
    "button[data-tooltip*='Copy response' i]",
    "button[mattooltip*='Copy response' i]",
    "button[title*='Copy response' i]",
    "[role='button'][aria-label*='উত্তর কপি করুন']",
    "[role='button'][aria-label*='Copy response' i]",
    "button:has(mat-icon[fonticon='content_copy'])",
    "button[aria-label*='কপি করুন']",
    "button[aria-label*='Copy' i]",
)


def _is_copy_response_button(el) -> bool:
    """Return True when `el` is the conversation's copy-response button (not a code-block copy button)."""
    label_text, icon_text = _candidate_label_and_icon(el)
    combined = f"{label_text} {icon_text}".strip()
    if not combined:
        return False
    if RE_COPY_CODE.search(combined) or RE_PRINT.search(combined):
        return False
    if RE_COPY_RESPONSE.search(label_text):
        return True
    if re.search(r"\bcontent_copy\b", icon_text, re.IGNORECASE):
        if not label_text or re.search(r"(copy|কপি)", label_text, re.IGNORECASE):
            return True
    return False


def _find_copy_response_button(page):
    """Locate the copy-response button at the bottom of the model's newest answer."""
    roots = []
    turns = _assistant_turns(page)
    if turns:
        roots.append(turns[-1])
    roots.append(page)

    for root in roots:
        best = None
        best_y = -1.0
        for sel in COPY_RESPONSE_SELECTORS:
            try:
                loc = root.locator(sel)
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    if not _safe_visible(el):
                        continue
                    if not _is_copy_response_button(el):
                        continue
                    try:
                        box = el.bounding_box(timeout=1000)
                    except Exception:
                        box = None
                    y = float(box["y"]) if box else 0.0
                    if y >= best_y:
                        best_y = y
                        best = el
            except Exception:
                continue
        if best is not None:
            return best

    try:
        loc = page.get_by_text(re.compile(r"^\s*(উত্তর কপি করুন|Copy response)\s*$", re.IGNORECASE))
        for i in range(min(loc.count(), 6)):
            el = loc.nth(i)
            if _safe_visible(el) and _is_copy_response_button(el):
                return el
    except Exception:
        pass
    return None


def _read_clipboard_with_permission_check(page) -> str:
    """Read text from `navigator.clipboard.readText()` after checking clipboard permission."""
    ctx = getattr(page, "context", None)
    if ctx is not None and hasattr(ctx, "grant_permissions"):
        try:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://gemini.google.com")
        except Exception:
            pass
        try:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass
    try:
        val = page.evaluate(
            """async () => {
                try {
                    if (navigator.permissions && navigator.permissions.query) {
                        try {
                            const st = await navigator.permissions.query({ name: 'clipboard-read' });
                            if (st && st.state === 'denied') {
                                return '';
                            }
                        } catch (_) {}
                    }
                    if (!navigator.clipboard || !navigator.clipboard.readText) {
                        return '';
                    }
                    return await Promise.race([
                        navigator.clipboard.readText(),
                        new Promise(resolve => setTimeout(() => resolve(''), 4000))
                    ]);
                } catch (_) {
                    return '';
                }
            }"""
        )
        return str(val or "").strip()
    except Exception:
        return ""


def _copy_and_save_response_summary(
    page,
    dest_dir: Path,
    chat_title: str = "",
    chat_url: str = "",
) -> tuple[Optional[Path], str]:
    """Click the conversation's copy-response button, read the clipboard, and save
    `<dest_dir>/argon_response_summary.md`.

    Never fails the run when the copy button cannot be found or the clipboard
    cannot be read: prints a warning and returns `(None, '')`.
    """
    _close_preview(page)
    dest_dir = Path(dest_dir)
    btn = _find_copy_response_button(page)
    if btn is None:
        print(
            "[!] Could not find the copy-response button ('উত্তর কপি করুন' / 'Copy response') - "
            "continuing without argon_response_summary.md.",
            file=sys.stderr,
        )
        return None, ""

    try:
        btn.click(timeout=4000)
        try:
            page.wait_for_timeout(600)
        except Exception:
            time.sleep(0.6)
    except Exception as exc:
        print(
            f"[!] Could not click the copy-response button ({exc}) - continuing without summary.",
            file=sys.stderr,
        )
        return None, ""

    copied = _read_clipboard_with_permission_check(page)
    if not copied:
        print(
            "[!] Copy-response button was clicked, but the clipboard could not be read "
            "(or was empty) - continuing without argon_response_summary.md.",
            file=sys.stderr,
        )
        return None, ""

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        summary_path = (dest_dir / "argon_response_summary.md").resolve()
        h1 = "#"
        b = "*" * 2
        nl = chr(10)
        title_str = (chat_title or _chat_title(page) or "(conversation)").strip()
        url_str = (chat_url or getattr(page, "url", "") or "").strip()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        doc_lines = [
            f"{h1} Argon Response Summary",
            f"{b}Conversation:{b} {title_str}",
            f"{b}Link:{b} {url_str}",
            f"{b}Captured:{b} {stamp}",
            "",
            copied.strip(),
            "",
        ]
        summary_path.write_text(nl.join(doc_lines), encoding="utf-8")
        print(f"[+] Saved Argon response summary to: {summary_path}")
        non_empty = [ln.strip() for ln in copied.splitlines() if ln.strip()]
        if non_empty:
            excerpt = " / ".join(non_empty[:3])
            if len(excerpt) > 220:
                excerpt = excerpt[:217] + "..."
            print(f"    Summary excerpt: {excerpt}")
        return summary_path, copied
    except Exception as exc:
        print(
            f"[!] Could not write argon_response_summary.md: {exc}",
            file=sys.stderr,
        )
        return None, copied


def _extract_notes(patch_text: str) -> str:
    """Pull the inconsistency notes Argon recorded out of the delivered script
    OR out of the copied markdown response summary ('Notes and suggestions').

    Strips single-quoted and double-quoted PowerShell here-strings first so a
    replacement block that mentions NOTES inside a target file is never
    mistaken for the script's own trailing NOTES block.
    """
    open_sq = chr(64) + chr(39)
    close_sq = chr(39) + chr(64)
    open_dq = chr(64) + chr(34)
    close_dq = chr(34) + chr(64)
    hs_pat = "".join([
        r"(?s)",
        re.escape(open_sq),
        r"\r?\n.*?\r?\n",
        re.escape(close_sq),
        r"|",
        re.escape(open_dq),
        r"\r?\n.*?\r?\n",
        re.escape(close_dq),
    ])
    cleaned = re.sub(hs_pat, "", patch_text or "")
    blocks = re.findall(
        r"(?ms)^[ \t]*#\s*---\s*NOTES\b.*?^[ \t]*#\s*---\s*END NOTES\s*---",
        cleaned,
    )
    if blocks:
        block_lines = [
            ln.strip()
            for ln in blocks[-1].splitlines()
            if ln.strip().startswith("#")
        ]
        if block_lines:
            return "\n".join(block_lines)

    # Also support a markdown 'Notes and suggestions' section in a copied chat reply.
    sec_match = re.search(
        r"(?ims)^[ \t]*(?:#{1,6}[ \t]*|[*_]{2})?notes\s+and\s+suggestions(?:[*_]{2})?[ \t]*:?[ \t]*\r?\n(.*?)(?=^[ \t]*#{1,6}[ \t]+\S|\Z)",
        cleaned,
    )
    if sec_match:
        sec_body = sec_match.group(1) or ""
        sec_lines = []
        for raw_ln in sec_body.splitlines():
            s_ln = raw_ln.strip()
            if not s_ln:
                continue
            if s_ln.startswith("#"):
                sec_lines.append(s_ln)
            else:
                cleaned_item = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", s_ln).strip()
                if cleaned_item:
                    sec_lines.append(f"# NOTE: {cleaned_item}")
        if sec_lines:
            return "\n".join(sec_lines)

    notes = []
    for raw in cleaned.splitlines():
        s = raw.strip()
        if not s.startswith("#") or re.match(r"^#{3,}\s", s):
            continue
        body = s.lstrip("#").strip()
        if re.search(
            r"(root\s*cause|note|inconsisten|issue|concern|warning|suggest|recommend|caveat|todo|"
            r"fixme|smell|drift|stale|mismatch)",
            body,
            re.IGNORECASE,
        ):
            notes.append(s)
    return "\n".join(notes)


def _save_notes(
    patch_text: str,
    project_path: Path,
    existing_notes_path: Optional[Path] = None,
) -> Optional[Path]:
    """Write the recorded notes next to the project so they are not lost.

    Reuses `existing_notes_path` when provided so a single run never writes
    more than one `argon_notes_<timestamp>.md` file.
    """
    notes = _extract_notes(patch_text)
    if not notes.strip():
        return existing_notes_path
    note_lines = [ln for ln in notes.splitlines() if ln.strip()]
    item_lines = [
        ln
        for ln in note_lines
        if not re.match(r"^#\s*---\s*(?:NOTES|END NOTES)\b", ln.strip(), re.IGNORECASE)
    ]
    note_count = len(item_lines) if item_lines else len(note_lines)
    try:
        out_dir = Path(project_path) / ".argon_backup"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = (
            Path(existing_notes_path)
            if existing_notes_path is not None
            else out_dir / ("argon_notes_" + time.strftime("%Y%m%d_%H%M%S") + ".md")
        )
        out.write_text(
            "# Argon notes and suggestions\n\n"
            "Recorded by Gemini 4 Argon while producing the patch.\n\n"
            + notes
            + "\n",
            encoding="utf-8",
        )
        print(f"[+] Argon recorded {note_count} note(s) - saved to: {out}")
        for line in note_lines:
            print("      " + line)
        return out
    except Exception as exc:
        print(f"[!] Could not save the Argon notes: {exc}", file=sys.stderr)
        return None


def _looks_like_patch(candidate: str) -> bool:
    """Accept only a real PowerShell patch script, not prompt prose.

    The page body contains the user's own prompt - including its example
    snippet - so a loose "it mentions WriteAllText" test happily picks the
    prompt back instead of Argon's answer.
    """
    if not candidate:
        return False
    lines = candidate.splitlines()
    if len(lines) < 8:
        return False
    # The last non-empty line must be a comment (#...) or a closing brace (}),
    # never a bare filename or stray Drive viewer caption such as 'apply_changes.ps1'.
    non_empty_lines = [ln.strip() for ln in lines if ln.strip()]
    if not non_empty_lines:
        return False
    last_non_empty = non_empty_lines[-1]
    if not (last_non_empty.startswith("#") or last_non_empty.endswith("}")):
        return False
    # The script must pin the target root to a quoted absolute path.
    if not re.search(r"\$TargetRoot\s*=\s*['\"][^'\"]{3,}['\"]", candidate):
        return False
    if not any(
        k in candidate
        for k in ("WriteAllText", "Set-Content", "Copy-Item", "Write-Host", ".Replace(")
    ):
        return False
    # Markdown structure means we grabbed prose. A single '#' is a PowerShell
    # comment and must stay allowed; markdown headings use three or more.
    outside_herestrings = re.sub(
        r"@'\r?\n.*?\r?\n'@|@\"\r?\n.*?\r?\n\"@",
        "@'\n'@",
        candidate,
        flags=re.DOTALL,
    )
    for raw in outside_herestrings.splitlines():
        s = raw.strip()
        if not s:
            continue
        if re.match(r"^#{3,}\s", s):
            return False
        if re.match(r"^[-*+]\s", s):
            return False
        if s.startswith("|"):
            return False
        if "**" in s:
            return False
    # Placeholders from the prompt template must never be applied.
    if "relative/path/to/file.ext" in candidate or "{{TARGET_DIRECTORY}}" in candidate:
        return False
    return True


def extract_powershell_patch(response_text: str) -> Optional[str]:
    """Extract the apply_changes.ps1 script from Gemini 4 Argon's response.

    Only the LAST plausible candidate is taken: the page body holds the
    user's prompt first and Argon's answer last.
    """
    patterns = [
        r"```(?:powershell|ps1|pwsh)\r?\n(.*?)\r?\n```",
        r"```\r?\n(\s*\$ErrorActionPreference.*?)\r?\n```",
    ]
    candidates: List[str] = []
    for pat in patterns:
        for m in re.findall(pat, response_text, flags=re.DOTALL | re.IGNORECASE):
            text = m.strip()
            if text and text not in candidates:
                candidates.append(text)
    valid = [c for c in candidates if _looks_like_patch(c)]
    if valid:
        return valid[-1]
    # Nothing plausible: return the longest candidate so the caller can report it.
    return max(candidates, key=len) if candidates else None


def apply_extracted_patch(
    project_path: Path,
    response_text: str,
    ps1_patch: Optional[str] = None,
) -> bool:
    """Save and execute apply_changes.ps1 (or fallback to Apply-ArgonPatch.ps1)."""
    project_path = project_path.resolve()
    if ps1_patch is None:
        ps1_patch = extract_powershell_patch(response_text)

    shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    if ps1_patch:
        if not refuse_invalid_patch(ps1_patch, project_path=project_path, patch_name="apply_changes.ps1"):
            return False
        before_files = _snapshot_project_files(project_path)
        patch_script_path = project_path / "apply_changes.ps1"
        patch_script_path.write_text(ps1_patch + "\n", encoding="utf-8")
        print(f"[+] Saved extracted PowerShell patch to: {patch_script_path}")
        try:
            res = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(patch_script_path),
                ],
                cwd=str(project_path),
                timeout=300,
            )
            if res.returncode != 0:
                return False
        except subprocess.TimeoutExpired:
            print(f"[!] Patch execution timed out after 300 s: {patch_script_path}", file=sys.stderr)
            return False
        if verify_written_files_bytes(project_path, before_files):
            return False
        return True

    raw_response_file = project_path / "argon_response.txt"
    raw_response_file.write_text(response_text, encoding="utf-8")
    if APPLY_PATCH_SCRIPT.is_file():
        try:
            res = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(APPLY_PATCH_SCRIPT),
                    "-PatchFile",
                    str(raw_response_file),
                    "-ProjectPath",
                    str(project_path),
                ],
                cwd=str(project_path),
                timeout=300,
            )
            return res.returncode == 0
        except subprocess.TimeoutExpired:
            print("[!] Apply-ArgonPatch.ps1 timed out after 300 s.", file=sys.stderr)
            return False
    return False


def get_target_url(
    mode: str,
    slot: Optional[str] = None,
    isolated_profile: bool = True,
) -> str:
    """
    Return the canonical workspace URL for the given mode.

    IMPORTANT - /u/N/ is an INDEX into the account list of the browser that
    opens the URL, not a fixed account. A per-slot profile
    (~/.gemini/argon_profiles/slot-uN/) holds exactly ONE Google account, and
    that account always sits at index 0. Opening /u/<n>/ on such a profile
    therefore lands on the ANONYMOUS GUEST page ("Sign in to save activity",
    model Flash-Lite) - which looks like a session that expired and silently
    sends the task nowhere.

    So when the run drives an isolated slot profile the URL must use /u/0/.
    The slot itself is still tracked separately (the lock file, the session map
    and the account pool), so nothing is lost.

    Gemini Web is the only supported workspace. Google AI Studio was removed
    because it consistently fails on agentic work, so any other mode is
    rejected with a clear message instead of silently doing something else.
    """
    clean_mode = (mode or "gemini").strip().lower()
    if clean_mode not in ("gemini", "web", ""):
        raise ValueError(
            f"Unsupported workspace mode {mode!r}. Google AI Studio is no longer "
            "supported - it consistently fails on agentic work. Use "
            "--mode gemini (Gemini Web)."
        )
    if isolated_profile:
        slot_part = "/u/0/"
    else:
        slot_part = normalize_slot(slot) if slot else "/u/0/"
    return GEMINI_WEB_URL.format(slot=slot_part)


def _profile_is_isolated_slot(profile_dir: Any) -> bool:
    """True when the profile is a per-slot profile (one account, index 0)."""
    if not profile_dir:
        return True
    try:
        resolved = Path(str(profile_dir)).expanduser().resolve()
        return resolved.parent.name == "argon_profiles" or resolved.name.startswith("slot-")
    except Exception:
        return False


def _normalize_gemini_chat_url(url: str, isolated_profile: bool = True) -> str:
    """Ensure a Gemini Web URL uses `/u/0/` when opened in a 1-account isolated slot profile."""
    raw = (url or "").strip()
    if not raw:
        return raw
    if isolated_profile and "gemini.google.com" in raw.lower():
        raw = re.sub(r"(gemini\.google\.com)/u/\d+/", r"\1/u/0/", raw, flags=re.IGNORECASE)
    return raw


def _has_signed_in_evidence(page) -> bool:
    """True when the page itself PROVES that a Google account is signed in.

    The account chip in the sidebar and the list of the account's own chats are
    both proof. Nothing else is: the account MENU always contains a "Sign in"
    entry (to add another account) and that entry is in the DOM of a perfectly
    signed-in page - MEASURED 2026-11-05.
    """
    try:
        avatars = page.locator(
            "a[aria-label*='Google Account'], button[aria-label*='Google Account'], "
            "a[aria-label*='Google অ্যাকাউন্ট'], button[aria-label*='Google অ্যাকাউন্ট']"
        )
        for i in range(min(avatars.count(), 4)):
            if _safe_visible(avatars.nth(i)):
                return True
    except Exception:
        pass
    try:
        if page.locator("a[href*='/app/']").count() > 0:
            return True
    except Exception:
        pass
    return False


def _gemini_page_is_guest(page, wait_seconds: float = 15.0) -> bool:
    """True when the page really shows a Google sign-in screen.

    NEVER decide from a phrase in the body. MEASURED 2026-11-04: the phrase
    "Sign in to save activity" appears in the CONVERSATION TEXT whenever the
    chat talks about the guest detection, so it proves nothing.

    And a "Sign in" CONTROL proves nothing either - MEASURED 2026-11-05: the
    Google account menu of a signed-in page contains exactly such an entry.

    The proof is the URL, or the ABSENCE of every signed-in signal on a page
    that really offers a sign-in. Because the sidebar needs a moment to
    render, the absence is confirmed over a short window before it counts.
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if any(
        tok in url
        for tok in (
            "accounts.google.com/v3/signin",
            "accounts.google.com/signin",
            "accounts.google.com/servicelogin",
            "/signin/rejected",
        )
    ):
        return True

    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        if _has_signed_in_evidence(page):
            return False
        if time.monotonic() >= deadline:
            break
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2.0)

    # No signed-in signal at all, and the page offers a sign-in: that is a
    # real guest page. A sign-in control INSIDE a conversation turn never
    # counts - the chat can talk about signing in.
    short_signin_re = re.compile(
        r"^\s*(sign\s*in|log\s*in|সাইন[\s-]*ইন(?:\s*করুন)?)\s*$",
        re.IGNORECASE,
    )
    for sel in (
        "a[href*='ServiceLogin']",
        "a[href*='accounts.google.com/v3/signin']",
        "a[aria-label*='Sign in' i]",
        "button[aria-label*='Sign in' i]",
        "a[aria-label*='সাইন ইন']",
        "button[aria-label*='সাইন ইন']",
    ):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                try:
                    in_turn = bool(
                        el.evaluate(
                            """node => {
                                let cur = node;
                                while (cur) {
                                    const tag = (cur.tagName || '').toLowerCase();
                                    const cls = (cur.getAttribute && (cur.getAttribute('class') || '')) || '';
                                    if (tag === 'user-query' || tag === 'model-response' || tag === 'message-turn' ||
                                        /\b(user-query|model-response)\b/i.test(cls)) {
                                        return true;
                                    }
                                    cur = cur.parentElement;
                                }
                                return false;
                            }"""
                        )
                    )
                except Exception:
                    in_turn = False
                if in_turn:
                    continue
                try:
                    vis_txt = (el.inner_text(timeout=400) or "").strip()
                except Exception:
                    vis_txt = ""
                try:
                    aria_txt = str(el.get_attribute("aria-label") or "").strip()
                except Exception:
                    aria_txt = ""
                try:
                    href_txt = str(el.get_attribute("href") or "").strip().lower()
                except Exception:
                    href_txt = ""
                if len(vis_txt) > 40 or len(aria_txt) > 40:
                    continue
                if "servicelogin" in href_txt or "accounts.google.com" in href_txt:
                    if not vis_txt or len(vis_txt) <= 32:
                        return True
                if (vis_txt and short_signin_re.match(vis_txt)) or (
                    aria_txt and short_signin_re.match(aria_txt)
                ):
                    return True
        except Exception:
            continue
    # The real guest page offers the sign-in as plain TEXT, not always as a
    # button. This is only reached when NO signed-in signal exists at all, so
    # the account menu's own "Sign in" entry cannot cause a false positive.
    try:
        txt = page.get_by_text(short_signin_re)
        for i in range(min(txt.count(), 4)):
            el = txt.nth(i)
            if not _safe_visible(el):
                continue
            try:
                in_turn = bool(
                    el.evaluate(
                        """node => {
                            let cur = node;
                            while (cur) {
                                const tag = (cur.tagName || '').toLowerCase();
                                const cls = (cur.getAttribute && (cur.getAttribute('class') || '')) || '';
                                if (tag === 'user-query' || tag === 'model-response' || tag === 'message-turn' ||
                                    /\b(user-query|model-response)\b/i.test(cls)) {
                                    return true;
                                }
                                cur = cur.parentElement;
                            }
                            return false;
                        }"""
                    )
                )
            except Exception:
                in_turn = False
            if not in_turn:
                return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# Session continuity                                                            #
#                                                                             #
# One Gemini conversation per project root. A follow-up task on the same      #
# project reopens that conversation, so the understanding Argon built earlier  #
# is still there. The map lives in ~/.gemini/argon_sessions.json.                #
# --------------------------------------------------------------------------- #

RE_CHAT_ID = re.compile(r"/app/([0-9a-f]{8,})|/prompts/(?!new_chat\b)([A-Za-z0-9_-]{8,})")


def _normalize_project_key(project_path: Any) -> str:
    """Return a comparable key for a project root.

    The path is resolved to its absolute long form and then case-folded, so
    `C:\\Users\\TechZone IT\\Proj` and `c:\\users\\techzone it\\proj` are the
    SAME project.
    """
    try:
        resolved = Path(str(project_path)).expanduser().resolve()
    except Exception:
        resolved = Path(str(project_path))
    return os.path.normcase(str(resolved))


def _extract_chat_id(url: str) -> Optional[str]:
    """Pull the conversation id out of a Gemini Web chat URL."""
    if not url:
        return None
    m = RE_CHAT_ID.search(url)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _wait_for_chat_id(page: Any, timeout: float = 20.0, interval: float = 1.0) -> Optional[str]:
    """Wait until Gemini has created the chat and its URL carries the id.

    On a fresh chat the id only appears once the first prompt created the
    conversation, so the URL is polled for a short while. On a continuation the
    id is already in the URL and this returns immediately.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            chat_id = _extract_chat_id(page.url or "")
        except Exception:
            chat_id = None
        if chat_id:
            return chat_id
        if time.monotonic() >= deadline:
            return None
        try:
            page.wait_for_timeout(int(interval * 1000))
        except Exception:
            time.sleep(interval)


def _slot_key(slot: Optional[str]) -> str:
    """Filesystem-safe key for a pool slot such as '/u/3/' -> 'u3'."""
    raw = (slot or "/u/0/").strip()
    m = re.search(r"/u/(\d+)/?", raw)
    if m:
        return f"u{int(m.group(1))}"
    if raw.isdigit():
        return f"u{int(raw)}"
    key = raw.strip("/").replace("/", "-")
    return key or "u0"


def _slot_lock_path(slot: Optional[str]) -> Path:
    return Path.home() / ".gemini" / "argon_locks" / f"slot-{_slot_key(slot)}.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return f'"{pid}"' in (out.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


class SlotLock:
    """A lock that says "this pool slot is in use by a running dispatcher".

    Chrome allows only one instance per profile directory, so two runs on the
    same slot cannot share a browser. The lock makes that visible instead of
    letting the second launch kill the first one.
    """

    def __init__(self, slot: Optional[str], profile_dir: Optional[Path] = None) -> None:
        self.slot = normalize_slot(slot) if slot else "/u/0/"
        self.path = _slot_lock_path(self.slot)
        self.profile_dir: Optional[Path] = Path(profile_dir) if profile_dir else None
        self.held = False
        self.started_at = ""

    def acquire(self, profile_dir: Optional[Path] = None) -> bool:
        if profile_dir is not None:
            self.profile_dir = Path(profile_dir)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_file():
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    info = {}
                pid = int(info.get("pid") or 0)
                if pid and pid != os.getpid() and _pid_alive(pid):
                    locked_profile = info.get("profile_dir") or "?"
                    print(
                        f"[!] Slot {self.slot} is already in use by pid {pid} "
                        f"(started {info.get('started_at', '?')}, profile {locked_profile})."
                    )
                    return False
                print(f"[+] The lock on slot {self.slot} was stale (pid {pid}) - reclaiming it.")
            self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._write_lock()
            self.held = True
            try:
                atexit.register(self.release)
            except Exception:
                pass
            return True
        except Exception as exc:
            print(f"[!] Could not take the slot lock: {exc}", file=sys.stderr)
            return False

    def update_profile_dir(self, profile_dir: Path) -> None:
        self.profile_dir = Path(profile_dir)
        if self.held:
            try:
                self._write_lock()
            except Exception:
                pass

    def _write_lock(self) -> None:
        payload = {
            "pid": os.getpid(),
            "slot": self.slot,
            "started_at": self.started_at or time.strftime("%Y-%m-%d %H:%M:%S"),
            "profile_dir": str(self.profile_dir) if self.profile_dir else "",
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def release(self) -> None:
        if not self.held:
            return
        try:
            if self.path.is_file():
                should_unlink = True
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8"))
                    recorded_pid = int(info.get("pid") or 0)
                    if recorded_pid and recorded_pid != os.getpid():
                        should_unlink = False
                except Exception:
                    should_unlink = True
                if should_unlink:
                    self.path.unlink(missing_ok=True)
        except Exception:
            pass
        self.held = False


def _profile_dir_for_slot(base_profile: Path, slot: Optional[str]) -> Path:
    """Each pool slot gets its own browser profile directory.

    Chrome allows a single instance per profile directory, so two runs that
    share one directory fight: the second launch kills the first. Giving every
    slot its own directory lets two agents work on two projects at the same
    time without touching each other's browser.
    """
    base_profile = Path(base_profile).expanduser().resolve()
    if base_profile.parent.name == "argon_profiles" and base_profile.name.startswith("slot-"):
        root = base_profile.parent
    else:
        root = base_profile.parent / "argon_profiles"
    return root / f"slot-{_slot_key(slot)}"


def _has_cookie_db(profile_dir: Path) -> bool:
    """Return True when profile_dir contains a non-empty Chrome Cookies SQLite file."""
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        candidate = Path(profile_dir) / rel
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _seed_profile(base_profile: Path, target: Path) -> bool:
    """Create a slot profile from the master profile when the master profile is not busy."""
    base_profile = Path(base_profile)
    target = Path(target)
    if target.is_dir() and (target / "Default").is_dir():
        if not _has_cookie_db(base_profile) or _has_cookie_db(target):
            return True
    if not base_profile.is_dir() or not (base_profile / "Default").is_dir():
        print(
            f"[!] Cannot seed slot profile {target}: master profile {base_profile} is empty or does not exist yet.",
            file=sys.stderr,
        )
        return False
    if _profile_is_busy(base_profile):
        print(
            f"[!] Cannot seed slot profile {target}: the master profile ({base_profile}) is currently in use by another run.",
            file=sys.stderr,
        )
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        copied = _copy_profile(base_profile, target)
        return copied is not None
    except Exception as exc:
        print(f"[!] Could not seed the slot profile {target}: {exc}", file=sys.stderr)
        return False


def _profile_is_busy(profile_dir: Path) -> bool:
    """True when another browser or active slot lock already holds the profile."""
    try:
        target = Path(profile_dir).expanduser().resolve()
    except Exception:
        target = Path(profile_dir)
    if not target.exists():
        return False

    # 1. Check active slot locks in ~/.gemini/argon_locks/
    locks_dir = Path.home() / ".gemini" / "argon_locks"
    try:
        if locks_dir.is_dir():
            for lock_file in locks_dir.glob("slot-*.lock"):
                try:
                    info = json.loads(lock_file.read_text(encoding="utf-8"))
                    pid = int(info.get("pid") or 0)
                    locked_prof = info.get("profile_dir") or ""
                    if (
                        pid
                        and pid != os.getpid()
                        and locked_prof
                        and Path(locked_prof).expanduser().resolve() == target
                        and _pid_alive(pid)
                    ):
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # 2. Check Chromium singleton markers
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        marker = target / name
        try:
            if marker.is_symlink():
                link_target = os.readlink(marker)
                m = re.search(r"-(\d+)$", str(link_target))
                if m and not _pid_alive(int(m.group(1))):
                    continue
                return True
            if marker.exists():
                return True
        except Exception:
            continue

    # 3. On Windows, Chrome holds an exclusive lock on 'lockfile' and 'Default/Network/Cookies'
    if sys.platform == "win32":
        for rel in ("lockfile", "Default/Network/Cookies"):
            candidate = target / rel
            if candidate.is_file():
                try:
                    with open(candidate, "r+b"):
                        pass
                except PermissionError:
                    return True
                except OSError:
                    continue

    return False


def _copy_profile(profile_dir: Path, target, allow_busy: bool = False) -> Optional[Path]:
    """Copy the browser profile so this run gets its own browser instance.

    The copy keeps the same signed-in account and cookies, but nobody else's
    browser can close it and it cannot close theirs. When `allow_busy=True`,
    allows snapshotting an active slot profile for a second concurrent task on
    the same account without blocking (Section 3: No Hard Blocking).
    """
    profile_dir = Path(profile_dir)
    target = Path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    def _safe_copy(src, dst):
        # A file Chrome currently holds open must not break the whole copy.
        try:
            return shutil.copy2(src, dst)
        except Exception:
            return dst

    if not allow_busy and _profile_is_busy(profile_dir):
        print(
            f"[!] Refusing to copy busy profile {profile_dir}: close the running browser first.",
            file=sys.stderr,
        )
        return None

    try:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(
            profile_dir,
            target,
            symlinks=True,
            copy_function=_safe_copy,
            ignore=shutil.ignore_patterns(
                "SingletonLock",
                "SingletonCookie",
                "SingletonSocket",
                "lockfile",
                "Cookies",
                "*-journal",
                "*-wal",
                "*-shm",
            ),
        )
        if not target.is_dir():
            return None
        if not _copy_sqlite_databases(profile_dir, target, allow_busy=allow_busy):
            shutil.rmtree(target, ignore_errors=True)
            return None
        return target
    except Exception as exc:
        print(f"[!] Could not duplicate the browser profile: {exc}", file=sys.stderr)
        return None


def _copy_sqlite_databases(src_root: Path, dst_root: Path, allow_busy: bool = False) -> bool:
    """Copy the Chrome SQLite databases properly using SQLite's online backup API.

    When `allow_busy=True`, falls back to immutable read or direct byte copy if
    another Chrome process holds a lock on the cookie file.
    """
    import sqlite3

    for rel in (
        "Default/Network/Cookies",
        "Default/Cookies",
        "Default/History",
        "Default/Favicons",
        "Default/Web Data",
    ):
        src = Path(src_root) / rel
        dst = Path(dst_root) / rel
        if not src.is_file():
            continue
        is_cookie_db = rel.endswith("Cookies")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(dst) + suffix)
                if sidecar.exists():
                    try:
                        sidecar.unlink()
                    except OSError:
                        pass
            try:
                source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
                try:
                    target = sqlite3.connect(str(dst))
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                finally:
                    source.close()
            except Exception:
                if not allow_busy:
                    raise
                try:
                    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro&immutable=1", uri=True)
                    try:
                        target = sqlite3.connect(str(dst))
                        try:
                            source.backup(target)
                        finally:
                            target.close()
                    finally:
                        source.close()
                except Exception:
                    dst.write_bytes(src.read_bytes())
            if is_cookie_db and (not dst.is_file() or dst.stat().st_size == 0):
                return False
        except Exception as exc:
            if is_cookie_db:
                print(
                    f"[!] Could not snapshot SQLite cookie database {src}: {exc}",
                    file=sys.stderr,
                )
                return False
            continue
    return True


def _launch_with_fallback(p, launch_kwargs: dict, chrome_exe: Optional[str], profile_dir: Path):
    """Launch the persistent context on profile_dir without touching a busy browser.

    Never launch Chrome against a profile directory that is already held by
    another run, because doing so hijacks or terminates the other browser.
    """
    if _profile_is_busy(profile_dir):
        raise RuntimeError(f"Profile directory {profile_dir} is already in use by another browser.")

    attempts = []
    if chrome_exe:
        attempts.append(("chrome", {"executable_path": chrome_exe, **launch_kwargs}))
    attempts.append(("chromium", dict(launch_kwargs)))

    last_exc: Optional[Exception] = None
    for label, kwargs in attempts:
        try:
            return p.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            last_exc = exc
            busy = "existing browser session" in str(exc).lower() or "profile is already in use" in str(exc).lower()
            print(f"[!] The {label} launch failed: {str(exc).splitlines()[0]}")
            if busy:
                break
    if last_exc is not None:
        raise last_exc
    return None


class TaskState:
    """Remembers, per project, which conversation the current task is in and how
    far it got.

    A browser can be closed at any moment - by the user, by a crash, or by
    another agent taking over the profile. The state file makes the run
    resumable: the conversation is reopened, and then either the delivered
    file is captured and applied, or the run keeps waiting for the answer.

    The file lives in the user profile, next to the browser profile, so every
    copy of this plugin shares it.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or TASK_STATE_PATH)
        self.data: Dict[str, Any] = {}
        self.load()

    # -------------------------------------------------------------- storage --
    def load(self) -> None:
        try:
            if self.path.is_file():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.setdefault("version", TASK_STATE_VERSION)
        self.data.setdefault("tasks", {})

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    # --------------------------------------------------------------- access --
    @staticmethod
    def _key(project_path: Any) -> str:
        try:
            return str(Path(str(project_path)).expanduser().resolve())
        except Exception:
            return str(Path(project_path))

    def _find_key(self, project_path: Any) -> Optional[str]:
        tasks = self.data.get("tasks") or {}
        wanted = _normalize_project_key(project_path)
        for key in tasks:
            if _normalize_project_key(key) == wanted:
                return key
        return None

    def get(self, project_path: Any, lane: Optional[str] = None) -> Optional[Dict[str, Any]]:
        self.load()
        key = self._find_key(project_path)
        if key is None:
            return None
        entry = (self.data.get("tasks") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        if lane:
            lanes = entry.get("lanes")
            if isinstance(lanes, dict) and isinstance(lanes.get(lane), dict):
                return dict(lanes[lane])
            return None
        return entry

    def get_running_lanes(self, project_path: Any) -> Dict[str, Dict[str, Any]]:
        """Return a dict of currently running lanes ('sent' or 'waiting') for project_path (D2.1, D2.4)."""
        entry = self.get(project_path)
        if not isinstance(entry, dict):
            return {}
        running: Dict[str, Dict[str, Any]] = {}
        lanes = entry.get("lanes")
        if isinstance(lanes, dict):
            for l_name, l_data in lanes.items():
                if isinstance(l_data, dict) and str(l_data.get("state") or "").strip().lower() in ("sent", "waiting"):
                    running[l_name] = dict(l_data)
        if not running and str(entry.get("state") or "").strip().lower() in ("sent", "waiting"):
            l_key = str(entry.get("lane") or "single")
            running[l_key] = dict(entry)
        return running

    def set(self, project_path: Any, lane: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """Persist task state with file locking and per-lane isolation (D2.4).

        Two concurrent dual lanes write to their own `entry['lanes'][lane]` slot so a
        pre-flight failure on one lane NEVER overwrites a running or delivered state
        on the sibling lane.
        """
        lock = _acquire_session_store_lock(self.path)
        try:
            self.load()
            tasks = self.data.setdefault("tasks", {})
            canonical = self._key(project_path)
            existing = self._find_key(canonical)
            entry: Dict[str, Any] = {}
            if existing is not None and isinstance(tasks.get(existing), dict):
                entry.update(tasks[existing])
                if existing != canonical:
                    tasks.pop(existing, None)

            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            eff_lane = str(lane or fields.get("lane") or "").strip().lower()
            lanes = dict(entry.get("lanes") or {}) if isinstance(entry.get("lanes"), dict) else {}

            if eff_lane:
                lane_rec = dict(lanes.get(eff_lane) or {})
                lane_rec.update(fields)
                lane_rec["lane"] = eff_lane
                lane_rec["updated_at"] = now_str
                lanes[eff_lane] = lane_rec
                entry["lanes"] = lanes

                # D2.4: Do not let a failed/preflight_failed lane overwrite a sibling lane's
                # active ('sent', 'waiting') or successful ('delivered', 'applied') top-level state.
                new_state = str(fields.get("state") or "").strip().lower()
                sibling_protected = False
                for other_name, other_rec in lanes.items():
                    if other_name == eff_lane or not isinstance(other_rec, dict):
                        continue
                    other_st = str(other_rec.get("state") or "").strip().lower()
                    if other_st in ("sent", "waiting", "delivered", "applied") and new_state in (
                        "preflight_failed",
                        "not_signed_in",
                        "quota_limited",
                        "failed",
                    ):
                        sibling_protected = True
                        break
                if not sibling_protected:
                    entry.update(fields)
            else:
                entry.update(fields)
                single_rec = dict(lanes.get("single") or {})
                single_rec.update(fields)
                single_rec["updated_at"] = now_str
                lanes["single"] = single_rec
                entry["lanes"] = lanes

            entry["updated_at"] = now_str
            tasks[canonical] = entry
            self.save()
            return entry
        finally:
            _release_session_store_lock(lock)

    def clear(self, project_path: Any, lane: Optional[str] = None) -> None:
        lock = _acquire_session_store_lock(self.path)
        try:
            self.load()
            existing = self._find_key(project_path)
            if existing is not None:
                if lane:
                    entry = (self.data.get("tasks") or {}).get(existing)
                    if isinstance(entry, dict) and isinstance(entry.get("lanes"), dict):
                        entry["lanes"].pop(lane, None)
                        self.save()
                else:
                    (self.data.get("tasks") or {}).pop(existing, None)
                    self.save()
        finally:
            _release_session_store_lock(lock)


class TaskRegistry:
    """Persistent agent-owned task registry (~/.gemini/argon_tasks.json) (D3, D4).

    Tracks every currently running task across all projects and slots:
      - project, slot, account, chat_id, chat_url, lane, start_time, label, screenshot_path
    Enforces at most MAX_TASKS_PER_SLOT (2) concurrent tasks on any single account
    and surfaces STALE tasks (with age and screenshot path) so they can be closed explicitly.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or TASK_REGISTRY_PATH)
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        try:
            if self.path.is_file():
                parsed = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    self.data = parsed
        except Exception:
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.setdefault("version", TASK_REGISTRY_VERSION)
        if not isinstance(self.data.get("entries"), list):
            self.data["entries"] = []
        self.data.setdefault("last_used_slot", "")
        return self.data

    def save(self) -> None:
        lock = _acquire_session_store_lock(self.path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(self.path))
        except Exception:
            pass
        finally:
            _release_session_store_lock(lock)

    def _canonical_proj(self, project_path: Any) -> str:
        try:
            return str(Path(str(project_path)).expanduser().resolve())
        except Exception:
            return str(project_path)

    def active_entries(self) -> List[Dict[str, Any]]:
        self.load()
        return [
            e
            for e in (self.data.get("entries") or [])
            if isinstance(e, dict) and str(e.get("status") or "").strip().lower() == "running"
        ]

    def active_tasks_for_slot(self, slot: str) -> List[Dict[str, Any]]:
        norm_s = normalize_slot(slot)
        return [
            e
            for e in self.active_entries()
            if normalize_slot(str(e.get("slot") or "/u/0/")) == norm_s
        ]

    def active_tasks_for_project(
        self,
        project_path: Any,
        lane: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        wanted = _normalize_project_key(project_path)
        out: List[Dict[str, Any]] = []
        for e in self.active_entries():
            if _normalize_project_key(e.get("project", "")) != wanted:
                continue
            if lane and str(e.get("lane") or "").strip().lower() != lane.strip().lower():
                continue
            out.append(e)
        return out

    def is_slot_available(self, slot: str, max_tasks: int = MAX_TASKS_PER_SLOT) -> tuple[bool, str]:
        """Return (available, reason) based on the registry count for `slot` (D4.3, D4.4)."""
        norm_s = normalize_slot(slot)
        active = self.active_tasks_for_slot(norm_s)
        count = len(active)
        if count >= max_tasks:
            labels = ", ".join(
                f"{Path(str(a.get('project') or '.')).name}[{a.get('lane') or 'main'}]"
                for a in active
            )
            return (
                False,
                f"slot {norm_s} already hosts {count}/{max_tasks} running tasks in registry ({labels})",
            )
        return (True, f"registry load {count}/{max_tasks}")

    def stale_tasks(self, max_age_seconds: float = 3600.0) -> List[Dict[str, Any]]:
        """Return running tasks older than `max_age_seconds` (or whose PID is no longer alive) (D4.5)."""
        now_ep = time.time()
        stale: List[Dict[str, Any]] = []
        for e in self.active_entries():
            started_ep = float(e.get("started_epoch") or 0.0)
            age = max(0.0, now_ep - started_ep) if started_ep > 0 else 0.0
            pid = int(e.get("pid") or 0)
            pid_dead = bool(pid > 0 and not _pid_alive(pid) and age > 120.0)
            if age >= max_age_seconds or pid_dead:
                copy_e = dict(e)
                copy_e["age_seconds"] = int(round(age))
                copy_e["age_human"] = _format_elapsed(age)
                stale.append(copy_e)
        return stale

    def register_start(
        self,
        project_path: Any,
        slot: str,
        account: str = "",
        chat_id: str = "",
        chat_url: str = "",
        lane: str = "single",
        label: str = "",
        screenshot_path: str = "",
        prompt_hash: str = "",
    ) -> Dict[str, Any]:
        """Mark a task as started in ~/.gemini/argon_tasks.json (D4.1, D4.2)."""
        self.load()
        proj_str = self._canonical_proj(project_path)
        norm_s = normalize_slot(slot) if slot else "/u/0/"
        lane_str = (lane or "single").strip().lower()
        entries: List[Dict[str, Any]] = self.data.setdefault("entries", [])

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        now_ep = time.time()
        task_id = f"{Path(proj_str).name}:{lane_str}:{norm_s.strip('/')}:{int(now_ep)}"

        # Update existing running entry for the same (project, lane) if already registered by agent
        for existing in entries:
            if (
                isinstance(existing, dict)
                and str(existing.get("status") or "") == "running"
                and _normalize_project_key(existing.get("project", "")) == _normalize_project_key(proj_str)
                and str(existing.get("lane") or "single").strip().lower() == lane_str
            ):
                existing["slot"] = norm_s
                if account:
                    existing["account"] = account
                if chat_id:
                    existing["chat_id"] = chat_id
                if chat_url:
                    existing["chat_url"] = chat_url
                if label:
                    existing["label"] = label
                if screenshot_path:
                    existing["screenshot_path"] = str(screenshot_path)
                if prompt_hash:
                    existing["prompt_hash"] = prompt_hash
                existing["pid"] = os.getpid()
                existing["updated_at"] = now_str
                self.data["last_used_slot"] = norm_s
                self.save()
                return existing

        rec: Dict[str, Any] = {
            "task_id": task_id,
            "project": proj_str,
            "slot": norm_s,
            "account": account,
            "chat_id": chat_id,
            "chat_url": chat_url,
            "lane": lane_str,
            "status": "running",
            "started_at": now_str,
            "started_epoch": now_ep,
            "updated_at": now_str,
            "pid": os.getpid(),
            "label": (label or "").strip()[:240],
            "screenshot_path": str(screenshot_path or ""),
            "prompt_hash": prompt_hash,
        }
        entries.append(rec)
        self.data["last_used_slot"] = norm_s
        self.save()
        return rec

    def update_running(
        self,
        project_path: Any,
        lane: Optional[str] = None,
        **fields: Any,
    ) -> Optional[Dict[str, Any]]:
        self.load()
        wanted = _normalize_project_key(project_path)
        lane_str = (lane or "").strip().lower()
        matched: Optional[Dict[str, Any]] = None
        for e in reversed(self.data.get("entries") or []):
            if not isinstance(e, dict) or str(e.get("status") or "") != "running":
                continue
            if _normalize_project_key(e.get("project", "")) != wanted:
                continue
            if lane_str and str(e.get("lane") or "single").strip().lower() != lane_str:
                continue
            e.update({k: v for k, v in fields.items() if v is not None})
            e["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if fields.get("slot"):
                self.data["last_used_slot"] = normalize_slot(str(fields["slot"]))
            matched = e
            break
        if matched is not None:
            self.save()
        return matched

    def register_done(
        self,
        project_path: Any,
        lane: Optional[str] = None,
        slot: Optional[str] = None,
        status: str = "completed",
        screenshot_path: str = "",
        label: Optional[str] = None,
    ) -> int:
        """Mark running task(s) for `project_path` (and optional `lane`/`slot`/`label`) as completed/abandoned/superseded (D4.2)."""
        self.load()
        wanted = _normalize_project_key(project_path)
        lane_str = (lane or "").strip().lower()
        norm_s = normalize_slot(slot) if slot else ""
        label_str = (label or "").strip().lower()
        count = 0
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        matching_entries = [
            e
            for e in (self.data.get("entries") or [])
            if isinstance(e, dict)
            and str(e.get("status") or "") == "running"
            and _normalize_project_key(e.get("project", "")) == wanted
            and (not lane_str or str(e.get("lane") or "single").strip().lower() == lane_str)
            and (not norm_s or normalize_slot(str(e.get("slot") or "/u/0/")) == norm_s)
        ]
        label_filtered = [
            e for e in matching_entries if label_str and str(e.get("label") or "").strip().lower() == label_str
        ]
        target_entries = label_filtered if label_filtered else matching_entries
        for e in target_entries:
            e["status"] = status
            e["completed_at"] = now_str
            e["updated_at"] = now_str
            if screenshot_path:
                e["screenshot_path"] = str(screenshot_path)
            count += 1
        if count:
            self.save()
        return count


def _acquire_session_store_lock(path: Path, timeout: float = 15.0) -> Optional[tuple]:
    """Take an exclusive lock on the session store file.

    The two dual lanes are two processes that both update the session store.
    Without a lock the lane that writes last restores its own older snapshot
    and the sibling lane's record is lost. The lock is a file that only one
    process can create, and a lock left behind by a dead process is reclaimed
    after `timeout` so a run can never block forever.

    Returns (handle, lock_path) or None when the lock cannot be taken.
    """
    lock_path = Path(str(path) + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return handle, lock_path
        except FileExistsError:
            if time.monotonic() > deadline:
                try:
                    os.unlink(str(lock_path))
                except Exception:
                    pass
                deadline = time.monotonic() + timeout
                continue
            time.sleep(0.05)
        except Exception:
            return None


def _release_session_store_lock(lock: Optional[tuple]) -> None:
    """Release the lock taken by _acquire_session_store_lock."""
    if not lock:
        return
    handle, lock_path = lock
    try:
        os.close(handle)
    except Exception:
        pass
    try:
        os.unlink(str(lock_path))
    except Exception:
        pass


class SessionStore:
    """Per-project map of the Gemini conversation to continue in.
    The store is a small JSON file at ``~/.gemini/argon_sessions.json``, next to
    the browser profile, so every installation of this plugin shares it. The key
    is the ABSOLUTE, resolved project root; the lookup is case-insensitive. One
    chat per project: a new delivery replaces the previous entry.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or SESSION_STORE_PATH)
        self.data: Dict[str, Any] = {}
        # The store exists from the moment it is used: a missing file is
        # created with the default shape.
        self.load()

    # -------------------------------------------------------------- storage --
    def load(self) -> Dict[str, Any]:
        """Read the store, creating it with the default shape when missing."""
        raw: Optional[str] = None
        try:
            if self.path.is_file():
                raw = self.path.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[!] Could not read the session store: {exc}")
            raw = None

        data: Dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception as exc:
                print(f"[!] The session store is not valid JSON ({exc}); starting a fresh one.")
                data = {}

        data.setdefault("version", SESSION_STORE_VERSION)
        data.setdefault("description", SESSION_STORE_DESCRIPTION)
        if not isinstance(data.get("sessions"), dict):
            data["sessions"] = {}

        self.data = data
        if not self.path.is_file():
            self.save()
        return self.data

    def save(self) -> None:
        """Write the store back to disk.

        The content goes to a temporary file that is then moved into place, so
        a reader never sees half a file and two writers cannot corrupt it.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_name(self.path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(self.path))
        except Exception as exc:
            print(f"[!] Could not save the session store: {exc}")

    # ---------------------------------------------------------------- lookup --
    def _find_key(self, project_path: Any) -> Optional[str]:
        """Return the stored key that matches this project, if any."""
        sessions = self.data.get("sessions") or {}
        wanted = _normalize_project_key(project_path)
        for key in sessions:
            if _normalize_project_key(key) == wanted:
                return key
        return None

    def lookup(self, project_path: Any, mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the stored session for a project, or None when there is none."""
        self.load()
        key = self._find_key(project_path)
        if key is None:
            return None
        entry = (self.data.get("sessions") or {}).get(key)
        if not isinstance(entry, dict):
            return None
        if mode is not None:
            stored_mode = (entry.get("mode") or "gemini").strip().lower()
            wanted_mode = (mode or "gemini").strip().lower()
            if stored_mode != wanted_mode:
                return None
        return entry

    @staticmethod
    def normalize_winner_lane(winner: Optional[str]) -> Optional[str]:
        """Normalize a winner lane label ('followup'/'old' or 'new')."""
        cleaned = (winner or "").strip().lower()
        if cleaned in ("followup", "follow-up", "follow_up", "old"):
            return "followup"
        if cleaned in ("new", "new-session", "new_session"):
            return "new"
        return None

    def remember(
        self,
        project_path: Any,
        mode: str = "gemini",
        slot: Optional[str] = None,
        chat_id: str = "",
        chat_title: str = "",
        profile_dir: str = "",
        lane: Optional[str] = None,
        patch_path: str = "",
        workspace_mode: str = "",
    ) -> Optional[str]:
        """Store the conversation for a project (including its workspace_mode: 'task' / 'chat') and return its chat URL.

        The two dual lanes run as two processes and both write this store, so
        the read-modify-write is serialised with a lock file: otherwise the
        lane that writes last would put its own (older) snapshot back and wipe
        the sibling lane's record.
        """
        lock = _acquire_session_store_lock(self.path)
        try:
            return self._remember_locked(
                project_path=project_path,
                mode=mode,
                slot=slot,
                chat_id=chat_id,
                chat_title=chat_title,
                profile_dir=profile_dir,
                lane=lane,
                patch_path=patch_path,
                workspace_mode=workspace_mode,
            )
        finally:
            _release_session_store_lock(lock)

    def _remember_locked(
        self,
        project_path: Any,
        mode: str = "gemini",
        slot: Optional[str] = None,
        chat_id: str = "",
        chat_title: str = "",
        profile_dir: str = "",
        lane: Optional[str] = None,
        patch_path: str = "",
        workspace_mode: str = "",
    ) -> Optional[str]:
        """The body of remember(); the caller holds the store lock."""
        if not chat_id:
            return None
        self.load()
        try:
            resolved = str(Path(str(project_path)).expanduser().resolve())
        except Exception:
            resolved = str(project_path)

        clean_mode = (mode or "gemini").strip().lower()
        clean_ws_mode = (workspace_mode or "").strip().lower()
        if clean_ws_mode not in ("task", "chat"):
            clean_ws_mode = ""
        slot_part = normalize_slot(slot) if slot else "/u/0/"
        url_slot = "/u/0/" if _profile_is_isolated_slot(profile_dir) else slot_part
        chat_url = f"https://gemini.google.com{url_slot}app/{chat_id}"
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        sessions = self.data.setdefault("sessions", {})
        existing_key = self._find_key(resolved)
        prev_entry: Dict[str, Any] = {}
        if existing_key is not None and isinstance(sessions.get(existing_key), dict):
            prev_entry = dict(sessions[existing_key])
            if existing_key != resolved:
                sessions.pop(existing_key, None)

        lanes: Dict[str, Any] = {}
        if isinstance(prev_entry.get("lanes"), dict):
            for lk, lv in prev_entry["lanes"].items():
                if isinstance(lv, dict):
                    lanes[lk] = dict(lv)

        norm_lane = self.normalize_winner_lane(lane) if lane else None
        lane_record: Dict[str, Any] = {
            "slot": slot_part,
            "chat_id": str(chat_id),
            "chat_url": chat_url,
            "chat_title": str(chat_title) if chat_title else "",
            "profile_dir": str(profile_dir) if profile_dir else "",
            "profile_number": _slot_number(slot_part),
            "updated_at": now_str,
        }
        if clean_ws_mode:
            lane_record["workspace_mode"] = clean_ws_mode
        if patch_path:
            lane_record["patch_path"] = str(patch_path)

        if norm_lane:
            merged_lane = dict(lanes.get(norm_lane) or {})
            for k, v in lane_record.items():
                if v != "" or k not in merged_lane:
                    merged_lane[k] = v
            lanes[norm_lane] = merged_lane

        entry: Dict[str, Any] = dict(prev_entry)
        entry["mode"] = clean_mode
        entry["updated_at"] = now_str
        entry["lanes"] = lanes
        entry.setdefault("winner", prev_entry.get("winner"))

        if norm_lane is None or norm_lane == "followup" or not entry.get("chat_id"):
            entry["slot"] = slot_part
            entry["profile_number"] = _slot_number(slot_part)
            entry["chat_id"] = str(chat_id)
            entry["chat_url"] = chat_url
            if clean_ws_mode:
                entry["workspace_mode"] = clean_ws_mode
            elif str(prev_entry.get("chat_id") or "") != str(chat_id):
                entry.pop("workspace_mode", None)
            if chat_title:
                entry["chat_title"] = str(chat_title)
            if profile_dir:
                entry["profile_dir"] = str(profile_dir)

        sessions[resolved] = entry
        self.save()
        return chat_url

    def get_recorded_workspace_mode(
        self,
        project_path: Any,
        chat_id: str = "",
        lane: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> str:
        """Return the recorded workspace_mode ('task' or 'chat') for a stored conversation, or 'unknown' (E2.1, E2.2)."""
        entry = self.lookup(project_path, mode=mode)
        if not isinstance(entry, dict):
            return "unknown"
        wanted_cid = (chat_id or "").strip()
        lanes = entry.get("lanes") if isinstance(entry.get("lanes"), dict) else {}
        norm_lane = self.normalize_winner_lane(lane) if lane else None
        if norm_lane and isinstance(lanes.get(norm_lane), dict):
            l_data = lanes[norm_lane]
            l_cid = str(l_data.get("chat_id") or "").strip()
            l_ws = str(l_data.get("workspace_mode") or "").strip().lower()
            if l_ws in ("task", "chat") and (not wanted_cid or l_cid == wanted_cid):
                return l_ws
        if wanted_cid:
            for l_data in lanes.values():
                if isinstance(l_data, dict) and str(l_data.get("chat_id") or "").strip() == wanted_cid:
                    l_ws = str(l_data.get("workspace_mode") or "").strip().lower()
                    if l_ws in ("task", "chat"):
                        return l_ws
        top_cid = str(entry.get("chat_id") or "").strip()
        top_ws = str(entry.get("workspace_mode") or "").strip().lower()
        if top_ws in ("task", "chat") and (not wanted_cid or top_cid == wanted_cid):
            return top_ws
        return "unknown"

    def remember_dual_round(
        self,
        project_path: Any,
        mode: str = "gemini",
        followup_lane: Optional[Dict[str, Any]] = None,
        new_lane: Optional[Dict[str, Any]] = None,
        round_id: str = "",
        dual_dir: str = "",
        comparison_html: str = "",
        merged_patch: str = "",
    ) -> Dict[str, Any]:
        """Store both lanes ('followup' and 'new') of a dual round and reset winner to None."""
        self.load()
        try:
            resolved = str(Path(str(project_path)).expanduser().resolve())
        except Exception:
            resolved = str(project_path)

        sessions = self.data.setdefault("sessions", {})
        existing_key = self._find_key(resolved)
        entry: Dict[str, Any] = {}
        if existing_key is not None and isinstance(sessions.get(existing_key), dict):
            entry = dict(sessions[existing_key])
            if existing_key != resolved:
                sessions.pop(existing_key, None)

        lanes: Dict[str, Any] = {}
        if isinstance(entry.get("lanes"), dict):
            for lk, lv in entry["lanes"].items():
                if isinstance(lv, dict):
                    lanes[lk] = dict(lv)

        for lane_key, lane_input in (("followup", followup_lane), ("new", new_lane)):
            if isinstance(lane_input, dict) and lane_input:
                cur = dict(lanes.get(lane_key) or {})
                cur.update({k: v for k, v in lane_input.items() if v is not None})
                if cur.get("slot"):
                    cur["slot"] = normalize_slot(cur["slot"])
                    cur["profile_number"] = _slot_number(cur["slot"])
                lanes[lane_key] = cur

        entry["mode"] = (mode or "gemini").strip().lower()
        entry["lanes"] = lanes
        entry["winner"] = None
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if round_id:
            entry["round_id"] = str(round_id)
        if dual_dir:
            entry["dual_dir"] = str(dual_dir)
        if comparison_html:
            entry["comparison_html"] = str(comparison_html)
        if merged_patch:
            entry["merged_patch"] = str(merged_patch)

        f_lane = lanes.get("followup") or {}
        if f_lane.get("chat_id"):
            entry["slot"] = f_lane.get("slot") or entry.get("slot") or "/u/0/"
            entry["profile_number"] = _slot_number(entry["slot"])
            entry["chat_id"] = str(f_lane["chat_id"])
            entry["chat_url"] = str(f_lane.get("chat_url") or _url_for_chat_id(str(f_lane["chat_id"])))

        sessions[resolved] = entry
        self.save()
        return entry

    def record_winner(self, project_path: Any, winner: str) -> Optional[Dict[str, Any]]:
        """Record which lane ('followup' or 'new') won the round and promote its chat to top level."""
        norm_winner = self.normalize_winner_lane(winner)
        if norm_winner not in ("followup", "new"):
            raise ValueError(f"Invalid winner lane {winner!r}; expected 'followup' (or 'old') or 'new'.")

        self.load()
        key = self._find_key(project_path)
        if key is None:
            return None
        sessions = self.data.get("sessions") or {}
        entry = sessions.get(key)
        if not isinstance(entry, dict):
            return None

        lanes = entry.get("lanes") if isinstance(entry.get("lanes"), dict) else {}
        lanes = {k: dict(v) for k, v in lanes.items() if isinstance(v, dict)}
        winning_lane = dict(lanes.get(norm_winner) or {})

        if not winning_lane.get("chat_id") and entry.get("chat_id") and norm_winner == "followup":
            # The top-level chat IS the follow-up chat (remember() promotes it),
            # so it may stand in for that lane. It must never stand in for the
            # 'new' lane - that would record the follow-up chat as the winner
            # of the new session.
            slot_fallback = normalize_slot(entry.get("slot") or "/u/0/")
            winning_lane = {
                "slot": slot_fallback,
                "chat_id": str(entry["chat_id"]),
                "chat_url": str(entry.get("chat_url") or _url_for_chat_id(str(entry["chat_id"]))),
                "chat_title": str(entry.get("chat_title") or ""),
                "profile_dir": str(entry.get("profile_dir") or ""),
                "profile_number": _slot_number(slot_fallback),
            }
            lanes[norm_winner] = winning_lane

        if not winning_lane.get("chat_id"):
            return None

        win_slot = normalize_slot(winning_lane.get("slot") or entry.get("slot") or "/u/0/")
        win_cid = str(winning_lane["chat_id"])
        win_prof = str(winning_lane.get("profile_dir") or entry.get("profile_dir") or "")
        url_slot = "/u/0/" if _profile_is_isolated_slot(win_prof) else win_slot
        win_url = str(winning_lane.get("chat_url") or f"https://gemini.google.com{url_slot}app/{win_cid}")
        win_title = str(winning_lane.get("chat_title") or entry.get("chat_title") or "")
        win_ws_mode = str(winning_lane.get("workspace_mode") or entry.get("workspace_mode") or "").strip().lower()

        winning_lane["slot"] = win_slot
        winning_lane["profile_number"] = _slot_number(win_slot)
        winning_lane["chat_id"] = win_cid
        winning_lane["chat_url"] = win_url
        if win_title:
            winning_lane["chat_title"] = win_title
        if win_prof:
            winning_lane["profile_dir"] = win_prof
        if win_ws_mode in ("task", "chat"):
            winning_lane["workspace_mode"] = win_ws_mode
        lanes[norm_winner] = winning_lane

        entry["slot"] = win_slot
        entry["profile_number"] = _slot_number(win_slot)
        entry["chat_id"] = win_cid
        entry["chat_url"] = win_url
        if win_title:
            entry["chat_title"] = win_title
        if win_prof:
            entry["profile_dir"] = win_prof
        if win_ws_mode in ("task", "chat"):
            entry["workspace_mode"] = win_ws_mode
        entry["lanes"] = lanes
        entry["winner"] = norm_winner
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sessions[key] = entry
        self.save()
        return entry

    def get_winner(self, project_path: Any, mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return the recorded winning lane metadata for `project_path`, falling back to the stored project chat when unambiguous."""
        entry = self.lookup(project_path, mode=mode)
        if not isinstance(entry, dict):
            return None
        lanes = entry.get("lanes") if isinstance(entry.get("lanes"), dict) else {}
        norm_winner = self.normalize_winner_lane(entry.get("winner"))
        if norm_winner not in ("followup", "new"):
            if isinstance(lanes.get("followup"), dict) and str(lanes["followup"].get("chat_id") or "").strip():
                norm_winner = "followup"
            elif str(entry.get("chat_id") or "").strip():
                norm_winner = "followup"
            elif isinstance(lanes.get("new"), dict) and str(lanes["new"].get("chat_id") or "").strip():
                norm_winner = "new"
            else:
                return None
        lane_data = lanes.get(norm_winner) if isinstance(lanes.get(norm_winner), dict) else {}
        chat_id = str(lane_data.get("chat_id") or entry.get("chat_id") or "").strip()
        if not chat_id:
            return None
        slot_val = normalize_slot(lane_data.get("slot") or entry.get("slot") or "/u/0/")
        prof_val = str(lane_data.get("profile_dir") or entry.get("profile_dir") or "")
        url_slot = "/u/0/" if _profile_is_isolated_slot(prof_val) else slot_val
        chat_url = str(lane_data.get("chat_url") or entry.get("chat_url") or f"https://gemini.google.com{url_slot}app/{chat_id}")
        ws_mode = str(lane_data.get("workspace_mode") or entry.get("workspace_mode") or "unknown").strip().lower()
        if ws_mode not in ("task", "chat"):
            ws_mode = "unknown"
        return {
            "winner": norm_winner,
            "slot": slot_val,
            "profile_number": _slot_number(slot_val),
            "chat_id": chat_id,
            "chat_url": chat_url,
            "chat_title": str(lane_data.get("chat_title") or entry.get("chat_title") or ""),
            "profile_dir": prof_val,
            "workspace_mode": ws_mode,
        }


# --------------------------------------------------------------------------- #
# Bilingual UI selectors                                                      #
#                                                                           #
# The Gemini UI renders in the language of the signed-in Google account. This #
# pool is Bangla, so English-only text selectors silently miss: the run then #
# stays in Chat mode, keeps the wrong model, and sends a context-free prompt #
# that Gemini answers with "the zip did not come through". Every matcher     #
# below therefore accepts both languages.                                      #
# --------------------------------------------------------------------------- #

RE_TASK = re.compile(r"^\s*(task|টাস্ক)\s*$", re.IGNORECASE)
RE_CHAT = re.compile(r"^\s*(chat|চ্যাট|চাট)\s*$", re.IGNORECASE)
# The Bangla model label carries a virama: আর্গন  (NOT আরগন).
# The word must stand on its own, otherwise a chat title such as
# "Argon Parallel Isolation" or "Argonaut" would count as the model.
RE_ARGON = re.compile(
    r"(4\s*argon|gemini[\s\-_]*4[\s\-_]*argon|(?<![\w\-])argon(?![\w\-])|আর\u09cd?গন)",
    re.IGNORECASE,
)
RE_UPLOAD = re.compile(
    r"(upload|attach|file|আপলোড|ডিজাইসো|ফাইল|সংযুক্ত|টুল)", re.IGNORECASE
)
RE_DOWNLOAD = re.compile(
    r"(download|ডাউনলোড|ডাউনলোড করুন|save|সংরক্ষণ)", re.IGNORECASE
)
RE_THINK_HIGH = re.compile(r"(^|\b)(high|উচ্চ|হাই)(\b|$)", re.IGNORECASE)
RE_PLUS = re.compile(r"^\s*\+\s*$")
RE_THINKING = re.compile(r"(thinking|চিন্তা|reasoning)", re.IGNORECASE)

# Gemini shows a "limited" state when the signed-in account reaches its usage
# window (the 3-4 hour limit). The dispatcher must then move on to the next
# pool account instead of waiting for an answer that will never come.
QUOTA_MARKERS = (
    "সীমাবদ্ধ",
    "siecabddh",
    "you've reached your limit",
    "you have reached your limit",
    "reached the limit",
    "too many requests",
    "rate limit",
    "quota exceeded",
    "upgrade to pro",
    "upgrade to google ai",
    "temporarily unavailable",
)


def _detect_quota_limit(page) -> Optional[str]:
    """Return a description when the account has hit its usage limit."""
    try:
        text = page.locator("body").inner_text(timeout=5000) or ""
    except Exception:
        text = ""
    low = text.lower()
    for marker in QUOTA_MARKERS:
        if marker.lower() in low:
            return marker
    # The model picker marks a limited model entry.
    try:
        limited = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll("[role=menuitem],[role=option],mat-option,li")
                  .forEach(e => {
                    if (!(e.offsetWidth || e.offsetHeight)) return;
                    const t = (e.innerText || '').trim();
                    if (t && t.length < 40 && /(limited|সীমাবদ্ধ)/i.test(t)) out.push(t);
                  });
                return out.slice(0, 3);
            }"""
        )
        if limited:
            return limited[0]
    except Exception:
        pass
    return None
# The Material segmented control marks the active segment on its PARENT.
RE_SEGMENT_SELECTED = re.compile(r"gem-segmented-button-selected", re.IGNORECASE)


def _click_matching(page, pattern, prefer_menu: bool = True) -> bool:
    """Click the first visible element whose text matches `pattern`.

    When `prefer_menu` is set, an element that looks like a menu entry
    (menuitem / option / listbox role) is preferred over a plain text node.
    """
    try:
        loc = page.get_by_text(pattern)
        count = min(loc.count(), 25)
        candidates = []
        for i in range(count):
            el = loc.nth(i)
            try:
                if not el.is_visible(timeout=600):
                    continue
                role = ""
                try:
                    role = (el.get_attribute("role") or "") + " " + (el.get_attribute("aria-label") or "")
                except Exception:
                    role = ""
                is_menu = any(k in role for k in ("menuitem", "option", "listbox", "menu"))
                candidates.append((0 if (is_menu and prefer_menu) else 1, i, el))
            except Exception:
                continue
        candidates.sort(key=lambda c: (c[0], c[1]))
        for _, _, el in candidates:
            try:
                el.click(timeout=3000)
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _element_state(el) -> str:
    """Collect the state attributes used to tell a selected control from an idle one."""
    try:
        return " ".join(
            str(el.get_attribute(a) or "")
            for a in ("aria-pressed", "aria-selected", "aria-current", "class", "data-state")
        ).lower()
    except Exception:
        return ""


def _mode_is_task(page) -> bool:
    """Return True ONLY when the Chat/Task toggle is rendered on the page AND shows Task selected (E2).

    Never returns True when no toggle is rendered: a missing toggle means the
    page's mode cannot be observed from the toggle alone (it may be an existing
    Chat-mode conversation or a half-rendered page).
    Returns False immediately if the visible model chip says 'Argon High'
    (which proves Chat mode with thinking level).
    """
    try:
        chip = _model_chip(page)
        if chip is not None:
            c_name = (_model_name(chip) or "").strip().lower()
            if "high" in c_name:
                return False
    except Exception:
        pass
    try:
        task = page.get_by_text(RE_TASK)
        for i in range(min(task.count(), 6)):
            el = task.nth(i)
            if not _safe_visible(el):
                continue
            try:
                marked = el.evaluate(
                    """e => {
                        let n = e;
                        for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
                            const c = (n.getAttribute && (n.getAttribute('class') || '')) || '';
                            const ap = (n.getAttribute && (n.getAttribute('aria-pressed') || '')) || '';
                            const asel = (n.getAttribute && (n.getAttribute('aria-selected') || '')) || '';
                            const achk = (n.getAttribute && (n.getAttribute('aria-checked') || '')) || '';
                            if (/gem-segmented-button-selected/.test(c) || ap === 'true' || asel === 'true' || achk === 'true') {
                                return c || 'selected';
                            }
                        }
                        return '';
                    }"""
                )
            except Exception:
                marked = ""
            if marked:
                return True
    except Exception:
        pass
    return False


def _observe_toggle_mode(page) -> str:
    """Observe the visible Chat/Task toggle and return 'task', 'chat', or 'unknown' (E2)."""
    if _mode_is_task(page):
        return "task"
    try:
        chat_btn = page.get_by_text(RE_CHAT)
        for i in range(min(chat_btn.count(), 6)):
            el = chat_btn.nth(i)
            if not _safe_visible(el):
                continue
            try:
                marked = el.evaluate(
                    """e => {
                        let n = e;
                        for (let i = 0; i < 4 && n; i++, n = n.parentElement) {
                            const c = (n.getAttribute && (n.getAttribute('class') || '')) || '';
                            const ap = (n.getAttribute && (n.getAttribute('aria-pressed') || '')) || '';
                            const asel = (n.getAttribute && (n.getAttribute('aria-selected') || '')) || '';
                            if (/gem-segmented-button-selected/.test(c) || ap === 'true' || asel === 'true') {
                                return 'chat';
                            }
                        }
                        return '';
                    }"""
                )
            except Exception:
                marked = ""
            if marked:
                return "chat"
    except Exception:
        pass
    if _task_toggle_present(page):
        return "chat"
    return "unknown"


def _retry(fn, page, attempts: int = 3, delay: float = 2.0) -> bool:
    """Run a pre-flight step a few times with automatic crash recovery (D17).

    If the browser or page closes during pre-flight or attach, invokes
    `page._argon_reopen_hook` when present so the browser is restarted and
    the step continues on the reopened conversation.
    """
    cur_page = page
    for attempt in range(attempts):
        try:
            if hasattr(cur_page, "is_closed") and cur_page.is_closed():
                raise RuntimeError("Target page, context or browser has been closed")
            if fn(cur_page):
                return True
            if _argon_picker_is_disabled(cur_page):
                return False
        except Exception as exc:
            if _is_target_closed_error(exc) or (hasattr(cur_page, "is_closed") and cur_page.is_closed()):
                reopen_hook = getattr(cur_page, "_argon_reopen_hook", None)
                if callable(reopen_hook):
                    recovered_page = reopen_hook(exc, getattr(fn, "__name__", "preflight_step"))
                    if recovered_page is not None:
                        cur_page = recovered_page
                        continue
        if attempt < attempts - 1:
            try:
                cur_page.wait_for_timeout(int(delay * 1000))
            except Exception:
                time.sleep(delay)
    return False


def _wait_for_workspace(page, timeout: float = 45.0, interval: float = 1.0) -> bool:
    """Wait until the Gemini composer is ready to accept a prompt.

    A fresh chat renders its composer quickly. An existing conversation loads
    the whole transcript first and can take ten seconds or more, so the
    pre-flight must not run against a half-rendered page.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _find_composer(page) is not None:
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(int(interval * 1000))
        except Exception:
            time.sleep(interval)
    return False


def _safe_visible(el) -> bool:
    try:
        return bool(el.is_visible(timeout=500))
    except Exception:
        return False


def _inside_navigation(el) -> bool:
    """True when the element sits in the sidebar / chat-history region."""
    try:
        return bool(
            el.evaluate(
                """e => {
                    let n = e;
                    for (let i = 0; i < 12 && n; i++, n = n.parentElement) {
                        const role = (n.getAttribute && (n.getAttribute('role') || '')) || '';
                        const tag = (n.tagName || '').toLowerCase();
                        const cls = (n.getAttribute && (n.getAttribute('class') || '')) || '';
                        const id = (n.id || '');
                        if (role === 'navigation' || tag === 'nav' || tag === 'aside' ||
                            /history|conversation|chat[-_ ]?list|sidebar/i.test(cls + ' ' + role + ' ' + id)) {
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        return False


def _composer_scope(page):
    """Return the container Locator that holds the prompt box AND the model chip.

    A loaded conversation renders the composer twice: the real one at the
    bottom and a floating quick-action bar in the middle of the page. Both
    carry a model chip, so every control lookup has to be scoped to the
    composer that also owns the prompt box.
    """
    composer = _find_composer(page)
    if composer is None:
        return None
    for xpath in (
        "xpath=ancestor::*[.//button[contains(@class, 'input-area-switch')]][1]",
        "xpath=ancestor::*[contains(@class, 'input-area')][1]",
        "xpath=ancestor::*[position()=2]",
    ):
        try:
            anc = composer.locator(xpath)
            if anc.count() > 0:
                cand = anc.first
                if hasattr(cand, "locator"):
                    return cand
        except Exception:
            continue
    return None


def _scoped_roots(page):
    """The composer first, then the whole page as a fallback."""
    roots = []
    scope = _composer_scope(page)
    if scope is not None and hasattr(scope, "locator"):
        roots.append(scope)
    roots.append(page)
    return roots


def _model_chip(page):
    """The composer control that opens the model picker.

    On the current Gemini UI it is `button.input-area-switch`; its label reads
    "Open mode picker, currently <model>" and its text is the model name.
    """
    for root in _scoped_roots(page):
        for sel in (
            "button.input-area-switch",
            "button[aria-haspopup='true'].input-area-switch",
            "button.input-area-switch[aria-haspopup]",
        ):
            try:
                loc = root.locator(sel)
                for i in range(min(loc.count(), 4)):
                    el = loc.nth(i)
                    if _safe_visible(el):
                        return el
            except Exception:
                continue
    return None


def _model_name(el) -> str:
    """The model name the chip actually shows.

    The chip text is the model itself ("Flash", "আর্গন", "4 Argon"). Its
    aria-label wraps it ("Open mode picker, currently Flash"), so the label is
    only used when there is no visible text - and only the part after
    "currently" is taken from it.
    """
    if el is None:
        return ""
    try:
        text = (el.inner_text(timeout=500) or "").strip()
    except Exception:
        text = ""
    if text:
        return text
    try:
        label = str(el.get_attribute("aria-label") or "").strip()
    except Exception:
        label = ""
    if not label:
        return ""
    m = re.search(r"currently\s+(.+)$", label, re.IGNORECASE)
    return (m.group(1) if m else label).strip()


def _chip_is_argon(el) -> bool:
    """True when this chip shows the Argon model.

    Only the chip's OWN model name counts. A chip that says "Flash" is not
    Argon, no matter what else the page says.
    """
    if el is None:
        return False
    name = _model_name(el)
    if not name:
        return False
    return bool(RE_ARGON.search(name))


def _argon_visible(page) -> bool:
    """True when the MODEL is Argon.

    The model chip is the ground truth: it shows the model that will actually
    answer. A chat history entry can also contain the words "4 Argon", and the
    OPEN model picker shows every model at once - so "4 Argon" being visible in
    the menu proves nothing while "3.8 Flash" is the checked entry. That false
    positive sent real tasks to Flash, so it must not happen again.

    Therefore:
      1. the chip has to name Argon, or
      2. the open picker has to have Argon as the CHECKED entry.
    """
    # 1. The model chip - the ground truth.
    chip = _model_chip(page)
    if chip is not None and _chip_is_argon(chip):
        return True

    # 2. A selected option that names Argon (the picker is open and Argon is
    #    the checked entry).
    try:
        opts = page.locator(
            "[role='option'][aria-selected='true'], [role='menuitem'][aria-checked='true'], "
            "[role='menuitem'].selected, [role='option'].selected"
        )
        for i in range(min(opts.count(), 8)):
            el = opts.nth(i)
            if not _safe_visible(el):
                continue
            try:
                if RE_ARGON.search(el.inner_text(timeout=400) or ""):
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # 3. REMOVED on purpose: "any visible Argon text" used to be accepted here.
    #    That is exactly what sent tasks to Flash - the OPEN model picker shows
    #    "4 Argon" as a line even while "3.8 Flash" is the checked model, and a
    #    chat history entry can read "Argon ..." as well. The chip (1) and the
    #    checked picker entry (2) are the only trustworthy signals.
    return False


def select_task_mode(page, require_verifiable: bool = False,
                     wait_for_toggle_seconds: float = 30.0) -> bool:
    """Switch Gemini Web into Task mode. Returns True only when verifiable.

    On an EXISTING conversation there is no Chat/Task toggle at all: the chat
    already runs in the mode it was created with, so there is nothing to switch
    and the inherited mode is accepted.

    On a BRAND-NEW chat there is NO inherited mode, so "the toggle is not
    there" must never be accepted as "Task mode": the toggle can simply not
    have rendered yet. MEASURED 2026-11-04: the owner kept getting prompts
    that went to Chat mode because this function returned True without ever
    proving the mode. With `require_verifiable=True` the toggle is WAITED for
    (bounded) and the run FAILS LOUDLY when the mode cannot be proven.
    """
    waited = 0.0
    while True:
        state = _select_task_mode_once(page)
        if state is True:
            return True
        if state is None:
            # The toggle is not on the page at all. On a continuation the chat
            # keeps the mode it was created with. On a NEW chat the mode must
            # be proven, so we wait for the toggle to render.
            if not require_verifiable:
                print("[+] No Chat/Task toggle on this page - the conversation keeps its mode.")
                return True
            if waited >= max(0.0, float(wait_for_toggle_seconds)):
                break
            waited += 3.0
            try:
                page.wait_for_timeout(3000)
            except Exception:
                time.sleep(3.0)
            continue
        # The toggle IS there but Task mode could not be confirmed.
        return False
    if require_verifiable:
        print(
            "[!] The Chat/Task toggle never appeared, so Task mode could NOT be "
            "verified on a NEW chat. Refusing to send - a prompt sent in Chat "
            "mode cannot do the work.",
            file=sys.stderr,
        )
    return False


def _select_task_mode_once(page):
    """One attempt at switching to Task mode. See select_task_mode().

    Returns True when Task mode is PROVEN, False when the toggle is there but
    the mode could not be confirmed, and None when the toggle is not on the
    page at all (the chat keeps the mode it was created with).
    """
    try:
        task_btn = page.get_by_text(RE_TASK)
        toggle_found = False
        for i in range(min(task_btn.count(), 10)):
            el = task_btn.nth(i)
            if not _safe_visible(el):
                continue
            toggle_found = True
            try:
                el.click(timeout=3000)
                page.wait_for_timeout(1400)
                if _mode_is_task(page):
                    return True
            except Exception:
                continue
        if not toggle_found:
            return None
    except Exception:
        pass
    return False


def _open_picker(page) -> bool:
    """Open a model / thinking-level picker by clicking a haspopup control."""
    for sel in (
        "button[aria-haspopup='listbox']",
        "button[aria-haspopup='menu']",
        "[role='button'][aria-haspopup='listbox']",
        "[role='button'][aria-haspopup='menu']",
    ):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                try:
                    el.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


_LAST_PICKER_ARGON_STATE: Dict[int, Dict[str, Any]] = {}


def _detect_signed_in_account(
    page: Any,
    fallback_email: Optional[str] = None,
    slot: Optional[str] = None,
) -> str:
    """Read which Google account is currently signed into the page."""
    try:
        avatars = page.locator(
            "a[aria-label*='Google Account'], button[aria-label*='Google Account'], "
            "a[aria-label*='Google অ্যাকাউন্ট'], button[aria-label*='Google অ্যাকাউন্ট'], "
            "[aria-label*='@']"
        )
        for i in range(min(avatars.count(), 6)):
            el = avatars.nth(i)
            for attr in ("aria-label", "title", "data-email"):
                try:
                    val = str(el.get_attribute(attr) or "").strip()
                except Exception:
                    val = ""
                if not val:
                    continue
                m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", val)
                if m:
                    return m.group(0)
    except Exception:
        pass
    if fallback_email:
        return str(fallback_email).strip()
    if slot:
        return f"slot {slot}"
    return "current profile account"


def _element_is_disabled(el) -> bool:
    """Return True when a picker option (or its enclosing menuitem/option) is disabled."""
    try:
        if (el.get_attribute("aria-disabled") or "").strip().lower() == "true":
            return True
    except Exception:
        pass
    try:
        if el.get_attribute("disabled") is not None:
            return True
    except Exception:
        pass
    try:
        if bool(
            el.evaluate(
                r"""node => {
                    let cur = node;
                    for (let depth = 0; depth < 5 && cur; depth++, cur = cur.parentElement) {
                        const role = (cur.getAttribute && (cur.getAttribute('role') || '')).toLowerCase();
                        if (role === 'listbox' || role === 'menu') break;
                        const ariaDis = (cur.getAttribute && (cur.getAttribute('aria-disabled') || '')).toLowerCase();
                        if (ariaDis === 'true') return true;
                        if (cur.hasAttribute && cur.hasAttribute('disabled')) return true;
                        if (cur.disabled === true) return true;
                        const cls = (cur.getAttribute && (cur.getAttribute('class') || '')) || '';
                        if (/(?:^|\s|-)disabled(?:\s|$)/i.test(cls)) return true;
                    }
                    return false;
                }"""
            )
        ):
            return True
    except Exception:
        pass
    return False


def _inspect_option_in_picker(page, pattern) -> str:
    """Inspect and click a matching option inside the open picker.

    Returns:
      - 'clicked'  : a matching enabled option was found and clicked.
      - 'disabled' : a matching option was present in the picker, but disabled
                     (aria-disabled='true' or disabled attribute) - meaning the
                     account has no Argon quota/tier available right now.
      - 'missing'  : no matching option was found in the picker at all (meaning
                     the picker did not open).
    """
    saw_disabled_match = False
    for sel in (
        "[role='listbox'] [role='option']",
        "[role='menu'] [role='menuitem']",
        "[role='menu'] [role='menuitemradio']",
        "mat-option",
        "li[role='option']",
        "[role='listbox'] *",
        "[role='menu'] *",
    ):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 30)):
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                try:
                    text = (el.inner_text(timeout=500) or "").strip()
                    label = str(el.get_attribute("aria-label") or "")
                except Exception:
                    continue
                if not (pattern.search(text) or pattern.search(label)):
                    continue
                if _element_is_disabled(el):
                    saw_disabled_match = True
                    continue
                try:
                    el.click(timeout=3000)
                    return "clicked"
                except Exception:
                    continue
        except Exception:
            continue
    if saw_disabled_match:
        return "disabled"
    return "missing"


def _click_option_in_picker(page, pattern) -> bool:
    """Click a matching option INSIDE the open picker.

    The sidebar chat history can also contain the text "4 Argon" (an old chat
    title). Clicking that would navigate away from the composer, so a global
    text match is only allowed when it really sits inside a listbox / menu.
    """
    return _inspect_option_in_picker(page, pattern) == "clicked"


def _argon_picker_is_disabled(page) -> bool:
    """Return True when the most recent model check saw '4 Argon' present but disabled."""
    info = _LAST_PICKER_ARGON_STATE.get(id(page)) or {}
    return bool(info.get("disabled"))


def ensure_argon_model(page, attempts: int = 3, wait_for_chip_seconds: float = 45.0,
                       require_chip: bool = True) -> bool:
    """THE double check: the model must be 4 Argon before anything is sent.

    Sending a task to Flash is the single worst failure this bridge can have -
    the work is lost and the quota is wasted. So the model is verified against
    the CHIP, which shows the model that will really answer, and not against
    loose text on the page.

    Distinguishes a disabled '4 Argon' option (aria-disabled='true' / disabled,
    meaning this account has no Argon available right now and the bridge must
    rotate to the next pool account) from a missing option (meaning the picker
    did not open).

    MEASURED 2026-11-04: the chip is rendered by the page itself, so right
    after a load it can simply be NOT THERE YET. That is not a failed check -
    the run must WAIT for it (bounded by `wait_for_chip_seconds`) instead of
    aborting a healthy account. Before this, a lane died in pre-flight with
    "model = 4 Argon : FAILED, model chip: (not visible)" while the screenshot
    showed a perfectly working, signed-in Gemini page.

    Returns True only when the chip reads Argon.
    """
    _LAST_PICKER_ARGON_STATE.pop(id(page), None)
    last_seen = ""
    last_picker_state = "missing"
    waited = 0.0
    attempt = 0
    while True:
        chip = _model_chip(page)
        if chip is None:
            if not require_chip:
                # MEASURED 2026-11-04: on an EXISTING conversation the model
                # switcher is not rendered at all - the composer reads
                # "Ask a follow-up" and there is no chip. The chat keeps the
                # model it was created with, so a missing chip is NOT a
                # failure; treating it as one aborted a healthy follow-up.
                print(
                    "[+] No model chip on this conversation - it keeps the model "
                    "it was created with (verified when the chat was started).",
                    file=sys.stderr,
                )
                return True
            # The chip has not rendered yet. Wait for it - but only for a
            # bounded time, and only when it is really absent.
            if waited >= max(0.0, float(wait_for_chip_seconds)):
                break
            waited += 3.0
            try:
                page.wait_for_timeout(3000)
            except Exception:
                time.sleep(3.0)
            continue
        if attempt >= max(1, attempts):
            break
        attempt += 1
        name = _model_name(chip)
        if name:
            last_seen = name
        if _chip_is_argon(chip):
            if attempt > 1:
                print(f"[+] Model switched to Argon ({name!r}).")
            return True
        # Not Argon (or no chip at all): open the picker and pick it.
        if chip is not None:
            try:
                chip.click(timeout=4000)
                page.wait_for_timeout(1200)
            except Exception:
                pass
        picker_state = _inspect_option_in_picker(page, RE_ARGON)
        last_picker_state = picker_state
        try:
            page.wait_for_timeout(1400)
        except Exception:
            time.sleep(1.4)
        # Close the picker again - an open picker must never be left behind,
        # and its menu text must not be mistaken for the selected model.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
        if picker_state == "disabled":
            acct = _detect_signed_in_account(page)
            _LAST_PICKER_ARGON_STATE[id(page)] = {
                "disabled": True,
                "account": acct,
                "chip": last_seen,
            }
            print(
                f"[!] The '4 Argon' option is present in the model picker but DISABLED "
                f"(aria-disabled) on account {acct}: this account has no Argon available right now.",
                file=sys.stderr,
            )
            return False
        chip2 = _model_chip(page)
        if chip2 is not None:
            name2 = _model_name(chip2)
            if name2:
                last_seen = name2
            if _chip_is_argon(chip2):
                print(f"[+] Model switched to Argon ({name2!r}).")
                return True
    if last_picker_state == "missing":
        print(
            f"[!] The '4 Argon' option was not found in the model picker (the picker may not "
            f"have opened) and the model chip reads {last_seen!r} - refusing to send.",
            file=sys.stderr,
        )
    else:
        print(
            f"[!] The model chip reads {last_seen!r} and not 4 Argon - refusing to send.",
            file=sys.stderr,
        )
    return False


def select_argon_model(page, require_chip: bool = True) -> bool:
    """Select the 4 Argon model. Returns True only when verifiable.

    The verification is delegated to `ensure_argon_model`, so the pre-flight
    and the gate right before the send use exactly the same rule.

    `require_chip=False` is for a CONTINUATION: such a conversation has no
    model switcher at all, so the model is inherited from the chat.
    """
    return ensure_argon_model(page, require_chip=require_chip)


def _picker_open(page) -> bool:
    """True when a listbox / menu panel is currently visible."""
    for sel in ("[role='listbox']", "[role='menu']", "mat-option", "[role='dialog']"):
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 6)):
                if _safe_visible(loc.nth(i)):
                    return True
        except Exception:
            continue
    return False


def _wait_for_model_chip(page, timeout: float = 45.0, interval: float = 1.0):
    """Wait up to `timeout` seconds for the model chip to render on a half-rendered page (D8.2)."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    while True:
        chip = _model_chip(page)
        if chip is not None:
            return chip
        if time.monotonic() >= deadline:
            return None
        try:
            page.wait_for_timeout(int(interval * 1000))
        except Exception:
            time.sleep(interval)


def _task_toggle_present(page) -> bool:
    """True when the page renders a visible Chat/Task toggle."""
    try:
        task_btn = page.get_by_text(RE_TASK)
        for i in range(min(task_btn.count(), 6)):
            if _safe_visible(task_btn.nth(i)):
                return True
    except Exception:
        pass
    return False


def select_task_mode(
    page,
    require_verifiable: Optional[bool] = None,
    wait_for_toggle_seconds: float = 30.0,
    recorded_workspace_mode: Optional[str] = None,
) -> bool:
    """Switch Gemini Web into Task mode and verify it (E2).

    Rules (E2.1 - E2.4):
      - On a NEW chat, the Chat/Task toggle must be present, switched to Task,
        and observed in Task mode before any prompt is sent.
      - On a CONTINUATION, if the toggle is visible and shows Task, Task mode is
        observed directly. If the toggle is absent, Task mode is accepted ONLY
        when the conversation's recorded `workspace_mode` in SessionStore is
        `'task'`. If the recorded mode is `'unknown'` or `'chat'`, reports
        `mode = Task : UNKNOWN` and returns False (never claims `mode = Task : OK`).
    """
    if require_verifiable is None:
        is_cont = bool(getattr(page, "_argon_is_continuation", False))
        require_verifiable = not is_cont

    rec_mode = (
        recorded_workspace_mode
        if recorded_workspace_mode is not None
        else str(getattr(page, "_argon_recorded_workspace_mode", "") or "")
    ).strip().lower()

    chip_el = _model_chip(page)
    chip_text = (_model_name(chip_el) or "").strip() if chip_el is not None else ""
    if chip_text:
        setattr(page, "_argon_chip_seen", chip_text)
    if chip_text and "high" in chip_text.lower():
        setattr(page, "_argon_workspace_mode", "chat")
        setattr(page, "_argon_mode_status", f"REFUSED (chip='{chip_text}')")
        print(
            f"[!] Mode = Chat : REFUSED (chip={chip_text!r} indicates Chat mode thinking level) - "
            "refusing to send in Chat mode.",
            file=sys.stderr,
        )
        return False

    deadline = time.monotonic() + (max(2.0, float(wait_for_toggle_seconds)) if require_verifiable else 4.0)
    while True:
        try:
            if _mode_is_task(page):
                c_now = getattr(page, "_argon_chip_seen", "")
                c_tag = f", chip='{c_now}'" if c_now else ""
                setattr(page, "_argon_workspace_mode", "task")
                setattr(page, "_argon_mode_status", f"OK (observed toggle=Task{c_tag})")
                return True
            task_btn = page.get_by_text(RE_TASK)
            toggle_found = False
            for i in range(min(task_btn.count(), 10)):
                el = task_btn.nth(i)
                if not _safe_visible(el):
                    continue
                toggle_found = True
                try:
                    el.click(timeout=3000)
                    page.wait_for_timeout(1400)
                    if _mode_is_task(page):
                        setattr(page, "_argon_workspace_mode", "task")
                        setattr(page, "_argon_mode_status", "OK (switched toggle=Task)")
                        return True
                except Exception:
                    continue
            if not toggle_found and not require_verifiable:
                if rec_mode == "task":
                    setattr(page, "_argon_workspace_mode", "task")
                    setattr(page, "_argon_mode_status", "OK (recorded workspace_mode=task)")
                    print(
                        "[+] Mode = Task : OK (continued conversation has recorded workspace_mode=task)."
                    )
                    return True
                break
        except Exception:
            pass
        if time.monotonic() >= deadline:
            break
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1.0)

    observed = _observe_toggle_mode(page)
    if not require_verifiable and observed == "unknown":
        setattr(page, "_argon_workspace_mode", rec_mode or "unknown")
        setattr(page, "_argon_mode_status", "UNKNOWN")
        print(
            f"[!] Mode = Task : UNKNOWN (continued conversation has recorded workspace_mode={rec_mode or 'unknown'!r} "
            "and no Chat/Task toggle is rendered on the page). Task mode is NOT proven.",
            file=sys.stderr,
        )
        return False

    setattr(page, "_argon_workspace_mode", observed)
    setattr(page, "_argon_mode_status", "UNKNOWN" if observed == "unknown" else f"FAILED (observed mode={observed})")
    print(
        f"[!] Mode = Task : {'UNKNOWN' if observed == 'unknown' else 'FAILED'} "
        f"(Chat/Task toggle could not be verified as Task after {int(wait_for_toggle_seconds)}s; observed={observed!r}) - "
        "refusing to send in Chat mode.",
        file=sys.stderr,
    )
    return False


def ensure_argon_model(
    page,
    attempts: int = 3,
    require_chip: Optional[bool] = None,
    wait_for_chip_seconds: float = 45.0,
) -> bool:
    """Verify 4 Argon model: waits for chip on half-rendered pages (D8.2) and inherits on continuation when chip is absent (D11)."""
    if require_chip is None:
        is_cont = bool(getattr(page, "_argon_is_continuation", False))
        require_chip = not is_cont

    _LAST_PICKER_ARGON_STATE.pop(id(page), None)
    last_seen = ""
    last_picker_state = "missing"

    # D8.2: When the chip is absent right after page load, wait boundedly for it to render.
    chip_wait = float(wait_for_chip_seconds) if require_chip else min(8.0, float(wait_for_chip_seconds))
    initial_chip = _wait_for_model_chip(page, timeout=chip_wait)
    if initial_chip is None and not require_chip:
        print(
            "[+] Model = 4 Argon : INHERITED from existing conversation "
            "(no model switcher chip rendered on continuation chat)."
        )
        return True

    for attempt in range(max(1, attempts)):
        chip = _model_chip(page)
        if chip is None and attempt == 0:
            chip = initial_chip
        if chip is not None:
            name = _model_name(chip)
            if name:
                last_seen = name
            if _chip_is_argon(chip):
                if attempt:
                    print(f"[+] Model switched to Argon ({name!r}).")
                return True
            try:
                chip.click(timeout=4000)
                page.wait_for_timeout(1200)
            except Exception:
                pass
        else:
            _open_picker(page)

        picker_state = _inspect_option_in_picker(page, RE_ARGON)
        last_picker_state = picker_state
        try:
            page.wait_for_timeout(1400)
        except Exception:
            time.sleep(1.4)
        try:
            if _picker_open(page):
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
        except Exception:
            pass
        if picker_state == "disabled":
            acct = _detect_signed_in_account(page)
            _LAST_PICKER_ARGON_STATE[id(page)] = {
                "disabled": True,
                "account": acct,
                "chip": last_seen,
            }
            print(
                f"[!] The '4 Argon' option is present in the model picker but DISABLED "
                f"(aria-disabled) on account {acct}: this account has no Argon available right now.",
                file=sys.stderr,
            )
            return False
        chip2 = _model_chip(page)
        if chip2 is not None:
            name2 = _model_name(chip2)
            if name2:
                last_seen = name2
            if _chip_is_argon(chip2):
                print(f"[+] Model switched to Argon ({name2!r}).")
                return True

    if not last_seen and not require_chip:
        print("[+] Model = 4 Argon : INHERITED from existing conversation.")
        return True
    if last_picker_state == "missing":
        print(
            f"[!] The '4 Argon' option was not found in the model picker "
            f"and the model chip reads {last_seen or '(not visible)'!r} - refusing to send.",
            file=sys.stderr,
        )
    else:
        print(
            f"[!] The model chip reads {last_seen!r} and not 4 Argon - refusing to send.",
            file=sys.stderr,
        )
    return False


def select_argon_model(
    page,
    require_chip: Optional[bool] = None,
    wait_for_chip_seconds: float = 45.0,
) -> bool:
    """Select and verify the 4 Argon model (delegates to ensure_argon_model)."""
    return ensure_argon_model(
        page,
        require_chip=require_chip,
        wait_for_chip_seconds=wait_for_chip_seconds,
    )


def _inspect_zip_archive(zip_path: Path) -> tuple[bool, int, int, str]:
    """Verify that `zip_path` exists, is non-empty, opens as a valid zip, contains readable TASK_BRIEF.md and API_INDEX.md, and has no binary poison (E1, D14, D15).

    Returns (ok, size_bytes, entry_count, error_reason).
    """
    import zipfile as _zf
    from package_context import DEFAULT_EXCLUDED_EXTS as _BIN_EXTS

    zp = Path(zip_path)
    if not zp.is_file():
        return False, 0, 0, f"archive does not exist: {zp}"
    try:
        sz = zp.stat().st_size
    except OSError as exc:
        return False, 0, 0, f"could not stat archive {zp}: {exc}"
    if sz <= 0:
        return False, 0, 0, f"archive {zp.name} is 0 bytes (empty)"
    try:
        with _zf.ZipFile(zp, "r") as zf:
            members = zf.namelist()
            if not members:
                return False, sz, 0, f"archive {zp.name} has 0 entries"
            for req_member in ("TASK_BRIEF.md", "API_INDEX.md"):
                if req_member not in members:
                    return False, sz, len(members), f"archive {zp.name} is missing required member {req_member}"
                raw_req = zf.read(req_member)
                if not raw_req or b"\x00" in raw_req:
                    return False, sz, len(members), f"archive {zp.name} member {req_member} is empty or binary"
                try:
                    if not raw_req.decode("utf-8", errors="strict").strip():
                        return False, sz, len(members), f"archive {zp.name} member {req_member} is blank"
                except UnicodeDecodeError:
                    return False, sz, len(members), f"archive {zp.name} member {req_member} is not valid UTF-8"
            for member in members:
                if member.endswith("/"):
                    continue
                m_ext = Path(member).suffix.lower()
                if m_ext in _BIN_EXTS:
                    return False, sz, len(members), f"archive {zp.name} contains binary member {member!r} ({m_ext})"
                with zf.open(member, "r") as mf:
                    sample = mf.read(8192)
                    if b"\x00" in sample:
                        return False, sz, len(members), f"archive {zp.name} contains binary member {member!r} (NUL byte in first 8 KB)"
            bad = zf.testzip()
            if bad is not None:
                return False, sz, len(members), f"archive {zp.name} has corrupted entry {bad!r}"
            return True, sz, len(members), ""
    except Exception as exc:
        return False, sz, 0, f"archive {zp.name} is corrupt or unreadable ({exc})"


def _attachment_present(page, zip_path: Path) -> bool:
    """Verify the composer carries the valid archive with matching byte size and TASK_BRIEF.md (D14)."""
    ok_zip, disk_size, member_count, err_reason = _inspect_zip_archive(Path(zip_path))
    if not ok_zip:
        print(f"[ERROR] Attachment check failed on disk: {err_reason}", file=sys.stderr)
        return False

    input_matched = False
    try:
        attached_sizes = page.evaluate(
            """() => {
                const inputs = Array.from(document.querySelectorAll("input[type='file']"));
                const sizes = [];
                for (const i of inputs) {
                    if (i.files && i.files.length) {
                        for (const f of Array.from(i.files)) {
                            sizes.push({ name: f.name || '', size: typeof f.size === 'number' ? f.size : -1 });
                        }
                    }
                }
                return sizes;
            }"""
        )
        if isinstance(attached_sizes, list) and attached_sizes:
            for item in attached_sizes:
                if isinstance(item, dict):
                    f_sz = int(item.get("size", -1))
                    if f_sz > 0 and f_sz != disk_size:
                        print(
                            f"[ERROR] Attached file size in input ({f_sz} bytes) does not match "
                            f"disk archive {Path(zip_path).name} ({disk_size} bytes)!",
                            file=sys.stderr,
                        )
                        return False
                    if f_sz == disk_size:
                        input_matched = True
    except Exception:
        pass

    chip_visible = False
    try:
        chip = page.get_by_text(re.compile(re.escape(Path(zip_path).name), re.IGNORECASE))
        for i in range(min(chip.count(), 5)):
            if _safe_visible(chip.nth(i)):
                chip_visible = True
                break
    except Exception:
        pass

    return bool(input_matched or chip_visible)


def _open_upload_menu(page) -> bool:
    """Open the composer's upload-and-tools menu (the '+' button).

    The hidden `input[type=file]` does NOT exist until this menu is open, so
    the menu has to be opened before the file can be attached.

    The button is looked for inside the REAL composer first - the container
    that holds the prompt box - and the bottom-most match wins. A loaded
    conversation can render a floating quick-action bar in the middle of the
    page with its own '+' button, and clicking THAT one is what made the
    cursor jump into the conversation and opened the wrong menu.
    """
    roots = []
    comp = _find_composer(page)
    if comp is not None:
        for xpath in (
            "xpath=ancestor::*[.//button[contains(@class,'input-area-switch')]][1]",
            "xpath=ancestor::*[contains(@class,'input-area')][1]",
            "xpath=ancestor::div[1]",
        ):
            try:
                anc = comp.locator(xpath)
                if anc.count() > 0:
                    roots.append(anc.first)
                    break
            except Exception:
                continue
    roots.append(page)

    for root in roots:
        try:
            loc = root.locator("button[aria-haspopup='menu']")
            best = None
            best_y = -1.0
            for i in range(min(loc.count(), 12)):
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                try:
                    aria = str(el.get_attribute("aria-label") or "")
                    tip = str(el.get_attribute("data-tooltip") or "")
                except Exception:
                    continue
                if not RE_UPLOAD.search(aria + " " + tip):
                    continue
                try:
                    box = el.bounding_box(timeout=1500)
                except Exception:
                    box = None
                y = float(box["y"]) if box else 0.0
                if y >= best_y:
                    best_y = y
                    best = el
            if best is not None:
                best.click(timeout=4000)
                page.wait_for_timeout(1600)
                return True
        except Exception:
            continue
    return False


def _zip_input(page):
    """Return the hidden file input that accepts .zip, if the DOM has one."""
    try:
        inputs = page.locator("input[type='file']")
        n = min(inputs.count(), 6)
        fallback = None
        for i in range(n):
            el = inputs.nth(i)
            try:
                accept = str(el.get_attribute("accept") or "")
            except Exception:
                accept = ""
            if ".zip" in accept.lower():
                return el
            if fallback is None:
                fallback = el
        return fallback
    except Exception:
        return None


def _verify_zip_has_no_credentials(zip_path: Path) -> bool:
    """Verify that a context archive is readable, contains TASK_BRIEF.md, and has no credentials (D14, D15).

    Never swallows a corrupt/truncated archive error: any unreadable or empty zip
    is refused loudly before upload.
    """
    zp = Path(zip_path)
    ok_zip, sz, entry_count, err_reason = _inspect_zip_archive(zp)
    if not ok_zip:
        print(
            f"[ERROR] Refusing to upload {zp.name}: {err_reason}.",
            file=sys.stderr,
        )
        return False
    try:
        import zipfile as _zf
        from package_context import is_secret

        with _zf.ZipFile(zp, "r") as zf:
            for member in zf.namelist():
                name = Path(member).name
                if is_secret(name) or re.search(r"accounts?[_-]?pool", name, re.IGNORECASE):
                    print(
                        f"[ERROR] Refusing to upload {zp.name}: archive contains credential file {member!r}.",
                        file=sys.stderr,
                    )
                    return False
        return True
    except Exception as exc:
        print(
            f"[ERROR] Refusing to upload corrupt or unreadable archive {zp.name}: {exc}",
            file=sys.stderr,
        )
        return False


def _composer_upload_in_progress(page) -> bool:
    """Return True while the composer attachment chip still shows an upload spinner/progressbar (D16)."""
    try:
        scope = _composer_scope(page) or page
        for sel in (
            "[role='progressbar']",
            "mat-progress-spinner",
            ".mat-progress-spinner",
            ".mat-mdc-progress-spinner",
            "[class*='upload-progress']",
            "[class*='loading-spinner']",
            "[aria-label*='Uploading' i]",
            "[aria-label*='আপলোড হচ্ছে']",
        ):
            loc = scope.locator(sel)
            for i in range(min(loc.count(), 6)):
                if _safe_visible(loc.nth(i)):
                    return True
    except Exception:
        pass
    return False


def _wait_for_upload_complete(page, zip_path: Path, timeout: float = 90.0) -> bool:
    """Wait until the attached archive's upload spinner clears before pasting/sending (D16)."""
    start_mono = time.monotonic()
    deadline = start_mono + max(5.0, float(timeout))
    clear_streak = 0
    while time.monotonic() < deadline:
        present = _attachment_present(page, zip_path)
        uploading = _composer_upload_in_progress(page)
        if present and not uploading:
            clear_streak += 1
            if clear_streak >= 2:
                waited = time.monotonic() - start_mono
                sz = Path(zip_path).stat().st_size if Path(zip_path).is_file() else 0
                print(
                    f"[+] Upload of {Path(zip_path).name} ({sz} bytes) finished in {waited:.1f}s "
                    "(attachment chip ready, upload spinner cleared)."
                )
                return True
        else:
            clear_streak = 0
        try:
            page.wait_for_timeout(900)
        except Exception:
            time.sleep(0.9)

    waited = time.monotonic() - start_mono
    print(
        f"[ERROR] Upload of {Path(zip_path).name} did not finish within {waited:.1f}s "
        "(upload spinner still active or attachment missing) - refusing to send a half-uploaded archive.",
        file=sys.stderr,
    )
    return False


def attach_zip(page, zip_path: Path) -> bool:
    """Attach the context zip to the composer, verify its size/members, and wait until upload finishes (D14, D15, D16)."""
    zp = Path(zip_path)
    if not _verify_zip_has_no_credentials(zp):
        return False
    ok_zip, sz, entries, _ = _inspect_zip_archive(zp)
    if not ok_zip:
        return False
    print(f"[+] Attaching archive {zp.name} ({sz} bytes, {entries} entries)...")

    # 1. Open the upload menu first - that is when the file input appears.
    _open_upload_menu(page)
    el = _zip_input(page)
    if el is not None:
        try:
            el.set_input_files(str(zp))
            page.wait_for_timeout(1500)
            if _wait_for_upload_complete(page, zp):
                return True
        except Exception:
            pass

    # 2. A file input may already have been present.
    try:
        inputs = page.locator("input[type='file']")
        for i in range(min(inputs.count(), 4)):
            try:
                inputs.nth(i).set_input_files(str(zp))
                page.wait_for_timeout(1500)
                if _wait_for_upload_complete(page, zp):
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # 3. Use the native file chooser from the upload menu entry.
    try:
        loc = page.get_by_text(re.compile(r"(ফাইল আপলোড|ফাইল|upload|আপলোড)", re.IGNORECASE))
        for i in range(min(loc.count(), 12)):
            el2 = loc.nth(i)
            if not _safe_visible(el2):
                continue
            try:
                with page.expect_file_chooser(timeout=8000) as fc_info:
                    el2.click(timeout=3000)
                fc_info.value.set_files(str(zp))
                page.wait_for_timeout(1500)
                if _wait_for_upload_complete(page, zp):
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _focus_composer_dom(composer) -> None:
    """Focus the contenteditable via the DOM, placing the caret at its end.

    There is deliberately NO mouse click here: a click moves the cursor, can
    land on something else in a loaded conversation and makes the page jump.
    The DOM focus is what actually matters - a click alone does not focus
    Gemini's Quill editor, the explicit focus does.
    """
    try:
        composer.evaluate(
            "(el) => { el.focus();"
            " const s = window.getSelection();"
            " const r = document.createRange(); r.selectNodeContents(el);"
            " r.collapse(false); s.removeAllRanges(); s.addRange(r); }"
        )
    except Exception:
        pass


def _composer_text(composer) -> str:
    """Return the current text inside the composer, stripped of surrounding whitespace."""
    for getter in (
        lambda: composer.inner_text(timeout=2000),
        lambda: composer.evaluate("el => (el.innerText || el.textContent || el.value || '')"),
        lambda: composer.input_value(timeout=1000),
    ):
        try:
            raw = getter()
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
        except Exception:
            continue
    return ""


def _page_body_snapshot(page) -> str:
    """Best-effort inner_text of the document body."""
    try:
        return page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return ""


def _composer_send_button(page, composer=None):
    """The composer's SEND button.

    It only exists while there is something to send. It is the fallback when
    the keyboard send does not empty the composer - a real button click is
    quick, while `composer.press()` has to wait for the element to become
    "stable", which on a huge Quill editor can take minutes.
    """
    roots = []
    if composer is not None:
        for xpath in (
            "xpath=ancestor::*[.//button[contains(@class,'input-area-switch')]][1]",
            "xpath=ancestor::*[contains(@class,'input-area')][1]",
            "xpath=ancestor::div[1]",
        ):
            try:
                anc = composer.locator(xpath)
                if anc.count() > 0:
                    roots.append(anc.first)
                    break
            except Exception:
                continue
    roots.append(page)
    for root in roots:
        try:
            loc = root.locator(
                "button.send-button, "
                "button[aria-label*='Send' i], "
                "button[aria-label*='পাঠান'], "
                "button[mat-icon-button]"
            )
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                if _safe_visible(el):
                    return el
        except Exception:
            continue
    return None


def _paste_into_composer(page, composer, prompt_text: str) -> str:
    """Paste the prompt into the composer and return the text that landed.

    THE RULE: a prompt is never TYPED. `keyboard.insert_text` and
    `press_sequentially` dispatch one event per character, and Quill turns a
    26 KB prompt into minutes of work that LOOKS like a hang - the run sits on
    "sending..." forever. A paste is ONE event, so it is equally fast for 1 KB
    and for 26 KB.

    A big prompt is pasted in chunks, and every chunk is verified, so a paste
    that silently loses part of the text is caught instead of sending half a
    prompt.
    """
    ctx = getattr(page, "context", None)
    if ctx is not None and hasattr(ctx, "grant_permissions"):
        perms = ["clipboard-read", "clipboard-write"]
        # The Gemini origin first, then a grant for every origin - a page that
        # ended up on another origin (a redirect, a different host) would
        # otherwise have no clipboard permission at all.
        for kwargs in ({"origin": "https://gemini.google.com"}, {}):
            try:
                ctx.grant_permissions(perms, **kwargs)
            except Exception:
                continue

    chunk_size = 8000
    chunks = [prompt_text[i:i + chunk_size] for i in range(0, len(prompt_text), chunk_size)]

    landed = ""
    for number, chunk in enumerate(chunks, start=1):
        try:
            page.evaluate(
                """(t) => Promise.race([
                    navigator.clipboard.writeText(t),
                    new Promise((_, reject) => setTimeout(() => reject(new Error('clipboard write timeout')), 5000))
                ])""",
                chunk,
            )
        except Exception as exc:
            print(f"[!] The clipboard is not usable: {exc}", file=sys.stderr)
            return landed
        _focus_composer_dom(composer)
        try:
            page.keyboard.press("Control+V")
        except Exception:
            try:
                composer.press("Control+V", timeout=4000)
            except Exception:
                pass
        try:
            page.wait_for_timeout(700)
        except Exception:
            time.sleep(0.7)
        landed = _composer_text(composer)
        if number < len(chunks):
            print(
                f"[+] Pasted {number}/{len(chunks)} of the prompt "
                f"({len(landed)} chars so far)."
            )
    return landed


def _type_and_send(page, composer, prompt_text: str) -> None:
    """Put the prompt into the composer, verify it landed, send it, and verify
    that the message was really delivered into the conversation.

    THE RULE: the prompt is PASTED, never typed, and every step is bounded in
    time. A 26 KB prompt that is typed into Gemini's Quill editor takes minutes
    of character-by-character work and looks exactly like a hang - the run sits
    on "sending..." and nothing happens. A paste is a single event, so it is
    fast at any size.

    In an already-loaded conversation a plain click also leaves the Quill editor
    unfocused with class `ql-blank`, so the contenteditable is focused through
    the DOM (no mouse click, no cursor jumps) before the paste.
    """
    clean_prompt = prompt_text.strip()
    if not clean_prompt:
        raise ValueError("Refusing to send an empty prompt.")

    if len(clean_prompt) > 8000:
        print(
            f"[!] The prompt is {len(clean_prompt)} characters long. It is pasted "
            "in chunks, but a prompt this big usually means the task description "
            "carries the codebase - the project belongs in argon_context.zip.",
            file=sys.stderr,
        )

    expected_min_len = max(1, min(len(clean_prompt) // 2, 40))
    first_line = next(
        (ln.strip() for ln in clean_prompt.splitlines() if len(ln.strip()) >= 8),
        clean_prompt[:80],
    )
    probe_snippet = first_line[:60]

    deadline = time.monotonic() + 150.0

    for attempt in range(2):
        if attempt > 0:
            print(
                "[!] The prompt was not delivered on attempt 1 - re-focusing the "
                "composer and retrying once...",
                file=sys.stderr,
            )
            fresh = _find_composer(page)
            if fresh is not None:
                composer = fresh

        body_before = _page_body_snapshot(page)
        body_len_before = len(body_before)
        snippet_count_before = body_before.count(probe_snippet) if probe_snippet else 0

        # 1. Paste the prompt (never type it).
        landed_text = _paste_into_composer(page, composer, prompt_text)

        # A tiny prompt that the clipboard refused can still be typed - typing
        # is only safe when it is short.
        if len(landed_text) < expected_min_len and len(clean_prompt) <= 2000:
            _focus_composer_dom(composer)
            try:
                page.keyboard.insert_text(clean_prompt)
                page.wait_for_timeout(400)
            except Exception:
                pass
            landed_text = _composer_text(composer)

        # Never send an empty composer.
        if len(landed_text) < expected_min_len:
            print(
                f"[!] The composer stayed empty or too short (attempt {attempt + 1}/2, "
                f"len={len(landed_text)}, expected>={expected_min_len}).",
                file=sys.stderr,
            )
            continue

        print(f"[+] Composer populated (length={len(landed_text)} chars). Sending...")

        # 2. Send. The keyboard is used first: a keyboard event goes to the
        #    focused element and never waits for the editor to become
        #    "stable", which on a huge Quill editor takes minutes.
        _focus_composer_dom(composer)
        try:
            page.keyboard.press("Control+Enter")
        except Exception:
            pass
        try:
            page.wait_for_timeout(1200)
        except Exception:
            time.sleep(1.2)

        remaining_text = _composer_text(composer)
        if len(remaining_text) >= expected_min_len:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
            try:
                page.wait_for_timeout(1000)
            except Exception:
                time.sleep(1.0)
            remaining_text = _composer_text(composer)

        # The composer did not empty: press the real send button. It is a
        # normal button, so the click is quick.
        if len(remaining_text) >= expected_min_len:
            button = _composer_send_button(page, composer)
            if button is not None:
                try:
                    button.click(timeout=8000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                remaining_text = _composer_text(composer)

        # 3. Verify that the message really went out: the composer emptied AND
        #    the conversation grew.
        delivered_ok = False
        while time.monotonic() < deadline:
            after_text = _composer_text(composer)
            composer_emptied = len(after_text) < expected_min_len
            body_after = _page_body_snapshot(page)
            snippet_count_after = body_after.count(probe_snippet) if probe_snippet else 0
            conversation_grew = (
                len(body_after) > body_len_before + 15
                or (bool(probe_snippet) and snippet_count_after > snippet_count_before)
            )
            if composer_emptied and conversation_grew:
                delivered_ok = True
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)

        if delivered_ok:
            return

        print(
            f"[!] The prompt delivery verification failed on attempt {attempt + 1}/2 "
            f"(composer_len={len(_composer_text(composer))}).",
            file=sys.stderr,
        )

    raise RuntimeError(
        "The prompt could not be pasted, sent and verified as delivered in the "
        "Gemini composer after one retry."
    )


def _find_composer(page):
    """Return the prompt composer element, or None when there is none.

    A loaded conversation can hold MORE than one editable: the real composer
    sits at the BOTTOM of the page, and a conversation that is still loading
    can also render a floating quick-action bar in the middle. Picking the
    first match or "the last in DOM order" is what made follow-ups land in the
    wrong box and made the page jump around.

    So every candidate is measured and the one whose box sits LOWEST on the
    screen wins - that is the real composer, in a fresh chat as well as in a
    long one.
    """
    best = None
    best_y = -1.0
    for sel in (
        "rich-textarea div[contenteditable='true']",
        "div.input-area-container div[contenteditable='true']",
        "div[contenteditable='true']",
        "textarea[aria-label*='prompt']",
        "textarea",
    ):
        try:
            loc = page.locator(sel)
            count = min(loc.count(), 12)
        except Exception:
            continue
        for i in range(count):
            try:
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                try:
                    box = el.bounding_box(timeout=1500)
                except Exception:
                    box = None
                y = float(box["y"]) if box else 0.0
            except Exception:
                continue
            if y >= best_y:
                best_y = y
                best = el
    return best


def manual_setup_instructions(mode: str, screenshot: Optional[str] = None) -> str:
    """Bilingual checklist for finishing the workspace setup by hand."""
    lines = [
        "",
        "!" * 74,
        "[ERROR] PRE-FLIGHT FAILED - the prompt was NOT sent.",
        "!" * 74,
        "",
        "Sending a prompt without the workspace configured is what makes Gemini reply",
        "'argon_context.zip did not come through with your message'. The dispatcher",
        "refuses to do that. Finish these steps in the browser window that is open:",
        "",
    ]
    lines += [
        "",
        "  1. Mode     : switch the Chat / Task toggle to 'Task' (টাস্ক).",
        "  2. Model    : pick '4 Argon (Complex tasks)' in the model dropdown.",
        "  3. Attach   : press '+' -> Upload files -> choose argon_context.zip.",
        "  4. Send     : paste the prompt and send it.",
    ]
    lines += [
        "",
        "Then re-run the dispatcher with --manual-setup: it will attach the file and",
        "send the prompt for you instead of sending anything context-free.",
    ]
    if screenshot:
        lines += ["", "  Screenshot: " + screenshot]
    lines += ["!" * 74, ""]
    return "\n".join(lines)


def _is_page_unauthenticated(page: Any) -> bool:
    """Return True only when the page is proven to be a guest or Google sign-in page (D8.1)."""
    return _gemini_page_is_guest(page)


def _select_profile_account_if_needed(page: Any, target_email: Optional[str]) -> None:
    """Select the target account inside the browser profile's Google account switcher if needed."""
    if not target_email:
        return
    try:
        avatar = page.locator(
            "a[aria-label*='Google Account'], button[aria-label*='Google Account']"
        )
        if avatar.count() > 0 and avatar.first.is_visible():
            label = avatar.first.get_attribute("aria-label") or ""
            if target_email.lower() in label.lower():
                return
            avatar.first.click(timeout=3000)
            page.wait_for_timeout(800)
            acct_item = page.get_by_text(target_email, exact=False)
            if acct_item.count() > 0 and acct_item.first.is_visible():
                acct_item.first.click(timeout=4000)
                page.wait_for_timeout(1500)
    except Exception:
        pass


def _is_target_closed_error(exc: BaseException) -> bool:
    """Return True when Playwright raised because the page, context, or browser closed."""
    name = type(exc).__name__
    if name == "TargetClosedError":
        return True
    msg = str(exc).lower()
    return any(
        tok in msg
        for tok in (
            "target page, context or browser has been closed",
            "target closed",
            "context or browser has been closed",
            "browser has been closed",
            "page has been closed",
        )
    )


def _resolve_reopen_url(
    proj_root: Path,
    mode: str,
    active_slot: str,
    current_url: str,
    fallback_url: str,
    task_state: TaskState,
    session_store: SessionStore,
    isolated_profile: bool = False,
) -> str:
    """Return the best conversation URL to reopen for an in-progress or resumed task.

    `isolated_profile` must be True only for a per-slot profile (one account,
    browser index 0). The shared master profile holds every account, so its URL
    has to keep the slot index - defaulting to True would open the wrong one.
    """
    slot_part = "/u/0/" if isolated_profile else (normalize_slot(active_slot) if active_slot else "/u/0/")

    def _url_for_chat_id(cid: str) -> str:
        return f"https://gemini.google.com{slot_part}app/{cid}"

    # 1. Check current_url for a conversation id
    cid = _extract_chat_id(current_url or "")
    if cid:
        return _url_for_chat_id(cid)

    # 2. Check TaskState for a recorded chat_id or chat_url
    ts_entry = task_state.get(proj_root) or {}
    ts_cid = str(ts_entry.get("chat_id") or "").strip() or _extract_chat_id(str(ts_entry.get("chat_url") or ""))
    if ts_cid:
        return _url_for_chat_id(ts_cid)

    # 3. Check SessionStore for this project and mode
    ss_entry = session_store.lookup(proj_root, mode=mode) or {}
    ss_cid = str(ss_entry.get("chat_id") or "").strip() or _extract_chat_id(str(ss_entry.get("chat_url") or ""))
    if ss_cid:
        return _url_for_chat_id(ss_cid)

    if ts_entry.get("chat_url"):
        return _normalize_gemini_chat_url(str(ts_entry["chat_url"]), isolated_profile=isolated_profile)
    if current_url:
        return _normalize_gemini_chat_url(current_url, isolated_profile=isolated_profile)
    return _normalize_gemini_chat_url(fallback_url, isolated_profile=isolated_profile)


GEMINI_ERROR_MARKERS = (
    "something went wrong",
    "please try again later",
    "try again later",
    "একটি সমস্যা হয়েছে",
    "আবার চেষ্টা করুন",
)

# Markers that mean Argon (or the page) is still busy. While one of them is on
# screen the run is NOT finished - it must keep waiting.
RE_WORKING = re.compile(
    r"(thinking it through|thinking|gemini is typing|generating|loading|"
    r"টাইপ করছে|ভাবছে|বিস্ময় নিয়ে ভাবছে|লোড হচ্ছে|প্রক্রিয়া করছে)",
    re.IGNORECASE,
)


def _is_argon_working(page) -> bool:
    """True while Argon is still thinking, typing or the chat is still loading.

    A conversation that is still LOADING shows the old error banner of an
    earlier round and looks like a failed run - but the work is actually still
    going. Carrying the task over then would cut a running job short, so the
    working state has to be checked before anything is declared finished.
    """
    try:
        text = page.locator("body").inner_text(timeout=6000) or ""
    except Exception:
        text = ""
    tail = text[-1200:]
    if RE_WORKING.search(tail):
        return True
    try:
        # The stop button only exists while a response is being produced.
        for sel in (
            "button[aria-label*='Stop' i]",
            "button[aria-label*='থামান']",
            "button[aria-label*='stop generating' i]",
        ):
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                if _safe_visible(loc.nth(i)):
                    return True
    except Exception:
        pass
    try:
        # A spinner means the conversation is still being loaded. A
        # `role=progressbar` is a loading indicator by definition, so its mere
        # presence counts - it is often rendered without a size.
        try:
            if page.locator("[role='progressbar']").count() > 0:
                return True
        except Exception:
            pass
        for sel in (".mat-progress-spinner", "[class*='spinner']", "[class*='loader']"):
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                if _safe_visible(loc.nth(i)):
                    return True
    except Exception:
        pass
    return False


STOPPED_RESPONSE_MARKERS = (
    "you stopped this response",
    "আপনি এই উত্তরটি থামিয়েছেন",
    "আপনি এই উত্তরটি থামিয়েছেন",
    "response was stopped",
    "stopped",
)


def _detect_stopped_response(page) -> Optional[str]:
    """Detect when Gemini itself stopped the response ('You stopped this response') (D10.4)."""
    try:
        turns = _assistant_turns(page)
        if turns and _element_is_after_last_user_turn(turns[-1]):
            t_txt = (turns[-1].inner_text(timeout=3000) or "").lower()
            for marker in STOPPED_RESPONSE_MARKERS:
                if marker in t_txt:
                    return "You stopped this response"
            lines = [ln.strip() for ln in t_txt.splitlines() if ln.strip()]
            if lines and (
                lines[-1] == "stopped"
                or (len(lines) >= 2 and lines[-2] == "gemini said" and lines[-1] == "stopped")
                or any(ln == "stopped" for ln in lines[-3:])
            ):
                return "You stopped this response"
    except Exception:
        pass
    try:
        text = page.locator("body").inner_text(timeout=4000) or ""
    except Exception:
        return None
    tail_lines = [ln.strip().lower() for ln in text.splitlines() if ln.strip()][-8:]
    tail_joined = "\n".join(tail_lines)
    for marker in STOPPED_RESPONSE_MARKERS:
        if marker in tail_joined:
            return "You stopped this response"
    if tail_lines and (
        tail_lines[-1] == "stopped"
        or (len(tail_lines) >= 2 and tail_lines[-2] == "gemini said" and tail_lines[-1] == "stopped")
        or any(ln == "stopped" for ln in tail_lines[-3:])
    ):
        return "You stopped this response"
    return None


def _verify_argon_answer_evidence(
    page: Any,
    body_text: str = "",
    delivered_path: Optional[Path] = None,
    extracted_patch: Optional[str] = None,
) -> Dict[str, Any]:
    """Inspect the answer for real agentic Argon evidence (D12.2).

    After the prompt is sent, the composer's model chip reverts to the account
    default (e.g. 'Flash'), so the real proof that Argon answered is in the
    answer itself: a generated apply_changes.ps1 chip/file, tool-call trace,
    root-cause notes, and structured PowerShell patch output.
    """
    signals: List[str] = []
    if delivered_path is not None and Path(delivered_path).is_file():
        signals.append(f"delivered file artifact ({Path(delivered_path).name})")
    if extracted_patch:
        signals.append("structured PowerShell patch block")
    try:
        if page is not None:
            turns = _assistant_turns(page)
            if turns:
                last_turn = turns[-1]
                for trace_sel in (
                    "generated-file",
                    "code-execution",
                    "thought-view",
                    "[data-test-id*='thought']",
                    "[class*='thought']",
                    "mat-expansion-panel",
                ):
                    try:
                        if last_turn.locator(trace_sel).count() > 0:
                            signals.append(f"DOM trace ({trace_sel})")
                            break
                    except Exception:
                        continue
    except Exception:
        pass

    sample = (extracted_patch or body_text or "")
    if "$TargetRoot" in sample:
        signals.append("$TargetRoot contract")
    if "## ROOT CAUSE" in sample:
        signals.append("## ROOT CAUSE analysis")
    if "[Verified]" in sample:
        signals.append("[Verified] self-check")

    verified = len(signals) > 0
    summary = ", ".join(signals) if signals else "no agentic signals detected"
    print(f"[+] Argon answer evidence: {'VERIFIED' if verified else 'MISSING'} ({summary})")
    return {"verified": verified, "signals": signals, "summary": summary}


def _detect_gemini_error(page) -> Optional[str]:
    """Detect that Gemini itself gave up ("Something went wrong" or "You stopped this response")."""
    stopped = _detect_stopped_response(page)
    if stopped:
        return stopped
    try:
        text = page.locator("body").inner_text(timeout=6000) or ""
    except Exception:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].lower()
    for marker in GEMINI_ERROR_MARKERS:
        if marker in last:
            return marker
    return None


def _collect_diagnostic_evidence(
    page: Any,
    proj_root: Path,
    reason: str,
    slot: str = "/u/0/",
    email: Optional[str] = None,
    profile_dir: Optional[Path] = None,
    chat_title: str = "",
    chat_url: str = "",
    screenshot_path: Optional[Path] = None,
    preflight: Optional[List[str]] = None,
    error_output: str = "",
    ruled_out: Optional[List[str]] = None,
    next_step: str = "",
) -> Dict[str, Any]:
    """Collect measured diagnostic evidence from the page's OWN conversation (D21)."""
    live_url = ""
    try:
        if page is not None:
            live_url = str(getattr(page, "url", "") or "")
    except Exception:
        live_url = ""
    effective_url = (live_url or chat_url or "").strip()
    cid = _extract_chat_id(effective_url) or ""
    # D21: Only report a conversation title when a conversation ID actually exists in the URL
    if not cid:
        title_now = "(no conversation created yet - new chat was never started)"
    else:
        title_now = chat_title
        if not title_now and page is not None:
            try:
                title_now = _chat_title(page)
            except Exception:
                title_now = ""
    chip_name = ""
    if page is not None:
        try:
            chip_el = _model_chip(page)
            chip_name = _model_name(chip_el) if chip_el is not None else ""
        except Exception:
            chip_name = ""
    page_excerpt = ""
    if page is not None:
        try:
            raw_body = _page_body_snapshot(page)
            non_empty = [ln.strip() for ln in raw_body.splitlines() if ln.strip()]
            tail_text = "\n".join(non_empty[-25:])
            page_excerpt = tail_text[-1500:] if len(tail_text) > 1500 else tail_text
        except Exception:
            page_excerpt = ""
    acct = (
        _detect_signed_in_account(page, fallback_email=email, slot=slot)
        if page is not None
        else (email or f"slot {slot}")
    )
    evidence: Dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "url": effective_url or "https://gemini.google.com/u/0/app",
        "chat_id": cid or "(none)",
        "chat_title": title_now or "(untitled / not yet created)",
        "slot": slot or "/u/0/",
        "account": acct,
        "profile_dir": str(profile_dir) if profile_dir else "",
        "model_chip": chip_name or "(not visible)",
        "argon_disabled": bool(_argon_picker_is_disabled(page)) if page is not None else False,
        "screenshot_path": str(screenshot_path) if screenshot_path else "",
        "preflight_checklist": list(preflight or []),
        "error_output": (error_output or "").strip()[-4000:],
        "page_text_excerpt": page_excerpt,
        "ruled_out": list(ruled_out or []),
        "next_step": next_step,
    }
    try:
        out_dir = Path(proj_root).resolve() / ".argon_backup"
        out_dir.mkdir(parents=True, exist_ok=True)
        ev_file = out_dir / "last_diagnostic_evidence.json"
        ev_file.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass
    return evidence


def _format_diagnostic_evidence_block(evidence: Dict[str, Any]) -> str:
    """Format the measured diagnostic evidence block to hand to Argon in the follow-up prompt."""
    if not evidence or not isinstance(evidence, dict):
        return ""
    h2 = "#" * 2
    h3 = "#" * 3
    b_char = chr(45)
    lines = [
        f"{h2} DIAGNOSTIC EVIDENCE FROM PREVIOUS FAILED RUN (MEASURED ON LIVE MACHINE)",
        "",
        "Use these measured facts to state the exact root cause under `## ROOT CAUSE` in your",
        "NOTES block before changing anything - do not guess:",
        "",
        f"{b_char} Open URL: `{evidence.get('url') or '(unknown)'}`",
        f"{b_char} Conversation ID: `{evidence.get('chat_id') or '(none)'}`",
        f"{b_char} Conversation Title: `{evidence.get('chat_title') or '(untitled)'}`",
        f"{b_char} Pool Slot & Account: `{evidence.get('slot') or '/u/0/'}` (`{evidence.get('account') or 'unknown'}`)",
        f"{b_char} Profile Directory: `{evidence.get('profile_dir') or '(unknown)'}`",
        f"{b_char} Model Chip Seen: `{evidence.get('model_chip') or '(not visible)'}`"
        + (" (4 Argon option was aria-disabled in picker)" if evidence.get("argon_disabled") else ""),
        f"{b_char} Screenshot Path: `{evidence.get('screenshot_path') or '(none)'}`",
        f"{b_char} Failure Reason: `{evidence.get('reason') or '(unknown)'}`",
    ]
    pf = evidence.get("preflight_checklist") or []
    if pf:
        lines += ["", f"{h3} Pre-Flight Checklist Output", "```text", *[str(x) for x in pf], "```"]
    ruled = evidence.get("ruled_out") or []
    if ruled:
        lines += ["", f"{h3} What the Bridge Already Ruled Out"]
        for item in ruled:
            lines.append(f"{b_char} {item}")
    err_out = (evidence.get("error_output") or "").strip()
    if err_out:
        lines += ["", f"{h3} Error / Verifier Diagnostics Output", "```text", err_out, "```"]
    excerpt = (evidence.get("page_text_excerpt") or "").strip()
    if excerpt:
        lines += ["", f"{h3} Measured Page Text Excerpt", "```text", excerpt, "```"]
    return "\n".join(lines)


def _print_run_failure_summary(evidence: Dict[str, Any]) -> None:
    """Print the conversation title, exact link, account, profile directory, screenshot, and next step."""
    if not evidence:
        return
    print("\n" + "=" * 78, file=sys.stderr)
    print("  [!] RUN ENDED WITHOUT A DELIVERY - DIAGNOSTIC SUMMARY", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    print(f"  reason             : {evidence.get('reason') or '(unknown)'}", file=sys.stderr)
    print(f"  conversation title : {evidence.get('chat_title') or '(not yet created / untitled)'}", file=sys.stderr)
    print(f"  conversation link  : {evidence.get('url') or 'https://gemini.google.com/u/0/app'}", file=sys.stderr)
    print(f"  account            : {evidence.get('slot') or '/u/0/'} ({evidence.get('account') or 'unknown'})", file=sys.stderr)
    if evidence.get("profile_dir"):
        print(f"  profile directory  : {evidence.get('profile_dir')}", file=sys.stderr)
    if evidence.get("model_chip"):
        print(f"  model chip         : {evidence.get('model_chip')}", file=sys.stderr)
    if evidence.get("screenshot_path"):
        print(f"  screenshot         : {evidence.get('screenshot_path')}", file=sys.stderr)
    if evidence.get("next_step"):
        print(f"  what to do next    : {evidence.get('next_step')}", file=sys.stderr)
    print("=" * 78 + "\n", file=sys.stderr)


def _build_carry_over_prompt(
    original_prompt: str,
    previous_text: str,
    notes: str,
    reason: str,
    diagnostic_evidence: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the prompt for a NEW session that carries the previous context and diagnostic evidence.

    The previous round may already have changed files, so the new prompt tells
    Argon what the previous conversation managed to do, includes the measured
    diagnostic evidence, and asks it to continue from there instead of starting
    the work over.
    """
    trimmed = (previous_text or "").strip()
    if len(trimmed) > 6000:
        trimmed = "...(earlier output trimmed)...\n" + trimmed[-6000:]
    parts = [
        (original_prompt or "").strip(),
        "",
        "---",
        "",
        "## PREVIOUS ROUND CONTEXT - CONTINUE FROM HERE",
        "",
        f"The previous run ENDED WITHOUT A DELIVERY ({reason}).",
        "It may already have changed files, so this is what it managed to do before it stopped.",
        "Continue from that state: do not start the work over, and do not undo what is",
        "already done.",
        "",
    ]
    if diagnostic_evidence:
        parts += [
            _format_diagnostic_evidence_block(diagnostic_evidence),
            "",
        ]
    if (notes or "").strip():
        parts += [
            "### Notes the previous round recorded",
            "",
            "```text",
            notes.strip(),
            "```",
            "",
        ]
    if trimmed:
        parts += [
            "### What the previous conversation produced",
            "",
            "```text",
            trimmed,
            "```",
            "",
        ]
    parts += [
        "### What to do now",
        "",
        "First state the exact root cause and measured evidence under `## ROOT CAUSE` in the",
        "NOTES block, then finish the remaining work in THIS conversation and deliver the",
        "complete patch as a generated file named `apply_changes.ps1`, exactly as the contract",
        "above requires. The full project context is attached again as `argon_context.zip`.",
        "",
    ]
    return "\n".join(parts)


_STEP_SCREENSHOT_COUNTER = 0


def _take_step_screenshot(
    page,
    proj_root: Path,
    filename: str,
    step_label: str = "",
    state_label: str = "",
    lane: Optional[str] = None,
) -> Optional[Path]:
    """Save a numbered step screenshot `.argon_backup/shots/<NN>-<step>.png`, record it in
    TaskState + TaskRegistry, and print `[+] <step> - <state> - Screenshot: <absolute path>` (D6.1, D6.2).
    """
    global _STEP_SCREENSHOT_COUNTER
    try:
        _STEP_SCREENSHOT_COUNTER += 1
        seq = _STEP_SCREENSHOT_COUNTER
        shots_dir = Path(proj_root).resolve() / ".argon_backup" / "shots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        clean_name = Path(filename).name
        if not clean_name.lower().endswith(".png"):
            clean_name = clean_name + ".png"
        stem = Path(clean_name).stem
        numbered_path = (shots_dir / f"{seq:02d}-{stem}.png").resolve()
        ts_path = (shots_dir / f"{stamp}_{clean_name}").resolve()
        canonical_path = (shots_dir / clean_name).resolve()
        try:
            if hasattr(page, "bring_to_front"):
                page.bring_to_front()
        except Exception:
            pass
        page.screenshot(path=str(numbered_path), timeout=20000)
        for mirror in (ts_path, canonical_path):
            try:
                shutil.copy2(str(numbered_path), str(mirror))
            except Exception:
                pass
        if clean_name == "preflight_failure.png":
            try:
                legacy = (Path(proj_root).resolve() / ".argon_backup" / "preflight_failure.png").resolve()
                shutil.copy2(str(numbered_path), str(legacy))
            except Exception:
                pass

        eff_step = step_label or stem
        eff_state = state_label or ("failure captured" if "fail" in stem else "recorded")
        print(f"[+] {eff_step} - {eff_state} - Screenshot: {numbered_path}")
        print(f"    (Agent rule: READ this screenshot with your image reader before deciding anything.)")

        try:
            eff_lane = lane or getattr(page, "_argon_lane", None)
            TaskState().set(proj_root, lane=eff_lane, last_screenshot=str(numbered_path))
            TaskRegistry().update_running(proj_root, lane=eff_lane, screenshot_path=str(numbered_path))
        except Exception:
            pass
        return numbered_path
    except Exception as exc:
        print(f"[!] Could not save screenshot ({filename}): {exc}", file=sys.stderr)
        return None


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _prompt_hash(text: str) -> str:
    return _hash_text((text or "").strip())


def _last_user_message_text(page) -> str:
    """Return the visible text of the last user message in the conversation, if any."""
    for sel in (
        "user-query",
        "[data-test-id='user-query']",
        ".user-query-text",
        ".query-text",
        "message-turn.user-query",
        "div.user-query",
        "[class*='user-query']",
    ):
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:
            continue
        if not n:
            continue
        for i in range(n - 1, -1, -1):
            try:
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                t = (el.inner_text(timeout=1500) or "").strip()
                if t:
                    return t
            except Exception:
                continue
    return ""


def _extract_hms(raw_time: str) -> str:
    m = re.search(r"(\d{2}:\d{2}:\d{2})", str(raw_time or ""))
    return m.group(1) if m else time.strftime("%H:%M:%S")


def _check_duplicate_prompt(
    page,
    prompt_text: str,
    task_text: str = "",
    prev_state: Optional[Dict[str, Any]] = None,
    new_session: bool = False,
) -> Optional[str]:
    """Return the HH:MM:SS timestamp when this prompt is already the last user message.

    Checks both:
      1. the last user message in the live conversation (using a hash of the
         trimmed prompt or its first 200 characters), and
      2. the stored task state in ~/.gemini/argon_task_state.json when an
         unfinished task for the same project carries the same prompt/task hash.

    A new chat, a new task, a `--new-session` run, or a follow-up after an
    earlier round already finished its delivery is treated as a new task and
    returns None.
    """
    if new_session:
        return None

    trimmed_prompt = (prompt_text or "").strip()
    trimmed_task = (task_text or "").strip()
    if not trimmed_prompt and not trimmed_task:
        return None

    p_hash = _prompt_hash(trimmed_prompt)
    t_hash = _prompt_hash(trimmed_task) if trimmed_task else ""
    p_norm = _normalize_ws(trimmed_prompt)
    t_norm = _normalize_ws(trimmed_task)
    p_prefix = p_norm[:200]
    t_prefix = t_norm[:200]

    prev = prev_state if isinstance(prev_state, dict) else {}
    prev_status = str(prev.get("state") or "").strip().lower()
    prev_sent_hms = _extract_hms(
        str(prev.get("sent_at") or prev.get("started_at") or prev.get("updated_at") or "")
    )

    # 1. Inspect the last user message in the conversation DOM.
    last_user_raw = _last_user_message_text(page)
    if last_user_raw:
        u_hash = _prompt_hash(last_user_raw)
        u_norm = _normalize_ws(last_user_raw)
        u_prefix = u_norm[:200]
        dom_matches = False
        if p_hash and u_hash == p_hash:
            dom_matches = True
        elif t_hash and u_hash == t_hash:
            dom_matches = True
        elif p_prefix and len(p_prefix) >= 20 and (p_prefix in u_norm or u_prefix in p_norm):
            dom_matches = True
        elif t_prefix and len(t_prefix) >= 20 and t_prefix in u_norm:
            dom_matches = True

        if dom_matches:
            # Rule 1.4: if the previous turn with this text ALREADY finished and
            # delivered its patch, a follow-up that repeats the task text is
            # still a new task. Only block when the last user message has no
            # completed delivery yet (or is still running).
            already_completed = (
                prev_status in ("delivered", "applied")
                and not _is_argon_working(page)
                and _generated_file_chip(page) is not None
            )
            if not already_completed:
                return prev_sent_hms

    # 2. Check the stored hash in ~/.gemini/argon_task_state.json for an
    #    in-progress run on the same conversation.
    if prev_status in ("sent", "waiting"):
        stored_p_hash = str(prev.get("prompt_hash") or "")
        stored_t_hash = str(prev.get("task_hash") or "")
        stored_task = (str(prev.get("task") or "")).strip()
        hash_matches = (
            (p_hash and stored_p_hash and p_hash == stored_p_hash)
            or (t_hash and stored_t_hash and t_hash == stored_t_hash)
            or (trimmed_task and stored_task and _prompt_hash(stored_task) == t_hash)
        )
        if hash_matches:
            page_cid = _extract_chat_id(getattr(page, "url", "") or "")
            stored_cid = str(prev.get("chat_id") or "") or _extract_chat_id(str(prev.get("chat_url") or ""))
            if not page_cid or not stored_cid or page_cid == stored_cid:
                return prev_sent_hms

    return None


def _print_duplicate_prompt_notice(sent_hms: str) -> None:
    print(
        f"[!] This prompt is already the last message in the conversation (sent at {sent_hms}).\n"
        "    Not sending it again - waiting for the answer."
    )


def _print_prompt_sent_banner(sent_hms: str, conv_title: str, conv_link: str) -> None:
    title_display = (conv_title or "").strip() or "(new conversation)"
    link_display = (conv_link or "").strip() or "https://gemini.google.com/u/0/app"
    print(
        f"[+] PROMPT SENT at {sent_hms}\n"
        f"    conversation : {title_display}\n"
        f"    link         : {link_display}\n"
        "    Argon is working. Do NOT send this prompt again - run with --resume to keep waiting."
    )


def _next_wait_interval(step: int, base: float = 3.0, cap: float = 30.0) -> float:
    """Geometric backoff interval: 3 s, 6 s, 12 s, 24 s, 30 s, 30 s, ..."""
    if step <= 0:
        return base
    return min(cap, base * pow(2, min(step, 5)))


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    mins, secs = divmod(total, 60)
    return f"{mins} min {secs} s"


def _check_superseded_prompt(
    page,
    expected_prompt_text: str = "",
    expected_task_text: str = "",
    prev_state: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return a conflict description when a newer, DIFFERENT prompt was sent into the same chat (D22)."""
    last_user_raw = _last_user_message_text(page)
    if not last_user_raw:
        return None
    u_norm = _normalize_ws(last_user_raw)
    u_hash = _prompt_hash(last_user_raw)
    u_prefix = u_norm[:160]

    prev = prev_state if isinstance(prev_state, dict) else {}
    exp_p = (expected_prompt_text or "").strip()
    exp_t = (expected_task_text or str(prev.get("task") or "")).strip()
    exp_p_hash = _prompt_hash(exp_p) if exp_p else str(prev.get("prompt_hash") or "")
    exp_t_hash = _prompt_hash(exp_t) if exp_t else str(prev.get("task_hash") or "")
    exp_p_norm = _normalize_ws(exp_p) if exp_p else str(prev.get("prompt_prefix") or "")
    exp_t_norm = _normalize_ws(exp_t) if exp_t else ""

    if not (exp_p_hash or exp_t_hash or exp_p_norm or exp_t_norm):
        return None

    if exp_p_hash and u_hash == exp_p_hash:
        return None
    if exp_t_hash and u_hash == exp_t_hash:
        return None
    if exp_p_norm and len(exp_p_norm) >= 20 and (exp_p_norm[:120] in u_norm or u_norm[:120] in exp_p_norm):
        return None
    if exp_t_norm and len(exp_t_norm) >= 20 and exp_t_norm[:120] in u_norm:
        return None

    return (
        f"chat now has a different last user prompt ({u_prefix!r}) that superseded "
        f"the tracked task ({(exp_t_norm or exp_p_norm)[:120]!r})"
    )


def _known_chat_ids_for_slot(slot: Optional[str]) -> set:
    """Return all conversation IDs previously recorded across SessionStore, TaskState, and TaskRegistry (E3, D19.2).

    Collects chat IDs for `slot` AND across all active/recorded project lanes so a
    new session can never reuse the `single` or `followup` chat ID from the same
    or earlier round.
    """
    norm_s = normalize_slot(slot) if slot else "/u/0/"
    known: set = set()
    try:
        s_data = SessionStore().load().get("sessions") or {}
        for _, entry in s_data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("chat_id"):
                known.add(str(entry["chat_id"]))
            lanes = entry.get("lanes")
            if isinstance(lanes, dict):
                for l_info in lanes.values():
                    if isinstance(l_info, dict) and l_info.get("chat_id"):
                        known.add(str(l_info["chat_id"]))
    except Exception:
        pass
    try:
        t_data = TaskState().data.get("tasks") or {}
        for _, entry in t_data.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("chat_id"):
                known.add(str(entry["chat_id"]))
            lanes = entry.get("lanes")
            if isinstance(lanes, dict):
                for l_info in lanes.values():
                    if isinstance(l_info, dict) and l_info.get("chat_id"):
                        known.add(str(l_info["chat_id"]))
    except Exception:
        pass
    try:
        for entry in (TaskRegistry().data.get("entries") or []):
            if isinstance(entry, dict) and entry.get("chat_id"):
                known.add(str(entry["chat_id"]))
            if isinstance(entry, dict) and normalize_slot(str(entry.get("slot") or "/u/0/")) == norm_s:
                cid_u = _extract_chat_id(str(entry.get("chat_url") or ""))
                if cid_u:
                    known.add(cid_u)
    except Exception:
        pass
    return known


def _wait_for_conversation_loaded(page, timeout: float = 15.0) -> None:
    """Wait until the page has finished loading the conversation and earlier turns are rendered (E3.2)."""
    _wait_for_workspace(page, timeout=min(25.0, max(5.0, timeout)))
    try:
        page.wait_for_timeout(1800)
    except Exception:
        time.sleep(1.8)
    deadline = time.monotonic() + max(2.0, float(timeout))
    prev_snapshot = None
    stable_streak = 0
    while time.monotonic() < deadline:
        try:
            state = page.evaluate(
                """() => {
                    const ready = document.readyState === 'complete';
                    const spinners = Array.from(
                        document.querySelectorAll("mat-progress-spinner, .mat-progress-spinner, [role='progressbar'], .loading-spinner")
                    ).filter(e => !!(e.offsetWidth || e.offsetHeight));
                    const userTurns = document.querySelectorAll(
                        "user-query, [data-test-id='user-query'], .user-query-text, .query-text, message-turn.user-query, div.user-query"
                    ).length;
                    const modelTurns = document.querySelectorAll(
                        "model-response, [data-test-id='model-response'], .model-response-text, message-turn.model-response, div.model-response"
                    ).length;
                    return {
                        ready,
                        spinners: spinners.length,
                        userTurns,
                        modelTurns,
                        url: String(location.href || '')
                    };
                }"""
            )
        except Exception:
            state = None
        if isinstance(state, dict) and state.get("ready") and int(state.get("spinners", 0)) == 0:
            snap = (state.get("url"), state.get("userTurns"), state.get("modelTurns"))
            if snap == prev_snapshot:
                stable_streak += 1
                if stable_streak >= 2:
                    break
            else:
                prev_snapshot = snap
                stable_streak = 1
        try:
            page.wait_for_timeout(900)
        except Exception:
            time.sleep(0.9)


def _conversation_emptiness_report(page, forbidden: Optional[set] = None) -> tuple[bool, str, int, int]:
    """Return (is_empty_and_fresh, chat_id, user_turn_count, assistant_turn_count) after conversation loads (E3.1, E3.2)."""
    try:
        u_now = str(getattr(page, "url", "") or "")
    except Exception:
        u_now = ""
    cid_now = _extract_chat_id(u_now) or ""
    user_txt = _last_user_message_text(page)
    turns = _assistant_turns(page)
    asst_count = len(turns) if turns else 0
    dom_counts = {"user": 1 if user_txt else 0, "asst": asst_count}
    try:
        raw_counts = page.evaluate(
            """() => {
                const vis = el => !!(el && (el.offsetWidth || el.offsetHeight) && (el.innerText || '').trim().length > 0);
                const u = Array.from(document.querySelectorAll(
                    "user-query, [data-test-id='user-query'], .user-query-text, .query-text, message-turn.user-query, div.user-query"
                )).filter(vis).length;
                const m = Array.from(document.querySelectorAll(
                    "model-response, [data-test-id='model-response'], .model-response-text, message-turn.model-response, div.model-response"
                )).filter(vis).length;
                return { u, m };
            }"""
        )
        if isinstance(raw_counts, dict):
            dom_counts["user"] = max(dom_counts["user"], int(raw_counts.get("u") or 0))
            dom_counts["asst"] = max(dom_counts["asst"], int(raw_counts.get("m") or 0))
    except Exception:
        pass
    forb = forbidden or set()
    is_fresh = (
        (not cid_now or cid_now not in forb)
        and dom_counts["user"] == 0
        and dom_counts["asst"] == 0
    )
    return is_fresh, cid_now, dom_counts["user"], dom_counts["asst"]


def _start_fresh_chat(
    page,
    slot: str,
    isolated_profile: bool = True,
    forbidden_chat_ids: Optional[set] = None,
) -> bool:
    """Open and verify a GENUINELY empty new conversation after the page finishes loading (E3.1, E3.2, D19).

    Verifies AFTER the conversation finishes loading that:
      1. there is no user message and no assistant turn in the conversation, AND
      2. the conversation's chat_id is not already known for `slot` (or `forbidden_chat_ids`).
    If not proven, opens another new chat; if it still cannot, reports the exact
    `chat_id` it ended up in and returns False (never silently continues an old chat).
    """
    slot_part = "/u/0/" if isolated_profile else (normalize_slot(slot) if slot else "/u/0/")
    app_url = f"https://gemini.google.com{slot_part}app"
    forbidden: set = set(forbidden_chat_ids or set())
    forbidden.update(_known_chat_ids_for_slot(slot))

    try:
        cur_url_now = str(getattr(page, "url", "") or "")
        if not cur_url_now or "gemini.google.com" not in cur_url_now.lower():
            page.goto(app_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        return False

    # E3.2: Wait until the page has finished loading the conversation before inspecting turns or URL.
    _wait_for_conversation_loaded(page, timeout=15.0)

    # E3.1: a conversation that ALREADY carries a chat id is a RESTORED conversation -
    # this run did not create it, so an empty restored chat is still NOT a new chat.
    # Only the plain home URL (no id at all) or a chat we just opened with the
    # "New chat" control counts as a brand-new conversation.
    opened_new_chat = False

    def _is_genuinely_fresh() -> bool:
        _wait_for_conversation_loaded(page, timeout=10.0)
        ok_fresh, cid_now, u_cnt, a_cnt = _conversation_emptiness_report(page, forbidden)
        if cid_now and not opened_new_chat:
            ok_fresh = False
        if not ok_fresh and cid_now:
            forbidden.add(cid_now)
        return ok_fresh

    ok_initial, initial_cid, init_u, init_a = _conversation_emptiness_report(page, forbidden)
    if initial_cid and (not ok_initial or initial_cid in forbidden):
        forbidden.add(initial_cid)
        print(
            f"[i] Conversation {initial_cid} on {slot} is not a fresh empty chat "
            f"(user_turns={init_u}, assistant_turns={init_a}); opening a brand-new chat."
        )
    elif ok_initial and not initial_cid:
        # No conversation id at all: this IS Gemini's new-chat state.
        print(
            f"[+] Verified empty brand-new chat session on {slot} "
            f"(chat_id=(new), URL={getattr(page, 'url', app_url)}, known_prior_ids={len(forbidden)})."
        )
        return True
    elif ok_initial:
        print(
            f"[i] The page restored the existing conversation {initial_cid} on {slot} "
            f"(it is empty, but this run did not create it); opening a brand-new chat."
        )

    new_chat_selectors = (
        "a[aria-label*='New chat' i]",
        "button[aria-label*='New chat' i]",
        "[data-test-id='new-chat-button']",
        "[data-test-id*='new-chat']",
        "a[aria-label*='নতুন চ্যাট']",
        "button[aria-label*='নতুন চ্যাট']",
        "button[aria-label*='নতুন']",
        "a:has-text('New chat')",
        "button:has-text('New chat')",
        "a:has-text('নতুন চ্যাট')",
        "button:has-text('নতুন চ্যাট')",
        "text=নতুন চ্যাট",
        "text=New chat",
    )

    for attempt in range(4):
        clicked_new = False
        for sel in new_chat_selectors:
            try:
                loc = page.locator(sel)
                for i in range(min(loc.count(), 4)):
                    el = loc.nth(i)
                    if _safe_visible(el):
                        el.click(timeout=5000)
                        clicked_new = True
                        break
                if clicked_new:
                    break
            except Exception:
                continue

        if not clicked_new:
            for menu_sel in (
                "button[aria-label*='Main menu' i]",
                "button[aria-label*='প্রধান মেনু']",
                "button[aria-label*='Menu' i]",
                "[data-test-id='side-nav-menu-button']",
            ):
                try:
                    m_loc = page.locator(menu_sel)
                    if m_loc.count() > 0 and _safe_visible(m_loc.first):
                        m_loc.first.click(timeout=3000)
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    continue
            for sel in new_chat_selectors:
                try:
                    loc = page.locator(sel)
                    for i in range(min(loc.count(), 4)):
                        el = loc.nth(i)
                        if _safe_visible(el):
                            el.click(timeout=5000)
                            clicked_new = True
                            break
                    if clicked_new:
                        break
                except Exception:
                    continue

        if clicked_new:
            # E3.1: we really opened a new chat, so a chat id that appears now is
            # one this run created - it may be accepted.
            opened_new_chat = True

        if _is_genuinely_fresh():
            _, verified_cid, _, _ = _conversation_emptiness_report(page, forbidden)
            print(
                f"[+] Verified brand-new empty chat session on {slot} "
                f"(chat_id={verified_cid or '(new)'}, URL={getattr(page, 'url', app_url)}, forbidden prior IDs={len(forbidden)})."
            )
            return True

        try:
            page.goto(app_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass

    _, ended_cid, ended_u, ended_a = _conversation_emptiness_report(page, forbidden)
    ended_url = str(getattr(page, "url", "") or app_url)
    print(
        f"[ERROR] Failed to open a verified empty new chat on {slot}: ended up in "
        f"chat_id={ended_cid or '(no-chat-id)'!r} (url={ended_url!r}, "
        f"user_messages={ended_u}, assistant_turns={ended_a}). "
        "Refusing to silently continue an old conversation!",
        file=sys.stderr,
    )
    return False


def _carry_over_to_new_session(
    page,
    prompt_text: str,
    proj_root: Path,
    mode: str,
    slot: str,
    email: str,
    zip_path: Path,
    manual_setup: bool,
    task_state: "TaskState",
    session_store: "SessionStore",
    previous_text: str,
    notes: str,
    reason: str,
    task: str = "",
    target_files: Optional[List[str]] = None,
    deadline: Optional[float] = None,
    profile_dir: Optional[Path] = None,
    diagnostic_evidence: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """Start a NEW session and send the carry-over prompt there.

    Used when a run finished WITHOUT a delivery: Gemini stopped working (or the
    answer never came), so the work is taken to a fresh conversation together
    with the context and measured diagnostic evidence of what the previous round
    already did.
    """
    print(
        "[!] The run finished WITHOUT a delivery - carrying the previous context "
        "and diagnostic evidence over into a NEW session.",
        file=sys.stderr,
    )
    base_prompt = (prompt_text or "").strip() or build_surgical_prompt(
        task or "Continue and complete the previous task",
        target_directory=str(proj_root),
        is_continuation=True,
    )
    new_prompt = _build_carry_over_prompt(
        base_prompt,
        previous_text,
        notes,
        reason,
        diagnostic_evidence=diagnostic_evidence,
    )

    isolated = _profile_is_isolated_slot(profile_dir) if profile_dir is not None else True
    if not _start_fresh_chat(page, slot, isolated_profile=isolated):
        print("[!] Could not open a new chat for the carry-over.", file=sys.stderr)
        return None

    # The workspace must be set up again in the new session.
    try:
        if not manual_setup:
            _retry(lambda p: select_task_mode(p, require_verifiable=True), page)
    except Exception:
        pass
    if not ensure_argon_model(page):
        print("[!] The new session could not be put on 4 Argon - carry-over abandoned.",
              file=sys.stderr)
        return None

    # The project may have changed in the previous round, so the context is
    # packed again and re-attached.
    try:
        package_context(
            project_path=str(proj_root),
            output_zip=str(zip_path),
            task_description=task,
            target_files=target_files,
        )
    except Exception as exc:
        print(f"[!] Could not repack the context for the carry-over: {exc}",
              file=sys.stderr)
    try:
        attach_zip(page, zip_path)
    except Exception as exc:
        print(f"[!] Could not attach the context in the new session: {exc}",
              file=sys.stderr)
        return None
    page.wait_for_timeout(5000)
    if not _attachment_present(page, zip_path):
        print("[!] The context did not attach in the new session.", file=sys.stderr)
        return None

    comp = _find_composer(page)
    if comp is None:
        print("[!] No composer in the new session.", file=sys.stderr)
        return None
    # The double check again - a new session starts on the default model.
    if not ensure_argon_model(page):
        print("[!] The new session is not on 4 Argon - carry-over abandoned.",
              file=sys.stderr)
        return None
    turns_before_co = _assistant_turns(page)
    responses_before_co = len(turns_before_co) if turns_before_co is not None else 0
    chips_before_co = len(_generated_file_chip_indices(page))
    try:
        _type_and_send(page, comp, new_prompt)
    except Exception as exc:
        print(f"[!] The carry-over prompt could not be sent: {exc}", file=sys.stderr)
        return None
    sent_hms = time.strftime("%H:%M:%S")
    _take_step_screenshot(page, proj_root, "sent.png")
    print("[+] Carry-over prompt sent in the NEW session.")

    # The new conversation replaces the stored one for this project.
    chat_id = _wait_for_chat_id(page)
    chat_title = _chat_title(page)
    chat_url = page.url
    if chat_id:
        try:
            prof_str = str(profile_dir) if profile_dir is not None else ""
            chat_url = (
                session_store.remember(
                    proj_root, mode, slot, chat_id, chat_title=chat_title, profile_dir=prof_str
                )
                or page.url
            )
            task_state.set(
                proj_root,
                chat_id=chat_id,
                chat_url=chat_url,
                chat_title=chat_title,
                slot=slot,
                email=email,
                mode=mode,
                state="sent",
                task=task or new_prompt,
                prompt_hash=_prompt_hash(new_prompt),
                task_hash=_prompt_hash(task or new_prompt),
                sent_at=sent_hms,
            )
            print(f"[+] The new session is remembered: {chat_url}")
        except Exception:
            pass
    _print_prompt_sent_banner(sent_hms, chat_title, chat_url)

    # Wait for the delivery in the new session with geometric backoff (passing previous_hashes - D1.1).
    co_previous_hashes: set = set()
    try:
        co_previous_hashes.update(DeliveryLedger().known_hashes(proj_root))
        prev_st = task_state.get(proj_root) or {}
        if prev_st.get("delivery_hash"):
            co_previous_hashes.add(str(prev_st["delivery_hash"]))
    except Exception:
        pass
    if deadline is None:
        deadline = time.monotonic() + 900.0
    co_step = 0
    while time.monotonic() < deadline:
        try:
            new_chip = _new_delivery_chip(page, chips_before_co, responses_before_co)
            if new_chip is None:
                new_chip = _generated_file_chip(page)
            if new_chip is not None:
                got = capture_generated_file(
                    page,
                    proj_root / "downloads",
                    chip=new_chip,
                    previous_hashes=co_previous_hashes,
                    project_path=proj_root,
                )
                if got is not None:
                    _take_step_screenshot(
                        page,
                        proj_root,
                        "delivered.png",
                        step_label="delivered",
                        state_label=f"Captured carry-over patch {got.name}",
                    )
                    print(f"[+] Captured the patch from the NEW session: {got}")
                    return got
        except Exception:
            pass
        wait_s = _next_wait_interval(co_step)
        co_step += 1
        try:
            page.wait_for_timeout(int(wait_s * 1000))
        except Exception:
            time.sleep(wait_s)
    print("[!] The NEW session did not deliver a patch either.", file=sys.stderr)
    return None


def _notes_from_body(text: str) -> str:
    """The NOTE lines a conversation recorded - the advice Argon left behind."""
    out = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s.startswith("#"):
            continue
        body = s.lstrip("#").strip()
        if re.match(
            r"(?i)^(note|inconsisten|issue|concern|warning|suggest|recommend|caveat|todo)\b",
            body,
        ):
            out.append(s)
    return "\n".join(out)


def list_sessions() -> int:
    """Print every recorded session, so a run can always be found again.

    Shows the project, the name of the conversation, the link, the account and
    the browser profile it was run in, plus whether the work finished.
    """
    print("=" * 78)
    print("  RECORDED SESSIONS")
    print("=" * 78)
    store = SessionStore()
    tasks = TaskState()
    sessions = store.data.get("sessions") or {}
    states = tasks.data.get("tasks") or {}

    if not sessions and not states:
        print("  (nothing recorded yet)")
        print(f"  session map : {store.path}")
        print(f"  task states : {tasks.path}")
        return 0

    keys = sorted(set(list(sessions.keys()) + list(states.keys())))
    for key in keys:
        entry = sessions.get(key) or {}
        state = states.get(key) or {}
        print("")
        print(f"  project      : {key}")
        title = entry.get("chat_title") or state.get("chat_title") or ""
        if title:
            print(f"  chat name    : {title}")
        url = entry.get("chat_url") or state.get("chat_url") or ""
        if url:
            print(f"  link         : {url}")
            if "aistudio.google.com" in url:
                print("                  !! STALE - this is a Google AI Studio link. "
                      "AI Studio was removed, so this link cannot be opened.")
        slot = entry.get("slot") or state.get("slot") or ""
        number = entry.get("profile_number")
        if number is None:
            number = state.get("profile_number")
        if number is None and slot:
            number = _slot_number(slot)
        if slot:
            print(f"  account      : {slot}" + (f"   (profile number {number})" if number is not None else ""))
        profile = entry.get("profile_dir") or state.get("profile_dir") or ""
        if profile:
            print(f"  profile dir  : {profile}")
        print(f"  state        : {state.get('state') or '(unknown)'}")
        delivered = state.get("delivered") or ""
        if delivered:
            print(f"  delivery     : {delivered}")
        winner_val = entry.get("winner")
        lanes_map = entry.get("lanes") if isinstance(entry.get("lanes"), dict) else {}
        if winner_val or lanes_map:
            print(f"  winner       : {winner_val or '(none recorded)'}")
        for lane_key in ("followup", "new"):
            ld = lanes_map.get(lane_key)
            if isinstance(ld, dict) and (ld.get("chat_id") or ld.get("chat_url") or ld.get("slot")):
                status_tag = ""
                if winner_val in ("followup", "new"):
                    status_tag = " [WINNER]" if winner_val == lane_key else " [dropped]"
                l_slot = ld.get("slot") or ""
                l_cid = ld.get("chat_id") or ""
                l_url = ld.get("chat_url") or ""
                print(f"  lane [{lane_key:<8}]: slot={l_slot} chat_id={l_cid} url={l_url}{status_tag}")
                if ld.get("patch_path"):
                    print(f"                   patch={ld['patch_path']}")
        updated = state.get("updated_at") or entry.get("updated_at") or ""
        if updated:
            print(f"  updated      : {updated}")
    print("")
    print(f"  session map : {store.path}")
    print(f"  task states : {tasks.path}")
    return 0


def record_winner_cli(project_path: str, winner: str, mode: str = "gemini") -> int:
    """Record the winning lane ('followup'/'old' or 'new') for `project_path` from the CLI."""
    store = SessionStore()
    try:
        updated = store.record_winner(project_path, winner)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if updated is None:
        print(
            f"[ERROR] Could not record winner {winner!r} for {Path(project_path).resolve()}: "
            "no matching session or lane chat_id found in the session store.",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print(f"[+] Recorded winner for project: {Path(project_path).resolve()}")
    print(f"    winner       : {updated.get('winner')}")
    print(f"    winning slot : {updated.get('slot')} (profile number {updated.get('profile_number')})")
    print(f"    winning chat : {updated.get('chat_id')}")
    print(f"    winning url  : {updated.get('chat_url')}")
    print(f"    session map  : {store.path}")
    return EXIT_SUCCESS


def forget_sessions(target: str = "") -> int:
    """Remove recorded sessions - for a project, or every one when empty."""
    store = SessionStore()
    tasks = TaskState()
    needle = (target or "").strip().lower()
    removed = 0
    for store_obj, bucket in ((store, "sessions"), (tasks, "tasks")):
        entries = store_obj.data.get(bucket) or {}
        for key in list(entries.keys()):
            if needle and needle not in str(key).lower():
                continue
            entries.pop(key, None)
            removed += 1
        store_obj.data[bucket] = entries
        store_obj.save()
    print(f"[+] Removed {removed} recorded session entr{'y' if removed == 1 else 'ies'}.")
    return 0


def _extract_verification_command_from_text(text: str) -> str:
    """Extract a verification command from TASK_BRIEF.md or Argon notes under VERIFICATION COMMAND."""
    if not text:
        return ""
    sec_m = re.search(
        r"(?ims)^[ \t]*#*[ \t]*VERIFICATION\s+COMMAND[ \t]*:?[ \t]*\r?\n(.*?)(?=^[ \t]*#{1,6}[ \t]+\S|\Z)",
        text,
    )
    candidates: List[str] = []
    if sec_m:
        sec_body = sec_m.group(1) or ""
        for m_bt in re.findall(r"`([^`\r\n]+)`", sec_body):
            candidates.append(m_bt.strip())
        for raw_ln in sec_body.splitlines():
            clean_ln = re.sub(r"^[ \t#>*+-]+", "", raw_ln).strip()
            for pfx in ("detected command:", "detected root marker command:", "archive verification command:"):
                if clean_ln.lower().startswith(pfx):
                    clean_ln = clean_ln.split(":", 1)[1].strip().strip("`").strip()
            if clean_ln:
                candidates.append(clean_ln)
    for m_inline in re.findall(r"(?im)^[ \t#]*verification\s+command\s*:\s*`?([^`\r\n]+)`?", text):
        candidates.append(m_inline.strip())
    for cand in candidates:
        low = cand.lower()
        if (
            not cand
            or low.startswith("(none")
            or "if automatic detection" in low
            or "detected command" in low
            or "verification command" in low
        ):
            continue
        if any(
            low.startswith(prefix)
            for prefix in (
                "cargo ", "npm ", "npx ", "pnpm ", "yarn ", "ruff ", "python", "pytest",
                "go ", "mvn ", "./gradlew", "gradlew", "dotnet ", "make", "mix ", "bundle ",
                "pwsh ", "powershell ",
            )
        ):
            return cand
    return ""


def detect_project_verification_command(
    project_path: Path,
    zip_path: Optional[Path] = None,
    extra_texts: Optional[List[str]] = None,
) -> str:
    """Detect the project verification command from root markers or fallback to TASK_BRIEF.md / notes."""
    from package_context import detect_verification_command as _pkg_detect

    proj_root = Path(project_path).resolve()
    cmd = _pkg_detect(proj_root)
    if cmd:
        return cmd

    texts: List[str] = list(extra_texts or [])
    brief_file = proj_root / "TASK_BRIEF.md"
    if brief_file.is_file():
        try:
            texts.append(brief_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
    if zip_path is not None and Path(zip_path).is_file():
        try:
            import zipfile as _zf

            with _zf.ZipFile(zip_path, "r") as zf:
                if "TASK_BRIEF.md" in zf.namelist():
                    texts.append(zf.read("TASK_BRIEF.md").decode("utf-8", errors="ignore"))
        except Exception:
            pass
    for t in texts:
        extracted = _extract_verification_command_from_text(t)
        if extracted:
            return extracted
    return ""


def _count_verification_errors(output: str, returncode: int) -> int:
    """Count how many compiler/linter/test error lines came back."""
    if returncode == 0:
        return 0
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return 1

    rust_diag = [
        ln for ln in lines
        if re.match(r"^error(\[[A-Z0-9]+\])?\s*:", ln)
        and not re.search(r"could not compile|aborting due to \d+ previous error", ln, re.IGNORECASE)
    ]
    if rust_diag:
        return len(rust_diag)

    summary_m = re.search(r"aborting due to (\d+) previous error", output or "", re.IGNORECASE)
    if summary_m:
        try:
            return max(1, int(summary_m.group(1)))
        except Exception:
            pass

    err_lines = [
        ln for ln in lines
        if (
            re.search(r"\berror\s+TS\d+\s*:", ln)
            or re.match(r"^\S+:\d+:\d+:\s*(?:error|fatal|[A-Z]+\d+)\b", ln, re.IGNORECASE)
            or re.match(r"^(?:FAILED|ERROR|E)\b", ln)
            or re.search(r"\b(?:SyntaxError|IndentationError|ImportError|TypeError|ValueError|CompileError):", ln)
            or (
                re.search(r"(?i)(?:^|\b)(?:error|fatal|failed|exception)(?:\b|\[|:)", ln)
                and not re.search(r"(?i)\b0\s+errors?\b|\b0\s+failed\b|could not compile", ln)
            )
        )
    ]
    if err_lines:
        return len(err_lines)
    return max(1, len(lines))


def run_verification_command(
    project_path: Path,
    command: str,
    timeout_seconds: int = 900,
) -> tuple[bool, int, str]:
    """Run the verification command in project_path with a deadline, capturing stdout and stderr."""
    project_path = Path(project_path).resolve()
    try:
        proc = subprocess.run(
            command,
            cwd=str(project_path),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=max(1, int(timeout_seconds)),
        )
        combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode == 0:
            return True, 0, combined
        err_count = _count_verification_errors(combined, proc.returncode)
        return False, err_count, combined
    except subprocess.TimeoutExpired as exc:
        out_part = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err_part = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        combined = (
            "\n".join([f"Verification command timed out after {timeout_seconds} seconds: {command}", out_part, err_part])
        ).strip()
        return False, max(1, _count_verification_errors(combined, 1)), combined
    except Exception as exc:
        msg = f"Could not execute verification command ({command}): {exc}"
        return False, 1, msg


def _trim_verification_diagnostics(output: str, max_chars: int = 10000) -> str:
    text = (output or "").strip()
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...(middle of compiler output trimmed)...\n" + text[-half:]


def _record_verification_in_notes(
    notes_path: Optional[Path],
    project_path: Path,
    command: str,
    round_num: int,
    max_rounds: int,
    passed: bool,
    err_count: int,
    diagnostics: str = "",
) -> Optional[Path]:
    """Record the verification result in the Argon notes file."""
    out_dir = Path(project_path) / ".argon_backup"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = notes_path if (notes_path is not None and Path(notes_path).is_file()) else (
        out_dir / ("argon_notes_" + time.strftime("%Y%m%d_%H%M%S") + ".md")
    )
    status_str = "clean" if passed else f"FAILED ({err_count} error(s))"
    h2 = "#" * 2
    section_lines = [
        "",
        f"{h2} Build / Lint Verification Result",
        f"Command: `{command or '(skipped - no matching project marker)'}`",
        f"Round: {round_num}/{max_rounds}",
        f"Result: {status_str}",
    ]
    if not passed and diagnostics.strip():
        fence = "`" * 3
        section_lines += [
            "",
            f"{fence}text",
            _trim_verification_diagnostics(diagnostics, max_chars=4000),
            f"{fence}",
        ]
    section_text = "\n".join(section_lines) + "\n"
    try:
        if target.is_file():
            existing = target.read_text(encoding="utf-8", errors="replace")
            target.write_text(existing.rstrip() + "\n" + section_text, encoding="utf-8")
        else:
            h1 = "#"
            target.write_text(
                f"{h1} Argon notes and suggestions\n" + section_text,
                encoding="utf-8",
            )
        return target
    except Exception:
        return notes_path


def _verify_project_locally(
    proj_root: Path,
    task_state: TaskState,
    notes_path: Optional[Path],
    zip_path: Optional[Path] = None,
    extra_texts: Optional[List[str]] = None,
    verify_timeout: int = 900,
    verify_cmd_override: Optional[str] = None,
    skip_verify: bool = False,
) -> int:
    """Run the project's own build/lint command ONCE, locally, and report it.

    NOTHING is sent to Gemini here. The build runs on this machine, so it costs
    no quota at all.

    When the project does not build, the errors are printed and recorded in the
    task state and in the notes file, and the run ends with EXIT_BUILD_FAILED.
    The AGENT that drove the bridge fixes those lint and compile errors itself -
    they are small, local and cheap to fix. Only a genuinely major or difficult
    task is worth another Argon round, and that decision belongs to the agent,
    not to this script.
    """
    if skip_verify:
        print("[+] Skipping project build verification (--skip-verify).")
        task_state.set(proj_root, state="applied", verification_state="skipped", verification_command="")
        return EXIT_SUCCESS

    cmd = (verify_cmd_override or "").strip() or detect_project_verification_command(
        proj_root, zip_path=zip_path, extra_texts=extra_texts
    )
    if not cmd:
        print(
            "[+] No verification command matched the project root markers "
            "(and none specified under VERIFICATION COMMAND) - skipping build verification."
        )
        task_state.set(proj_root, state="applied", verification_state="skipped", verification_command="")
        _record_verification_in_notes(notes_path, proj_root, "", 1, 1, True, 0)
        return EXIT_SUCCESS

    passed, err_count, verify_out = run_verification_command(
        proj_root, cmd, timeout_seconds=verify_timeout
    )
    err_word = "error" if err_count == 1 else "errors"

    if passed:
        print(f"[+] Project verification: {cmd} -> clean")
        task_state.set(
            proj_root,
            state="applied",
            verification_state="clean",
            verification_command=cmd,
            verification_rounds=1,
            verification_errors=0,
        )
        _record_verification_in_notes(
            notes_path, proj_root, cmd, 1, 1, True, 0, verify_out
        )
        return EXIT_SUCCESS

    print(f"[+] Project verification: {cmd} -> {err_count} {err_word}")
    print(
        "[!] The patched project does not build yet. The errors are recorded for "
        "the agent: fix them locally - they are small and cost no quota.",
        file=sys.stderr,
    )
    if verify_out.strip():
        print(verify_out.strip(), file=sys.stderr)
    task_state.set(
        proj_root,
        state="verify_failed",
        verification_state="failed",
        verification_command=cmd,
        verification_rounds=1,
        verification_errors=err_count,
        verification_output=(verify_out or "")[-8000:],
    )
    _record_verification_in_notes(
        notes_path, proj_root, cmd, 1, 1, False, err_count, verify_out
    )
    return EXIT_BUILD_FAILED


def dispatch_to_argon(
    task: str,
    project_path: str = ".",
    mode: str = "gemini",
    slot: Optional[str] = None,
    headless: bool = False,
    auto_apply: bool = False,
    manual_setup: bool = False,
    target_files: Optional[List[str]] = None,
    output_zip: str = "argon_context.zip",
    new_session: bool = False,
    resume: bool = False,
    carry_over: bool = True,
    verify_timeout: int = 900,
    verify_cmd: Optional[str] = None,
    skip_verify: bool = False,
    prompt_file: Optional[str] = None,
    delivery_dir: Optional[str] = None,
    lane: Optional[str] = None,
    skip_package: bool = False,
    force: bool = False,
    deadline_seconds: int = 1800,
    brief_summary: Optional[str] = None,
) -> int:
    proj_root = Path(project_path).resolve()
    zip_path = proj_root / output_zip if not Path(output_zip).is_absolute() else Path(output_zip)
    if new_session and not lane:
        lane = "new"
    if delivery_dir:
        active_downloads_dir = Path(delivery_dir).resolve()
    elif lane in ("followup", "new"):
        active_downloads_dir = (proj_root / "downloads" / lane).resolve()
    else:
        active_downloads_dir = (proj_root / "downloads").resolve()
    active_downloads_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load account pool configuration & session/task state/registry stores
    pool_cfg = PoolConfig()
    session_store = SessionStore()
    task_state_store = TaskState()
    task_registry = TaskRegistry()
    delivery_ledger = DeliveryLedger()

    # D4.5: Surface any STALE tasks in the registry (with their age and screenshot)
    stale_list = task_registry.stale_tasks()
    if stale_list:
        print("[!] STALE tasks detected in ~/.gemini/argon_tasks.json (started but never marked completed):", file=sys.stderr)
        for st_item in stale_list:
            print(
                f"    - project={st_item.get('project')} lane={st_item.get('lane')} "
                f"slot={st_item.get('slot')} chat_id={st_item.get('chat_id') or '(none)'} "
                f"age={st_item.get('age_human')} screenshot={st_item.get('screenshot_path') or '(none)'}",
                file=sys.stderr,
            )
        print(
            f'    Close stale tasks explicitly with: python "{SCRIPTS_DIR / "dispatch_argon.py"}" '
            f'--project-path "{proj_root}" --register-task-done',
            file=sys.stderr,
        )

    # D2.1, D2.2, D22.1: Before starting a new run, check whether a task is already running
    # for this project (and lane, if specified). Refuse with EXIT_ALREADY_RUNNING (9) unless
    # --resume or --force was explicitly passed.
    if not resume:
        existing_lane_state = task_state_store.get(proj_root, lane=lane) if lane else None
        running_lanes = task_state_store.get_running_lanes(proj_root)
        blocking_entry: Optional[Dict[str, Any]] = None
        if lane:
            if existing_lane_state and str(existing_lane_state.get("state") or "").strip().lower() in ("sent", "waiting"):
                blocking_entry = existing_lane_state
        elif running_lanes:
            first_k = next(iter(running_lanes))
            blocking_entry = running_lanes[first_k]

        if blocking_entry is None:
            reg_active = task_registry.active_tasks_for_project(proj_root, lane=lane)
            if reg_active and (reg_active[0].get("chat_id") or reg_active[0].get("chat_url")):
                blocking_entry = reg_active[0]

        if blocking_entry is not None:
            b_slot = str(blocking_entry.get("slot") or "/u/0/")
            b_cid = str(blocking_entry.get("chat_id") or "") or (_extract_chat_id(str(blocking_entry.get("chat_url") or "")) or "(pending)")
            b_url = str(blocking_entry.get("chat_url") or "https://gemini.google.com/u/0/app")
            b_state = str(blocking_entry.get("state") or blocking_entry.get("status") or "running")
            b_shot = str(blocking_entry.get("last_screenshot") or blocking_entry.get("screenshot_path") or "(none)")
            b_sent = str(blocking_entry.get("sent_at") or blocking_entry.get("started_at") or "?")
            if not force:
                print("\n" + "!" * 78, file=sys.stderr)
                print(
                    f"[ERROR] A task is ALREADY RUNNING for project {proj_root} (state={b_state!r}, sent_at={b_sent})!",
                    file=sys.stderr,
                )
                print(
                    "  Sending another prompt now would SUPERSEDE the unfinished task or block the account.",
                    file=sys.stderr,
                )
                print(f"  - slot        : {b_slot} ({blocking_entry.get('email') or blocking_entry.get('account') or 'pool account'})", file=sys.stderr)
                print(f"  - chat_id     : {b_cid}", file=sys.stderr)
                print(f"  - link        : {b_url}", file=sys.stderr)
                print(f"  - state       : {b_state}", file=sys.stderr)
                print(f"  - screenshot  : {b_shot}", file=sys.stderr)
                print("\n  Choose one of the two explicit actions:", file=sys.stderr)
                print(
                    f'    1. Resume waiting for the running task:\n'
                    f'       python "{SCRIPTS_DIR / "dispatch_argon.py"}" --project-path "{proj_root}" --resume',
                    file=sys.stderr,
                )
                print(
                    f'    2. Force a new dispatch (explicitly abandoning/superseding the previous task):\n'
                    f'       python "{SCRIPTS_DIR / "dispatch_argon.py"}" --project-path "{proj_root}" --task "..." --force',
                    file=sys.stderr,
                )
                print("!" * 78 + "\n", file=sys.stderr)
                return EXIT_ALREADY_RUNNING
            else:
                print(
                    f"[!] --force specified: marking previous running task (chat={b_cid}, slot={b_slot}) "
                    "as 'superseded' before starting new run."
                )
                task_state_store.set(
                    proj_root,
                    lane=lane,
                    state="superseded",
                    superseded_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                task_registry.register_done(proj_root, lane=lane, status="superseded")

    # RESUME: the browser was closed during an earlier run. The task state
    # remembers which conversation the task was in, so the run reopens it
    # instead of packaging and sending a second prompt.
    resume_entry = (task_state_store.get(proj_root, lane=lane) or task_state_store.get(proj_root)) if resume else None
    resume_chat = bool(
        resume_entry
        and resume_entry.get("chat_url")
        and resume_entry.get("state") in ("sent", "waiting", "delivered")
    )
    if resume and not resume_chat:
        print(
            f"[!] No unfinished task found in {task_state_store.path} for {proj_root}.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    if resume_chat and not task and resume_entry:
        task = str(resume_entry.get("task") or "")

    # If --resume sees a delivered file that is already on disk and valid,
    # apply or report it directly without even needing a browser.
    if resume_chat and resume_entry and resume_entry.get("delivered"):
        existing_delivery = Path(str(resume_entry["delivered"]))
        if existing_delivery.is_file():
            try:
                existing_text = existing_delivery.read_text(encoding="utf-8", errors="replace")
            except Exception:
                existing_text = ""
            if _looks_like_patch(existing_text):
                print(f"[+] Resuming from already-captured patch file: {existing_delivery}")
                saved_resume_notes = _save_notes(existing_text, proj_root)
                if auto_apply:
                    ok = apply_delivered_patch(proj_root, existing_delivery)
                    if not ok:
                        return EXIT_FAILURE
                    task_state_store.set(proj_root, state="applied", delivered=str(existing_delivery))
                    return _verify_project_locally(
                        proj_root=proj_root,
                        task_state=task_state_store,
                        notes_path=saved_resume_notes,
                        zip_path=zip_path,
                        extra_texts=[existing_text],
                        verify_timeout=verify_timeout,
                        verify_cmd_override=verify_cmd,
                        skip_verify=skip_verify,
                    )
                return EXIT_SUCCESS

    # 2. Package codebase context (skipped on --resume or when --skip-package reuses a pre-built archive)
    if not resume_chat and not (skip_package and zip_path.is_file()):
        package_context(
            project_path=str(proj_root),
            output_zip=str(zip_path),
            task_description=task,
            target_files=target_files,
        )
    if not resume_chat and zip_path.is_file():
        ok_z, z_bytes, z_entries, z_err = _inspect_zip_archive(zip_path)
        if not ok_z:
            print(f"[ERROR] Archive validation failed before upload: {z_err}", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED
        print(f"[+] Archive ready to upload: {zip_path} ({z_bytes} bytes, {z_entries} entries)")

    # D3: Apply Account Policy when selecting active_slot
    if slot:
        active_slot = normalize_slot(slot)
        slot_reason = f"explicitly requested slot {active_slot}"
    else:
        active_slot, slot_reason = select_account_slot(
            pool_cfg=pool_cfg,
            project_path=proj_root,
            mode=mode,
            requested_slot=slot,
            new_session=new_session,
            resume=resume_chat,
            lane=lane,
        )
    print(f"[+] Chosen account slot: {active_slot} ({slot_reason})")

    # Section 3 (No Hard Blocking) & Rule 1-3: Never refuse or block when an account is busy.
    if not resume_chat:
        slot_ok, slot_cap_reason = task_registry.is_slot_available(active_slot, max_tasks=MAX_TASKS_PER_SLOT)
        if not slot_ok:
            print(
                f"[+] Account at {active_slot} is busy ({slot_cap_reason}); "
                "continuing on this slot without blocking (limit is advisory, never a hard block)."
            )

    # 3. Session continuity: a follow-up task on the same project continues in
    #    the SAME Gemini conversation when running on the same mode and slot.
    base_profile = pool_cfg.profile_dir
    base_profile.mkdir(parents=True, exist_ok=True)
    slot_profile = _profile_dir_for_slot(base_profile, active_slot)
    print(f"[+] Slot browser profile: {slot_profile}")

    session = None if (new_session or lane == "new" or resume_chat) else session_store.lookup(proj_root, mode=mode)
    if session and (session.get("mode") or "gemini") != (mode or "gemini").strip().lower():
        session = None
    if session and lane == "followup":
        s_lanes = session.get("lanes") if isinstance(session.get("lanes"), dict) else {}
        f_lane_entry = s_lanes.get("followup") if isinstance(s_lanes.get("followup"), dict) else {}
        if f_lane_entry.get("chat_url") and (not slot or normalize_slot(str(f_lane_entry.get("slot") or active_slot)) == active_slot):
            session = dict(session)
            session.update({k: v for k, v in f_lane_entry.items() if v})
    # A Gemini conversation URL belongs to the Google account slot it was created in.
    # If an explicit slot was requested (e.g. during pool rotation) that differs from
    # the stored session's slot, start a fresh chat on the requested slot.
    if session and slot is not None and session.get("slot"):
        if normalize_slot(session["slot"]) != active_slot:
            session = None

    is_continuation = bool(session and session.get("chat_url") and not resume_chat and not new_session and lane != "new")
    if resume_chat and resume_entry is not None:
        if slot is None and resume_entry.get("slot"):
            active_slot = normalize_slot(resume_entry["slot"])
            slot_profile = _profile_dir_for_slot(base_profile, active_slot)
        pool_cfg.set_active_slot(active_slot)
        active_account = pool_cfg.get_by_slot(active_slot)
        target_email = resume_entry.get("email") or (active_account.get("email") if active_account else None)
        isolated_slot = _profile_is_isolated_slot(slot_profile)
        target_url = _resolve_reopen_url(
            proj_root=proj_root,
            mode=mode,
            active_slot=active_slot,
            current_url=str(resume_entry.get("chat_url") or ""),
            fallback_url=get_target_url(mode, active_slot, isolated_profile=isolated_slot),
            task_state=task_state_store,
            session_store=session_store,
            isolated_profile=isolated_slot,
        )
    elif is_continuation and session is not None:
        if slot is None and session.get("slot"):
            active_slot = normalize_slot(session["slot"])
            slot_profile = _profile_dir_for_slot(base_profile, active_slot)
        pool_cfg.set_active_slot(active_slot)
        active_account = pool_cfg.get_by_slot(active_slot)
        target_email = active_account.get("email") if active_account else None
        isolated_slot = _profile_is_isolated_slot(slot_profile)
        target_url = _normalize_gemini_chat_url(str(session["chat_url"]), isolated_profile=isolated_slot)
    else:
        pool_cfg.set_active_slot(active_slot)
        active_account = pool_cfg.get_by_slot(active_slot)
        target_email = active_account.get("email") if active_account else None
        isolated_slot = _profile_is_isolated_slot(slot_profile)
        target_url = get_target_url(mode, active_slot, isolated_profile=isolated_slot)

    prior_task_entry = task_state_store.get(proj_root) or {}
    prior_diag_evidence: Optional[Dict[str, Any]] = None
    if isinstance(prior_task_entry.get("diagnostic_evidence"), dict) and str(
        prior_task_entry.get("state") or ""
    ).strip().lower() not in ("applied", "delivered"):
        prior_diag_evidence = prior_task_entry["diagnostic_evidence"]

    # 4. Build surgical prompt (or read pre-rendered prompt from --prompt-file for dual lanes)
    if resume_chat:
        prompt_text = ""
    elif prompt_file and Path(prompt_file).is_file():
        prompt_text = Path(prompt_file).read_text(encoding="utf-8")
    else:
        prompt_text = build_surgical_prompt(
            task,
            target_directory=str(proj_root),
            is_continuation=is_continuation,
            diagnostic_evidence=prior_diag_evidence,
        )
    print(f"[+] Target directory: {proj_root}")
    print(f"[+] Target Mode: {mode} -> {target_url}")
    print(f"[+] Active Pool Slot: {active_slot} ({target_email or 'default profile session'})")
    if resume_chat:
        print(f"[+] Resuming unfinished task in conversation: {target_url}")
    elif is_continuation:
        print(f"[+] Continuing in the existing session for this project: {target_url}")
    else:
        print("[+] Starting a new session for this project (no stored chat to continue).")

    from playwright.sync_api import sync_playwright


    # Every pool slot gets its own browser profile directory, so two runs on
    # two different slots never fight over one Chrome profile. The slot lock
    # stops two runs from using the SAME slot at once.
    slot_lock = SlotLock(active_slot, profile_dir=slot_profile)
    lock_acquired = slot_lock.acquire(slot_profile)
    temp_concurrent_profile: Optional[Path] = None

    context = None
    try:
        if not lock_acquired or _profile_is_busy(slot_profile):
            print(
                f"[+] Slot {active_slot} profile ({slot_profile}) already has an active run; "
                f"cloning a concurrent profile directory for {active_slot} so this run continues without blocking."
            )
            concurrent_cand = slot_profile.parent / f"{slot_profile.name}-concurrent-{os.getpid()}"
            src_for_clone = slot_profile if (slot_profile / "Default").is_dir() else base_profile
            cloned_prof = _copy_profile(src_for_clone, concurrent_cand, allow_busy=True)
            if cloned_prof is not None:
                temp_concurrent_profile = cloned_prof
                slot_profile = cloned_prof

        profile_dir = slot_profile
        try:
            needs_seed = not (slot_profile.is_dir() and (slot_profile / "Default").is_dir()) or (
                _has_cookie_db(base_profile) and not _has_cookie_db(slot_profile)
            )
            seeded_ok = True
            if needs_seed:
                print(f"[+] Preparing a browser profile for slot {active_slot}: {slot_profile}")
                seeded_ok = _seed_profile(base_profile, slot_profile)

            if seeded_ok and slot_profile.is_dir() and (slot_profile / "Default").is_dir():
                profile_dir = slot_profile
            elif not (base_profile / "Default").is_dir():
                profile_dir = slot_profile
            else:
                if _profile_is_busy(base_profile):
                    print(
                        f"[!] Could not prepare slot profile for {active_slot} because the master profile "
                        f"({base_profile}) is busy, and the shared profile cannot be used while busy.",
                        file=sys.stderr,
                    )
                    return EXIT_SLOT_BUSY
                print(
                    f"[!] Could not prepare a separate profile for slot {active_slot}; "
                    f"using the shared profile ({base_profile}) because it is currently free.",
                    file=sys.stderr,
                )
                profile_dir = base_profile
        except Exception as exc:
            print(f"[!] Could not prepare the slot profile: {exc}", file=sys.stderr)
            if _profile_is_busy(base_profile):
                return EXIT_SLOT_BUSY
            profile_dir = base_profile

        slot_lock.update_profile_dir(profile_dir)
        chrome_exe = find_chrome_executable()

        with sync_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": headless,
                "args": list(CHROME_LAUNCH_ARGS),
                "ignore_default_args": ["--enable-automation"],
                "no_viewport": True,
            }
            if chrome_exe:
                print(f"[+] Launching installed Google Chrome binary: {chrome_exe}")
            try:
                context = _launch_with_fallback(p, launch_kwargs, chrome_exe, profile_dir)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                busy_err = (
                    "existing browser session" in str(exc).lower()
                    or "profile is already in use" in str(exc).lower()
                    or "already in use by another browser" in str(exc).lower()
                )
                if busy_err:
                    print(
                        f"[!] Profile {profile_dir} for slot {active_slot} is busy. "
                        f"Refusing to hijack the running browser.",
                        file=sys.stderr,
                    )
                    return EXIT_SLOT_BUSY
                print(
                    "[ERROR] Could not start a browser on the selected profile.",
                    file=sys.stderr,
                )
                print(f"  {str(exc).splitlines()[0]}", file=sys.stderr)
                print(
                    "  Close every other window that uses the Argon profile and try again.",
                    file=sys.stderr,
                )
                return EXIT_BROWSER_LAUNCH_FAILED

            page = context.pages[0] if context.pages else context.new_page()

            # Argon delivers apply_changes.ps1 as an attached file. Capture the
            # browser download only when it passes URL and .ps1 validation.
            delivered: dict = {}

            def _on_download(download) -> None:
                try:
                    downloads_dir = active_downloads_dir
                    dest, discard_reason = _validate_and_save_download(download, downloads_dir)
                    if dest is not None:
                        delivered["path"] = dest
                        print(f"[+] Downloaded delivered file: {dest}")
                    elif discard_reason:
                        print(f"[!] Ignored browser download ({discard_reason}).", file=sys.stderr)
                except Exception as exc:
                    print(f"[!] Could not capture the delivered file: {exc}")

            try:
                page.on("download", _on_download)
            except Exception:
                pass

            # D4: Register the task as running in TaskRegistry before navigation
            task_registry.register_start(
                proj_root,
                slot=active_slot,
                account=target_email or "",
                chat_id=_extract_chat_id(target_url) or "",
                chat_url=target_url,
                lane=lane or "single",
                label=task[:200] if task else "resume",
                prompt_hash=_prompt_hash(prompt_text) if prompt_text else "",
            )

            # D17: Crash-recovery helper covering pre-flight, attach, paste, and send
            preflight_recoveries = 0
            max_preflight_recoveries = 2
            last_step_shot: Optional[Path] = None

            def _recover_preflight_or_send_browser(exc: BaseException, step_name: str = "preflight"):
                nonlocal context, page, preflight_recoveries
                preflight_recoveries += 1
                print(
                    f"\n[!] Browser or page closed during {step_name} ({exc}) - "
                    f"recovery attempt {preflight_recoveries}/{max_preflight_recoveries}...",
                    file=sys.stderr,
                )
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                context = None
                while preflight_recoveries <= max_preflight_recoveries:
                    time.sleep(2.5)
                    if _profile_is_busy(profile_dir):
                        continue
                    try:
                        context = _launch_with_fallback(p, launch_kwargs, chrome_exe, profile_dir)
                        page = context.pages[0] if context.pages else context.new_page()
                        try:
                            page.on("download", _on_download)
                        except Exception:
                            pass
                        setattr(page, "_argon_is_continuation", bool(is_continuation))
                        setattr(page, "_argon_lane", lane)
                        setattr(page, "_argon_reopen_hook", _recover_preflight_or_send_browser)
                        reopen_target = _resolve_reopen_url(
                            proj_root=proj_root,
                            mode=mode,
                            active_slot=active_slot,
                            current_url=target_url,
                            fallback_url=target_url,
                            task_state=task_state_store,
                            session_store=session_store,
                            isolated_profile=_profile_is_isolated_slot(profile_dir),
                        )
                        page.goto(reopen_target, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        _wait_for_workspace(page, timeout=30.0)
                        if not is_continuation and not resume_chat:
                            _start_fresh_chat(
                                page,
                                active_slot,
                                isolated_profile=_profile_is_isolated_slot(profile_dir),
                                forbidden_chat_ids=forbidden_new_chat_ids,
                            )
                        print(f"[+] Recovered browser during {step_name} at {getattr(page, 'url', reopen_target)} - continuing step.")
                        return page
                    except Exception as rec_exc:
                        print(f"[!] Recovery attempt {preflight_recoveries} failed: {rec_exc}", file=sys.stderr)
                        preflight_recoveries += 1
                return None

            forbidden_new_chat_ids = _known_chat_ids_for_slot(active_slot)
            recorded_ws_mode = (
                session_store.get_recorded_workspace_mode(
                    proj_root,
                    chat_id=_extract_chat_id(target_url) or "",
                    lane=lane,
                    mode=mode,
                )
                if is_continuation
                else "unknown"
            )
            setattr(page, "_argon_is_continuation", bool(is_continuation))
            setattr(page, "_argon_recorded_workspace_mode", recorded_ws_mode)
            setattr(page, "_argon_lane", lane)
            setattr(page, "_argon_reopen_hook", _recover_preflight_or_send_browser)

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
            except Exception as nav_exc:
                if _is_target_closed_error(nav_exc):
                    if _recover_preflight_or_send_browser(nav_exc, "navigation") is None:
                        print(
                            f"[ERROR] Browser closed during navigation to {target_url} and could not be recovered. "
                            "Nothing was sent yet, so repeating this task costs zero quota.",
                            file=sys.stderr,
                        )
                        task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed")
                        return EXIT_BROWSER_CLOSED
                else:
                    raise

            if not _wait_for_workspace(page):
                print(
                    "[!] The Gemini workspace did not become ready in time.",
                    file=sys.stderr,
                )
                shot_ws = _take_step_screenshot(
                    page,
                    proj_root,
                    "preflight_failure.png",
                    step_label="preflight_failure",
                    state_label="Workspace composer did not become ready",
                    lane=lane,
                )
                ev_ws = _collect_diagnostic_evidence(
                    page=page,
                    proj_root=proj_root,
                    reason="Gemini workspace composer did not become ready before timeout",
                    slot=active_slot,
                    email=target_email,
                    profile_dir=profile_dir,
                    chat_url=target_url,
                    screenshot_path=shot_ws,
                    ruled_out=["Browser launched on slot profile"],
                    next_step="Run Open_Argon_Login.bat to verify the account session, then re-run this task.",
                )
                task_state_store.set(proj_root, lane=lane, state="preflight_failed", diagnostic_evidence=ev_ws)
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_ws or ""))
                _print_run_failure_summary(ev_ws)
                return EXIT_PREFLIGHT_FAILED

            if _is_page_unauthenticated(page):
                print("\n" + "!" * 72, file=sys.stderr)
                print(
                    "[ERROR] No Google account is signed into the Argon browser profile.",
                    file=sys.stderr,
                )
                print(
                    "Run the Account Pool Login Assistant to sign in your accounts first:",
                    file=sys.stderr,
                )
                print(f'  python "{LOGIN_ASSISTANT_SCRIPT}"', file=sys.stderr)
                print(
                    "Or run the automatic CLI login across all pool accounts:",
                    file=sys.stderr,
                )
                print(
                    f'  python "{SCRIPTS_DIR / "argon_pool_login.py"}" --all',
                    file=sys.stderr,
                )
                print("!" * 72 + "\n", file=sys.stderr)
                shot_unauth = _take_step_screenshot(
                    page,
                    proj_root,
                    "failed.png",
                    step_label="not_signed_in",
                    state_label="Anonymous guest page or Google sign-in screen",
                    lane=lane,
                )
                ev_unauth = _collect_diagnostic_evidence(
                    page=page,
                    proj_root=proj_root,
                    reason="Profile is not signed in (anonymous guest page or Google sign-in screen)",
                    slot=active_slot,
                    email=target_email,
                    profile_dir=profile_dir,
                    chat_url=target_url,
                    screenshot_path=shot_unauth,
                    ruled_out=["Browser launched and page loaded"],
                    next_step="Run Open_Argon_Login.bat, then re-run this task.",
                )
                task_state_store.set(proj_root, lane=lane, state="not_signed_in", diagnostic_evidence=ev_unauth)
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_unauth or ""))
                _print_run_failure_summary(ev_unauth)
                return EXIT_NOT_SIGNED_IN

            _select_profile_account_if_needed(page, target_email)

            early_quota = _detect_quota_limit(page)
            if early_quota:
                acct_now = _detect_signed_in_account(page, target_email, active_slot)
                print(
                    f"\n[!] Account {acct_now} at slot {active_slot} has hit its usage limit ({early_quota}).",
                    file=sys.stderr,
                )
                print("    Moving on to the next account in the pool.", file=sys.stderr)
                _mark_quota_limited(active_slot)
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="quota_limited")
                return EXIT_QUOTA_LIMITED

            # D19: On a NEW session (not a continuation and not a resume), explicitly click
            # 'New chat' and verify the conversation is genuinely fresh before pasting or attaching anything.
            if not is_continuation and not resume_chat:
                if not _start_fresh_chat(
                    page,
                    active_slot,
                    isolated_profile=_profile_is_isolated_slot(profile_dir),
                    forbidden_chat_ids=forbidden_new_chat_ids,
                ):
                    shot_fc = _take_step_screenshot(
                        page,
                        proj_root,
                        "preflight_failure.png",
                        step_label="new_chat_failed",
                        state_label="Could not start a genuinely fresh chat",
                        lane=lane,
                    )
                    ev_fc = _collect_diagnostic_evidence(
                        page=page,
                        proj_root=proj_root,
                        reason="New session remained on a restored foreign conversation instead of opening a fresh chat",
                        slot=active_slot,
                        email=target_email,
                        profile_dir=profile_dir,
                        chat_url=getattr(page, "url", "") or target_url,
                        screenshot_path=shot_fc,
                        ruled_out=["Browser launched and Google session authenticated"],
                        next_step="Rotating to the next pool account automatically.",
                    )
                    task_state_store.set(proj_root, lane=lane, state="preflight_failed", diagnostic_evidence=ev_fc)
                    task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_fc or ""))
                    _print_run_failure_summary(ev_fc)
                    return EXIT_PREFLIGHT_FAILED

            # Rule 1.1 & 1.2 + D22: Before attaching the zip or sending, check whether
            # this prompt is already the last user message in the conversation
            # or whether this chat already has an unfinished running task.
            existing_task_entry = (task_state_store.get(proj_root, lane=lane) or task_state_store.get(proj_root)) or {}
            if not resume_chat and is_continuation and _is_argon_working(page) and not force:
                live_working_shot = _take_step_screenshot(
                    page,
                    proj_root,
                    "already_running.png",
                    step_label="already_running",
                    state_label="Chat already has Argon actively working",
                    lane=lane,
                )
                print(
                    f"\n[ERROR] This chat ({getattr(page, 'url', target_url)}) already has a running task "
                    "(Argon is actively working); sending again will SUPERSEDE it!",
                    file=sys.stderr,
                )
                print(
                    f"        Screenshot: {live_working_shot}\n"
                    f"        Use --resume to wait for the running answer, or --force to supersede it explicitly.",
                    file=sys.stderr,
                )
                return EXIT_ALREADY_RUNNING

            dup_sent_hms = (
                None
                if resume_chat
                else _check_duplicate_prompt(
                    page,
                    prompt_text=prompt_text,
                    task_text=task,
                    prev_state=existing_task_entry,
                    new_session=new_session,
                )
            )
            if dup_sent_hms is not None:
                _print_duplicate_prompt_notice(dup_sent_hms)
                resume_chat = True
                if not resume_entry:
                    resume_entry = existing_task_entry

            # ---------------------------------------------------------- pre-flight --
            preflight: List[str] = []
            shot_dir = proj_root / ".argon_backup"
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = shot_dir / "preflight_failure.png"

            if resume_chat:
                # The prompt was already sent in an earlier run. Nothing has to be
                # configured now - the run only waits for the answer.
                print(
                    f"[+] Resuming the task in: {target_url}"
                    + (f" (state: {resume_entry.get('state')})" if resume_entry else "")
                )
                mode_ok = model_ok = think_ok = attached = True
            else:
                # E2.1 - E2.4: Never report `mode = Task : OK` unless Task mode was actually
                # observed via the toggle or recorded as `workspace_mode == 'task'` in SessionStore.
                mode_ok = True if manual_setup else _retry(
                    lambda p: select_task_mode(
                        p,
                        require_verifiable=not is_continuation,
                        recorded_workspace_mode=recorded_ws_mode,
                    ),
                    page,
                )
                chip_seen_pf = getattr(page, "_argon_chip_seen", "")
                if not chip_seen_pf:
                    chip_el_pf = _model_chip(page)
                    chip_seen_pf = (_model_name(chip_el_pf) or "").strip() if chip_el_pf is not None else ""

                if not manual_setup and chip_seen_pf and "high" in chip_seen_pf.lower():
                    preflight.append(f"mode = Chat : REFUSED (chip='{chip_seen_pf}')")
                    mode_ok = False
                else:
                    chip_tag = f" (chip='{chip_seen_pf}')" if (chip_seen_pf and mode_ok and "chip=" not in str(getattr(page, "_argon_mode_status", ""))) else ""
                    mode_status_str = (
                        "OK (manual)"
                        if manual_setup
                        else (
                            str(getattr(page, "_argon_mode_status", "") or ("OK" if mode_ok else "UNKNOWN"))
                        )
                    )
                    preflight.append(f"mode = Task : {mode_status_str}{chip_tag}")
                model_ok = True if manual_setup else _retry(
                    lambda p: select_argon_model(p, require_chip=not is_continuation),
                    page,
                )
                check_basis = "recorded/inherited on continuation" if is_continuation else "proven on new chat"
                preflight.append(
                    f"model = 4 Argon : {'OK (manual)' if manual_setup else ('OK' if model_ok else 'FAILED')} ({check_basis})"
                )
                think_ok = True

            attached = True if resume_chat else _retry(lambda p: attach_zip(p, zip_path), page)
            if not resume_chat:
                ok_z, z_sz, z_cnt, _ = _inspect_zip_archive(zip_path)
                size_tag = f" ({z_sz} bytes, {z_cnt} entries, TASK_BRIEF.md verified)" if ok_z else " (invalid/corrupt archive)"
                preflight.append(f"attachment {zip_path.name}{size_tag} : {'OK' if attached else 'FAILED'}")

            # D17: If the browser/page closed during pre-flight or attach, recover and re-run pre-flight + attach
            if not (mode_ok and model_ok and think_ok and attached) and (hasattr(page, "is_closed") and page.is_closed()):
                recovered_pf_page = _recover_preflight_or_send_browser(
                    RuntimeError("page closed during pre-flight/attach"),
                    "preflight_and_attach",
                )
                if recovered_pf_page is not None:
                    page = recovered_pf_page
                    preflight.clear()
                    mode_ok = True if manual_setup else _retry(
                        lambda p: select_task_mode(p, require_verifiable=not is_continuation),
                        page,
                    )
                    check_basis = "inherited from existing conversation" if is_continuation else "proven on new chat"
                    preflight.append(f"mode = Task : {'OK' if mode_ok else 'FAILED'} ({check_basis})")
                    model_ok = True if manual_setup else _retry(
                        lambda p: select_argon_model(p, require_chip=not is_continuation),
                        page,
                    )
                    preflight.append(f"model = 4 Argon : {'OK' if model_ok else 'FAILED'} ({check_basis})")
                    think_ok = True
                    attached = _retry(lambda p: attach_zip(p, zip_path), page)
                    ok_z, z_sz, z_cnt, _ = _inspect_zip_archive(zip_path)
                    size_tag = f" ({z_sz} bytes, {z_cnt} entries, TASK_BRIEF.md verified)" if ok_z else ""
                    preflight.append(f"attachment {zip_path.name}{size_tag} : {'OK' if attached else 'FAILED'}")

            print("[i] Pre-flight:")
            for line in preflight:
                print("      " + line)

            if not (mode_ok and model_ok and think_ok and attached):
                if _argon_picker_is_disabled(page):
                    acct_now = _detect_signed_in_account(page, target_email, active_slot)
                    shot_dis = _take_step_screenshot(
                        page,
                        proj_root,
                        "preflight_failure.png",
                        step_label="preflight_failure",
                        state_label="4 Argon option disabled in picker",
                        lane=lane,
                    )
                    print(
                        f"\n[!] Account {acct_now} (slot {active_slot}): this account has no Argon available "
                        f"('4 Argon' option is disabled in the model picker).",
                        file=sys.stderr,
                    )
                    print(
                        f"    Marking {active_slot} as quota_limited and rotating to the next pool account.",
                        file=sys.stderr,
                    )
                    _mark_quota_limited(active_slot)
                    ev_dis = _collect_diagnostic_evidence(
                        page=page,
                        proj_root=proj_root,
                        reason=f"Account {acct_now} ({active_slot}) has no Argon available ('4 Argon' is aria-disabled in the model picker)",
                        slot=active_slot,
                        email=acct_now,
                        profile_dir=profile_dir,
                        chat_url=getattr(page, "url", "") or target_url,
                        screenshot_path=shot_dis,
                        preflight=preflight,
                        ruled_out=[
                            "Google session is signed in (not guest)",
                            "Model picker opened and '4 Argon' option was located in the menu",
                        ],
                        next_step=(
                            "Rotating to the next pool account automatically. If all pool accounts are "
                            "exhausted, run Open_Argon_Login.bat to add or sign in another account, then re-run this task."
                        ),
                    )
                    task_state_store.set(proj_root, lane=lane, state="quota_limited", diagnostic_evidence=ev_dis)
                    task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="quota_limited", screenshot_path=str(shot_dis or ""))
                    _print_run_failure_summary(ev_dis)
                    return EXIT_QUOTA_LIMITED
                preflight_quota = _detect_quota_limit(page)
                if preflight_quota:
                    acct_now = _detect_signed_in_account(page, target_email, active_slot)
                    print(
                        f"\n[!] Account {acct_now} at slot {active_slot} has hit its usage limit ({preflight_quota}).",
                        file=sys.stderr,
                    )
                    print("    Moving on to the next account in the pool.", file=sys.stderr)
                    _mark_quota_limited(active_slot)
                    task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="quota_limited")
                    return EXIT_QUOTA_LIMITED
                shot_saved = _take_step_screenshot(
                    page,
                    proj_root,
                    "preflight_failure.png",
                    step_label="preflight_failure",
                    state_label="Pre-flight workspace check failed",
                    lane=lane,
                )
                print(
                    manual_setup_instructions(mode, str(shot_saved) if shot_saved else None),
                    file=sys.stderr,
                )
                ruled = ["Browser launched and workspace composer rendered", "Google account is signed in (not guest)"]
                if mode_ok:
                    ruled.append("Task mode toggle verified active")
                if model_ok:
                    ruled.append("Model chip verified as 4 Argon")
                if attached:
                    ruled.append(f"Archive {zip_path.name} verified attached")
                ev_pf = _collect_diagnostic_evidence(
                    page=page,
                    proj_root=proj_root,
                    reason="Pre-flight workspace check failed (" + ", ".join(preflight) + ")",
                    slot=active_slot,
                    email=target_email,
                    profile_dir=profile_dir,
                    chat_url=getattr(page, "url", "") or target_url,
                    screenshot_path=shot_saved,
                    preflight=preflight,
                    ruled_out=ruled,
                    next_step=(
                        "Check the conversation link and screenshot above, or run Open_Argon_Login.bat "
                        "if the session expired, then re-run this task."
                    ),
                )
                task_state_store.set(proj_root, lane=lane, state="preflight_failed", diagnostic_evidence=ev_pf)
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_saved or ""))
                _print_run_failure_summary(ev_pf)
                return EXIT_PREFLIGHT_FAILED

            # D6.1: Take step screenshot right after pre-flight and upload completion
            if not resume_chat:
                last_step_shot = _take_step_screenshot(
                    page,
                    proj_root,
                    "preflight.png",
                    step_label="preflight",
                    state_label=f"Task mode, 4 Argon, and {zip_path.name} verified",
                    lane=lane,
                )

            # ------------------------------------------------------------- send --
            dispatch_started = (
                float(resume_entry.get("started_epoch") or 0.0)
                if (resume_chat and resume_entry)
                else time.time()
            )
            task_state = TaskState()
            chips_before = 0 if resume_chat else len(_generated_file_chip_indices(page))
            turns_before = _assistant_turns(page)
            if resume_chat:
                # When resuming or continuing an already-sent prompt, only a turn
                # that sits AFTER the last user message counts as the answer.
                responses_before = (
                    max(0, len(turns_before) - 1)
                    if (turns_before and _element_is_after_last_user_turn(turns_before[-1]))
                    else (len(turns_before) if turns_before is not None else 0)
                )
            else:
                responses_before = len(turns_before) if turns_before is not None else 0

            # D1.1 & D1.2: Load all already-handled digests for this project from DeliveryLedger + TaskState + disk
            handled_delivery_hashes = set()
            try:
                handled_delivery_hashes.update(delivery_ledger.known_hashes(proj_root))
            except Exception:
                pass
            try:
                prev_state = (task_state.get(proj_root, lane=lane) or task_state.get(proj_root)) or {}
                prev_hash = str(prev_state.get("delivery_hash") or "")
                if prev_hash:
                    handled_delivery_hashes.add(prev_hash)
            except Exception:
                prev_state = {}
            for stale in (active_downloads_dir / "apply_changes.ps1", proj_root / "downloads" / "apply_changes.ps1"):
                try:
                    if stale.is_file():
                        st_txt = stale.read_text(encoding="utf-8", errors="replace")
                        handled_delivery_hashes.add(_hash_text(st_txt))
                        handled_delivery_hashes.add(_sha256_full(st_txt))
                except Exception:
                    pass

            sent_hms = time.strftime("%H:%M:%S")
            just_sent_prompt = False
            try:
                if resume_chat:
                    if dup_sent_hms is None:
                        print("[+] The prompt was already sent - checking the conversation and waiting for the answer.")
                    task_state.set(proj_root, lane=lane, state="waiting", chat_url=page.url, slot=active_slot)
                else:
                    # Double-check duplicate prompt right before sending as well.
                    late_dup_hms = _check_duplicate_prompt(
                        page,
                        prompt_text=prompt_text,
                        task_text=task,
                        prev_state=prev_state,
                        new_session=new_session,
                    )
                    if late_dup_hms is not None:
                        _print_duplicate_prompt_notice(late_dup_hms)
                        resume_chat = True
                        task_state.set(proj_root, lane=lane, state="waiting", chat_url=page.url, slot=active_slot)
                    else:
                        composer = _find_composer(page)
                        if composer is not None:
                            if not ensure_argon_model(page, require_chip=not is_continuation):
                                shot_send_gate = _take_step_screenshot(page, proj_root, "preflight_failure.png")
                                if _argon_picker_is_disabled(page):
                                    acct_now = _detect_signed_in_account(page, target_email, active_slot)
                                    print(
                                        f"[!] Account {acct_now} (slot {active_slot}): this account has no Argon available "
                                        f"('4 Argon' is disabled in the model picker).",
                                        file=sys.stderr,
                                    )
                                    _mark_quota_limited(active_slot)
                                    ev_sg = _collect_diagnostic_evidence(
                                        page=page,
                                        proj_root=proj_root,
                                        reason=f"Account {acct_now} ({active_slot}) has no Argon available ('4 Argon' disabled before send)",
                                        slot=active_slot,
                                        email=acct_now,
                                        profile_dir=profile_dir,
                                        chat_url=getattr(page, "url", "") or target_url,
                                        screenshot_path=shot_send_gate,
                                        preflight=preflight,
                                        ruled_out=["Task mode active", f"Archive {zip_path.name} attached"],
                                        next_step="Rotating to the next pool account automatically.",
                                    )
                                    task_state_store.set(proj_root, state="quota_limited", diagnostic_evidence=ev_sg)
                                    _print_run_failure_summary(ev_sg)
                                    return EXIT_QUOTA_LIMITED
                                print(
                                    "[!] Refusing to send: the model is not 4 Argon.",
                                    file=sys.stderr,
                                )
                                print(
                                    "    Switch the model to '4 Argon (Complex tasks)' in the "
                                    "browser and re-run, or use --manual-setup after doing it "
                                    "by hand.",
                                    file=sys.stderr,
                                )
                                ev_sg = _collect_diagnostic_evidence(
                                    page=page,
                                    proj_root=proj_root,
                                    reason="Model chip was not '4 Argon' right before send",
                                    slot=active_slot,
                                    email=target_email,
                                    profile_dir=profile_dir,
                                    chat_url=getattr(page, "url", "") or target_url,
                                    screenshot_path=shot_send_gate,
                                    preflight=preflight,
                                    ruled_out=["Task mode active", f"Archive {zip_path.name} attached"],
                                    next_step="Switch the model to '4 Argon (Complex tasks)' in the browser or run Open_Argon_Login.bat, then re-run this task.",
                                )
                                task_state_store.set(proj_root, state="preflight_failed", diagnostic_evidence=ev_sg)
                                _print_run_failure_summary(ev_sg)
                                return EXIT_PREFLIGHT_FAILED
                            # E3.1 & E3.2: Right before send on a NEW chat, verify once more (after pre-flight & upload
                            # have given the page plenty of time to finish loading) that the conversation is still EMPTY
                            # and its chat_id is not already known.
                            if not is_continuation and not resume_chat:
                                ok_pre_send, pre_send_cid, pre_u, pre_a = _conversation_emptiness_report(
                                    page, forbidden_new_chat_ids
                                )
                                if not ok_pre_send:
                                    shot_foreign_pre = _take_step_screenshot(
                                        page,
                                        proj_root,
                                        "preflight_failure.png",
                                        step_label="foreign_chat_before_send",
                                        state_label=f"New chat is not empty before send (chat_id={pre_send_cid})",
                                        lane=lane,
                                    )
                                    print(
                                        f"[ERROR] New session check failed right before send on {active_slot}: "
                                        f"ended up in non-empty/known conversation chat_id={pre_send_cid or '(no-chat-id)'!r} "
                                        f"(url={getattr(page, 'url', target_url)!r}, user_messages={pre_u}, assistant_turns={pre_a}). "
                                        f"Screenshot: {shot_foreign_pre}",
                                        file=sys.stderr,
                                    )
                                    task_registry.register_done(
                                        proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_foreign_pre or "")
                                    )
                                    return EXIT_PREFLIGHT_FAILED
                            if brief_summary:
                                bs_path = Path(brief_summary).resolve()
                                if bs_path.is_file():
                                    try:
                                        print("\n" + "=" * 78)
                                        print(f"[i] Brief Summary ({bs_path}):")
                                        print("=" * 78)
                                        print(bs_path.read_text(encoding="utf-8"))
                                        print("=" * 78 + "\n")
                                    except Exception as exc:
                                        print(f"[!] Warning: Could not read brief summary {bs_path}: {exc}", file=sys.stderr)
                                else:
                                    print(f"[!] Warning: Brief summary file not found at {bs_path}; continuing without summary display.", file=sys.stderr)
                            _type_and_send(page, composer, prompt_text)
                            sent_hms = time.strftime("%H:%M:%S")
                            just_sent_prompt = True
                            sent_shot = _take_step_screenshot(
                                page,
                                proj_root,
                                "sent.png",
                                step_label="sent",
                                state_label=f"Prompt submitted at {sent_hms}",
                                lane=lane,
                            )
                            # D12.1: After the message is sent, the composer chip reverts to the
                            # ACCOUNT DEFAULT (e.g. 'Flash') even though 4 Argon answered this turn.
                            chip_after = _model_chip(page)
                            name_after = _model_name(chip_after)
                            if name_after:
                                print(
                                    f"[i] Post-send model chip reads {name_after!r}: the chip after send shows "
                                    "the account default for future turns, not the model answering this turn "
                                    "(4 Argon was verified before send; real proof is verified in the answer)."
                                )
                            print("[+] Prompt submitted in a verified Task/Argon workspace with the zip attached.")
                            try:
                                profile_dir_str = str(profile_dir)
                            except Exception:
                                profile_dir_str = ""
                            task_state.set(
                                proj_root,
                                lane=lane,
                                chat_url=page.url,
                                chat_title=_chat_title(page),
                                slot=active_slot,
                                profile_number=_slot_number(active_slot),
                                profile_dir=profile_dir_str,
                                email=target_email,
                                mode=mode,
                                state="sent",
                                task=task,
                                prompt_hash=_prompt_hash(prompt_text),
                                task_hash=_prompt_hash(task),
                                prompt_prefix=_normalize_ws(prompt_text)[:200],
                                sent_at=sent_hms,
                                started_epoch=dispatch_started,
                                started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                                last_screenshot=str(sent_shot or ""),
                            )
                            task_registry.update_running(
                                proj_root,
                                lane=lane,
                                slot=active_slot,
                                chat_url=page.url,
                                screenshot_path=str(sent_shot or ""),
                                prompt_hash=_prompt_hash(prompt_text),
                            )
                            print(f"[+] Task state saved: {task_state.path}")
                            print(f"    account / profile : {active_slot}  ({profile_dir_str})")
                        else:
                            print("[!] Composer not visible - the prompt was not sent.", file=sys.stderr)
                            _take_step_screenshot(
                                page,
                                proj_root,
                                "preflight_failure.png",
                                step_label="composer_missing",
                                state_label="Composer not visible before send",
                                lane=lane,
                            )
                            task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed")
                            return EXIT_PREFLIGHT_FAILED
            except Exception as exc:
                if _is_target_closed_error(exc):
                    # D17: Recover browser when a crash occurs during paste or send!
                    rec_page = _recover_preflight_or_send_browser(exc, "paste_and_send")
                    if rec_page is not None:
                        page = rec_page
                        dup_after_crash = _check_duplicate_prompt(
                            page,
                            prompt_text=prompt_text,
                            task_text=task,
                            prev_state=prev_state,
                            new_session=False,
                        )
                        if dup_after_crash is not None:
                            print("[+] Prompt had already landed before browser crash - continuing to wait for answer.")
                            resume_chat = True
                            just_sent_prompt = True
                        else:
                            print("[+] Re-applying Task/Argon + zip attachment after crash recovery before sending...")
                            if not manual_setup:
                                _retry(lambda p: select_task_mode(p, require_verifiable=not is_continuation), page)
                                _retry(lambda p: select_argon_model(p, require_chip=not is_continuation), page)
                            if _retry(lambda p: attach_zip(p, zip_path), page):
                                comp_rec = _find_composer(page)
                                if comp_rec is not None:
                                    _type_and_send(page, comp_rec, prompt_text)
                                    sent_hms = time.strftime("%H:%M:%S")
                                    just_sent_prompt = True
                                    _take_step_screenshot(
                                        page,
                                        proj_root,
                                        "sent.png",
                                        step_label="sent_after_recovery",
                                        state_label=f"Prompt submitted at {sent_hms} after crash recovery",
                                        lane=lane,
                                    )
                    if not just_sent_prompt and not resume_chat:
                        print(
                            f"[ERROR] Browser closed during paste/send step and could not complete send.\n"
                            f"        Lost step       : paste_and_send (before prompt was sent)\n"
                            f"        Last screenshot : {last_step_shot or '(none)'}\n"
                            f"        Nothing was sent, so repeating this task costs zero quota.",
                            file=sys.stderr,
                        )
                        task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed")
                        return EXIT_BROWSER_CLOSED
                else:
                    print(f"[!] Prompt submission failed: {exc}", file=sys.stderr)
                    shot_pf_err = _take_step_screenshot(
                        page,
                        proj_root,
                        "failed.png",
                        step_label="send_failed",
                        state_label=f"Prompt submission failed: {exc}",
                        lane=lane,
                    )
                    task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_pf_err or ""))
                    return EXIT_PREFLIGHT_FAILED

            # ------------------------------------------------- remember session --
            chat_id = _wait_for_chat_id(page)
            # D19.2: On a new session, compare chat_id against forbidden_new_chat_ids
            if not is_continuation and not resume_chat and chat_id and chat_id in forbidden_new_chat_ids:
                shot_foreign = _take_step_screenshot(
                    page,
                    proj_root,
                    "failed.png",
                    step_label="foreign_chat_detected",
                    state_label=f"New session reused existing chat_id {chat_id}",
                    lane=lane,
                )
                print(
                    f"[ERROR] New session chat_id ({chat_id}) matches a previous conversation on {active_slot}! "
                    f"The new chat was NOT started. Screenshot: {shot_foreign}",
                    file=sys.stderr,
                )
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_foreign or ""))
                return EXIT_PREFLIGHT_FAILED

            chat_title = _chat_title(page)
            try:
                profile_dir_str = str(profile_dir)
            except Exception:
                profile_dir_str = ""
            effective_chat_url = page.url or target_url
            proven_ws_mode = str(getattr(page, "_argon_workspace_mode", "") or ("task" if mode_ok else "unknown"))
            if chat_id:
                chat_url = session_store.remember(
                    proj_root, mode, active_slot, chat_id,
                    chat_title=chat_title, profile_dir=profile_dir_str,
                    lane=lane,
                    workspace_mode=proven_ws_mode,
                )
                effective_chat_url = chat_url or effective_chat_url
                task_state.set(
                    proj_root,
                    lane=lane,
                    chat_id=chat_id,
                    chat_url=effective_chat_url,
                    chat_title=chat_title,
                    slot=active_slot,
                    profile_number=_slot_number(active_slot),
                    profile_dir=profile_dir_str,
                    email=target_email,
                    mode=mode,
                    state="waiting",
                )
                task_registry.update_running(
                    proj_root,
                    lane=lane,
                    slot=active_slot,
                    chat_id=chat_id,
                    chat_url=effective_chat_url,
                )
                print(f"[+] Remembered the session for this project: {effective_chat_url}")
                if chat_title:
                    print(f"    conversation name : {chat_title}")
                print(f"    account / profile : {active_slot}  ({profile_dir_str})")
            else:
                print(
                    "[!] No conversation id in the URL - the next task on this project "
                    "will start a fresh chat."
                )

            if just_sent_prompt:
                _print_prompt_sent_banner(sent_hms, chat_title, effective_chat_url)

            eff_deadline_secs = max(60, int(deadline_seconds or 1800))
            print(f"[+] Waiting for Gemini 4 Argon response (deadline: {_format_elapsed(eff_deadline_secs)})...")
            deadline = time.monotonic() + float(eff_deadline_secs)
            extracted_patch: Optional[str] = None
            delivered_path: Optional[Path] = delivered.get("path")
            last_body = ""
            run_failed_reason = ""
            last_state_write = 0.0
            # Rule 5.1 & 5.2: If for 3 minutes (180 s) the conversation text has
            # not changed, there is no stop button, no thinking indicator, no
            # spinner and no delivery, the page looks stalled: take a stall
            # screenshot and refresh the page instead of waiting it out or
            # re-sending the prompt.
            STALL_SECONDS = 180.0
            HEARTBEAT_SECONDS = 120.0
            WAITING_SHOT_SECONDS = 120.0
            MAX_WAITING_SHOTS = 5
            MAX_STALL_REFRESHES = 2
            last_signature = ("", "")
            wait_started_mono = time.monotonic()
            quiet_since = wait_started_mono
            last_heartbeat_mono = wait_started_mono
            last_waiting_shot_mono = wait_started_mono
            waiting_shot_count = 0
            stall_count = 0
            wait_step = 0
            last_known_url = page.url or target_url
            reopen_attempts = 0
            max_reopen_attempts = 5

            def _recover_closed_browser() -> bool:
                """Reopen the active conversation without resending the prompt."""
                nonlocal context, page, last_known_url, reopen_attempts
                reopen_url = _resolve_reopen_url(
                    proj_root=proj_root,
                    mode=mode,
                    active_slot=active_slot,
                    current_url=last_known_url,
                    fallback_url=target_url,
                    task_state=task_state,
                    session_store=session_store,
                    # The shared master profile holds EVERY account, so the slot
                    # index must be kept there; only a per-slot profile forces
                    # /u/0/. Defaulting to True here would silently talk to the
                    # wrong account.
                    isolated_profile=_profile_is_isolated_slot(profile_dir),
                )
                reopen_attempts += 1
                print(
                    f"\n[!] The browser or page closed while waiting for Argon's answer "
                    f"(recovery attempt {reopen_attempts}/{max_reopen_attempts}).",
                    file=sys.stderr,
                )
                print(
                    f"[+] Reopening the conversation without resending the prompt: {reopen_url}",
                    file=sys.stderr,
                )
                try:
                    if context is not None:
                        context.close()
                except Exception:
                    pass
                context = None

                isolated_reopen = _profile_is_isolated_slot(profile_dir)
                reopen_url = _normalize_gemini_chat_url(reopen_url, isolated_profile=isolated_reopen)
                while time.monotonic() < deadline and reopen_attempts <= max_reopen_attempts:
                    time.sleep(3.0)
                    if _profile_is_busy(profile_dir):
                        print(
                            f"[!] Profile {profile_dir} is currently busy in another process - "
                            f"waiting for it to become free before reopening...",
                            file=sys.stderr,
                        )
                        continue
                    try:
                        context = _launch_with_fallback(p, launch_kwargs, chrome_exe, profile_dir)
                        page = context.pages[0] if context.pages else context.new_page()
                        try:
                            page.on("download", _on_download)
                        except Exception:
                            pass
                        page.goto(reopen_url, wait_until="domcontentloaded", timeout=60000)
                        try:
                            page.wait_for_timeout(2500)
                        except Exception:
                            time.sleep(2.5)
                        _wait_for_workspace(page, timeout=30.0)
                        last_known_url = page.url or reopen_url
                        live_cid = _extract_chat_id(last_known_url)
                        if live_cid:
                            session_store.remember(
                                proj_root, mode, active_slot, live_cid,
                                # The stored chat URL must match the profile the
                                # run actually used: a per-slot profile stores
                                # /u/0/, the shared master profile stores /u/<slot>/.
                                profile_dir=str(profile_dir),
                                lane=lane,
                            )
                        task_state.set(
                            proj_root,
                            state="waiting",
                            chat_id=live_cid or _extract_chat_id(reopen_url) or "",
                            chat_url=last_known_url,
                            slot=active_slot,
                        )
                        print(f"[+] Reopened conversation at: {last_known_url}")
                        return True
                    except BaseException as reopen_exc:
                        if not isinstance(reopen_exc, Exception):
                            raise
                        print(
                            f"[!] Could not reopen conversation yet ({str(reopen_exc).splitlines()[0]}); retrying...",
                            file=sys.stderr,
                        )
                        try:
                            if context is not None:
                                context.close()
                        except Exception:
                            pass
                        context = None
                        reopen_attempts += 1
                return False

            while time.monotonic() < deadline:
                try:
                    if hasattr(page, "is_closed") and page.is_closed():
                        raise RuntimeError("Target page, context or browser has been closed")
                    try:
                        if page.url:
                            last_known_url = page.url
                    except Exception:
                        pass

                    quota = _detect_quota_limit(page)
                    if quota:
                        acct_now = _detect_signed_in_account(page, target_email, active_slot)
                        shot_q = _take_step_screenshot(page, proj_root, "quota_limited.png")
                        print(
                            f"\n[!] Account {acct_now} (slot {active_slot}) hit its usage limit ({quota}).",
                            file=sys.stderr,
                        )
                        print(
                            "    Moving on to the next account in the pool.",
                            file=sys.stderr,
                        )
                        _mark_quota_limited(active_slot)
                        ev_q = _collect_diagnostic_evidence(
                            page=page,
                            proj_root=proj_root,
                            reason=f"Account {acct_now} ({active_slot}) hit usage limit ({quota}) while waiting",
                            slot=active_slot,
                            email=acct_now,
                            profile_dir=profile_dir,
                            chat_title=chat_title,
                            chat_url=last_known_url or effective_chat_url,
                            screenshot_path=shot_q,
                            preflight=preflight,
                            ruled_out=["Workspace pre-flight passed", "Prompt was sent to conversation"],
                            next_step="Rotating to the next pool account automatically.",
                        )
                        task_state.set(proj_root, state="quota_limited", diagnostic_evidence=ev_q)
                        _print_run_failure_summary(ev_q)
                        return EXIT_QUOTA_LIMITED

                    now_mono = time.monotonic()
                    rem_secs = max(0.0, deadline - now_mono)
                    if (
                        waiting_shot_count < MAX_WAITING_SHOTS
                        and now_mono - last_waiting_shot_mono >= WAITING_SHOT_SECONDS
                    ):
                        last_waiting_shot_mono = now_mono
                        waiting_shot_count += 1
                        _take_step_screenshot(
                            page,
                            proj_root,
                            f"waiting-{waiting_shot_count}.png",
                            step_label=f"waiting-{waiting_shot_count}",
                            state_label=f"Waiting for Argon (elapsed {_format_elapsed(now_mono - wait_started_mono)}, remaining {_format_elapsed(rem_secs)})",
                            lane=lane,
                        )
                    if now_mono - last_heartbeat_mono >= HEARTBEAT_SECONDS:
                        last_heartbeat_mono = now_mono
                        print(
                            f"[+] Still waiting for Argon (elapsed: {_format_elapsed(now_mono - wait_started_mono)}, "
                            f"remaining: {_format_elapsed(rem_secs)}) ..."
                        )

                    # D22.2 & D22.3: Check whether a newer, different prompt superseded this task in the same chat
                    superseded_reason = _check_superseded_prompt(
                        page,
                        expected_prompt_text=prompt_text,
                        expected_task_text=task,
                        prev_state=task_state.get(proj_root, lane=lane) or prev_state,
                    )
                    if superseded_reason:
                        sup_shot = _take_step_screenshot(
                            page,
                            proj_root,
                            "superseded.png",
                            step_label="superseded",
                            state_label="Task superseded by a newer prompt in the same chat",
                            lane=lane,
                        )
                        run_failed_reason = f"Task superseded: {superseded_reason}"
                        print(
                            f"\n[ERROR] {run_failed_reason}\n"
                            f"        Marking this task as 'superseded' so future runs do not wait for a dropped prompt.\n"
                            f"        Screenshot: {sup_shot}",
                            file=sys.stderr,
                        )
                        task_state.set(
                            proj_root,
                            lane=lane,
                            state="superseded",
                            superseded_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                            last_screenshot=str(sup_shot or ""),
                        )
                        task_registry.register_done(
                            proj_root,
                            lane=lane,
                            slot=active_slot,
                            status="superseded",
                            screenshot_path=str(sup_shot or ""),
                        )
                        break

                    if time.time() - last_state_write > 15:
                        last_state_write = time.time()
                        live_cid = _extract_chat_id(last_known_url)
                        if live_cid:
                            session_store.remember(
                                proj_root, mode, active_slot, live_cid,
                                profile_dir=str(profile_dir),
                                lane=lane,
                            )
                        task_state.set(
                            proj_root,
                            lane=lane,
                            state="waiting",
                            chat_id=live_cid or "",
                            chat_url=last_known_url,
                            slot=active_slot,
                        )
                        task_registry.update_running(
                            proj_root,
                            lane=lane,
                            slot=active_slot,
                            chat_id=live_cid or "",
                            chat_url=last_known_url,
                        )

                    # Check for a delivery first so we react immediately.
                    if delivered_path is None:
                        delivered_path = delivered.get("path")
                    if delivered_path is None:
                        search_folders = (
                            [active_downloads_dir]
                            if delivery_dir
                            else [active_downloads_dir, Path.home() / "Downloads"]
                        )
                        delivered_path = find_delivered_patch(
                            search_folders,
                            newer_than=dispatch_started,
                        )
                    if delivered_path is not None:
                        try:
                            delivered_text = delivered_path.read_text(
                                encoding="utf-8", errors="replace"
                            )
                        except Exception:
                            delivered_text = ""
                        if (
                            not _is_leftover_delivery(delivered_text, handled_delivery_hashes, project_path=proj_root)
                            and _looks_like_patch(delivered_text)
                            and _patch_is_complete(delivered_text)
                        ):
                            _take_step_screenshot(
                                page,
                                proj_root,
                                "delivered.png",
                                step_label="delivered",
                                state_label=f"Validated patch delivery {delivered_path.name}",
                                lane=lane,
                            )
                            break
                        delivered_path = None

                    new_chip = _new_delivery_chip(page, chips_before, responses_before)
                    if new_chip is None:
                        new_chip = _generated_file_chip(page)
                    if new_chip is not None:
                        _take_step_screenshot(
                            page,
                            proj_root,
                            "chip_detected.png",
                            step_label="chip_detected",
                            state_label="Delivery chip appeared in newest assistant turn",
                            lane=lane,
                        )
                        captured = capture_generated_file(
                            page,
                            active_downloads_dir,
                            chip=new_chip,
                            previous_hashes=handled_delivery_hashes,
                            project_path=proj_root,
                        )
                        if captured is not None:
                            try:
                                cap_txt = captured.read_text(encoding="utf-8", errors="replace")
                                handled_delivery_hashes.add(_hash_text(cap_txt))
                                handled_delivery_hashes.add(_sha256_full(cap_txt))
                                delivery_ledger.record(
                                    proj_root,
                                    cap_txt,
                                    chat_id=chat_id or _extract_chat_id(last_known_url) or "",
                                    lane=lane or "",
                                    file_path=str(captured),
                                )
                            except Exception:
                                pass
                            delivered_path = captured
                            _take_step_screenshot(
                                page,
                                proj_root,
                                "delivered.png",
                                step_label="delivered",
                                state_label=f"Captured patch file {captured.name}",
                                lane=lane,
                            )
                            print(f"[+] Captured the generated patch file: {captured}")
                            break

                    working = _is_argon_working(page)
                    try:
                        body_now = page.locator("body").inner_text(timeout=6000) or ""
                    except Exception:
                        body_now = last_body
                    last_body = body_now

                    # D10.4: Immediately recognize 'You stopped this response' when not working
                    if not working:
                        stopped_msg = _detect_stopped_response(page)
                        if stopped_msg:
                            run_failed_reason = "Gemini reported: 'You stopped this response' (response generation was stopped before delivery)"
                            print(
                                f"\n[!] {run_failed_reason}",
                                file=sys.stderr,
                            )
                            break

                    candidate = extract_powershell_patch(last_body)
                    if (
                        candidate
                        and not _is_leftover_delivery(candidate, handled_delivery_hashes, project_path=proj_root)
                        and _looks_like_patch(candidate)
                        and _patch_is_complete(candidate)
                    ):
                        extracted_patch = candidate
                        _take_step_screenshot(
                            page,
                            proj_root,
                            "delivered.png",
                            step_label="delivered",
                            state_label="Extracted complete patch block from response",
                            lane=lane,
                        )
                        break

                    signature = (len(body_now), body_now[-300:])
                    if signature != last_signature:
                        last_signature = signature
                        quiet_since = time.monotonic()
                    if working:
                        quiet_since = time.monotonic()

                    # Rule 5.1, 5.2, 5.3 + D10.1: Stall detection and browser refresh ONLY when NOT working.
                    if not working and (time.monotonic() - quiet_since >= STALL_SECONDS):
                        gem_error = _detect_gemini_error(page)
                        if stall_count < MAX_STALL_REFRESHES and not gem_error:
                            stall_count += 1
                            stall_shot = _take_step_screenshot(
                                page,
                                proj_root,
                                f"stall-{stall_count}.png",
                                step_label=f"stall-{stall_count}",
                                state_label="No progress for 3 min; refreshing page",
                                lane=lane,
                            )
                            print(
                                f"[!] No progress for 3 min - the page looks stalled. Screenshot: {stall_shot}\n"
                                "    Refreshing the page; the work continues on the server."
                            )
                            try:
                                page.reload(wait_until="domcontentloaded", timeout=60000)
                            except Exception:
                                page.goto(last_known_url, wait_until="domcontentloaded", timeout=60000)
                            try:
                                page.wait_for_timeout(2500)
                            except Exception:
                                time.sleep(2.5)
                            _wait_for_workspace(page, timeout=30.0)
                            quiet_since = time.monotonic()
                            wait_step = 0
                            continue
                        run_failed_reason = (
                            f"Gemini reported: '{gem_error}'"
                            if gem_error == "You stopped this response"
                            else (
                                f"Gemini error: {gem_error}"
                                if gem_error
                                else "the answer finished without a delivery"
                            )
                        )
                        print(
                            f"\n[!] The conversation has been quiet for "
                            f"{int(STALL_SECONDS)}s and no delivery appeared "
                            f"({run_failed_reason}).",
                            file=sys.stderr,
                        )
                        break
                except Exception as exc:
                    if _is_target_closed_error(exc):
                        if _recover_closed_browser():
                            continue
                        print(
                            "\n[!] The browser was closed while waiting for Argon's answer and could not be reopened.",
                            file=sys.stderr,
                        )
                        print(
                            "    Nothing was applied. Re-run with --resume when the profile is free.",
                            file=sys.stderr,
                        )
                        return EXIT_BROWSER_CLOSED

                wait_s = _next_wait_interval(wait_step)
                wait_step += 1
                try:
                    page.wait_for_timeout(int(wait_s * 1000))
                except Exception as exc:
                    if _is_target_closed_error(exc):
                        if _recover_closed_browser():
                            continue
                        print(
                            "\n[!] The browser was closed while waiting for Argon's answer and could not be reopened.",
                            file=sys.stderr,
                        )
                        print(
                            "    Nothing was applied. Re-run with --resume when the profile is free.",
                            file=sys.stderr,
                        )
                        return EXIT_BROWSER_CLOSED
                    raise

            def _save_notes_once(script_text: str, summary_md: str) -> Optional[Path]:
                """Save notes from the delivered script, or from the copied summary if
                the script did not carry them. Writes at most one notes file per run.
                """
                saved_notes: Optional[Path] = None
                try:
                    script_notes = _extract_notes(script_text)
                    has_real_script_notes = bool(
                        script_notes.strip()
                        and "no inconsistencies found in the reviewed files" not in script_notes.lower()
                    )
                    if has_real_script_notes:
                        saved_notes = _save_notes(script_text, proj_root)
                except Exception:
                    saved_notes = None
                if saved_notes is None and (summary_md or "").strip():
                    try:
                        saved_notes = _save_notes(summary_md, proj_root, existing_notes_path=saved_notes)
                    except Exception:
                        saved_notes = None
                if saved_notes is None and (script_text or "").strip():
                    try:
                        saved_notes = _save_notes(script_text, proj_root, existing_notes_path=saved_notes)
                    except Exception:
                        saved_notes = None
                return saved_notes

            if delivered_path is not None:
                if delivery_dir:
                    canonical_lane_patch = active_downloads_dir / "apply_changes.ps1"
                    try:
                        if delivered_path.resolve() != canonical_lane_patch.resolve():
                            _copy_with_retry(delivered_path, canonical_lane_patch)
                            delivered_path = canonical_lane_patch
                    except Exception:
                        pass
                if chat_id and lane:
                    session_store.remember(
                        proj_root, mode, active_slot, chat_id,
                        chat_title=chat_title, profile_dir=profile_dir_str,
                        lane=lane, patch_path=str(delivered_path),
                        workspace_mode=proven_ws_mode,
                    )
                print(f"[SUCCESS] Delivered patch file: {delivered_path}")
                _, summary_text = _copy_and_save_response_summary(
                    page,
                    delivered_path.parent,
                    chat_title=chat_title,
                    chat_url=effective_chat_url,
                )
                try:
                    delivered_text = delivered_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    delivered_text = ""
                _verify_argon_answer_evidence(
                    page,
                    body_text=delivered_text or last_body,
                    delivered_path=delivered_path,
                )
                delivery_ledger.record(
                    proj_root,
                    delivered_text,
                    chat_id=chat_id or _extract_chat_id(effective_chat_url) or "",
                    lane=lane or "",
                    file_path=str(delivered_path),
                )
                saved_notes_path = _save_notes_once(delivered_text, summary_text)
                task_state.set(
                    proj_root,
                    lane=lane,
                    state="delivered",
                    delivered=str(delivered_path),
                    delivery_hash=_hash_text(delivered_text),
                )
                task_registry.register_done(
                    proj_root,
                    lane=lane,
                    slot=active_slot,
                    status="completed",
                )
                if auto_apply:
                    ok = apply_delivered_patch(proj_root, delivered_path)
                    if not ok:
                        return EXIT_FAILURE
                    task_state.set(proj_root, lane=lane, state="applied", delivered=str(delivered_path))
                    return _verify_project_locally(
                        proj_root=proj_root,
                        task_state=task_state,
                        notes_path=saved_notes_path,
                        zip_path=zip_path,
                        extra_texts=[delivered_text, summary_text],
                        verify_timeout=verify_timeout,
                        verify_cmd_override=verify_cmd,
                        skip_verify=skip_verify,
                    )
                else:
                    if not refuse_invalid_patch(delivered_text, project_path=proj_root, patch_name=delivered_path.name):
                        return EXIT_FAILURE
                    runner = SCRIPTS_DIR / "Run-ArgonPatch.ps1"
                    print("[+] Apply it with one command:")
                    print(f'    pwsh -NoProfile -ExecutionPolicy Bypass -File "{runner}" '
                          f'-ProjectPath "{proj_root}" -PatchFile "{delivered_path}"')
                    ok = True
                return EXIT_SUCCESS if ok else EXIT_FAILURE

            if extracted_patch:
                if not refuse_invalid_patch(extracted_patch, project_path=proj_root, patch_name="apply_changes.ps1"):
                    return EXIT_FAILURE
                out_ps1 = (active_downloads_dir / "apply_changes.ps1") if delivery_dir else (proj_root / "apply_changes.ps1")
                out_ps1.write_text(extracted_patch + "\n", encoding="utf-8")
                if chat_id and lane:
                    session_store.remember(
                        proj_root, mode, active_slot, chat_id,
                        chat_title=chat_title, profile_dir=profile_dir_str,
                        lane=lane, patch_path=str(out_ps1),
                    )
                print(f"[SUCCESS] Extracted patch script to: {out_ps1}")
                _, summary_text = _copy_and_save_response_summary(
                    page,
                    active_downloads_dir,
                    chat_title=chat_title,
                    chat_url=effective_chat_url,
                )
                _verify_argon_answer_evidence(
                    page,
                    body_text=extracted_patch,
                    extracted_patch=extracted_patch,
                )
                delivery_ledger.record(
                    proj_root,
                    extracted_patch,
                    chat_id=chat_id or _extract_chat_id(effective_chat_url) or "",
                    lane=lane or "",
                    file_path=str(out_ps1),
                )
                saved_notes_path = _save_notes_once(extracted_patch, summary_text)
                task_state.set(proj_root, lane=lane, state="delivered", delivered=str(out_ps1), delivery_hash=_hash_text(extracted_patch))
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="completed")
                if auto_apply:
                    ok = apply_extracted_patch(proj_root, last_body, ps1_patch=extracted_patch)
                    if not ok:
                        return EXIT_FAILURE
                    task_state.set(proj_root, lane=lane, state="applied", delivered=str(out_ps1))
                    return _verify_project_locally(
                        proj_root=proj_root,
                        task_state=task_state,
                        notes_path=saved_notes_path,
                        zip_path=zip_path,
                        extra_texts=[extracted_patch, summary_text],
                        verify_timeout=verify_timeout,
                        verify_cmd_override=verify_cmd,
                        skip_verify=skip_verify,
                    )
                return EXIT_SUCCESS
            else:
                still_working_at_deadline = False
                try:
                    still_working_at_deadline = _is_argon_working(page)
                except Exception:
                    still_working_at_deadline = False

                # D10.3: When the deadline is reached while the chat is STILL working,
                # do NOT throw the work away or navigate away for carry-over: save state='waiting'
                # so --resume can pick it up, and explain that closing the browser may end the response.
                if still_working_at_deadline and not run_failed_reason:
                    live_title = _chat_title(page) or chat_title
                    live_url = getattr(page, "url", "") or last_known_url or effective_chat_url
                    deadline_shot = _take_step_screenshot(
                        page,
                        proj_root,
                        "deadline_still_working.png",
                        step_label="deadline_reached",
                        state_label="Argon is still actively working at deadline",
                        lane=lane,
                    )
                    task_state.set(
                        proj_root,
                        lane=lane,
                        state="waiting",
                        chat_id=chat_id or _extract_chat_id(live_url) or "",
                        chat_title=live_title,
                        chat_url=live_url,
                        slot=active_slot,
                        profile_dir=str(profile_dir),
                        last_screenshot=str(deadline_shot or ""),
                    )
                    task_registry.update_running(
                        proj_root,
                        lane=lane,
                        slot=active_slot,
                        chat_url=live_url,
                        screenshot_path=str(deadline_shot or ""),
                    )
                    print("\n" + "=" * 78, file=sys.stderr)
                    print("  [!] DEADLINE REACHED WHILE ARGON IS STILL WORKING - WORK IS NOT LOST", file=sys.stderr)
                    print("=" * 78, file=sys.stderr)
                    print(f"  conversation title : {live_title or '(active conversation)'}", file=sys.stderr)
                    print(f"  conversation link  : {live_url}", file=sys.stderr)
                    print(f"  slot / account     : {active_slot} ({target_email or 'pool account'})", file=sys.stderr)
                    print(f"  screenshot         : {deadline_shot}", file=sys.stderr)
                    print(
                        "  NOTE: The response is still being generated on Gemini's server. "
                        "Closing the browser while a response is streaming may interrupt it.",
                        file=sys.stderr,
                    )
                    print(
                        f'  Resume waiting at any time with:\n'
                        f'    python "{SCRIPTS_DIR / "dispatch_argon.py"}" --project-path "{proj_root}" --resume',
                        file=sys.stderr,
                    )
                    print("=" * 78 + "\n", file=sys.stderr)
                    return EXIT_FAILURE

                stopped_now = _detect_stopped_response(page)
                if stopped_now and not run_failed_reason:
                    run_failed_reason = "Gemini reported: 'You stopped this response' (generation stopped before delivery)"

                reason = run_failed_reason or "no delivery and no patch block before the timeout"
                previous_body = last_body
                if not previous_body:
                    try:
                        previous_body = page.locator("body").inner_text(timeout=8000) or ""
                    except Exception:
                        previous_body = ""
                failed_shot = _take_step_screenshot(
                    page,
                    proj_root,
                    "failed.png",
                    step_label="failed",
                    state_label=reason,
                    lane=lane,
                )
                live_title = _chat_title(page) or chat_title
                live_url = getattr(page, "url", "") or last_known_url or effective_chat_url
                diag_ev = _collect_diagnostic_evidence(
                    page=page,
                    proj_root=proj_root,
                    reason=reason,
                    slot=active_slot,
                    email=target_email,
                    profile_dir=profile_dir,
                    chat_title=live_title,
                    chat_url=live_url,
                    screenshot_path=failed_shot,
                    preflight=preflight,
                    ruled_out=[
                        "Google session authenticated (not on guest page)",
                        "Pre-flight checks passed (Task mode, 4 Argon model, and context zip attached)",
                        "Prompt was pasted and verified as delivered into the conversation",
                    ],
                    next_step=(
                        f"Open the conversation link above in slot profile {profile_dir} "
                        f"(or run Open_Argon_Login.bat -> 'Open in Gemini Web'), download apply_changes.ps1 "
                        f"if Argon finished late and run Open_Argon_Apply.bat, or re-run with --resume."
                    ),
                )
                final_state_name = "superseded" if "superseded" in reason.lower() else "failed"
                task_state.set(
                    proj_root,
                    lane=lane,
                    state=final_state_name,
                    chat_title=live_title,
                    chat_url=live_url,
                    slot=active_slot,
                    profile_dir=str(profile_dir),
                    diagnostic_evidence=diag_ev,
                    last_screenshot=str(failed_shot or ""),
                )
                task_registry.register_done(
                    proj_root,
                    lane=lane,
                    slot=active_slot,
                    status=final_state_name,
                    screenshot_path=str(failed_shot or ""),
                )
                carried = None
                if carry_over and final_state_name != "superseded":
                    carried = _carry_over_to_new_session(
                        page=page,
                        prompt_text=prompt_text,
                        proj_root=proj_root,
                        mode=mode,
                        slot=active_slot,
                        email=target_email,
                        zip_path=zip_path,
                        manual_setup=manual_setup,
                        task_state=task_state,
                        session_store=session_store,
                        previous_text=previous_body,
                        notes=_notes_from_body(previous_body),
                        reason=reason,
                        task=task,
                        target_files=target_files,
                        profile_dir=profile_dir,
                        diagnostic_evidence=diag_ev,
                    )
                if carried is not None:
                    delivered_path = carried
                    _, summary_text = _copy_and_save_response_summary(
                        page,
                        delivered_path.parent,
                        chat_title=_chat_title(page) or chat_title,
                        chat_url=getattr(page, "url", "") or effective_chat_url,
                    )
                    try:
                        delivered_text = delivered_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except Exception:
                        delivered_text = ""
                    saved_notes_path = _save_notes_once(delivered_text, summary_text)
                    task_state.set(
                        proj_root,
                        state="delivered",
                        delivered=str(delivered_path),
                        delivery_hash=_hash_text(delivered_text),
                        diagnostic_evidence=None,
                    )
                    if auto_apply:
                        ok = apply_delivered_patch(proj_root, delivered_path)
                        if not ok:
                            return EXIT_FAILURE
                        task_state.set(proj_root, state="applied", delivered=str(delivered_path))
                        return _verify_project_locally(
                            proj_root=proj_root,
                            task_state=task_state,
                            notes_path=saved_notes_path,
                            zip_path=zip_path,
                            extra_texts=[delivered_text, summary_text],
                            verify_timeout=verify_timeout,
                            verify_cmd_override=verify_cmd,
                            skip_verify=skip_verify,
                        )
                    else:
                        if not refuse_invalid_patch(delivered_text, project_path=proj_root, patch_name=delivered_path.name):
                            return EXIT_FAILURE
                        runner = SCRIPTS_DIR / "Run-ArgonPatch.ps1"
                        print("[+] Apply it with one command:")
                        print(f'    pwsh -NoProfile -ExecutionPolicy Bypass -File "{runner}" '
                              f'-ProjectPath "{proj_root}" -PatchFile "{delivered_path}"')
                        ok = True
                    return EXIT_SUCCESS if ok else EXIT_FAILURE

                _print_run_failure_summary(diag_ev)
                print(f"[!] Neither a delivered file nor a PowerShell patch block was detected ({reason}).")
                print(f"    If Argon delivered a file, run: Open_Argon_Apply.bat  (searches {Path.home() / 'Downloads'})")
                return EXIT_FAILURE
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
            context = None
        slot_lock.release()
        if temp_concurrent_profile is not None and temp_concurrent_profile.exists():
            try:
                shutil.rmtree(temp_concurrent_profile, ignore_errors=True)
            except Exception:
                pass
        print("[+] Browser closed and the slot lock released.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch coding tasks to Gemini 4 Argon")
    parser.add_argument("--task", default="", help="Task or bug description for Gemini 4 Argon")
    parser.add_argument("--project-path", default=".", help="Path to the project root")
    parser.add_argument(
        "--mode",
        default="gemini",
        help="Target workspace ('gemini') or workspace mode ('task'/'chat' when using --record-workspace-mode).",
    )
    parser.add_argument(
        "--record-workspace-mode",
        default=None,
        metavar="CHAT_ID",
        help="Record workspace mode ('task' or 'chat') for an existing conversation in session store.",
    )
    parser.add_argument(
        "--brief-summary",
        default=None,
        metavar="PATH",
        help="Print this brief summary file before sending prompt and record in task registry.",
    )
    parser.add_argument(
        "--brief-text",
        default=None,
        metavar="FILE",
        help="Send the contents of this brief file as the task instead of --task.",
    )
    parser.add_argument("--slot", default=None, help="Account pool slot (e.g. /u/0/)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument(
        "--auto-apply",
        action="store_true",
        help="Automatically execute the returned apply_changes.ps1 patch script",
    )
    parser.add_argument("--files", nargs="*", default=None, help="Optional target file patterns")
    parser.add_argument("--output-zip", default="argon_context.zip", help="Output zip path")
    parser.add_argument(
        "--manual-setup",
        action="store_true",
        help="Skip the automatic Task-mode / model selection and assume you already "
             "switched the workspace to Task mode with 4 Argon selected. The dispatcher "
             "still attaches the zip and sends the prompt.",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Start a FRESH Gemini chat for this project instead of continuing in the "
             "conversation stored for it (~/.gemini/argon_sessions.json).",
    )
    parser.add_argument(
        "--max-accounts",
        type=int,
        default=0,
        help="How many pool accounts to try when one of them hits its usage limit. "
             "0 means every account in the pool.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an unfinished task instead of sending a new prompt. The conversation "
             "recorded in ~/.gemini/argon_task_state.json is reopened: if the answer is "
             "already there it is captured and applied, otherwise the run keeps waiting.",
    )
    parser.add_argument(
        "--forget-session",
        default=None,
        metavar="PROJECT",
        help="Remove the recorded session of a project (or every one when the "
             "value is empty). Use it for the stale Google AI Studio entries.",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="Print every recorded session - the project, the conversation name, "
             "the link, the account and the browser profile - so a run can "
             "always be found again.",
    )
    parser.add_argument(
        "--no-carry-over",
        action="store_true",
        help="Do not carry a run over into a new session when it finishes without a "
             "delivery. By default a run that ends without a patch writes a new prompt "
             "that carries the previous editing context, starts a NEW session and sends "
             "the prompt there.",
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=900,
        help="Timeout in seconds for each project verification command run (default: 900 = 15 minutes).",
    )
    parser.add_argument(
        "--verify-cmd",
        default=None,
        help="Explicit verification command override to run in the project root after applying the patch.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip post-apply project build/lint verification.",
    )
    parser.add_argument(
        "--dual",
        action="store_true",
        help="Run dual-session dispatch: single dispatch on the first round when the project "
             "has no session history; from the second round onward, send a follow-up prompt "
             "to the recorded winner's chat and a new-session prompt to a brand-new chat on "
             "a second pool slot concurrently.",
    )
    parser.add_argument(
        "--record-winner",
        choices=["followup", "new", "old"],
        default=None,
        metavar="LANE",
        help="Record which lane won the previous round ('followup'/'old' or 'new') in the "
             "project's session entry (~/.gemini/argon_sessions.json).",
    )
    parser.add_argument(
        "--second-slot",
        default=None,
        metavar="SLOT",
        help="Explicit pool slot for the new-session lane during --dual dispatch.",
    )
    parser.add_argument(
        "--lane",
        choices=["followup", "new", "single"],
        default=None,
        help="Tag this dispatch run as the 'followup', 'new', or 'single' lane.",
    )
    parser.add_argument(
        "--prompt-file",
        default=None,
        metavar="PATH",
        help="Read the pre-rendered surgical prompt from PATH instead of building it in memory.",
    )
    parser.add_argument(
        "--delivery-dir",
        default=None,
        metavar="DIR",
        help="Directory where the delivered apply_changes.ps1 for this lane is saved.",
    )
    parser.add_argument(
        "--skip-package",
        action="store_true",
        help="Reuse an existing argon_context.zip without re-running package_context.",
    )
    parser.add_argument(
        "--open-comparison",
        action="store_true",
        help="Open comparison.html in the default browser when --dual finishes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Explicitly override an already-running task for this project/lane (marking the earlier run as superseded).",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=1800,
        help="Maximum seconds to wait for Argon's response before saving state for --resume (default: 1800 = 30 min).",
    )
    parser.add_argument(
        "--register-task-start",
        action="store_true",
        help="Agent action: explicitly mark a task as started in ~/.gemini/argon_tasks.json before dispatching.",
    )
    parser.add_argument(
        "--register-task-done",
        "--complete-task",
        dest="register_task_done",
        action="store_true",
        help="Agent action: explicitly mark the running task for --project-path (and optional --lane/--slot) as completed in ~/.gemini/argon_tasks.json.",
    )
    parser.add_argument(
        "--label",
        "--task-label",
        dest="task_label",
        default="",
        help="Free-text label stored in ~/.gemini/argon_tasks.json when using --register-task-start or --register-task-done.",
    )
    parser.add_argument(
        "--list-tasks",
        action="store_true",
        help="List all currently running and stale tasks in ~/.gemini/argon_tasks.json with per-slot load counts.",
    )
    parser.add_argument(
        "--max-verify-rounds",
        type=int,
        default=1,
        help=argparse.SUPPRESS,
    )
    return parser


def select_account_slot(
    pool_cfg: Optional[PoolConfig],
    project_path: Any,
    mode: str = "gemini",
    requested_slot: Optional[str] = None,
    new_session: bool = False,
    resume: bool = False,
    is_carry_over: bool = False,
    lane: Optional[str] = None,
    exclude_slots: Optional[set] = None,
) -> tuple[str, str]:
    """Select the pool slot according to the D3 Account Policy and D4 TaskRegistry.

    Policy:
      - FOLLOW-UP: the account that holds the chat (the recorded winner's slot,
        never rotated, because the conversation lives in that profile).
      - CARRY-OVER: the account that held the failed run, because the context lives there.
      - NEW TASK: always a DIFFERENT account than the one that just ran - the next
        free account in pool order, skipping every slot at its registry limit, a
        quota_limited slot, and the account that ran the previous round.
    """
    excluded = {normalize_slot(s) for s in (exclude_slots or set()) if s}
    reg = TaskRegistry()
    sess_store = SessionStore()
    task_store = TaskState()

    if requested_slot:
        norm_req = normalize_slot(requested_slot)
        return norm_req, f"explicitly requested slot {norm_req}"

    if resume:
        r_entry = task_store.get(project_path, lane=lane) or task_store.get(project_path) or {}
        if r_entry.get("slot"):
            r_slot = normalize_slot(str(r_entry["slot"]))
            return r_slot, f"RESUMING running task on its original slot {r_slot}"

    if is_carry_over:
        c_entry = task_store.get(project_path, lane=lane) or task_store.get(project_path) or {}
        if c_entry.get("slot"):
            c_slot = normalize_slot(str(c_entry["slot"]))
            return c_slot, f"CARRY-OVER policy: staying on {c_slot} where the failed run's context lives"

    # Rule 1: The follow-up always goes to the account that holds the chat - never rotated.
    # Even when that account is already busy with two tasks, that chat lives there, so USE it and say so in one line.
    existing_session = None if (new_session or lane == "new") else sess_store.lookup(project_path, mode=mode)
    if existing_session and existing_session.get("chat_url") and existing_session.get("slot"):
        f_slot = normalize_slot(str(existing_session["slot"]))
        f_cid = str(existing_session.get("chat_id") or "")
        f_load = len(reg.active_tasks_for_slot(f_slot))
        busy_note = f"; slot already has {f_load} active task(s), using it anyway because the chat lives there" if f_load >= MAX_TASKS_PER_SLOT else f" (registry load {f_load}/{MAX_TASKS_PER_SLOT})"
        return (
            f_slot,
            f"FOLLOW-UP policy: continuing conversation {f_cid or '(stored)'} on the account that holds it ({f_slot}{busy_note})",
        )

    # Rule 2 & 3: A new task prefers an account that is not busy. When no account is free,
    # continue on the least loaded account and print the reason. Never refuse to work because an account is busy.
    last_ran_slots: set = set(excluded)
    if existing_session and existing_session.get("slot"):
        last_ran_slots.add(normalize_slot(str(existing_session["slot"])))
    t_entry = task_store.get(project_path) or {}
    if t_entry.get("slot"):
        last_ran_slots.add(normalize_slot(str(t_entry["slot"])))
    reg_last = str(reg.load().get("last_used_slot") or "").strip()
    if reg_last:
        last_ran_slots.add(normalize_slot(reg_last))
    if pool_cfg is not None and pool_cfg.data.get("active_slot"):
        last_ran_slots.add(normalize_slot(str(pool_cfg.data["active_slot"])))

    anchor_slot = (
        normalize_slot(str(existing_session["slot"]))
        if (existing_session and existing_session.get("slot"))
        else (normalize_slot(reg_last) if reg_last else (pool_cfg.active_slot() if pool_cfg else "/u/0/"))
    )
    ordered_pool = _pool_slots_in_order(pool_cfg, anchor_slot, skip_start_slot=True)
    if not ordered_pool:
        ordered_pool = [f"/u/{i}/" for i in range(8)]

    base_prof = pool_cfg.profile_dir if pool_cfg is not None else (Path.home() / ".gemini" / "argon_browser_profile")

    # Pass 1: Prefer a completely idle slot (0 tasks in registry, profile not busy) different from last_ran_slots
    for cand in ordered_pool:
        norm_c = normalize_slot(cand)
        if norm_c in last_ran_slots:
            continue
        active_on_slot = reg.active_tasks_for_slot(norm_c)
        if len(active_on_slot) == 0 and not _profile_is_busy(_profile_dir_for_slot(base_prof, norm_c)):
            return (
                norm_c,
                f"NEW-TASK policy: rotated to next free account {norm_c} (skipped previous {sorted(last_ran_slots)}, registry load 0/{MAX_TASKS_PER_SLOT})",
            )

    # Pass 2: Any slot with 0 tasks in registry and profile not busy
    for cand in ordered_pool:
        norm_c = normalize_slot(cand)
        if norm_c in excluded:
            continue
        active_on_slot = reg.active_tasks_for_slot(norm_c)
        if len(active_on_slot) == 0 and not _profile_is_busy(_profile_dir_for_slot(base_prof, norm_c)):
            return (
                norm_c,
                f"NEW-TASK policy: selected idle account {norm_c} (registry load 0/{MAX_TASKS_PER_SLOT})",
            )

    # Pass 3: Any slot under MAX_TASKS_PER_SLOT (2) different from last_ran_slots
    for cand in ordered_pool:
        norm_c = normalize_slot(cand)
        if norm_c in last_ran_slots:
            continue
        ok_s, cap_reason = reg.is_slot_available(norm_c, max_tasks=MAX_TASKS_PER_SLOT)
        if ok_s and not _profile_is_busy(_profile_dir_for_slot(base_prof, norm_c)):
            return (
                norm_c,
                f"NEW-TASK policy: rotated to available account {norm_c} (skipped previous {sorted(last_ran_slots)}, {cap_reason})",
            )

    # Pass 4: Least-loaded slot across the pool (never refuse because an account is busy)
    pool_candidates = [normalize_slot(c) for c in ordered_pool if normalize_slot(c) not in excluded] or [
        normalize_slot(c) for c in ordered_pool
    ]
    least_loaded = min(
        pool_candidates,
        key=lambda s: (
            len(reg.active_tasks_for_slot(s)),
            1 if s in last_ran_slots else 0,
            1 if _profile_is_busy(_profile_dir_for_slot(base_prof, s)) else 0,
        ),
    )
    ll_count = len(reg.active_tasks_for_slot(least_loaded))
    return (
        least_loaded,
        f"NEW-TASK least-loaded fallback: all accounts are busy; continuing on least-loaded account {least_loaded} (registry load {ll_count}/{MAX_TASKS_PER_SLOT})",
    )


def _pool_slots_in_order(
    pool_cfg,
    start_slot: Optional[str],
    skip_start_slot: bool = False,
) -> list:
    """Pool slots to try in wrap-around order.

    When `skip_start_slot=True` (used for NEW tasks under D3), starts at the slot
    IMMEDIATELY AFTER `start_slot` so a new task never reuses the account that
    just ran unless every other account is unavailable.
    """
    if pool_cfg is None:
        return []
    accounts = [a for a in (pool_cfg.accounts or []) if a.get("slot")]
    if not accounts:
        return []

    def _slot_sort_key(acc: Dict[str, Any]) -> tuple:
        raw_slot = str(acc.get("slot", ""))
        m = re.search(r"/u/(\d+)/?", raw_slot)
        num = int(m.group(1)) if m else int(acc.get("index", 0))
        return (num, int(acc.get("index", 0)))

    ordered = sorted(accounts, key=_slot_sort_key)
    target_start = normalize_slot(start_slot) if start_slot else pool_cfg.active_slot()
    start_idx = 0
    for idx, acc in enumerate(ordered):
        if normalize_slot(acc.get("slot", "")) == target_start:
            start_idx = idx
            break
    if skip_start_slot and len(ordered) > 1:
        start_idx = (start_idx + 1) % len(ordered)
    rotated = ordered[start_idx:] + ordered[:start_idx]

    skip_statuses = {"quota_limited", "argon_disabled", "no_argon"}
    fresh = [
        a for a in rotated
        if str(a.get("status", "")).strip().lower() not in skip_statuses
    ]
    return [normalize_slot(a["slot"]) for a in fresh if a.get("slot")]


def _mark_quota_limited(slot: str) -> None:
    """Remember that this account hit its usage limit."""
    try:
        from argon_pool_login import PoolConfig

        PoolConfig().set_status(slot, "quota_limited")
        print(f"[+] Marked {slot} as quota limited; the next run will skip it.")
    except Exception as exc:
        print(f"[!] Could not record the quota limit for {slot}: {exc}", file=sys.stderr)


def list_registered_tasks_cli() -> int:
    """Print all active and stale tasks in ~/.gemini/argon_tasks.json (D4)."""
    reg = TaskRegistry()
    active = reg.active_entries()
    stale = reg.stale_tasks()
    stale_ids = {s.get("task_id") for s in stale}
    print("=" * 78)
    print(f"  ARGON TASK REGISTRY ({reg.path})")
    print("=" * 78)
    if not active:
        print("  No active running tasks in the registry.")
    else:
        for e in active:
            tid = e.get("task_id", "?")
            tag = " [STALE]" if tid in stale_ids else ""
            print(
                f"  - {tid}{tag}\n"
                f"      project    : {e.get('project')}\n"
                f"      slot/lane  : {e.get('slot')} (lane={e.get('lane')}, account={e.get('account') or '?'})\n"
                f"      chat       : {e.get('chat_id') or '(pending)'} ({e.get('chat_url') or ''})\n"
                f"      started_at : {e.get('started_at')}\n"
                f"      label      : {e.get('label') or ''}\n"
                f"      screenshot : {e.get('screenshot_path') or '(none)'}"
            )
    print("=" * 78)
    return EXIT_SUCCESS


def _make_console_utf8_safe() -> None:
    """Never let a print kill the dispatcher with UnicodeEncodeError.

    A redirected pipe on Windows defaults to cp1252. The brief summary that
    --brief-summary prints contains emoji and Bengali text, so printing it raw
    raises UnicodeEncodeError; the surrounding try/except then swallows the
    summary instead of showing it. Keep the stream's own encoding but replace
    whatever it cannot represent, so the summary is always displayed.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _make_console_utf8_safe()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.list_sessions:
        return list_sessions()

    if getattr(args, "list_tasks", False):
        return list_registered_tasks_cli()

    if args.forget_session is not None:
        return forget_sessions(args.forget_session)

    if args.record_winner is not None:
        return record_winner_cli(args.project_path, args.record_winner, mode=args.mode)

    if getattr(args, "record_workspace_mode", None) is not None:
        cid = str(args.record_workspace_mode).strip()
        wanted_mode = (getattr(args, "workspace_mode", "") or args.mode or "").strip().lower()
        if wanted_mode not in ("task", "chat"):
            print(f"[!] Error: Mode must be 'task' or 'chat', got {wanted_mode!r}.", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED
        sess_store = SessionStore()
        sess_store.load()
        proj_key = sess_store._find_key(args.project_path)
        if not proj_key:
            print(
                f"[!] Error: Project {Path(args.project_path).resolve()} not found in session store ({sess_store.path}).",
                file=sys.stderr,
            )
            return EXIT_PREFLIGHT_FAILED
        s_data = sess_store.data.get("sessions", {}).get(proj_key, {})
        matched = False
        if s_data.get("chat_id") == cid:
            s_data["workspace_mode"] = wanted_mode
            matched = True
        lanes = s_data.get("lanes", {})
        if isinstance(lanes, dict):
            for l_key, l_val in lanes.items():
                if isinstance(l_val, dict) and l_val.get("chat_id") == cid:
                    l_val["workspace_mode"] = wanted_mode
                    matched = True
        if not matched:
            print(
                f"[!] Error: Chat ID {cid!r} is not known for project {Path(args.project_path).resolve()}; "
                f"cannot record workspace_mode.",
                file=sys.stderr,
            )
            return EXIT_PREFLIGHT_FAILED
        sess_store.save()
        print(
            f"[+] Recorded workspace_mode={wanted_mode!r} for conversation {cid} in session store ({sess_store.path})."
        )
        return EXIT_SUCCESS

    if getattr(args, "register_task_done", False):
        reg = TaskRegistry()
        closed = reg.register_done(
            args.project_path,
            lane=args.lane,
            slot=args.slot,
            status="completed",
            label=args.task_label,
        )
        print(
            f"[+] Marked {closed} running task(s) as completed in {reg.path} "
            f"for project {Path(args.project_path).resolve()}"
            + (f" (label={args.task_label!r})." if args.task_label else ".")
        )
        return EXIT_SUCCESS

    if getattr(args, "register_task_start", False):
        try:
            pool_cfg_init = PoolConfig()
        except Exception:
            pool_cfg_init = None
        chosen_slot, reason = select_account_slot(
            pool_cfg=pool_cfg_init,
            project_path=args.project_path,
            mode=args.mode,
            requested_slot=args.slot,
            new_session=args.new_session,
            lane=args.lane,
        )
        reg = TaskRegistry()
        ok_s, cap_msg = reg.is_slot_available(chosen_slot, max_tasks=MAX_TASKS_PER_SLOT)
        if not ok_s:
            print(
                f"[+] Slot {chosen_slot} is busy ({cap_msg}); continuing on {chosen_slot} without blocking ({reason})."
            )
        rec = reg.register_start(
            args.project_path,
            slot=chosen_slot,
            lane=args.lane or "single",
            label=args.task_label or args.task or "agent-registered task",
        )
        print(
            f"[+] Registered task start in {reg.path}: task_id={rec['task_id']} "
            f"slot={chosen_slot} ({reason})"
        )
        return EXIT_SUCCESS

    if getattr(args, "brief_text", None):
        bt_path = Path(args.brief_text).resolve()
        if not bt_path.is_file():
            parser.error(f"--brief-text file not found: {args.brief_text}")
        args.task = bt_path.read_text(encoding="utf-8")

    if not args.resume and not (args.task or "").strip():
        parser.error("the following arguments are required: --task (unless --resume or --brief-text is used)")

    if args.dual:
        from dual_dispatch import run_dual_dispatch

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
            verify_timeout=args.verify_timeout,
            verify_cmd=args.verify_cmd,
            skip_verify=args.skip_verify,
            open_comparison=args.open_comparison,
        )

    # A run that finishes WITHOUT a delivery is carried over into a NEW session
    # by default (see --no-carry-over).
    carry_over = not args.no_carry_over

    try:
        from argon_pool_login import PoolConfig

        pool_cfg = PoolConfig()
    except Exception:
        pool_cfg = None

    if args.resume:
        resume_entry = TaskState().get(args.project_path, lane=args.lane) or TaskState().get(args.project_path)
        resume_slot = args.slot or (resume_entry.get("slot") if resume_entry else None)
        return dispatch_to_argon(
            task=args.task,
            project_path=args.project_path,
            mode=args.mode,
            slot=resume_slot,
            headless=args.headless,
            auto_apply=args.auto_apply,
            manual_setup=args.manual_setup,
            target_files=args.files,
            output_zip=args.output_zip,
            new_session=args.new_session,
            resume=True,
            carry_over=carry_over,
            verify_timeout=args.verify_timeout,
            verify_cmd=args.verify_cmd,
            skip_verify=args.skip_verify,
            lane=args.lane,
            force=args.force,
            deadline_seconds=args.deadline_seconds,
        )

    existing_session = None if args.new_session else SessionStore().lookup(args.project_path, mode=args.mode)
    is_followup_bound = bool(existing_session and existing_session.get("chat_url") and not args.new_session)

    chosen_slot, slot_reason = select_account_slot(
        pool_cfg=pool_cfg,
        project_path=args.project_path,
        mode=args.mode,
        requested_slot=args.slot,
        new_session=args.new_session,
        lane=args.lane,
    )
    print(f"[+] Account policy selected start slot {chosen_slot}: {slot_reason}")

    if is_followup_bound:
        # D3: A FOLLOW-UP stays on the account that holds the chat (never rotated to another slot)
        slots = [chosen_slot]
    else:
        slots = _pool_slots_in_order(pool_cfg, chosen_slot, skip_start_slot=False)
        if chosen_slot not in slots:
            slots.insert(0, chosen_slot)
        if args.max_accounts and args.max_accounts > 0 and slots:
            slots = slots[: args.max_accounts]

    if not slots:
        return dispatch_to_argon(
            task=args.task,
            project_path=args.project_path,
            mode=args.mode,
            slot=chosen_slot,
            headless=args.headless,
            auto_apply=args.auto_apply,
            manual_setup=args.manual_setup,
            target_files=args.files,
            output_zip=args.output_zip,
            new_session=args.new_session,
            resume=args.resume,
            carry_over=carry_over,
            verify_timeout=args.verify_timeout,
            verify_cmd=args.verify_cmd,
            skip_verify=args.skip_verify,
            prompt_file=args.prompt_file,
            delivery_dir=args.delivery_dir,
            lane=args.lane,
            skip_package=args.skip_package,
            force=args.force,
            deadline_seconds=args.deadline_seconds,
        )

    # D20: Continue rotating across pool accounts on ANY pre-flight / sign-in / launch / busy / quota failure
    # of a new task (only marking the account quota_limited when the reason is quota / Argon disabled).
    last = EXIT_FAILURE
    saw_quota = False
    saw_busy = False
    skipped_accounts: List[tuple[str, str]] = []
    rotatable_codes = {
        EXIT_QUOTA_LIMITED,
        EXIT_SLOT_BUSY,
        EXIT_PREFLIGHT_FAILED,
        EXIT_NOT_SIGNED_IN,
        EXIT_BROWSER_LAUNCH_FAILED,
    }
    code_labels = {
        EXIT_QUOTA_LIMITED: "quota_limited / 4 Argon disabled",
        EXIT_SLOT_BUSY: "slot busy or at 2-task registry limit",
        EXIT_PREFLIGHT_FAILED: "pre-flight check failed",
        EXIT_NOT_SIGNED_IN: "profile not signed in",
        EXIT_BROWSER_LAUNCH_FAILED: "browser launch failed",
    }

    for index, slot in enumerate(slots):
        if index:
            print(f"\n[+] Rotating to next pool account ({index + 1}/{len(slots)}): {slot}")
        last = dispatch_to_argon(
            task=args.task,
            project_path=args.project_path,
            mode=args.mode,
            slot=slot,
            headless=args.headless,
            auto_apply=args.auto_apply,
            manual_setup=args.manual_setup,
            target_files=args.files,
            output_zip=args.output_zip,
            new_session=args.new_session,
            resume=args.resume,
            carry_over=carry_over,
            verify_timeout=args.verify_timeout,
            verify_cmd=args.verify_cmd,
            skip_verify=args.skip_verify,
            prompt_file=args.prompt_file,
            delivery_dir=args.delivery_dir,
            lane=args.lane,
            skip_package=args.skip_package,
            force=args.force,
            deadline_seconds=args.deadline_seconds,
        )
        if last == EXIT_SUCCESS or last == EXIT_ALREADY_RUNNING:
            if skipped_accounts:
                print(f"[+] Account rotation summary: tried {index + 1} account(s); succeeded on {slot}.")
                for sk_slot, sk_reason in skipped_accounts:
                    print(f"    - skipped {sk_slot}: {sk_reason}")
            return last
        if not is_followup_bound and last in rotatable_codes and len(slots) > 1:
            reason_label = code_labels.get(last, f"exit code {last}")
            skipped_accounts.append((slot, reason_label))
            if last == EXIT_QUOTA_LIMITED:
                saw_quota = True
                _mark_quota_limited(slot)
            elif last == EXIT_SLOT_BUSY:
                saw_busy = True
            print(
                f"[+] Account {slot} failed ({reason_label}) - continuing rotation across remaining pool accounts."
            )
            continue
        return last

    if skipped_accounts:
        print(f"\n[!] Pool rotation exhausted after trying {len(skipped_accounts)} account(s):", file=sys.stderr)
        for sk_slot, sk_reason in skipped_accounts:
            print(f"    - {sk_slot}: {sk_reason}", file=sys.stderr)
    if saw_busy and not saw_quota:
        print("\n[!] Every account slot in the pool is currently busy.", file=sys.stderr)
        return EXIT_SLOT_BUSY
    if saw_quota:
        print("\n[!] Every available account in the pool is rate limited right now.", file=sys.stderr)
        return EXIT_QUOTA_LIMITED
    return last


if __name__ == "__main__":
    sys.exit(main())
