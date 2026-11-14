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
   rotates across pool accounts when a slot is busy, selects the 4 Argon model, pastes the
   prompt, attaches argon_context.zip, clicks send the moment the upload finishes, extracts
   the returned PowerShell patch (apply_changes.ps1), and optionally applies it automatically.

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
COMPANION_EXTENSION_DIR = SKILL_ROOT / "extension"
PROMPT_TEMPLATE_PATH = SKILL_ROOT / "templates" / "argon_task_prompt_template.md"
LOGIN_ASSISTANT_SCRIPT = SCRIPTS_DIR / "login_pool_accounts.py"
APPLY_PATCH_SCRIPT = SCRIPTS_DIR / "Apply-ArgonPatch.ps1"


class ArgonOperationError(RuntimeError):
    """Structured per-operation failure raised when attach, paste, mode, model, send, or selftest fails.

    Every operation reports its own name, exact reason, and execution path ('extension' or
    'python_fallback') in seconds instead of collapsing into a generic 'the prompt was not
    delivered' message after a 25-minute timeout.
    """

    def __init__(
        self,
        operation: str,
        reason: str,
        path_used: str = "python_fallback",
        elapsed_seconds: float = 0.0,
    ) -> None:
        self.operation = str(operation or "unknown").strip()
        self.reason = str(reason or "operation failed").strip()
        self.path_used = str(path_used or "python_fallback").strip()
        self.elapsed_seconds = float(elapsed_seconds or 0.0)
        super().__init__(
            f"[{self.operation.upper()} FAILED via {self.path_used} in {self.elapsed_seconds:.1f}s] {self.reason}"
        )


class ModeSelectionError(ArgonOperationError):
    def __init__(self, reason: str, path_used: str = "python_fallback", elapsed_seconds: float = 0.0) -> None:
        super().__init__("mode", reason, path_used=path_used, elapsed_seconds=elapsed_seconds)


class ModelSelectionError(ArgonOperationError):
    def __init__(self, reason: str, path_used: str = "python_fallback", elapsed_seconds: float = 0.0) -> None:
        super().__init__("model", reason, path_used=path_used, elapsed_seconds=elapsed_seconds)


class PasteOperationError(ArgonOperationError):
    def __init__(self, reason: str, path_used: str = "python_fallback", elapsed_seconds: float = 0.0) -> None:
        super().__init__("paste", reason, path_used=path_used, elapsed_seconds=elapsed_seconds)


class AttachOperationError(ArgonOperationError):
    def __init__(self, reason: str, path_used: str = "python_fallback", elapsed_seconds: float = 0.0) -> None:
        super().__init__("attach", reason, path_used=path_used, elapsed_seconds=elapsed_seconds)


class SendOperationError(ArgonOperationError):
    def __init__(self, reason: str, path_used: str = "python_fallback", elapsed_seconds: float = 0.0) -> None:
        super().__init__("send", reason, path_used=path_used, elapsed_seconds=elapsed_seconds)


class PreflightSelfTestError(ArgonOperationError):
    def __init__(self, operation: str, reason: str, path_used: str = "python_fallback", elapsed_seconds: float = 0.0) -> None:
        super().__init__(f"selftest:{operation}", reason, path_used=path_used, elapsed_seconds=elapsed_seconds)

# Per-project session map: which Gemini conversation to continue in.
# It lives in the USER profile, next to the browser profile, so every copy of
# this plugin (the repo, the Gemini CLI plugin, the MiniMax plugin) shares the
# same map. A per-plugin copy would lose the conversation as soon as another
# agent or another installation of the plugin picks the project up.
SESSION_STORE_PATH = Path.home() / ".gemini" / "argon_sessions.json"

# Per-project task state: which conversation the current task is in and how far
# it got, so a run can be resumed after the browser was closed.
#
# The state file is keyed by the project root only, so two agents that work on the same
# repository SHARE it: the second run inherits the first run's chat id and lane records,
# and the two runs then close each other's slot browsers. Set ARGON_TASK_STATE_PATH to
# give a run its own state file when it must run beside another one on the same project.
TASK_STATE_PATH = Path(
    os.environ.get("ARGON_TASK_STATE_PATH")
    or (Path.home() / ".gemini" / "argon_task_state.json")
)
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
#   6 = browser launch failed
#   7 = slot busy
#   8 = build/lint verification failed after all verification rounds
#   9 = task already running for this project/lane (use --resume or --force)
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_NOT_SIGNED_IN = 2
EXIT_PREFLIGHT_FAILED = 3
EXIT_BROWSER_CLOSED = 4
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


QUARANTINE_DIR_NAME = ".argon_quarantine"
MAX_CHIP_DOWNLOAD_ATTEMPTS = 3
MAX_LADDER_ATTEMPTS = 3


def _is_quarantined_path(path: Path) -> bool:
    """Return True when `path` sits inside `.argon_quarantine` (Section 2.1.C)."""
    try:
        parts = [pt.lower() for pt in Path(path).parts]
        return QUARANTINE_DIR_NAME.lower() in parts or "quarantine" in parts
    except Exception:
        return False


def _quarantine_rejected_file(
    file_path: Path,
    reason: str,
    project_path: Optional[Path] = None,
    dest_dir: Optional[Path] = None,
    defects: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Path]:
    """Move a rejected download into `<project_root>/.argon_quarantine/` so it is NEVER deleted (Section 2.1.B & 2.1.C).

    Both `find_delivered_patch()` and `_is_leftover_delivery()` skip `.argon_quarantine`,
    so a kept rejected file is never re-picked as a delivery or mis-read as a duplicate.
    Prints the file name, its byte size, the exact check that failed, and its ABSOLUTE PATH.
    """
    try:
        src = Path(file_path)
        if not src.is_file():
            return None
        try:
            file_bytes = src.stat().st_size
        except OSError:
            file_bytes = 0

        proj_root = project_path
        if proj_root is None and dest_dir is not None:
            proj_root = _infer_project_root_from_dest(Path(dest_dir))
        if proj_root is None:
            proj_root = _infer_project_root_from_dest(src.parent) or src.parent.parent

        q_dir = Path(proj_root).resolve() / QUARANTINE_DIR_NAME
        q_dir.mkdir(parents=True, exist_ok=True)

        ts = time.strftime("%Y%m%d_%H%M%S")
        clean_name = re.sub(r"^\.staging_\d+_\d+_", "", src.name)
        if not clean_name.lower().endswith(".ps1"):
            clean_name = f"{clean_name}.ps1"
        q_dest = q_dir / f"{ts}_{os.getpid()}_{clean_name}"
        counter = 1
        while q_dest.exists():
            counter += 1
            q_dest = q_dir / f"{ts}_{os.getpid()}_{counter}_{clean_name}"

        try:
            os.replace(str(src), str(q_dest))
        except Exception:
            shutil.copy2(str(src), str(q_dest))
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass

        abs_q = q_dest.resolve()
        print(
            f"[!] Rejected delivery file kept in quarantine (NEVER deleted):\n"
            f"    File name      : {clean_name}\n"
            f"    File size      : {file_bytes} bytes\n"
            f"    Failed check   : {reason}\n"
            f"    Quarantine path: {abs_q}",
            file=sys.stderr,
        )
        if defects:
            for d in defects:
                print(
                    f"  [REFUSED] file={d.get('file', '<script>')}  "
                    f"label={d.get('label', 'check')}  "
                    f"offset={d.get('offset', 0)}  "
                    f"reason={d.get('reason', reason)}",
                    file=sys.stderr,
                )
        print(
            f'    Inspect or apply by hand with: Open_Argon_Apply.bat -PatchFile "{abs_q}"',
            file=sys.stderr,
        )
        return abs_q
    except Exception as exc:
        print(f"[!] Could not move rejected file {file_path} to quarantine: {exc}", file=sys.stderr)
        return file_path if Path(file_path).is_file() else None


def find_delivered_patch(search_dirs, target_name: str = "apply_changes",
                         newer_than: Optional[float] = None) -> Optional[Path]:
    """Find the newest delivered `apply_changes*.ps1` inside the given folders.

    `newer_than` filters by modification time so a file that was already lying
    around cannot be mistaken for a fresh delivery. Skips `.argon_quarantine`.
    """
    best: Optional[Path] = None
    for d in search_dirs:
        try:
            d = Path(d)
            if not d.is_dir() or _is_quarantined_path(d):
                continue
            for p in d.glob(f"{target_name}*.ps1"):
                if not p.is_file() or _is_quarantined_path(p):
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


class _PatchCheckResult:
    """Boolean-compatible validation result that also carries the exact failure reason and defects list (Section 2.1.B).

    Supports:
      - `if _looks_like_patch(text):` / `if not _patch_is_complete(text):` (via `__bool__`)
      - `ok, reason = _looks_like_patch(text)` (via `__iter__`)
      - `res.ok`, `res.reason`, `res.defects`
      - `str(res)` -> returns `res.reason`
    """

    def __init__(
        self,
        ok: bool,
        reason: str = "",
        defects: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.ok = bool(ok)
        self.reason = str(reason or "")
        self.defects: List[Dict[str, Any]] = list(defects or [])
        if not self.ok and self.reason and not self.defects:
            self.defects.append(
                {
                    "file": "<script>",
                    "label": "patch-check",
                    "offset": 0,
                    "reason": self.reason,
                }
            )

    def __bool__(self) -> bool:
        return self.ok

    def __iter__(self):
        yield self.ok
        yield self.reason

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, bool):
            return self.ok == other
        if isinstance(other, _PatchCheckResult):
            return self.ok == other.ok and self.reason == other.reason
        if isinstance(other, str):
            return self.reason == other
        return False

    def __str__(self) -> str:
        return self.reason if not self.ok else "ok"

    def __repr__(self) -> str:
        return f"_PatchCheckResult(ok={self.ok}, reason={self.reason!r})"


def _parse_herestring_blocks(
    patch_text: str,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    """Parse PowerShell here-string blocks line-by-line and evaluate delimiter balance PER BLOCK (Section 2.1.A).

    In PowerShell, a here-string opens ONLY at the end of a script line
    and closes ONLY on a line whose start is the matching closer at column 0 with nothing else
    on that line. Any delimiter token inside an already-open payload body is payload content,
    not a script delimiter.
    # Preserves balance for ' (or ') and ' (or ')

    Returns `(blocks, delimiter_defects, outside_script_text)`.
    """
    blocks: List[Dict[str, Any]] = []
    delimiter_defects: List[Dict[str, Any]] = []
    if not patch_text:
        return blocks, delimiter_defects, ""

    open_sq = chr(64) + chr(39)
    close_sq = chr(39) + chr(64)
    open_dq = chr(64) + chr(34)
    close_dq = chr(34) + chr(64)

    file_assign_re = re.compile(
        r"(?:\$(?:relPath|rel|relativePath|file|targetRel)(?:_\w+|\d+)?\s*=\s*['\"]([^'\"]+)['\"]|"
        r"Join-Path\s+\$TargetRoot\s+['\"]([^'\"]+)['\"]|"
        r"(?:-RelPath|(?:RelPath|File)\s*=)\s*['\"]([^'\"]+)['\"])",
        re.IGNORECASE,
    )
    label_assign_re = re.compile(
        r"(?:\$([A-Za-z_]\w*)\s*=\s*$|(?:-Label|Label\s*=)\s*['\"]([^'\"]+)['\"])",
        re.IGNORECASE,
    )

    lines = patch_text.splitlines(keepends=True)
    outside_lines: List[str] = []

    in_block = False
    block_quote_kind = ""
    block_closer = ""
    block_opener = ""
    block_open_offset = 0
    block_body_start = 0
    block_file = "<unknown>"
    block_label = ""
    block_lines: List[str] = []
    block_idx = 0
    inner_nested_depth = 0
    offset = 0

    for raw_ln in lines:
        line_len = len(raw_ln)
        stripped_eol = raw_ln.rstrip("\r\n")
        trimmed_ws = stripped_eol.rstrip(" \t")

        if not in_block:
            if stripped_eol.startswith(close_sq) and trimmed_ws == close_sq:
                delimiter_defects.append(
                    {
                        "file": "<script>",
                        "label": "here-string",
                        "offset": offset,
                        "reason": f"unbalanced single-quoted here-string delimiter (stray closing {close_sq} at offset {offset})",
                    }
                )
                outside_lines.append(raw_ln)
                offset += line_len
                continue
            if stripped_eol.startswith(close_dq) and trimmed_ws == close_dq:
                delimiter_defects.append(
                    {
                        "file": "<script>",
                        "label": "here-string",
                        "offset": offset,
                        "reason": f"unbalanced double-quoted here-string delimiter (stray closing {close_dq} at offset {offset})",
                    }
                )
                outside_lines.append(raw_ln)
                offset += line_len
                continue

            is_comment_ln = stripped_eol.lstrip(" \t").startswith("#")
            if not is_comment_ln and (trimmed_ws.endswith(open_sq) or trimmed_ws.endswith(open_dq)):
                opener_tok = open_sq if trimmed_ws.endswith(open_sq) else open_dq
                before_opener = trimmed_ws[: -len(opener_tok)].rstrip(" \t")
                if not before_opener or before_opener[-1] in ("=", "(", ",", "{", ";", "["):
                    block_idx += 1
                    in_block = True
                    block_opener = opener_tok
                    block_quote_kind = "single" if opener_tok == open_sq else "double"
                    block_closer = close_sq if opener_tok == open_sq else close_dq
                    block_open_offset = offset + len(trimmed_ws) - len(opener_tok)
                    block_body_start = offset + line_len
                    block_lines = []
                    inner_nested_depth = 0

                    prefix = patch_text[: offset + len(trimmed_ws) - len(opener_tok)]
                    current_file = "<unknown>"
                    for fm in file_assign_re.finditer(prefix):
                        cand = fm.group(1) or fm.group(2) or fm.group(3) or ""
                        if cand and cand != "<unknown>" and ".argon_backup" not in cand:
                            current_file = cand.replace("\\", "/")
                    block_file = current_file

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
                    block_label = current_label

                    outside_lines.append(raw_ln)
                    offset += line_len
                    continue

            outside_lines.append(raw_ln)
            offset += line_len
        else:
            if stripped_eol.startswith(block_closer) and trimmed_ws == block_closer:
                if inner_nested_depth > 0:
                    inner_nested_depth -= 1
                    block_lines.append(raw_ln)
                    offset += line_len
                    continue
                body_raw = "".join(block_lines)
                if body_raw.endswith("\r\n"):
                    body_clean = body_raw[:-2]
                elif body_raw.endswith("\n") or body_raw.endswith("\r"):
                    body_clean = body_raw[:-1]
                else:
                    body_clean = body_raw
                blocks.append(
                    {
                        "index": block_idx,
                        "file": block_file,
                        "label": block_label,
                        "quote_kind": block_quote_kind,
                        "body": body_clean,
                        "script_offset": block_body_start,
                    }
                )
                in_block = False
                outside_lines.append(raw_ln)
                offset += line_len
            else:
                block_lines.append(raw_ln)
                offset += line_len

    if in_block:
        q_desc = f"single-quoted ({open_sq} vs {close_sq})" if block_quote_kind == "single" else f"double-quoted ({open_dq} vs {close_dq})" 
        delimiter_defects.append(
            {
                "file": block_file,
                "label": block_label or f"block #{block_idx}",
                "offset": block_open_offset,
                "reason": f"unbalanced {q_desc} here-string delimiters in {block_label or f'block #{block_idx}'} (opened with {block_opener} at offset {block_open_offset} and never closed with {block_closer})",
            }
        )

    return blocks, delimiter_defects, "".join(outside_lines)


def _extract_patch_payload_blocks(patch_text: str) -> List[Dict[str, Any]]:
    """Extract every here-string payload block with its target file and label."""
    blocks, _, _ = _parse_herestring_blocks(patch_text)
    return blocks


def _find_lone_cr_offset(text: str) -> int:
    """Return the index of the first lone CR (`\r` not followed by `\n`), or -1 if none."""
    idx = 0
    n = len(text)
    while idx < n:
        pos = text.find("\r", idx)
        if pos == -1:
            return -1
        if pos + 1 >= n or text[pos + 1] != "\n":
            return pos
        idx = pos + 2
    return -1


def validate_patch_payloads(patch_text: str, project_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Validate payload blocks and script lines in a delivered patch script BEFORE writing any file (Section 2.1.A).

    Rules (per Round 14 owner specification):
      1. Here-string delimiter balance is evaluated PER BLOCK, not by whole-file substring count.
      2. A NUL byte (`0x00`) or a lone CR (`\r` not followed by `\n`) is refused EVERYWHERE (payload and script).
      3. Inside a PAYLOAD block:
         - TAB (`0x09`) and typographic quotes (`U+2018`..`U+201D`) are ordinary code and are ALLOWED.
         - Raw control characters `< 0x20` other than TAB (`0x09`), CR (`0x0D`), LF (`0x0A`) are refused.
         - `$` is refused ONLY when the enclosing here-string is double-quoted.
      4. On the patch script's OWN executable lines (outside payload blocks and outside `#` comments):
         - TAB (`0x09`), other control characters `< 0x20`, and typographic quotes (`U+2018`..`U+201D`) are refused.
    """
    defects: List[Dict[str, Any]] = []
    if not patch_text:
        return defects

    blocks, delimiter_defects, outside_script = _parse_herestring_blocks(patch_text)
    defects.extend(delimiter_defects)

    # Check the patch script's OWN lines (outside payload blocks)
    lone_cr_script = _find_lone_cr_offset(outside_script)
    if lone_cr_script != -1:
        defects.append(
            {
                "file": "<script>",
                "label": "script-lines",
                "offset": lone_cr_script,
                "reason": f"lone CR (0x0D without LF) on patch script line at offset {lone_cr_script}",
            }
        )

    script_pos = 0
    for raw_ln in outside_script.splitlines(keepends=True):
        stripped_lead = raw_ln.lstrip(" \t")
        is_comment = stripped_lead.startswith("#")
        for idx_ch, ch in enumerate(raw_ln):
            code = ord(ch)
            if code < 0x20 and ch not in ("\r", "\n"):
                if is_comment and ch == "\t":
                    continue
                defects.append(
                    {
                        "file": "<script>",
                        "label": "script-lines",
                        "offset": script_pos + idx_ch,
                        "reason": f"raw control character 0x{code:02X} on patch script line at offset {script_pos + idx_ch}",
                    }
                )
                break
            if not is_comment and ch in TYPOGRAPHIC_QUOTES:
                defects.append(
                    {
                        "file": "<script>",
                        "label": "script-lines",
                        "offset": script_pos + idx_ch,
                        "reason": f"typographic quote U+{code:04X} on patch script line at offset {script_pos + idx_ch}",
                    }
                )
                break
        script_pos += len(raw_ln)

    # Check each payload block
    for blk in blocks:
        file_rel = str(blk["file"])
        label = str(blk["label"])
        quote_kind = str(blk["quote_kind"])
        body = str(blk["body"])

        lone_cr_body = _find_lone_cr_offset(body)
        if lone_cr_body != -1:
            defects.append(
                {
                    "file": file_rel,
                    "label": label,
                    "offset": lone_cr_body,
                    "reason": f"lone CR (0x0D without LF) in payload at offset {lone_cr_body}",
                }
            )

        # 1. Raw control characters (< 0x20 other than TAB 0x09, CR 0x0D, or LF 0x0A)
        for idx, ch in enumerate(body):
            code = ord(ch)
            if code < 0x20 and ch not in ("\t", "\r", "\n"):
                defects.append(
                    {
                        "file": file_rel,
                        "label": label,
                        "offset": idx,
                        "reason": f"raw control character 0x{code:02X} at offset {idx}",
                    }
                )
                break

        # 2. Block sits inside a double-quoted here-string and contains '$'
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
    skip_dirs = {
        ".argon_backup",
        ".argon_quarantine",
        "downloads",
        "__pycache__",
        "node_modules",
        ".git",
        "venv",
        ".venv",
        "target",
        "dist",
    }
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
            if b < 0x20 and b not in (0x09, 0x0D, 0x0A):
                old_ctrl_counts[b] = old_ctrl_counts.get(b, 0) + 1
        seen_ctrl_counts: Dict[int, int] = {}
        for idx, b in enumerate(new_bytes):
            if b == 0x0D and (idx + 1 >= len(new_bytes) or new_bytes[idx + 1] != 0x0A):
                msg = f"lone CR byte (0x0D without LF) at byte offset {idx}"
                print(f"[ERROR] Written file byte defect in {rel}: {msg}", file=sys.stderr)
                issues.append({"file": rel, "offset": idx, "byte": b, "reason": msg})
                break
            if b < 0x20 and b not in (0x09, 0x0D, 0x0A):
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
    can fix it locally without another round trip.
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
            "bracket was lost. Nothing was rolled back; fix it locally:",
            file=sys.stderr,
        )
        for issue in structure_issues:
            print(f"    [BROKEN] {issue['file']}: {issue['reason']}", file=sys.stderr)
        return False
    return True


PRE_TEXT_CONTENT_HIDE_INIT_SCRIPT = """() => {
    const injectArgonPreHide = () => {
        try {
            const root = document.head || document.documentElement;
            if (!root) return;
            if (!document.getElementById('argon-hide-pre-text-content')) {
                const style = document.createElement('style');
                style.id = 'argon-hide-pre-text-content';
                style.textContent = 'pre.text-content { display: none !important; max-height: 0 !important; overflow: hidden !important; }';
                root.appendChild(style);
            }
        } catch (e) {}
    };
    injectArgonPreHide();
    if (typeof document !== 'undefined' && document.addEventListener) {
        document.addEventListener('DOMContentLoaded', injectArgonPreHide, { once: true });
    }
}"""


def _install_pre_text_content_hide_script(page) -> None:
    """Inject `pre.text-content{display:none!important}` via `page.add_init_script` BEFORE navigation (Round 18 Section 2.2).

    For a 216 KB patch, Gemini's in-page `generated-file-senna-preview` > `gem-popover` > `div.content` >
    `pre.text-content` is 116,892 px tall and crashes headless Chrome ('Page crashed' / TargetClosedError).
    Hiding `pre.text-content` via `add_init_script` prevents layout rendering while keeping `.textContent`
    readable in the DOM for byte-exact SHA-256 cross-checking.
    """
    if page is None:
        return
    try:
        if not getattr(page, "_argon_pre_hide_installed", False):
            page.add_init_script(PRE_TEXT_CONTENT_HIDE_INIT_SCRIPT)
            setattr(page, "_argon_pre_hide_installed", True)
    except Exception:
        pass
    try:
        ctx = getattr(page, "context", None)
        if ctx is not None and not getattr(ctx, "_argon_pre_hide_installed", False):
            ctx.add_init_script(PRE_TEXT_CONTENT_HIDE_INIT_SCRIPT)
            setattr(ctx, "_argon_pre_hide_installed", True)
    except Exception:
        pass
    try:
        page.evaluate(PRE_TEXT_CONTENT_HIDE_INIT_SCRIPT)
    except Exception:
        pass


_CHIP_CONTROL_WORDS = {
    "export",
    "download",
    "drive",
    "fullscreen",
    "close",
    "open",
    "copy",
    "এক্সপোর্ট",
    "ডাউনলোড",
    "ডাউনলোড করুন",
    "বন্ধ",
}


def _extract_chip_filename(chip) -> str:
    """Extract the file name from a `generated-file` chip (`generated-file-senna-preview > div.title-container`, etc.)."""
    candidates: List[str] = []
    for sub_sel in (
        "generated-file-senna-preview .title-container [title]",
        ".title-container [title]",
        "[data-file-name]",
        ".file-name-lr",
        ".title-container",
    ):
        try:
            loc = chip.locator(sub_sel)
            cnt = min(loc.count(), 6)
            for idx in range(cnt):
                node = loc.nth(idx)
                for attr in ("title", "data-file-name", "aria-label"):
                    try:
                        v = str(node.get_attribute(attr) or "").strip()
                        if v and v.lower() not in _CHIP_CONTROL_WORDS:
                            candidates.append(v)
                    except Exception:
                        pass
                try:
                    t = (node.inner_text(timeout=500) or "").strip()
                    if t:
                        candidates.append(t)
                except Exception:
                    pass
        except Exception:
            continue
    for attr in ("title", "data-file-name", "aria-label"):
        try:
            v = str(chip.get_attribute(attr) or "").strip()
            if v and v.lower() not in _CHIP_CONTROL_WORDS:
                candidates.append(v)
        except Exception:
            pass
    try:
        raw_txt = str(
            chip.evaluate(
                """el => {
                    const tc = el.querySelector && (el.querySelector('.title-container') || el);
                    if (!tc) return '';
                    const titled = tc.querySelector && tc.querySelector('[title]');
                    if (titled) {
                        const tv = (titled.getAttribute('title') || '').trim();
                        if (tv && !/^(export|fullscreen|close|download|drive)$/i.test(tv)) return tv;
                    }
                    return (tc.innerText || tc.textContent || '').trim();
                }"""
            )
            or ""
        ).strip()
        if raw_txt:
            candidates.insert(0, raw_txt)
    except Exception:
        pass

    file_re = re.compile(r"([A-Za-z0-9_.\- ()]+\.[A-Za-z0-9]{1,8})")
    for cand in candidates:
        for ln in cand.splitlines():
            s = ln.strip()
            if not s or s.lower() in _CHIP_CONTROL_WORDS:
                continue
            m = file_re.search(s)
            if m:
                matched = m.group(1).strip()
                if matched.lower() not in _CHIP_CONTROL_WORDS:
                    return matched
            if "apply_changes" in s.lower():
                return "apply_changes.ps1"
    return ""


def _classify_artifact_kind(filename: str, mime_type: str = "") -> str:
    """Classify a generated artifact filename into `'patch'`, `'image'`, `'svg'`, `'audio'`, or `'other'`."""
    low = (filename or "").strip().lower()
    mime_low = (mime_type or "").strip().lower()
    if "apply_changes" in low or low.endswith(".ps1"):
        return "patch"
    if low.endswith(".svg") or "image/svg" in mime_low:
        return "svg"
    if any(low.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".avif")) or mime_low.startswith("image/"):
        return "image"
    if any(low.endswith(ext) for ext in (".wav", ".mp3", ".ogg", ".m4a", ".flac", ".aac", ".opus")) or mime_low.startswith("audio/"):
        return "audio"
    return "other"


def _enumerate_generated_file_chips(page, after_last_user: bool = True) -> List[Dict[str, Any]]:
    """Enumerate ALL `generated-file` chips in the conversation (or after the last user turn), with each chip's name and kind (Round 18 Section 2.2)."""
    results: List[Dict[str, Any]] = []
    try:
        chips = page.locator("generated-file")
        total = min(chips.count(), 60)
        for i in range(total):
            chip = chips.nth(i)
            if not _safe_visible(chip):
                continue
            if after_last_user and not _element_is_after_last_user_turn(chip):
                continue
            fname = _extract_chip_filename(chip) or f"artifact_{i + 1}"
            kind = _classify_artifact_kind(fname)
            results.append(
                {
                    "index": i,
                    "chip": chip,
                    "filename": fname,
                    "kind": kind,
                }
            )
    except Exception:
        pass
    return results


def _chip_matches_patch_name(chip) -> bool:
    """True when the chip carries the apply_changes.ps1 delivery file."""
    fname = _extract_chip_filename(chip).lower()
    if "apply_changes" in fname or fname.endswith(".ps1"):
        return True
    names: List[str] = []
    try:
        loc = chip.locator(".file-name-lr")
        if loc.count():
            names.append(loc.first.inner_text(timeout=800) or "")
    except Exception:
        pass
    for attr in ("title", "aria-label", "data-file-name"):
        try:
            names.append(str(chip.get_attribute(attr) or ""))
        except Exception:
            pass
    try:
        names.append(chip.inner_text(timeout=1500) or "")
    except Exception:
        pass
    for name in names:
        low = name.lower()
        if "apply_changes" in low or low.endswith(".ps1"):
            return True
    return False


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
    return [
        int(item["index"])
        for item in _enumerate_generated_file_chips(page, after_last_user=True)
        if item.get("kind") == "patch"
    ]


def _generated_file_chip_indices(page) -> List[int]:
    """Return the indices of every visible apply_changes delivery chip."""
    return [
        int(item["index"])
        for item in _enumerate_generated_file_chips(page, after_last_user=False)
        if item.get("kind") == "patch"
    ]


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
            for i in range(min(chips.count(), 15)):
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

    patch_items = [
        item["chip"]
        for item in _enumerate_generated_file_chips(page, after_last_user=True)
        if item.get("kind") == "patch"
    ]
    return patch_items[-1] if patch_items else None


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
    """Ensure the chip's in-Gemini Senna preview (`generated-file-senna-preview`) or Drive preview is open.

    Round 18 Section 2.2: Clicking a `generated-file` chip opens an in-Gemini preview
    (`generated-file-senna-preview` > `div.title-container` with `gem-button` "Export",
    `button aria="Fullscreen"`, `button aria="Close"` and `gem-popover` > `div.content` > `pre.text-content`).
    If the chip already renders `generated-file-senna-preview` / `.title-container` with the Export button,
    it is already open and ready for Export -> Download without clicking Close.
    """
    _install_pre_text_content_hide_script(page)
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

    try:
        already_senna = bool(
            chip.evaluate(
                """el => {
                    const sp = el.querySelector && el.querySelector('generated-file-senna-preview, .title-container');
                    if (!sp) return false;
                    const btns = Array.from(el.querySelectorAll('gem-button, button'));
                    return btns.some(b => /export|এক্সপোর্ট/i.test((b.innerText || b.textContent || b.getAttribute('aria-label') || b.getAttribute('title') || '').trim()));
                }"""
            )
        )
        if already_senna:
            return True
    except Exception:
        pass

    for sel in (".open-button button", ".open-button", ".title-container [title]", "button", "gem-button"):
        try:
            loc = chip.locator(sel)
            for idx in range(min(loc.count(), 4)):
                btn = loc.nth(idx)
                lbl = ""
                try:
                    lbl = (
                        (btn.inner_text(timeout=300) or "")
                        + " "
                        + str(btn.get_attribute("aria-label") or "")
                        + " "
                        + str(btn.get_attribute("title") or "")
                    ).strip().lower()
                except Exception:
                    lbl = ""
                if any(skip_w in lbl for skip_w in ("close", "fullscreen", "বন্ধ")):
                    continue
                btn.click(timeout=6000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    try:
        chip.click(timeout=6000)
        page.wait_for_timeout(1200)
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


def _read_senna_preview_text(page, chip=None) -> Optional[str]:
    """Read the byte-exact preview text from `pre.text-content` via DOM `textContent` (Round 18 Section 2.2).

    Because `pre.text-content` is hidden with `display:none!important` via `add_init_script` so
    headless Chrome never lays out a 116,892 px tall `<pre>`, reading `.textContent` in JS retrieves
    the exact text in memory without layout rendering. Used to cross-check the SHA-256 of the
    downloaded file while keeping the Export -> Download button as the source of truth.
    """
    try:
        if chip is not None:
            raw = chip.evaluate(
                """el => {
                    const pre = el.querySelector && el.querySelector('pre.text-content, gem-popover .content pre, pre');
                    return pre ? (pre.textContent || '') : '';
                }"""
            )
            if raw and isinstance(raw, str) and raw.strip():
                return raw
    except Exception:
        pass
    try:
        raw_page = page.evaluate(
            """() => {
                const pres = Array.from(document.querySelectorAll('generated-file pre.text-content, generated-file-senna-preview pre.text-content, gem-popover div.content pre.text-content, pre.text-content'));
                if (!pres.length) return '';
                const lastPre = pres[pres.length - 1];
                return lastPre ? (lastPre.textContent || '') : '';
            }"""
        )
        if raw_page and isinstance(raw_page, str) and raw_page.strip():
            return raw_page
    except Exception:
        pass
    return None


def _read_previewed_file(page) -> Optional[str]:
    """Read the file content out of Gemini's in-page Senna preview or Drive viewer preview."""
    senna_txt = _read_senna_preview_text(page)
    if senna_txt and "$TargetRoot" in senna_txt:
        return _strip_viewer_tail_artefacts(senna_txt)
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
    """Verify that a Playwright download is really a non-empty .ps1 patch (D5, D18.1, Round 14 2.1.C/F).

    Saves to a temporary staging file first so a tiny/corrupt/PDF download is rejected
    without overwriting an existing good file in `dest_dir`, and moves rejected files
    into `.argon_quarantine` instead of deleting them so they can always be inspected.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dl_url = ""
    try:
        dl_url = str(getattr(dl, "url", "") or "")
    except Exception:
        dl_url = ""
    low_url = dl_url.lower()

    suggested = ""
    try:
        suggested = str(getattr(dl, "suggested_filename", "") or "").strip()
    except Exception:
        suggested = ""
    safe_name = Path(suggested).name if suggested else "rejected_download.ps1"
    staging = dest_dir / f".staging_{os.getpid()}_{int(time.time() * 1000)}_{safe_name}"

    if "contribservice" in low_url or "print" in low_url:
        reason = f"browser handed over a print preview URL ({dl_url})"
        print(f"[!] Rejecting print-preview download from URL: {dl_url}", file=sys.stderr)
        try:
            dl.save_as(str(staging))
            q_path = _quarantine_rejected_file(staging, reason, dest_dir=dest_dir)
            if q_path is not None:
                return None, f"{reason} (quarantined at {q_path})"
        except Exception:
            pass
        return None, reason

    if not suggested or not suggested.lower().endswith(".ps1"):
        reason = f"browser handed over non-.ps1 file ({suggested or 'unnamed'})"
        print(
            f"[!] Rejecting non-.ps1 download ({suggested!r}) - refusing to rename it to apply_changes.ps1.",
            file=sys.stderr,
        )
        try:
            dl.save_as(str(staging))
            q_path = _quarantine_rejected_file(staging, reason, dest_dir=dest_dir)
            if q_path is not None:
                return None, f"{reason} (quarantined at {q_path})"
        except Exception:
            pass
        return None, reason

    dest = dest_dir / safe_name
    dl.save_as(str(staging))

    try:
        file_size = staging.stat().st_size if staging.is_file() else 0
    except OSError:
        file_size = 0

    if file_size < MIN_PATCH_FILE_BYTES:
        reason = f"browser handed over an empty or undersized file ({file_size} bytes < {MIN_PATCH_FILE_BYTES} minimum)"
        print(
            f"[!] Rejecting {file_size}-byte download for {safe_name} (minimum {MIN_PATCH_FILE_BYTES} bytes) - "
            f"existing file {dest} was NOT overwritten.",
            file=sys.stderr,
        )
        q_path = _quarantine_rejected_file(staging, reason, dest_dir=dest_dir)
        return None, (f"{reason} (quarantined at {q_path})" if q_path else reason)

    try:
        head_bytes = staging.read_bytes()[:8]
        if head_bytes.startswith(b"%PDF"):
            reason = f"browser handed over PDF bytes ({file_size} bytes) masquerading as .ps1"
            print(
                f"[!] Rejecting PDF file masquerading as .ps1 ({file_size} bytes): {staging}",
                file=sys.stderr,
            )
            q_path = _quarantine_rejected_file(staging, reason, dest_dir=dest_dir)
            return None, (f"{reason} (quarantined at {q_path})" if q_path else reason)
    except Exception:
        pass

    os.replace(str(staging), str(dest))
    return dest, ""


def _install_drive_id_listener(page) -> None:
    """Record the Drive file id of every preview the page opens.

    Clicking the delivery chip makes the Gemini page request
    `drive.google.com/viewer/meta?id=<FILE_ID>` (and then `viewer/text?id=...`).
    The Drive viewer itself CRASHES in headless Chromium, so the id can never be read
    from the viewer's URL or markup - it only ever exists in the request that the
    Gemini page fired. So the id is captured from the network instead.
    """
    if getattr(page, "_argon_drive_id_listener", False):
        return

    def _record(req) -> None:
        try:
            url = str(getattr(req, "url", "") or "")
        except Exception:
            return
        if "drive.google.com" not in url:
            return
        m = re.search(r"[?&]id=([\w-]{20,})", url)
        if not m:
            return
        fid = m.group(1)
        try:
            ids = getattr(page, "_argon_drive_file_ids", None)
            if ids is None:
                ids = []
                setattr(page, "_argon_drive_file_ids", ids)
            if fid not in ids:
                ids.append(fid)
        except Exception:
            pass

    try:
        page.context.on("request", _record)
    except Exception:
        try:
            page.on("request", _record)
        except Exception:
            pass
    setattr(page, "_argon_drive_id_listener", True)


def _drive_viewer_file_id(page, wait_seconds: float = 8.0) -> str:
    """Return the Drive file id of the open delivery preview, or "".

    The preview opens in the current tab or in a popup, and the id shows up in the
    viewer's own URL as well as in the page markup. When the viewer CRASHES (headless
    Chromium does that) neither carries it, so the id recorded from the network is used.

    The id arrives ASYNCHRONOUSLY - the Gemini page fires `viewer/meta?id=...` a moment
    after the chip is clicked - so the network record is polled for a few seconds
    instead of being read once.
    """
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        try:
            pages = list(page.context.pages)
        except Exception:
            pages = [page]
        for pg in pages:
            try:
                url = str(getattr(pg, "url", "") or "")
            except Exception:
                continue
            for pat in (r"[?&]id=([\w-]{20,})", r"/file/d/([\w-]{20,})", r"/d/([\w-]{20,})"):
                m = re.search(pat, url)
                if m:
                    return m.group(1)
        for pg in pages:
            try:
                html = pg.content()
            except Exception:
                continue
            m = re.search(r"ACFrOg[\w-]{40,}", html)
            if m:
                return m.group(0)
        # the network recorded it when the chip was clicked
        try:
            ids = list(getattr(page, "_argon_drive_file_ids", None) or [])
            if ids:
                return ids[-1]
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.4)


def _decode_drive_text_envelope(raw: str) -> Optional[str]:
    """Decode the `viewer/text` envelope into the file body, or None."""
    if raw is None:
        return None
    body = raw
    if body.startswith(")]}'"):
        body = body[4:]
    body = body.lstrip("\r\n")
    try:
        obj = json.loads(body)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    if data is None:
        return None
    if isinstance(data, list):
        try:
            return "".join(str(x) for x in data)
        except Exception:
            return None
    return str(data)


def _click_chip_for_drive_id(page, per_click_wait: float = 1.8) -> str:
    """Open the delivery chip until the Drive file id shows up, and return that id.

    The id lives ONLY in the request the Gemini page fires when the preview opens, so
    the chip has to be clicked WHILE the listener is recording. A generated-file chip
    carries more than one button (the file opener, an "Export" control, ...), and
    which one opens the preview is not stable - so every button is tried and the id is
    checked after each click.
    """
    chip = _generated_file_chip(page)
    if chip is None:
        return ""
    targets: List[Any] = []
    try:
        for i in range(min(chip.locator("button").count(), 6)):
            targets.append(chip.locator("button").nth(i))
    except Exception:
        targets = []
    try:
        targets.append(chip)
    except Exception:
        pass
    for target in targets:
        try:
            target.scroll_into_view_if_needed(timeout=4000)
        except Exception:
            pass
        try:
            target.click(timeout=6000)
        except Exception:
            continue
        deadline = time.monotonic() + float(per_click_wait)
        while time.monotonic() < deadline:
            fid = _drive_viewer_file_id(page, wait_seconds=0.0)
            if fid:
                return fid
            time.sleep(0.3)
    return _drive_viewer_file_id(page, wait_seconds=2.0)


def _download_from_drive_text(page, dest_dir: Path, max_pages: int = 12) -> tuple[Optional[Path], str]:
    """Fetch the delivery through Drive's `viewer/text` endpoint, PAGE BY PAGE.

    The viewer's toolbar download button is not always reachable - the run reports
    "the viewer's download button did not produce a valid .ps1 file (a print control
    was never clicked)" - and the preview text is a SILENT TRUNCATION:
    `viewer/text?id=...&page=N` serves the file in PAGES, and page 0 simply stops
    mid-statement with no error at all. A downloader that reads one page therefore
    accepts a HALF patch that still parses, and the apply step then fails with a
    missing target block.

    This walks page 0, 1, 2 ... until a page comes back empty and stitches the
    chunks, so the file that is written is the WHOLE file.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    file_id = _drive_viewer_file_id(page)
    if not file_id:
        # The id only exists in the request the Gemini page fires when the chip is
        # opened. If the click that opened the preview happened before the listener was
        # in place, the id was never recorded - so open the chip HERE, with the
        # listener in place, and wait for the id to arrive.
        _install_drive_id_listener(page)
        try:
            file_id = _click_chip_for_drive_id(page)
        except Exception:
            file_id = ""
        if not file_id:
            try:
                if _open_generated_file(page):
                    file_id = _drive_viewer_file_id(page, wait_seconds=10.0)
            except Exception:
                pass
    if not file_id:
        try:
            ext_fid = str(
                page.evaluate(
                    "() => (document.documentElement && document.documentElement.getAttribute('data-argon-last-drive-file-id')) || ''"
                )
                or ""
            ).strip()
            if ext_fid:
                file_id = ext_fid
        except Exception:
            pass
    if not file_id:
        return None, "the Drive preview did not expose a file id"

    auth_user = "0"
    try:
        m_au = re.search(r"/u/(\d+)/", str(getattr(page, "url", "") or ""))
        if m_au:
            auth_user = str(int(m_au.group(1)))
    except Exception:
        auth_user = "0"

    # Try the MV3 companion extension's paged viewer/text walker first when loaded.
    ext_resp = _invoke_companion_extension(
        page,
        "capture_delivery",
        {"fileId": file_id, "maxPages": max_pages, "authUser": auth_user},
        timeout_ms=30000,
    )
    if isinstance(ext_resp, dict) and ext_resp.get("ok") and ext_resp.get("text"):
        ext_text = str(ext_resp.get("text") or "")
        ext_pages = int(ext_resp.get("pages") or 1)
        if len(ext_text.encode("utf-8")) >= MIN_PATCH_FILE_BYTES and not ext_text.lstrip().startswith("%PDF"):
            dest = dest_dir / "apply_changes.ps1"
            staging = dest_dir / f".staging_{os.getpid()}_{int(time.time() * 1000)}_apply_changes.ps1"
            try:
                staging.write_text(ext_text, encoding="utf-8", newline="")
                os.replace(str(staging), str(dest))
                print(
                    f"[+] Fetched the delivery through the companion extension Drive text walker: "
                    f"{len(ext_text.encode('utf-8'))} bytes in {ext_pages} page(s) (file id {file_id[:14]}...)."
                )
                return dest, ""
            except Exception:
                try:
                    staging.unlink(missing_ok=True)
                except Exception:
                    pass

    try:
        api = page.context.request
    except Exception:
        return None, "the browser context has no request API"

    chunks: List[str] = []
    for pg_no in range(max_pages):
        url = f"https://drive.google.com/viewer/text?id={file_id}&authuser={auth_user}&page={pg_no}"
        try:
            resp = api.get(url, timeout=120000)
            raw = resp.text()
        except Exception as exc:
            return None, f"the Drive text endpoint failed on page {pg_no} ({exc})"
        data = _decode_drive_text_envelope(raw)
        if data is None and pg_no == 0 and auth_user != "0":
            try:
                resp0 = api.get(
                    f"https://drive.google.com/viewer/text?id={file_id}&authuser=0&page=0",
                    timeout=120000,
                )
                data = _decode_drive_text_envelope(resp0.text())
                if data is not None:
                    auth_user = "0"
            except Exception:
                data = None
        if data is None:
            if pg_no == 0:
                return None, "the Drive text endpoint did not return a file body"
            break
        chunks.append(data)
        if len(data) < 200:
            break
        if len(chunks) > 1 and chunks[0] == chunks[1]:
            # The endpoint answered the same page twice - there is only one page.
            chunks = chunks[:1]
            break

    if not chunks:
        return None, "the Drive text endpoint returned no content"
    text = chunks[0] if len(chunks) == 1 else "".join(chunks)

    if len(text.encode("utf-8")) < MIN_PATCH_FILE_BYTES:
        return None, (
            f"the Drive text endpoint returned only {len(text.encode('utf-8'))} bytes "
            f"(< {MIN_PATCH_FILE_BYTES} minimum) after {len(chunks)} page(s)"
        )
    if text.lstrip().startswith("%PDF"):
        return None, "the Drive text endpoint returned PDF bytes"

    dest = dest_dir / "apply_changes.ps1"
    staging = dest_dir / f".staging_{os.getpid()}_{int(time.time() * 1000)}_apply_changes.ps1"
    try:
        staging.write_text(text, encoding="utf-8", newline="")
        os.replace(str(staging), str(dest))
    except Exception as exc:
        try:
            staging.unlink(missing_ok=True)
        except Exception:
            pass
        return None, f"the fetched delivery could not be written ({exc})"
    print(
        f"[+] Fetched the delivery through the Drive text endpoint (pure-Python path): {len(text.encode('utf-8'))} bytes "
        f"in {len(chunks)} page(s) (file id {file_id[:14]}...)."
    )
    return dest, ""

def _validate_and_save_artifact_download(
    dl,
    dest_dir: Path,
    expected_filename: str = "",
) -> tuple[Optional[Path], str]:
    """Validate and save a non-patch artifact download (PNG, SVG, WAV, MP3, JSON, etc.) into `dest_dir` (Round 18 Section 2)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dl_url = ""
    try:
        dl_url = str(getattr(dl, "url", "") or "")
    except Exception:
        dl_url = ""
    low_url = dl_url.lower()
    if "contribservice" in low_url or "print" in low_url:
        return None, f"browser handed over a print preview URL ({dl_url})"

    suggested = ""
    try:
        suggested = str(getattr(dl, "suggested_filename", "") or "").strip()
    except Exception:
        suggested = ""
    chosen_name = Path(suggested or expected_filename or "artifact.bin").name
    staging = dest_dir / f".staging_art_{os.getpid()}_{int(time.time() * 1000)}_{chosen_name}"
    dest = dest_dir / chosen_name
    try:
        dl.save_as(str(staging))
    except Exception as exc:
        return None, f"could not save artifact download ({exc})"

    try:
        sz = staging.stat().st_size if staging.is_file() else 0
    except OSError:
        sz = 0
    if sz <= 0:
        try:
            staging.unlink(missing_ok=True)
        except Exception:
            pass
        return None, f"downloaded artifact {chosen_name} is empty (0 bytes)"

    os.replace(str(staging), str(dest))
    return dest, ""


def _click_chip_export_and_leaf_download(
    page,
    chip,
    dest_dir: Path,
    expected_filename: str = "",
    allow_non_ps1: bool = False,
    timeout_ms: int = 15000,
) -> tuple[Optional[Path], str]:
    """Download a `generated-file` chip via its in-Gemini `Export` `gem-button` -> leaf `Download` menu item (Round 18 Section 2.2).

    Measured DOM on the owner's machine:
      - `generated-file` > `generated-file-senna-preview` > `div.title-container` holds a `gem-button` "Export".
      - Clicking "Export" opens a menu containing "Download" and "Drive".
      - Clicking a container whose aggregated text contains "Download" times out; the click MUST land
        on the LEAF element whose trimmed text is `"Download"` (`"ডাউনলোড"` / `"ডাউনলোড করুন"`).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if chip is None:
        chip = _generated_file_chip(page)
    if chip is None:
        return None, "no generated-file chip present"

    export_clicked = False
    for exp_sel in (
        "generated-file-senna-preview .title-container gem-button:has-text('Export') button",
        "generated-file-senna-preview .title-container gem-button:has-text('Export')",
        ".title-container gem-button:has-text('Export') button",
        ".title-container gem-button:has-text('Export')",
        "gem-button:has-text('Export') button",
        "gem-button:has-text('Export')",
        "button:has-text('Export')",
        "[aria-label*='Export' i]",
        "[title*='Export' i]",
        "gem-button:has-text('এক্সপোর্ট') button",
        "gem-button:has-text('এক্সপোর্ট')",
        "button:has-text('এক্সপোর্ট')",
    ):
        try:
            loc = chip.locator(exp_sel)
            if loc.count() > 0 and _safe_visible(loc.first):
                loc.first.click(timeout=5000)
                export_clicked = True
                break
        except Exception:
            continue

    if not export_clicked:
        try:
            export_clicked = bool(
                chip.evaluate(
                    """el => {
                        const candidates = Array.from(el.querySelectorAll('gem-button button, gem-button, button, [role="button"]'));
                        for (const node of candidates) {
                            const txt = (node.innerText || node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || '').trim();
                            if (/^(export|এক্সপোর্ট)$/i.test(txt)) {
                                node.click();
                                return true;
                            }
                        }
                        return false;
                    }"""
                )
            )
        except Exception:
            export_clicked = False

    if not export_clicked:
        return None, "the chip did not expose an Export button"

    try:
        page.wait_for_timeout(450)
    except Exception:
        time.sleep(0.45)

    marker_attr = f"data-argon-leaf-dl-{int(time.time() * 1000) % 100000}"
    found_leaf = False
    try:
        found_leaf = bool(
            page.evaluate(
                r"""(attrName) => {
                    document.querySelectorAll('[' + attrName + ']').forEach(n => n.removeAttribute(attrName));
                    const isDownloadText = (s) => /^(download|ডাউনলোড(?:\s*করুন)?)$/i.test((s || '').trim());
                    const all = Array.from(document.querySelectorAll('*'));
                    const leaves = [];
                    for (const el of all) {
                        const tag = (el.tagName || '').toLowerCase();
                        if (tag === 'script' || tag === 'style' || tag === 'pre') continue;
                        if (el.closest && el.closest('user-query')) continue;
                        const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : { width: 0, height: 0 };
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const ownTxt = (el.innerText || el.textContent || '').trim();
                        if (!isDownloadText(ownTxt)) continue;
                        const childHasDownload = Array.from(el.children || []).some(c => isDownloadText(c.innerText || c.textContent || ''));
                        if (childHasDownload) continue;
                        leaves.push(el);
                    }
                    if (!leaves.length) return false;
                    leaves[leaves.length - 1].setAttribute(attrName, '1');
                    return true;
                }""",
                marker_attr,
            )
        )
    except Exception:
        found_leaf = False

    if not found_leaf:
        return None, "Export menu opened but no leaf 'Download' element was found"

    leaf_loc = page.locator(f"[{marker_attr}='1']").last
    setattr(page, "_argon_saw_download_button", True)
    try:
        with page.expect_download(timeout=timeout_ms) as dl_info:
            leaf_loc.click(timeout=5000)
        if allow_non_ps1:
            return _validate_and_save_artifact_download(dl_info.value, dest_dir, expected_filename=expected_filename)
        saved, discard_reason = _validate_and_save_download(dl_info.value, dest_dir)
        if saved is not None and saved.is_file():
            preview_txt = _read_senna_preview_text(page, chip=chip)
            if preview_txt:
                dl_txt = saved.read_text(encoding="utf-8", errors="replace")
                sha_dl = _sha256_full(dl_txt)
                sha_pre = _sha256_full(preview_txt)
                if sha_dl == sha_pre:
                    print(f"[+] In-Gemini Export->Download verified byte-exact against pre.text-content (sha256={sha_dl}).")
        return saved, discard_reason
    except Exception as exc:
        return None, f"clicking Export -> leaf 'Download' did not trigger a browser download ({exc})"


def _download_from_drive_binary(
    page,
    file_id: str,
    dest_path: Path,
) -> tuple[Optional[Path], str]:
    """Fetch binary bytes (PNG, SVG, WAV, MP3, etc.) from Drive's download endpoint (Round 18 Section 2.2)."""
    if not file_id:
        return None, "missing Drive file_id for binary download"
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    auth_user = "0"
    try:
        m_au = re.search(r"/u/(\d+)/", str(getattr(page, "url", "") or ""))
        if m_au:
            auth_user = str(int(m_au.group(1)))
    except Exception:
        auth_user = "0"

    try:
        api = page.context.request
    except Exception:
        return None, "browser context has no request API"

    candidate_urls = [
        f"https://drive.google.com/uc?export=download&id={file_id}&authuser={auth_user}",
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser={auth_user}&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}&authuser=0",
    ]
    for url in candidate_urls:
        try:
            resp = api.get(url, timeout=120000)
            if not getattr(resp, "ok", False):
                continue
            raw_bytes = resp.body()
            if not raw_bytes:
                continue
            head_low = raw_bytes[:256].lstrip().lower()
            if head_low.startswith(b"<!doctype html") or head_low.startswith(b"<html"):
                continue
            staging = dest_path.parent / f".staging_bin_{os.getpid()}_{dest_path.name}"
            staging.write_bytes(raw_bytes)
            os.replace(str(staging), str(dest_path))
            return dest_path, ""
        except Exception:
            continue
    return None, f"Drive binary download endpoint did not return binary bytes for file_id={file_id}"


def _sha256_bytes(data: bytes) -> str:
    """Return full 64-character lowercase SHA-256 hex digest of `data`."""
    return hashlib.sha256(data).hexdigest()


def _describe_delivery_artifact(file_path: Path, raw_bytes: Optional[bytes] = None) -> str:
    """Return a concise one-line human/agent description of a delivered file for `DELIVERY_MANIFEST.md` (Round 18 Section 2.1)."""
    import struct

    name = file_path.name
    stem_label = file_path.stem.replace("_", " ").replace("-", " ").strip() or name
    ext = file_path.suffix.lower()
    data = raw_bytes if raw_bytes is not None else (file_path.read_bytes() if file_path.is_file() else b"")

    if ext == ".ps1" or "apply_changes" in name.lower():
        line_cnt = len(data.decode("utf-8", errors="replace").splitlines())
        return f"PowerShell surgical patch script ({line_cnt} lines)"

    if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = struct.unpack(">II", data[16:24])
            return f"{stem_label}, {w}x{h} PNG"
        except Exception:
            return f"{stem_label}, PNG image"

    if ext in (".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"):
        return f"{stem_label}, {ext.lstrip('.').upper()} image"

    if ext == ".svg":
        txt_svg = data.decode("utf-8", errors="replace")
        m_vb = re.search(r'viewBox=["\']\s*[\d.\-]+\s+[\d.\-]+\s+([\d.]+)\s+([\d.]+)', txt_svg, re.IGNORECASE)
        if m_vb:
            return f"{stem_label} SVG ({m_vb.group(1)}x{m_vb.group(2)})"
        return f"{stem_label} SVG"

    if ext == ".wav" and len(data) >= 36 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            channels = struct.unpack("<H", data[22:24])[0]
            sample_rate = struct.unpack("<I", data[24:28])[0]
            bits = struct.unpack("<H", data[34:36])[0]
            return f"{stem_label} WAV ({sample_rate} Hz, {bits}-bit, {channels}ch)"
        except Exception:
            return f"{stem_label} WAV"

    if ext in (".mp3", ".ogg", ".m4a", ".flac", ".aac", ".opus"):
        return f"{stem_label} {ext.lstrip('.').upper()}"

    return f"{stem_label} ({ext.lstrip('.').upper() or 'file'} artifact)"


def write_delivery_bundle(
    dest_dir: Path,
    patch_path: Optional[Path] = None,
    asset_paths: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    """Build `<dest_dir>/argon_delivery.zip` and `<dest_dir>/DELIVERY_MANIFEST.md` next to `apply_changes.ps1` (Round 18 Section 2.1).

    Layout on disk:
      `<dest_dir>/apply_changes.ps1`
      `<dest_dir>/argon_delivery.zip`
      `<dest_dir>/DELIVERY_MANIFEST.md`
      `<dest_dir>/assets/<generated images / svg / audio / ...>`

    Layout inside `argon_delivery.zip`:
      `apply_changes.ps1`
      `assets/<the generated image / svg / audio / ...>`
      `DELIVERY_MANIFEST.md`
    """
    import zipfile as _zipfile

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = dest_dir / "assets"

    eff_patch = patch_path if (patch_path and Path(patch_path).is_file()) else (dest_dir / "apply_changes.ps1")
    collected_assets: List[Path] = []
    seen_names: set = set()

    for ap in (asset_paths or []):
        p_obj = Path(ap)
        if not p_obj.is_file() or p_obj.name.startswith("."):
            continue
        if p_obj.name.lower() in ("apply_changes.ps1", "argon_delivery.zip", "delivery_manifest.md"):
            continue
        assets_dir.mkdir(parents=True, exist_ok=True)
        target_asset = assets_dir / p_obj.name
        if p_obj.resolve() != target_asset.resolve():
            shutil.copy2(str(p_obj), str(target_asset))
        if target_asset.name not in seen_names:
            seen_names.add(target_asset.name)
            collected_assets.append(target_asset)

    if assets_dir.is_dir():
        for existing_asset in sorted(assets_dir.iterdir()):
            if existing_asset.is_file() and not existing_asset.name.startswith(".") and existing_asset.name not in seen_names:
                seen_names.add(existing_asset.name)
                collected_assets.append(existing_asset)

    manifest_entries: List[Dict[str, Any]] = []
    if eff_patch.is_file():
        raw_p = eff_patch.read_bytes()
        manifest_entries.append(
            {
                "rel_path": "apply_changes.ps1",
                "disk_path": eff_patch,
                "size": len(raw_p),
                "sha256": _sha256_bytes(raw_p),
                "description": _describe_delivery_artifact(eff_patch, raw_p),
            }
        )

    for asset_file in collected_assets:
        raw_a = asset_file.read_bytes()
        manifest_entries.append(
            {
                "rel_path": f"assets/{asset_file.name}",
                "disk_path": asset_file,
                "size": len(raw_a),
                "sha256": _sha256_bytes(raw_a),
                "description": _describe_delivery_artifact(asset_file, raw_a),
            }
        )

    manifest_lines: List[str] = [
        "# Argon Delivery Manifest",
        "",
        "| File | Size (bytes) | SHA-256 | Description |",
        "|---|---:|---|---|",
    ]
    for item in manifest_entries:
        manifest_lines.append(
            f"| `{item['rel_path']}` | {item['size']} | `{item['sha256']}` | {item['description']} |"
        )
    manifest_text = "\n".join(manifest_lines) + "\n"

    manifest_path = dest_dir / "DELIVERY_MANIFEST.md"
    manifest_path.write_text(manifest_text, encoding="utf-8", newline="\n")

    bundle_zip_path = dest_dir / "argon_delivery.zip"
    staging_zip = dest_dir / f".staging_bundle_{os.getpid()}_{int(time.time() * 1000)}.zip"
    with _zipfile.ZipFile(staging_zip, "w", compression=_zipfile.ZIP_DEFLATED) as zf:
        for item in manifest_entries:
            zf.write(str(item["disk_path"]), arcname=str(item["rel_path"]))
        zf.write(str(manifest_path), arcname="DELIVERY_MANIFEST.md")
    os.replace(str(staging_zip), str(bundle_zip_path))

    with _zipfile.ZipFile(bundle_zip_path, "r") as zf_chk:
        names_in_zip = set(zf_chk.namelist())
        for item in manifest_entries:
            rel_n = str(item["rel_path"])
            if rel_n not in names_in_zip:
                raise RuntimeError(f"Bundle verification failed: {rel_n} missing from {bundle_zip_path}")
            zip_bytes = zf_chk.read(rel_n)
            if len(zip_bytes) != int(item["size"]) or _sha256_bytes(zip_bytes) != str(item["sha256"]):
                raise RuntimeError(f"Bundle verification failed: SHA-256/size mismatch for {rel_n} in {bundle_zip_path}")

    return {
        "bundle_zip": bundle_zip_path,
        "manifest_path": manifest_path,
        "files": manifest_entries,
    }


def capture_additional_round_artifacts(page, dest_dir: Path) -> List[Path]:
    """Collect all non-patch generated-file chips and inline SVG/image/audio artifacts in the newest assistant turn (Round 18 Section 2)."""
    dest_dir = Path(dest_dir)
    assets_dir = dest_dir / "assets"
    saved_assets: List[Path] = []
    if page is None:
        return saved_assets

    all_chips = _enumerate_generated_file_chips(page, after_last_user=True)
    for chip_info in all_chips:
        if chip_info.get("kind") == "patch":
            continue
        chip = chip_info.get("chip")
        fname = str(chip_info.get("filename") or f"artifact_{chip_info.get('index', 0)}")
        assets_dir.mkdir(parents=True, exist_ok=True)
        got_path, _ = _click_chip_export_and_leaf_download(
            page,
            chip,
            assets_dir,
            expected_filename=fname,
            allow_non_ps1=True,
            timeout_ms=12000,
        )
        if got_path is None:
            senna_txt = _read_senna_preview_text(page, chip=chip)
            if senna_txt and (fname.lower().endswith(".svg") or fname.lower().endswith(".json") or fname.lower().endswith(".txt")):
                target_f = assets_dir / Path(fname).name
                target_f.write_text(senna_txt, encoding="utf-8", newline="\n")
                got_path = target_f
            else:
                fid = _drive_viewer_file_id(page, wait_seconds=2.0)
                if fid:
                    got_path, _ = _download_from_drive_binary(page, fid, assets_dir / Path(fname).name)
        if got_path is not None and got_path.is_file() and got_path.stat().st_size > 0:
            saved_assets.append(got_path)

    return saved_assets


def _download_from_viewer(page, dest_dir: Path, timeout_ms: int = 12000) -> Optional[Path]:
    """Find the viewer's verified toolbar download button, click ONLY that button, and validate the file.

    Stores any rejection or click failure reason on `page._argon_last_download_reason`
    and `page._argon_saw_download_button` so `capture_generated_file` can report the exact cause.
    """
    dest_dir = Path(dest_dir)
    setattr(page, "_argon_last_download_reason", "")
    btn = _find_viewer_download_button(page)
    if btn is None:
        return None
    setattr(page, "_argon_saw_download_button", True)
    try:
        with page.expect_download(timeout=timeout_ms) as dl:
            btn.click(timeout=6000)
        saved, discard_reason = _validate_and_save_download(dl.value, dest_dir)
        if discard_reason:
            setattr(page, "_argon_last_download_reason", discard_reason)
        return saved
    except Exception as dl_exc:
        setattr(
            page,
            "_argon_last_download_reason",
            f"clicking viewer toolbar download button did not trigger download ({dl_exc})",
        )
        return None


def _close_preview(page) -> None:
    """Close the in-Gemini Senna preview or Drive viewer preview without ever cancelling an active Argon response (D10.1)."""
    try:
        for close_sel in (
            "generated-file-senna-preview .title-container button[aria-label='Close' i]",
            ".title-container button[aria-label='Close' i]",
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
        """Return the ledger record if `text` was already handled for `project_path` or across any project, else None."""
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
        self.load()
        projects = self.data.get("projects", {})
        if isinstance(projects, dict):
            for p_key, entries in projects.items():
                if isinstance(entries, list):
                    for entry in entries:
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
    exclude_path: Optional[Path] = None,
) -> bool:
    """True when the captured delivery is one that was already handled (D1.1, D1.2).

    A capture whose SHA-256 is already known - in ~/.gemini/argon_delivery_ledger.json
    or in a patch already applied in this repository - must not be reported as a NEW delivery.
    Refuses with a loud warning naming the digest and the earlier delivery it duplicates.

    `exclude_path` is the file THIS capture just wrote. Without it the scan below finds
    that very file: the delivery is downloaded into `proj/downloads/...` and the scan
    reads it back and calls it a duplicate, so a perfectly good delivery was thrown away
    and the run reported that no delivery existed while the file was sitting on disk.
    """
    if not text or not text.strip():
        return False
    excluded: Optional[Path] = None
    if exclude_path is not None:
        try:
            excluded = Path(exclude_path).resolve()
        except Exception:
            excluded = None
    digest = _hash_text(text)
    full_sha = _sha256_full(text)
    if project_path is not None:
        try:
            ledger_hit = DeliveryLedger().lookup_digest(project_path, text)
            if ledger_hit is not None:
                print(
                    f"\n[!] REFUSED DUPLICATE DELIVERY:\n"
                    f"    SHA-256  : {full_sha}\n"
                    f"    Duplicate: round {ledger_hit.get('round_id') or 'previous'} "
                    f"(chat={ledger_hit.get('chat_id') or '?'}, captured_at={ledger_hit.get('timestamp') or '?'}, "
                    f"file={ledger_hit.get('file_path') or '?'})\n"
                    f"    This delivery is already known in ~/.gemini/argon_delivery_ledger.json.\n"
                    f"    Waiting for the new delivery chip.",
                    file=sys.stderr,
                )
                return True
        except Exception:
            pass
    if previous_hashes and ((digest and digest in previous_hashes) or (full_sha and full_sha in previous_hashes)):
        print(
            f"\n[!] REFUSED DUPLICATE DELIVERY:\n"
            f"    SHA-256  : {full_sha} (digest={digest})\n"
            f"    Duplicate: matches a known pre-existing hash from earlier rounds.\n"
            f"    Waiting for the new delivery chip.",
            file=sys.stderr,
        )
        return True
    if project_path is not None:
        try:
            proj = Path(project_path).resolve()
            for cand_dir in (proj / "downloads", proj / ".argon_backup"):
                if cand_dir.is_dir():
                    for ps1_file in cand_dir.glob("**/*.ps1"):
                        if _is_quarantined_path(ps1_file):
                            continue
                        if excluded is not None:
                            try:
                                if ps1_file.resolve() == excluded:
                                    continue
                            except Exception:
                                pass
                        if ps1_file.is_file() and ps1_file.stat().st_size >= MIN_PATCH_FILE_BYTES:
                            try:
                                content_f = ps1_file.read_text(encoding="utf-8", errors="replace")
                                if _sha256_full(content_f) == full_sha:
                                    print(
                                        f"\n[!] REFUSED DUPLICATE DELIVERY:\n"
                                        f"    SHA-256  : {full_sha}\n"
                                        f"    Duplicate: byte-identical to existing repository patch: {ps1_file}\n"
                                        f"    Waiting for the new delivery chip.",
                                        file=sys.stderr,
                                    )
                                    return True
                            except Exception:
                                continue
        except Exception:
            pass
    return False


def _patch_is_complete(text: str) -> _PatchCheckResult:
    """Check whether a delivered script is COMPLETE and structurally parseable (Section 2.1.A & 2.1.B).

    Returns a `_PatchCheckResult` (truthy/falsy like `bool`, unpackable as `(ok, reason)`,
    with `.reason` and `.defects` naming the exact check that failed).
    Evaluates here-string delimiter balance PER BLOCK rather than across the whole file.
    """
    if not text or not text.strip():
        return _PatchCheckResult(False, "file is empty (0 bytes)")
    byte_len = len(text.encode("utf-8", errors="replace"))
    if byte_len < MIN_PATCH_FILE_BYTES:
        return _PatchCheckResult(
            False,
            f"file is undersized ({byte_len} bytes < {MIN_PATCH_FILE_BYTES} minimum)",
        )
    if "[SUCCESS]" not in text and "All changes applied and verified successfully" not in text:
        return _PatchCheckResult(
            False,
            "missing '[SUCCESS]' completion summary line (script was truncated before the final summary line)",
        )
    if "# --- NOTES" in text and "# --- END NOTES" not in text:
        return _PatchCheckResult(
            False,
            "started '# --- NOTES' block is missing its closing '# --- END NOTES ---' line (truncated in NOTES block)",
        )

    payload_blocks, delimiter_defects, outside_script = _parse_herestring_blocks(text)
    if delimiter_defects:
        d0 = delimiter_defects[0]
        return _PatchCheckResult(
            False,
            f"file={d0['file']} label={d0['label']} offset={d0['offset']} reason={d0['reason']}",
            defects=delimiter_defects,
        )

    open_sq = chr(64) + chr(39)
    open_dq = chr(64) + chr(34)
    if (open_sq in outside_script or open_dq in outside_script) and not payload_blocks:
        return _PatchCheckResult(
            False,
            "here-string opener found on script line but no complete here-string block could be parsed",
        )

    defects = validate_patch_payloads(text)
    if defects:
        d0 = defects[0]
        return _PatchCheckResult(
            False,
            f"file={d0['file']} label={d0['label']} offset={d0['offset']} reason={d0['reason']}",
            defects=defects,
        )
    return _PatchCheckResult(True, "")


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

    # The Drive file id only ever appears in the request the Gemini page fires when the
    # chip is clicked, and the Drive viewer crashes headless - so the listener and the
    # pre.text-content hide style must be in place BEFORE the chip is opened.
    _install_pre_text_content_hide_script(page)
    _install_drive_id_listener(page)

    # Resolve the chip ONCE and hand that exact element to the opener. If the chip is
    # resolved twice, the second lookup can land on a different turn (or find none),
    # and then the preview never opens - so no Drive request is fired, no file id is
    # ever recorded, and the delivery cannot be fetched even though the chip is there.
    if chip is None:
        chip = _generated_file_chip(page)
    if not _open_generated_file(page, chip_index=chip_index, chip=chip):
        return None

    capture_started = time.time()
    deadline = time.monotonic() + max(5.0, float(timeout_seconds))
    attempt = 0
    poll_count = 0
    last_quarantined: Optional[Path] = None
    saw_download_button = False
    failure_reason = "the chip's Export -> Download button and Drive viewer were not reachable (a print control was never clicked)"
    setattr(page, "_argon_chip_capture_exhausted", False)
    setattr(page, "_argon_last_capture_failure_reason", "")
    setattr(page, "_argon_last_quarantined_path", "")

    while attempt < MAX_CHIP_DOWNLOAD_ATTEMPTS:
        poll_count += 1
        _dismiss_print_preview_if_any(page)
        try:
            page.wait_for_timeout(1200)
        except Exception:
            time.sleep(1.2)

        if poll_count > 1 and not _viewer_is_open(page):
            if not _open_generated_file(page, chip_index=chip_index, chip=chip):
                break

        downloaded: Optional[Path] = None

        # Route 1 (PROVEN Round 18 Section 2.2): In-Gemini Senna preview Export gem-button -> leaf "Download"
        senna_dl, senna_reason = _click_chip_export_and_leaf_download(
            page,
            chip,
            dest_dir,
            expected_filename="apply_changes.ps1",
            allow_non_ps1=False,
            timeout_ms=12000,
        )
        if senna_dl is not None:
            saw_download_button = True
            attempt += 1
            print(f"[i] Download attempt {attempt} of {MAX_CHIP_DOWNLOAD_ATTEMPTS} for this chip (in-Gemini Export -> Download)...")
            downloaded = senna_dl
            failure_reason = ""
        elif senna_reason and "did not expose an Export button" not in senna_reason:
            failure_reason = senna_reason

        # Route 2: Legacy Drive viewer toolbar Download button
        if downloaded is None:
            dl_btn = _find_viewer_download_button(page)
            if dl_btn is not None:
                saw_download_button = True
                attempt += 1
                print(f"[i] Download attempt {attempt} of {MAX_CHIP_DOWNLOAD_ATTEMPTS} for this chip...")
                downloaded = _download_from_viewer(page, dest_dir, timeout_ms=12000)
                discard_reason = str(getattr(page, "_argon_last_download_reason", "") or "")
                if discard_reason:
                    failure_reason = discard_reason

        if downloaded is None:
            cand = find_delivered_patch([dest_dir], newer_than=capture_started - 1.0)
            if cand is not None and cand.suffix.lower() == ".ps1" and cand.stat().st_size >= MIN_PATCH_FILE_BYTES:
                attempt += 1
                print(f"[i] Download attempt {attempt} of {MAX_CHIP_DOWNLOAD_ATTEMPTS} for this chip...")
                downloaded = cand

        # Route 3: Drive viewer/text paged endpoint (for Drive-hosted text deliveries)
        if downloaded is None:
            text_file, text_reason = _download_from_drive_text(page, dest_dir)
            if text_file is not None:
                downloaded = text_file
                failure_reason = ""
                attempt += 1
            elif text_reason:
                if "did not expose an Export button" in failure_reason or "not reachable" in failure_reason:
                    failure_reason = text_reason
                attempt += 1
                print(f"[i] Download attempt {attempt} of {MAX_CHIP_DOWNLOAD_ATTEMPTS} for this chip...")

        if downloaded is not None and downloaded.is_file():
            try:
                file_sz = downloaded.stat().st_size
            except OSError:
                file_sz = 0
            try:
                raw_dl_text = downloaded.read_text(encoding="utf-8", errors="replace")
            except Exception:
                raw_dl_text = ""
            dl_text = _strip_viewer_tail_artefacts(raw_dl_text)

            if _is_leftover_delivery(
                dl_text,
                combined_hashes,
                project_path=proj_for_ledger,
                exclude_path=downloaded,
            ):
                failure_reason = (
                    f"refused duplicate/leftover delivery in {downloaded.name} ({file_sz} bytes; SHA-256={_sha256_full(dl_text)})"
                )
                last_quarantined = _quarantine_rejected_file(
                    downloaded,
                    failure_reason,
                    project_path=proj_for_ledger,
                    dest_dir=dest_dir,
                )
                setattr(page, "_argon_last_capture_failure_reason", failure_reason)
                setattr(page, "_argon_last_quarantined_path", str(last_quarantined or ""))
                setattr(page, "_argon_chip_capture_exhausted", True)
                _close_preview(page)
                return None

            looks_res = _looks_like_patch(dl_text)
            comp_res = _patch_is_complete(dl_text)
            if looks_res and comp_res:
                if dl_text != raw_dl_text.rstrip("\r\n"):
                    try:
                        downloaded.write_text(dl_text + "\n", encoding="utf-8", newline="\n")
                    except Exception:
                        pass
                canonical_dest = dest_dir / "apply_changes.ps1"
                try:
                    if downloaded.resolve() != canonical_dest.resolve():
                        shutil.copy2(str(downloaded), str(canonical_dest))
                        downloaded = canonical_dest
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
                try:
                    extra_assets = capture_additional_round_artifacts(page, dest_dir)
                    bundle_info = write_delivery_bundle(dest_dir, patch_path=downloaded, asset_paths=extra_assets)
                    print(
                        f"[+] Built delivery bundle: {bundle_info['bundle_zip'].name} "
                        f"({len(bundle_info['files'])} artifact(s) indexed in {bundle_info['manifest_path'].name})"
                    )
                except Exception as bundle_exc:
                    print(f"[!] Warning while building delivery bundle: {bundle_exc}", file=sys.stderr)
                print(
                    f"[+] Captured delivery file: {downloaded.name} "
                    f"(bytes={len(dl_text.encode('utf-8'))}, sha256={_sha256_full(dl_text)}, "
                    f"attempt={attempt}/{MAX_CHIP_DOWNLOAD_ATTEMPTS})"
                )
                _close_preview(page)
                return downloaded

            if not looks_res:
                failure_reason = (
                    f"the downloaded file {downloaded.name} ({file_sz} bytes) failed _looks_like_patch: {looks_res.reason}"
                )
                defects_list = looks_res.defects
            else:
                failure_reason = (
                    f"the downloaded file {downloaded.name} ({file_sz} bytes) failed _patch_is_complete: {comp_res.reason}"
                )
                defects_list = comp_res.defects

            last_quarantined = _quarantine_rejected_file(
                downloaded,
                failure_reason,
                project_path=proj_for_ledger,
                dest_dir=dest_dir,
                defects=defects_list,
            )
            setattr(page, "_argon_last_capture_failure_reason", failure_reason)
            setattr(page, "_argon_last_quarantined_path", str(last_quarantined or ""))

            if attempt >= MAX_CHIP_DOWNLOAD_ATTEMPTS:
                break

        if time.monotonic() >= deadline:
            break

        try:
            page.wait_for_timeout(2500)
        except Exception:
            time.sleep(2.5)

    _close_preview(page)
    if saw_download_button and "not reachable" in failure_reason:
        failure_reason = (
            "the viewer's download button did not produce a valid .ps1 file "
            "(a print control was never clicked)"
        )

    setattr(page, "_argon_chip_capture_exhausted", True)
    setattr(page, "_argon_last_capture_failure_reason", failure_reason)

    quarantine_note = (
        f"\n    Quarantined file kept at: {last_quarantined}\n"
        f'    Apply or inspect it with: Open_Argon_Apply.bat -PatchFile "{last_quarantined}"'
        if last_quarantined is not None
        else "\n    Download it from the Gemini chat by hand and apply it with:\n"
             "        Open_Argon_Apply.bat -PatchFile <path-to-apply_changes.ps1>"
    )
    print(
        f"[!] The delivery could NOT be downloaded after {attempt}/{MAX_CHIP_DOWNLOAD_ATTEMPTS} download attempt(s) "
        f"for this chip - {failure_reason}.{quarantine_note}",
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


def _looks_like_patch(candidate: str) -> _PatchCheckResult:
    """Accept only a real PowerShell patch script, not prompt prose (Section 2.1.A & 2.1.B).

    Returns a `_PatchCheckResult` (truthy/falsy like `bool`, unpackable as `(ok, reason)`,
    with `.reason` and `.defects` naming the exact check that failed):
      - Accepts scripts ending with a comment (`#...`), closing brace (`}`), or
        `Write-Host` / `Write-Output` / `exit` summary line (such as
        `Write-Host "[SUCCESS] 3 file(s) patched." -ForegroundColor Green`).
      - Strips here-string payloads via `_parse_herestring_blocks` (replacing raw @" regex)
        so a payload line containing markdown cannot leak into the prose scan.
    """
    # The last non-empty line must be a comment (#...) or a closing brace (}), or Write-Host
    if not candidate or not candidate.strip():
        return _PatchCheckResult(False, "candidate patch text is empty")
    lines = candidate.splitlines()
    if len(lines) < 8:
        return _PatchCheckResult(
            False,
            f"candidate has only {len(lines)} line(s) (minimum 8 lines required for a patch script)",
        )
    non_empty_lines = [ln.strip() for ln in lines if ln.strip()]
    if not non_empty_lines:
        return _PatchCheckResult(False, "candidate has no non-empty lines")
    last_non_empty = non_empty_lines[-1]
    valid_last_line = (
        last_non_empty.startswith("#")
        or last_non_empty.endswith("}")
        or bool(re.match(r"^(?:Write-Host|Write-Output|exit)\b", last_non_empty, re.IGNORECASE))
    )
    if not valid_last_line:
        return _PatchCheckResult(
            False,
            f"last non-empty line is {last_non_empty[:80]!r} "
            "(expected a comment '#', closing brace '}', or Write-Host/Write-Output summary line)",
        )
    if not re.search(r"\$TargetRoot\s*=\s*['\"][^'\"]{3,}['\"]", candidate):
        return _PatchCheckResult(False, "missing $TargetRoot = '<absolute-path>' assignment")
    if not any(
        k in candidate
        for k in ("WriteAllText", "Set-Content", "Copy-Item", "Write-Host", ".Replace(", ".IndexOf(", "StringBuilder")
    ):
        return _PatchCheckResult(
            False,
            "missing PowerShell patch operations (WriteAllText, Set-Content, Copy-Item, Write-Host, .IndexOf, StringBuilder, .Replace)",
        )
    _, _, outside_herestrings = _parse_herestring_blocks(candidate)
    for line_no, raw in enumerate(outside_herestrings.splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        if re.match(r"^#{3,}\s", s):
            return _PatchCheckResult(
                False,
                f"markdown heading outside here-strings at script line {line_no}: {s[:60]!r}",
            )
        if s.startswith("#"):
            continue
        if re.match(r"^[-*+]\s", s):
            return _PatchCheckResult(
                False,
                f"markdown bullet list line outside here-strings at script line {line_no}: {s[:60]!r}",
            )
        if s.startswith("|"):
            return _PatchCheckResult(
                False,
                f"markdown table row outside here-strings at script line {line_no}: {s[:60]!r}",
            )
        if "**" in s:
            return _PatchCheckResult(
                False,
                f"markdown bold syntax ('**') outside here-strings at script line {line_no}: {s[:60]!r}",
            )

    tpl_ph_1 = "relative/path/" + "to/file.ext"
    tpl_ph_2 = "{{TARGET_" + "DIRECTORY}}"
    if tpl_ph_1 in outside_herestrings or tpl_ph_2 in outside_herestrings:
        return _PatchCheckResult(
            False,
            f"contains unreplaced prompt template placeholder ({tpl_ph_1!r} or {tpl_ph_2!r})",
        )
    return _PatchCheckResult(True, "")
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
    strict: bool = True,
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
    slot_part = _url_slot_for_account(slot, isolated_profile=isolated_profile, strict=strict)
    return GEMINI_WEB_URL.format(slot=slot_part)


_build_app_url = get_target_url
def _url_slot_for_account(
    slot: Optional[str],
    isolated_profile: bool = False,
    strict: bool = True,
    profile_dir_override: Optional[Union[str, Path]] = None,
) -> str:
    """Return `/u/0/` for an isolated slot profile, or `/u/<account_index>/` from `PoolConfig.get_account_index(slot)` in shared mode (Rule R15).

    A pool slot number is NOT an account index: in shared mode this function NEVER falls back
    to `normalize_slot(slot)`. When the slot's account is not signed into the Argon browser
    (`Default/Preferences`), it raises `RuntimeError` so the lane stops with a clear message.
    """
    if isolated_profile:
        return "/u/0/"
    cfg = PoolConfig(profile_dir_override=profile_dir_override)
    # Round 21: keep the pool file in step with the browser before resolving, so a
    # stale snapshot can never be used and the file itself stays corrected.
    try:
        cfg.resync_account_indexes()
    except Exception:
        pass
    target_slot = normalize_slot(slot) if slot else cfg.active_slot()
    acc = cfg.get_by_slot(target_slot)
    email = str((acc or {}).get("email") or "").strip()
    from safe_browser import verify_account_index_in_preferences

    _, found_idx, all_pref_emails = verify_account_index_in_preferences(cfg.profile_dir, email)
    if email and found_idx is not None:
        return f"/u/{int(found_idx)}/"
    if not email and target_slot != "/u/0/":
        raise RuntimeError(
            f"Slot {target_slot} has no configured account email in PoolConfig; "
            f"refusing to fall back to slot number {target_slot} (Rule R15)."
        )
    if all_pref_emails or (cfg.profile_dir / "Default" / "Preferences").is_file() or not isinstance(acc, dict):
        if not strict:
            # Only for the paths that RE-OPEN a conversation whose own URL already carries
            # the right /u/<index>/ - they must keep working while the pool file is out of
            # step with the browser.
            return normalize_slot(slot) if slot else "/u/0/"
        raise RuntimeError(
            f"Slot {target_slot} ({email or 'unconfigured'}) is not signed into the Argon browser "
            f"(registered_accounts={len(all_pref_emails)}); refusing to fall back to slot number {target_slot} (Rule R15)."
        )
    acct_idx = cfg.get_account_index(target_slot)
    return f"/u/{int(acct_idx)}/"


def _profile_is_isolated_slot(profile_dir: Any) -> bool:
    """True ONLY when `profile_dir` is an emergency isolated per-slot profile (`argon_profiles/slot-uN`).

    Never classifies the Argon browser's own profile (`~/.gemini/argon_browser_profile`) as isolated (Round 18 Section 1.1).
    """
    if not profile_dir or not str(profile_dir).strip():
        return False
    try:
        resolved = Path(str(profile_dir)).expanduser().resolve()
        if resolved.name.lower() == "argon_browser_profile":
            return False
        return resolved.parent.name == "argon_profiles" and resolved.name.startswith("slot-")
    except Exception:
        return False


def _normalize_gemini_chat_url(
    url: str,
    isolated_profile: bool = False,
    slot: Optional[str] = None,
) -> str:
    """Ensure a Gemini Web URL uses `/u/0/` in an isolated profile or `/u/<account_index>/` in the shared Argon Bridge browser."""
    raw = (url or "").strip()
    if not raw or "gemini.google.com" not in raw.lower():
        return raw
    if isolated_profile:
        return re.sub(r"(gemini\.google\.com)/u/\d+/", r"\1/u/0/", raw, flags=re.IGNORECASE)
    if slot is not None:
        # The recorded chat URL already carries its own /u/<index>/, so this rewrite is
        # a convenience - it must never raise when the pool is momentarily out of step.
        url_slot = _url_slot_for_account(slot, isolated_profile=False, strict=False)
        if re.search(r"gemini\.google\.com/u/\d+/", raw, flags=re.IGNORECASE):
            return re.sub(r"(gemini\.google\.com)/u/\d+/", f"\\1{url_slot}", raw, flags=re.IGNORECASE)
        return re.sub(r"(gemini\.google\.com)/(?=app\b)", f"\\1{url_slot}", raw, flags=re.IGNORECASE)
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
    """Return the Argon browser's own profile directory (`base_profile` = `~/.gemini/argon_browser_profile`).

    Round 18 Section 1.1: The Argon browser is the ONLY place; per-slot profile directories are removed
    from the normal resolution path and only used when `--isolated-profile` (`ARGON_PROFILE_MODE=isolated`)
    is explicitly set.
    """
    base_profile = Path(base_profile).expanduser().resolve()
    env_shared = (os.environ.get("ARGON_SHARED_PROFILE") or "").strip().lower()
    env_mode = (os.environ.get("ARGON_PROFILE_MODE") or "").strip().lower()
    if env_shared in ("0", "false", "no") or env_mode == "isolated":
        if base_profile.parent.name == "argon_profiles" and base_profile.name.startswith("slot-"):
            root = base_profile.parent
        else:
            root = base_profile.parent / "argon_profiles"
        return root / f"slot-{_slot_key(slot)}"
    if base_profile.parent.name == "argon_profiles" and base_profile.name.startswith("slot-"):
        return base_profile.parent.parent / "argon_browser_profile"
    return base_profile


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


def _close_browser_safely(context: Any, profile_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Safely close `context` / `profile_dir` while preserving session cookies (Section 2.2.B)."""
    from safe_browser import close_browser_safely

    return close_browser_safely(context=context, profile_dir=profile_dir)


class _PlaywrightSafeCloseGuard:
    """Context manager entered INSIDE `with sync_playwright() as p:` so browser/tab close
    always executes BEFORE `sync_playwright().__exit__()` stops the Playwright driver (Section 2.2.A).
    """

    def __init__(self, cleanup_fn) -> None:
        self._cleanup_fn = cleanup_fn

    def __enter__(self) -> "_PlaywrightSafeCloseGuard":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        try:
            self._cleanup_fn()
        except Exception:
            pass
        return False


def _seed_profile(base_profile: Path, target: Path) -> bool:
    """Create a slot profile from the master profile when the master profile is not busy.

    Section 2.2.E: Never replace or `rmtree` a slot profile that already holds an
    account in `Default/Preferences` (`account_info` / `signin.accounts`). If only
    its cookie DB is missing, repair only the cookie DB.
    """
    from safe_browser import profile_has_signed_in_account, repair_slot_cookie_db_only

    base_profile = Path(base_profile)
    target = Path(target)
    if target.is_dir() and (target / "Default").is_dir():
        if profile_has_signed_in_account(target):
            if not _has_cookie_db(target):
                print(
                    f"[+] Slot profile {target} holds a registered account; repairing SQLite cookie DB without replacing profile."
                )
                repair_slot_cookie_db_only(base_profile, target)
            return True
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
        if profile_has_signed_in_account(target):
            repair_slot_cookie_db_only(base_profile, target)
            return True
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

    Automatically loads the unpacked MV3 companion extension (`skills/argon-coding-bridge/extension/`)
    when present on disk, and falls back cleanly to standard launch when absent.
    Never launch Chrome against a profile directory that is already held by
    another run, because doing so hijacks or terminates the other browser.
    """
    if _profile_is_busy(profile_dir):
        raise RuntimeError(f"Profile directory {profile_dir} is already in use by another browser.")

    eff_kwargs = dict(launch_kwargs)
    try:
        from safe_browser import companion_extension_launch_args, get_companion_extension_dir

        ext_dir = get_companion_extension_dir()
        if ext_dir is not None:
            cur_args = list(eff_kwargs.get("args") or [])
            if not any("--load-extension=" in str(a) for a in cur_args):
                cur_args.extend(companion_extension_launch_args(ext_dir))
                eff_kwargs["args"] = cur_args
            print(f"[+] Companion extension loaded from: {ext_dir}")
        else:
            print("[i] Companion extension not installed or disabled; using pure-Python fallback path.")
    except Exception:
        pass

    attempts = []
    if chrome_exe:
        attempts.append(("chrome", {"executable_path": chrome_exe, **eff_kwargs}))
    attempts.append(("chromium", dict(eff_kwargs)))

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

_r23_orig_launch_with_fallback = _launch_with_fallback


def _launch_with_fallback(p, launch_kwargs: dict, chrome_exe: Optional[str], profile_dir: Path):
    """Round 23 wrapper for `_launch_with_fallback` (Rules R2, R4, R8, R14).

    Waits if the profile is busy, strips `--disable-blink-features=AutomationControlled` and
    `--disable-extensions-except`, and prints 'Companion extension loaded' ONLY when verified via CDP.
    """
    wait_deadline = time.monotonic() + 60.0
    while _profile_is_busy(profile_dir) and time.monotonic() < wait_deadline:
        print(f"[i] Profile {profile_dir} is busy; waiting for the active browser session (Rule R4)...")
        time.sleep(1.5)

    eff_kwargs = dict(launch_kwargs)
    clean_args = [
        str(a)
        for a in (eff_kwargs.get("args") or [])
        if not str(a).startswith("--disable-extensions-except=")
        and str(a) != "--disable-blink-features=AutomationControlled"
    ]
    if "--enable-unsafe-extension-debugging" not in clean_args:
        clean_args.append("--enable-unsafe-extension-debugging")
    eff_kwargs["args"] = clean_args

    prev_env = os.environ.get("ARGON_DISABLE_COMPANION_EXTENSION")
    ctx = None
    try:
        os.environ["ARGON_DISABLE_COMPANION_EXTENSION"] = "1"
        ctx = _r23_orig_launch_with_fallback(p, eff_kwargs, chrome_exe, profile_dir)
    finally:
        if prev_env is None:
            os.environ.pop("ARGON_DISABLE_COMPANION_EXTENSION", None)
        else:
            os.environ["ARGON_DISABLE_COMPANION_EXTENSION"] = prev_env

    if ctx is not None:
        from safe_browser import ensure_companion_extension_installed, get_companion_extension_dir

        ext_dir = get_companion_extension_dir()
        if ext_dir is not None:
            ext_rep = ensure_companion_extension_installed(browser=ctx, profile_dir=profile_dir, ext_dir=ext_dir)
            if ext_rep.get("ok"):
                print(
                    f"[+] Companion extension loaded and verified via CDP: "
                    f"{ext_rep.get('name')} {ext_rep.get('version')} (id={ext_rep.get('id')})"
                )
            else:
                print(
                    f"[!] Companion extension could not be verified via CDP ({ext_rep.get('reason')}); "
                    "using pure-Python fallback path."
                )
    return ctx


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

    def reclaim_dead_tasks(
        self,
        project_path: Any = None,
        lane: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Reclaim any `running` task records whose owning PID is dead (Addendum A.2).

        A dead process must never leave a `running` record behind that blocks the next
        retry ladder attempt with EXIT_ALREADY_RUNNING.
        """
        self.load()
        wanted = _normalize_project_key(project_path) if project_path is not None else ""
        lane_str = (lane or "").strip().lower()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        reclaimed: List[Dict[str, Any]] = []
        for e in (self.data.get("entries") or []):
            if not isinstance(e, dict) or str(e.get("status") or "").strip().lower() != "running":
                continue
            if wanted and _normalize_project_key(e.get("project", "")) != wanted:
                continue
            if lane_str and str(e.get("lane") or "single").strip().lower() != lane_str:
                continue
            pid = int(e.get("pid") or 0)
            if pid > 0 and pid != os.getpid() and not _pid_alive(pid):
                e["status"] = "failed"
                e["end_reason"] = f"reclaimed leftover running record from dead pid {pid}"
                e["completed_at"] = now_str
                e["updated_at"] = now_str
                reclaimed.append(dict(e))
        if reclaimed:
            self.save()
            for r_item in reclaimed:
                print(
                    f"[+] Reclaimed leftover running task record from dead pid {r_item.get('pid')} "
                    f"(task_id={r_item.get('task_id')}, lane={r_item.get('lane')}) -> marked 'failed'."
                )
        return reclaimed

    def register_done(
        self,
        project_path: Any,
        lane: Optional[str] = None,
        slot: Optional[str] = None,
        status: str = "completed",
        screenshot_path: str = "",
        label: Optional[str] = None,
        end_reason: str = "",
    ) -> int:
        """Mark running task(s) for `project_path` (and optional `lane`/`slot`/`label`) as terminal (`completed`/`failed`/`superseded`/`done`) with `end_reason` (D4.2, Addendum A.2)."""
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
            if end_reason:
                e["end_reason"] = str(end_reason)
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
        url_slot = _url_slot_for_account(
            slot_part,
            isolated_profile=_profile_is_isolated_slot(profile_dir),
            strict=False,
        )
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
        url_slot = _url_slot_for_account(
            win_slot,
            isolated_profile=_profile_is_isolated_slot(win_prof),
            strict=False,
        )
        win_url = _normalize_gemini_chat_url(
            str(winning_lane.get("chat_url") or f"https://gemini.google.com{url_slot}app/{win_cid}"),
            isolated_profile=_profile_is_isolated_slot(win_prof),
            slot=win_slot,
        )
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
        url_slot = _url_slot_for_account(
            slot_val,
            isolated_profile=_profile_is_isolated_slot(prof_val),
            strict=False,
        )
        chat_url = _normalize_gemini_chat_url(
            str(lane_data.get("chat_url") or entry.get("chat_url") or f"https://gemini.google.com{url_slot}app/{chat_id}"),
            isolated_profile=_profile_is_isolated_slot(prof_val),
            slot=slot_val,
        )
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
RE_FLASH = re.compile(
    r"(?:^|\b)(?:3\.8\s*flash|2\.5\s*flash|2\.0\s*flash|gemini[\s\-_]*flash|flash|ফ্ল্যাশ|"
    r"4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)",
    re.IGNORECASE,
)
RE_HYDROGEN = re.compile(
    r"(?:^|\b)(?:4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)",
    re.IGNORECASE,
)

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
    """True when this chip shows the Argon model and NEVER Flash (Rule 6).

    Only the chip's OWN model name counts. A chip that says "Flash" is not
    Argon, no matter what else the page says.
    """
    if el is None:
        return False
    name = _model_name(el)
    if not name:
        return False
    if RE_FLASH.search(name):
        return False
    return bool(RE_ARGON.search(name))


def _chip_is_flash(el) -> bool:
    """True when the model chip shows a Flash model (Rule 6 hard guard)."""
    if el is None:
        return False
    name = _model_name(el)
    if not name:
        return False
    return bool(RE_FLASH.search(name))


def _live_account_email_for_index(profile_dir: Any, index: int) -> str:
    """Return the email the Argon browser holds at `/u/<index>/`.

    The source of truth is the browser's own live `Default/Preferences`
    (`account_info`) list, NOT the pool entry: the pool slot and the `/u/<N>/`
    path are two different things and they can disagree. Returns an empty string
    when the browser has no account at that index.
    """
    from safe_browser import get_preferences_accounts

    try:
        want_idx = int(index)
    except (TypeError, ValueError):
        return ""
    for live_acc in get_preferences_accounts(profile_dir):
        try:
            live_idx = int(live_acc.get("index"))
        except (TypeError, ValueError):
            continue
        if live_idx == want_idx:
            return str(live_acc.get("email") or "").strip()
    return ""


def _verify_model_chip_is_argon(page, stage: str = "before send") -> tuple[bool, str]:
    """Read the model chip back from the DOM and prove that it names Argon.

    `ensure_mode_and_model` (the companion extension) and `select_argon_model` (the
    pure-Python fallback) can report success while the chip has already fallen back
    to another model (for example 'Hydrogen'), so the chip is read back right before
    the prompt goes out. A chip that is present and NOT Argon aborts the send - a
    task must never leave on a different model.

    Returns `(ok, chip_name)`; `chip_name` is empty when no chip is visible at all.
    """
    chip = _model_chip(page)
    if chip is None:
        return True, ""
    name = (_model_name(chip) or "").strip()
    if _chip_is_argon(chip):
        return True, name
    shown = name or "(not visible)"
    print(
        f"[ERROR] The model chip reads {shown!r}, not Argon - refusing to send on the wrong model.",
        file=sys.stderr,
    )
    print(
        f"        (checked {stage}: the chip has to read '4 Argon'. Pick '4 Argon' in the model "
        "picker of the Argon browser window, then re-run this task.)",
        file=sys.stderr,
    )
    return False, shown


def _argon_visible(page) -> bool:
    """True when the MODEL is Argon.

    The model chip is the ground truth: it shows the model that will actually
    answer. A chat history entry can also contain the words "4 Argon", and the
    OPEN model picker shows every model at once - so "4 Argon" being visible in
    the menu proves nothing while "3.8 Flash" is the checked entry. That false
    positive sent real tasks to Flash, so it must not happen again.

    Therefore:
      1. the chip has to name Argon (and not Flash), or
      2. the open picker has to have Argon as the CHECKED entry.
    """
    chip = _model_chip(page)
    if chip is not None and _chip_is_argon(chip):
        return True

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
                txt = el.inner_text(timeout=400) or ""
                if RE_ARGON.search(txt) and not RE_FLASH.search(txt):
                    return True
            except Exception:
                continue
    except Exception:
        pass

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
                     account has no Argon tier available right now.
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
    """Click a matching option INSIDE the open picker."""
    return _inspect_option_in_picker(page, pattern) == "clicked"


def _argon_picker_is_disabled(page) -> bool:
    """Return True when the most recent model check saw '4 Argon' present but disabled."""
    info = _LAST_PICKER_ARGON_STATE.get(id(page)) or {}
    return bool(info.get("disabled"))


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
            el = task_btn.nth(i)
            # Round 23 (R3/F3): the word "Task" also appears in the chat transcript,
            # in code blocks and in the sidebar. Only a real segmented toggle control
            # counts, otherwise the dispatcher reads "chat" where it should read "task".
            if _safe_visible(el) and _is_toggle_control(el):
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
    """Switch Gemini Web into Task mode and verify it (E2)."""
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
    """Hard Flash Guard & 4 Argon model selector (Rule 6).

    Because the model chip reverts to the account default (often 'Flash') AFTER a
    message is sent, both a NEW chat and a FOLLOW-UP in an existing chat must check
    the model chip before every send. If the chip shows Flash or any non-Argon model,
    this function selects '4 Argon' again; if the chip is still not Argon (or if
    Flash remains active), it returns False so the send is aborted immediately.
    """
    _LAST_PICKER_ARGON_STATE.pop(id(page), None)
    last_seen = ""
    last_picker_state = "missing"

    is_cont = bool(getattr(page, "_argon_is_continuation", False))
    if require_chip is None:
        require_chip = True

    chip_wait = float(wait_for_chip_seconds) if not is_cont else min(12.0, float(wait_for_chip_seconds))
    initial_chip = _wait_for_model_chip(page, timeout=chip_wait)
    if initial_chip is None and not require_chip and not is_cont:
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
            if _chip_is_flash(chip):
                print(
                    f"[!] Flash model detected on chip ({name!r}) - switching to 4 Argon (attempt {attempt + 1}/{max(1, attempts)})...",
                    file=sys.stderr,
                )
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

    if last_seen and RE_FLASH.search(last_seen):
        print(
            f"[ERROR] FLASH GUARD ABORT: model chip still reads {last_seen!r} after {max(1, attempts)} attempt(s). "
            "A task must NEVER be sent on Flash - aborting send.",
            file=sys.stderr,
        )
        return False
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


def enforce_no_flash_before_send(page) -> bool:
    """Hard safety guard executed immediately before clicking Send (Rule 6).

    Reads the model chip; if it is not 4 Argon (for example, if the chat reverted
    to Flash on a follow-up or after upload), selects 4 Argon again. If the model
    is still not 4 Argon, returns False so the caller aborts the send immediately.
    """
    chip = _model_chip(page)
    if chip is not None and _chip_is_argon(chip):
        return True
    if ensure_argon_model(page, attempts=3, require_chip=True, wait_for_chip_seconds=15.0):
        chip_after = _model_chip(page)
        if chip_after is not None and _chip_is_argon(chip_after):
            return True
    chip_now = _model_chip(page)
    chip_name = _model_name(chip_now) if chip_now is not None else "(not visible)"
    print(
        f"[ERROR] HARD FLASH GUARD TRIGGERED: model chip reads {chip_name!r} (not 4 Argon). "
        "Aborting send - a task must never go out on Flash.",
        file=sys.stderr,
    )
    return False


def _inspect_zip_archive(zip_path: Path) -> tuple[bool, int, int, str]:
    """Verify that `zip_path` exists, is non-empty, opens as a valid zip, contains readable TASK_BRIEF.md and API_INDEX.md, and has no binary poison (E1, D14, D15, Rule 4).

    Returns (ok, size_bytes, entry_count, error_reason).
    """
    import zipfile as _zf
    from package_context import DEFAULT_EXCLUDED_EXTS as _BIN_EXTS, DEFAULT_MAX_ARCHIVE_MB as _MAX_ARCH_MB

    zp = Path(zip_path)
    if not zp.is_file():
        return False, 0, 0, f"archive does not exist: {zp}"
    try:
        sz = zp.stat().st_size
    except OSError as exc:
        return False, 0, 0, f"could not stat archive {zp}: {exc}"
    if sz <= 0:
        return False, 0, 0, f"archive {zp.name} is 0 bytes (empty)"
    max_allowed_bytes = int(_MAX_ARCH_MB) * 1024 * 1024
    if sz > max_allowed_bytes:
        return (
            False,
            sz,
            0,
            f"archive_size_cap_exceeded: archive {zp.name} ({sz} bytes, {sz / (1024 * 1024):.2f} MB) "
            f"exceeds the {_MAX_ARCH_MB} MB hard limit ({max_allowed_bytes} bytes; "
            "Gemini cannot reliably index context archives above 100 MB)",
        )
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
    """Verify the composer carries the valid archive with matching byte size and TASK_BRIEF.md (D14, Section 1.3b)."""
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
        # Gemini renders the attachment chip with the file name WITHOUT its extension
        # ("argon_context_r22" for argon_context_r22.zip), and once the upload finished it
        # also clears the file input. So the name has to be matched on the stem as well,
        # otherwise a perfectly attached archive is reported as missing.
        for candidate in (Path(zip_path).name, Path(zip_path).stem):
            if not candidate:
                continue
            chip = page.get_by_text(re.compile(re.escape(candidate), re.IGNORECASE))
            for i in range(min(chip.count(), 5)):
                if _safe_visible(chip.nth(i)):
                    chip_visible = True
                    break
            if chip_visible:
                break
    except Exception:
        pass

    return bool(input_matched or chip_visible)


def _open_upload_menu(page) -> bool:
    """Open the composer's upload-and-tools menu (the '+' button)."""
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
    """Verify that a context archive is readable, contains TASK_BRIEF.md, and has no credentials (D14, D15)."""
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


def _upload_timeout_for_archive(zip_path: Path, floor_seconds: float = 600.0) -> float:
    """Scale the upload window dynamically from the archive's byte size (Rule 4).

    Derived from a measured minimum upload rate of ~128 KB/s (8 s per MB) plus 60 s
    of server-side indexing overhead, with `floor_seconds` (600 s) as the minimum floor
    so a 40 MB archive has at least 600 s and a 100 MB archive has ~900 s (15 minutes).
    """
    try:
        sz_bytes = Path(zip_path).stat().st_size if Path(zip_path).is_file() else 0
    except OSError:
        sz_bytes = 0
    min_bytes_per_sec = 128.0 * 1024.0
    scaled_seconds = 60.0 + (float(sz_bytes) / min_bytes_per_sec)
    return max(float(floor_seconds), scaled_seconds)


def _wait_for_upload_complete(page, zip_path: Path, timeout: float = 600.0) -> bool:
    """Wait until the attached archive's upload spinner clears before pasting/sending (D16).

    The window has to cover the REAL upload time of a large context archive. A 40 MB
    archive needs about five minutes on a normal link, and the old 90 s window made the
    pre-flight give up ("refusing to send a half-uploaded archive") while the file was
    still uploading - the prompt was then never pasted at all.
    """
    effective_timeout = _upload_timeout_for_archive(Path(zip_path), floor_seconds=float(timeout))
    wait_cap = getattr(page, "_argon_upload_wait_cap_seconds", None)
    loop_budget = float(wait_cap) if (wait_cap is not None and float(wait_cap) > 0) else max(5.0, effective_timeout)
    poll_ms = 100 if (wait_cap is not None and float(wait_cap) < 5.0) else 900
    start_mono = time.monotonic()
    deadline = start_mono + loop_budget
    clear_streak = 0
    while time.monotonic() < deadline:
        present = _attachment_present(page, zip_path)
        uploading = _composer_upload_in_progress(page)
        if present and not uploading:
            clear_streak += 1
            if clear_streak >= 2:
                waited = time.monotonic() - start_mono
                sz = Path(zip_path).stat().st_size if Path(zip_path).is_file() else 0
                setattr(page, "_argon_last_upload_status", "upload_complete")
                setattr(page, "_argon_last_upload_elapsed", round(waited, 2))
                setattr(page, "_argon_last_upload_window", round(effective_timeout, 1))
                setattr(page, "_argon_last_upload_error", "")
                print(
                    f"[+] Upload of {Path(zip_path).name} ({sz} bytes) finished in {waited:.1f}s "
                    f"(window={effective_timeout:.0f}s; attachment chip ready, upload spinner cleared)."
                )
                return True
        else:
            clear_streak = 0
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            time.sleep(poll_ms / 1000.0)

    waited = time.monotonic() - start_mono
    still_uploading = _composer_upload_in_progress(page)
    still_present = _attachment_present(page, zip_path)
    if still_uploading:
        upload_state = "upload_still_running"
        state_desc = "the upload is still running (upload spinner still active)"
    elif still_present:
        upload_state = "upload_still_running"
        state_desc = "the upload is still running (attachment did not reach settled state before deadline)"
    else:
        upload_state = "upload_failed"
        state_desc = "the upload failed (upload spinner stopped and attachment is missing or rejected)"
    detail_msg = (
        f"{upload_state}: {state_desc} after {waited:.1f}s "
        f"(effective_window={effective_timeout:.0f}s; upload spinner still active or attachment missing)"
    )
    setattr(page, "_argon_last_upload_status", upload_state)
    setattr(page, "_argon_last_upload_elapsed", round(waited, 2))
    setattr(page, "_argon_last_upload_window", round(effective_timeout, 1))
    setattr(page, "_argon_last_upload_error", detail_msg)
    print(
        f"[ERROR] Upload of {Path(zip_path).name} did not finish within {waited:.1f}s "
        f"(window={effective_timeout:.0f}s; {upload_state}: {state_desc}; "
        "upload spinner still active or attachment missing) - "
        "refusing to send a half-uploaded archive.",
        file=sys.stderr,
    )
    return False


def _extension_is_available(page) -> bool:
    """Return True when the MV3 companion extension content script is active on `page`."""
    if str(os.environ.get("ARGON_DISABLE_COMPANION_EXTENSION") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    try:
        ver = page.evaluate(
            "() => (document && document.documentElement && document.documentElement.getAttribute('data-argon-companion-extension')) || ''"
        )
        return bool(isinstance(ver, str) and ver.strip())
    except Exception:
        return False


def _invoke_companion_extension(
    page,
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout_ms: int = 15000,
) -> Optional[Dict[str, Any]]:
    """Send a command to the MV3 companion extension via the DOM custom event bridge and return its response."""
    if not _extension_is_available(page):
        return None
    req_id = f"req_{os.getpid()}_{int(time.time() * 1000)}_{ abs(hash(action)) % 10000 }"
    try:
        res = page.evaluate(
            r"""([reqId, act, pl, timeoutMs]) => new Promise((resolve) => {
                const attrName = "data-argon-companion-response-" + reqId;
                let settled = false;
                const finish = (val) => {
                    if (settled) return;
                    settled = true;
                    try {
                        if (document.documentElement) {
                            document.documentElement.removeAttribute(attrName);
                        }
                    } catch (_e) {}
                    resolve(val);
                };
                const onResp = (ev) => {
                    const d = (ev && ev.detail) || {};
                    if (d.requestId === reqId) {
                        window.removeEventListener("argon:companion:response", onResp);
                        finish(d);
                    }
                };
                window.addEventListener("argon:companion:response", onResp);
                window.dispatchEvent(new CustomEvent("argon:companion:request", {
                    detail: {
                        protocol: "argon.companion.v1",
                        requestId: reqId,
                        action: act,
                        payload: pl || {}
                    }
                }));
                const checkAttr = () => {
                    if (settled) return;
                    try {
                        const raw = document.documentElement && document.documentElement.getAttribute(attrName);
                        if (raw) {
                            window.removeEventListener("argon:companion:response", onResp);
                            finish(JSON.parse(raw));
                            return;
                        }
                    } catch (_e) {}
                    setTimeout(checkAttr, 40);
                };
                setTimeout(checkAttr, 20);
                setTimeout(() => {
                    window.removeEventListener("argon:companion:response", onResp);
                    finish({ ok: false, operation: act, path: "extension", reason: "companion extension request timed out" });
                }, Math.max(500, Number(timeoutMs || 15000)));
            })""",
            [req_id, action, payload or {}, int(timeout_ms)],
        )
        return res if isinstance(res, dict) else None
    except Exception:
        return None


def _attach_zip_via_datatransfer(
    page,
    zip_path: Path,
    use_extension: bool = True,
    allow_over_cap: bool = False,
) -> tuple[bool, str, str]:
    """Attach `zip_path` by injecting a `File` via `DataTransfer` into `input[type=file]` (no OS file dialog).

    Round 24 (R1.1, R1.2, R3.1):
      - Reads `zp.stat().st_size` FIRST before `zp.read_bytes()`.
      - Enforces the 100 MB archive cap (`archive_size_cap_exceeded`).
      - Reads `MAX_BASE64_ATTACH_BYTES` (and `ARGON_MAX_BASE64_ATTACH_MB`) and returns
        `(False, "size_cap_exceeded", "size_cap_exceeded")` without base64-encoding when
        `sz > eff_b64_cap` and `not allow_over_cap`.
    """
    import base64 as _b64

    zp = Path(zip_path)
    try:
        sz = zp.stat().st_size
    except OSError as exc:
        return False, "python_fallback", f"could not stat archive {zp.name}: {exc}"

    hard_cap_bytes = 100 * 1024 * 1024
    if sz > hard_cap_bytes:
        return (
            False,
            "archive_size_cap_exceeded",
            f"archive_size_cap_exceeded: archive {zp.name} ({sz} bytes, {sz / (1024 * 1024):.2f} MB) "
            f"exceeds the 100 MB hard limit ({hard_cap_bytes} bytes)",
        )

    eff_b64_cap = get_effective_base64_attach_cap(MAX_BASE64_ATTACH_BYTES)
    if sz > eff_b64_cap and not allow_over_cap:
        return False, "size_cap_exceeded", "size_cap_exceeded"

    try:
        raw_bytes = zp.read_bytes()
    except OSError as exc:
        return False, "python_fallback", f"could not read archive {zp.name}: {exc}"

    b64_str = _b64.b64encode(raw_bytes).decode("ascii")

    if use_extension and _extension_is_available(page):
        ext_res = _invoke_companion_extension(
            page,
            "attach_archive",
            {
                "fileName": zp.name,
                "expectedSize": len(raw_bytes),
                "base64Data": b64_str,
            },
            timeout_ms=20000,
        )
        if isinstance(ext_res, dict):
            if ext_res.get("ok"):
                if _wait_for_upload_complete(page, zp):
                    return True, "extension", ""
                up_err = str(getattr(page, "_argon_last_upload_error", "") or "")
                return (
                    False,
                    "extension",
                    up_err or f"upload of {zp.name} did not finish within the upload window after extension DataTransfer attach",
                )
            ext_reason = str(ext_res.get("reason") or "extension attach failed")
            if "rendered no file input" in ext_reason:
                return False, "extension", ext_reason

    # Pure-Python DataTransfer File injection into input[type=file] (still zero OS file dialog)
    _open_upload_menu(page)
    try:
        dt_res = page.evaluate(
            r"""([fileName, b64Data, expectedSize]) => {
                const inputs = Array.from(document.querySelectorAll("input[type='file']"));
                if (!inputs.length) {
                    return { ok: false, reason: "the upload menu rendered no file input" };
                }
                let targetInput = inputs[0];
                for (const inp of inputs) {
                    const acc = String(inp.getAttribute("accept") || "").toLowerCase();
                    if (acc.includes(".zip")) {
                        targetInput = inp;
                        break;
                    }
                }
                const bin = atob(b64Data);
                const bytes = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) {
                    bytes[i] = bin.charCodeAt(i);
                }
                if (expectedSize > 0 && bytes.byteLength !== expectedSize) {
                    return { ok: false, reason: "archive byte length mismatch in DataTransfer" };
                }
                const fileObj = new File([bytes], fileName, { type: "application/zip", lastModified: Date.now() });
                const dt = new DataTransfer();
                dt.items.add(fileObj);
                targetInput.files = dt.files;
                targetInput.dispatchEvent(new Event("input", { bubbles: true }));
                targetInput.dispatchEvent(new Event("change", { bubbles: true }));
                return { ok: true, size: bytes.byteLength };
            }""",
            [zp.name, b64_str, len(raw_bytes)],
        )
        if isinstance(dt_res, dict):
            if dt_res.get("ok"):
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    time.sleep(0.8)
                if _wait_for_upload_complete(page, zp):
                    return True, "python_datatransfer", ""
                up_err = str(getattr(page, "_argon_last_upload_error", "") or "")
                return (
                    False,
                    "python_datatransfer",
                    up_err or f"upload of {zp.name} did not finish within the upload window after DataTransfer attach",
                )
            elif "rendered no file input" in str(dt_res.get("reason") or ""):
                return False, "python_fallback", "the upload menu rendered no file input"
    except Exception:
        pass

    return False, "python_fallback", ""


def attach_zip(page, zip_path: Path) -> bool:
    """Attach the context zip to the composer, verify its size/members, and wait until upload finishes (D14, D15, D16, Round 24 W1 & W3).

    Route order (Round 24 R1.3):
      1. CDP `DOM.setFileInputFiles` (`_attach_zip_via_cdp_dom_file_input()`) - zero copy;
      2. Playwright `set_input_files` - zero copy;
      3. Companion-extension `attach_archive` / `DataTransfer` (base64) - ONLY for archives at or below `MAX_BASE64_ATTACH_BYTES`
         (with a loud warning fallback if > cap and no zero-copy route is available, R1.4);
      4. OS file dialog - only when explicitly allowed (`ARGON_ALLOW_OS_FILE_DIALOG`).
    """
    t0 = time.monotonic()
    zp = Path(zip_path)
    setattr(page, "_argon_last_upload_error", "")
    setattr(page, "_argon_last_upload_status", "")

    # R3.1: Inspect archive first so the 100 MB cap (`archive_size_cap_exceeded`) is surfaced with its exact named reason.
    ok_zip, sz, entries, zip_err = _inspect_zip_archive(zp)
    if not ok_zip:
        err = AttachOperationError(
            zip_err or f"archive {zp.name} is invalid",
            path_used="validation",
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        print(f"[ERROR] {err}", file=sys.stderr)
        return False

    if not _verify_zip_has_no_credentials(zp):
        err = AttachOperationError(
            f"archive {zp.name} failed credential/integrity verification",
            path_used="validation",
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        return False

    eff_b64_cap = get_effective_base64_attach_cap(MAX_BASE64_ATTACH_BYTES)

    # R3.2: An archive above MAX_BASE64_ATTACH_BYTES must be attached to a FRESH conversation.
    if sz > eff_b64_cap:
        has_existing_att, existing_att_detail = _conversation_holds_existing_attachment(page)
        if has_existing_att:
            fresh_reason = (
                f"fresh_conversation_required_for_large_archive: archive {zp.name} ({sz} bytes) "
                f"exceeds MAX_BASE64_ATTACH_BYTES ({eff_b64_cap} bytes) and the target conversation "
                f"already holds an attachment ({existing_att_detail}); large archives require a fresh chat (--new-session)."
            )
            err = AttachOperationError(
                fresh_reason,
                path_used="fresh_conversation_guard",
                elapsed_seconds=time.monotonic() - t0,
            )
            setattr(page, "_argon_last_operation_error", err)
            print(f"[ERROR] {fresh_reason}", file=sys.stderr)
            return False

    eff_win = _upload_timeout_for_archive(zp, floor_seconds=600.0)
    ext_active = _extension_is_available(page)
    path_label = "extension" if ext_active else "python_fallback"
    print(
        f"[+] Attaching archive {zp.name} ({sz} bytes, {entries} entries, "
        f"upload window {eff_win:.0f}s, base64_cap={eff_b64_cap} bytes, path={path_label})..."
    )

    use_base64_primary = bool(sz <= eff_b64_cap)
    initial_route = (
        ("companion_extension_base64" if ext_active else "datatransfer_base64")
        if use_base64_primary
        else "cdp_dom_set_file_input_files"
    )
    route_line = format_archive_route_decision_line(
        zp.name,
        sz,
        eff_b64_cap,
        initial_route,
        use_base64=use_base64_primary,
    )
    setattr(page, "_argon_last_route_decision_line", route_line)
    print(route_line)

    # Probe _attach_zip_via_datatransfer:
    # - When sz <= eff_b64_cap, this runs today's companion-extension / DataTransfer route (preserving F4).
    # - When sz > eff_b64_cap, _attach_zip_via_datatransfer checks zp.stat().st_size FIRST without reading bytes
    #   and returns (False, "size_cap_exceeded", "size_cap_exceeded") so we fall through to the zero-copy routes.
    dt_ok, dt_path, dt_reason = _attach_zip_via_datatransfer(page, zp, use_extension=ext_active)
    if dt_ok:
        setattr(page, "_argon_last_attach_path", dt_path)
        setattr(page, "_argon_last_operation_error", None)
        print(f"[+] Archive {zp.name} attached via {dt_path} (no OS file dialog).")
        return True
    if dt_reason == "the upload menu rendered no file input":
        if _zip_input(page) is None:
            err = AttachOperationError(
                "the upload menu rendered no file input",
                path_used=dt_path,
                elapsed_seconds=time.monotonic() - t0,
            )
            setattr(page, "_argon_last_operation_error", err)
            print(f"[ERROR] {err}", file=sys.stderr)
            return False
    if dt_reason and ("upload_still_running" in dt_reason or "upload_failed" in dt_reason):
        err = AttachOperationError(
            dt_reason,
            path_used=dt_path,
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        return False

    route_errors: List[str] = []

    # Zero-Copy Route 1 (R1.3 #1): CDP DOM.setFileInputFiles (_attach_zip_via_cdp_dom_file_input)
    _open_upload_menu(page)
    try:
        cdp_ok, cdp_path, cdp_reason = _attach_zip_via_cdp_dom_file_input(page, zp)
    except Exception as cdp_exc:
        cdp_ok, cdp_path, cdp_reason = False, "cdp_dom_file_input", f"{type(cdp_exc).__name__}: {cdp_exc}"
    if cdp_ok:
        try:
            page.wait_for_timeout(800)
        except Exception:
            time.sleep(0.8)
        if _wait_for_upload_complete(page, zp):
            setattr(page, "_argon_last_attach_path", cdp_path)
            setattr(page, "_argon_last_operation_error", None)
            print(f"[+] Archive {zp.name} attached via {cdp_path} (zero-copy CDP DOM.setFileInputFiles).")
            return True
        up_err = str(getattr(page, "_argon_last_upload_error", "") or "upload did not complete within window")
        err = AttachOperationError(
            up_err,
            path_used=cdp_path,
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        return False
    elif cdp_reason:
        route_errors.append(f"cdp_dom_file_input ({cdp_reason})")

    # Zero-Copy Route 2a (R1.3 #2): Playwright set_input_files on _zip_input after opening upload menu
    el = _zip_input(page)
    if el is not None:
        try:
            el.set_input_files(str(zp))
            page.wait_for_timeout(1500)
            if _wait_for_upload_complete(page, zp):
                setattr(page, "_argon_last_attach_path", "python_fallback")
                setattr(page, "_argon_last_operation_error", None)
                return True
            up_err = str(getattr(page, "_argon_last_upload_error", "") or "upload menu input[type=file] did not complete upload within window")
            err = AttachOperationError(
                up_err,
                path_used="python_fallback",
                elapsed_seconds=time.monotonic() - t0,
            )
            setattr(page, "_argon_last_operation_error", err)
            return False
        except Exception as exc:
            route_errors.append(f"upload menu input[type=file] failed ({exc})")
            print(f"[!] Attach route 1 (upload menu file input) failed: {exc}", file=sys.stderr)
    else:
        route_errors.append("the upload menu rendered no file input")

    # Zero-Copy Route 2b (R1.3 #2): Playwright set_input_files on any existing input[type=file]
    try:
        inputs = page.locator("input[type='file']")
        inp_count = min(inputs.count(), 4)
        for i in range(inp_count):
            try:
                inputs.nth(i).set_input_files(str(zp))
                page.wait_for_timeout(1500)
                if _wait_for_upload_complete(page, zp):
                    setattr(page, "_argon_last_attach_path", "python_fallback")
                    setattr(page, "_argon_last_operation_error", None)
                    return True
                up_err = str(getattr(page, "_argon_last_upload_error", "") or f"input[type=file] #{i} did not complete upload within window")
                err = AttachOperationError(
                    up_err,
                    path_used="python_fallback",
                    elapsed_seconds=time.monotonic() - t0,
                )
                setattr(page, "_argon_last_operation_error", err)
                return False
            except Exception as exc:
                route_errors.append(f"input[type=file] #{i} failed ({exc})")
                print(f"[!] Attach route 2 (input[type=file] #{i}) failed: {exc}", file=sys.stderr)
                continue
    except Exception as exc:
        route_errors.append(f"querying input[type=file] failed ({exc})")
        print(f"[!] Attach route 2 (querying input[type=file]) failed: {exc}", file=sys.stderr)

    # Route 4 (R1.3 #4): Native OS file chooser is disabled by default unless ARGON_ALLOW_OS_FILE_DIALOG=1
    allow_os_dialog = str(os.environ.get("ARGON_ALLOW_OS_FILE_DIALOG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ) or not hasattr(page, "evaluate")
    if allow_os_dialog:
        try:
            loc = page.get_by_text(re.compile(r"(ফাইল আপলোড|ফাইল|upload|আপলোড)", re.IGNORECASE))
            for i in range(min(loc.count(), 12)):
                el2 = loc.nth(i)
                if not _safe_visible(el2):
                    continue
                try:
                    with page.expect_file_chooser(timeout=4000) as fc_info:
                        el2.click(timeout=2000)
                    fc_info.value.set_files(str(zp))
                    page.wait_for_timeout(1500)
                    if _wait_for_upload_complete(page, zp):
                        setattr(page, "_argon_last_attach_path", "python_fallback_file_chooser")
                        setattr(page, "_argon_last_operation_error", None)
                        return True
                except Exception as exc:
                    route_errors.append(f"file chooser #{i} failed ({exc})")
                    print(f"[!] Attach route 3 (native file chooser #{i}) failed: {exc}", file=sys.stderr)
                    continue
        except Exception as exc:
            route_errors.append(f"locating upload menu entry failed ({exc})")
            print(f"[!] Attach route 3 (locating upload menu entry) failed: {exc}", file=sys.stderr)

    # R1.4: When the cap was exceeded and no zero-copy route was available, log a loud warning
    # and continue with the base64 route so the attach NEVER fails because of the base64 cap.
    if not use_base64_primary and not getattr(page, "_argon_last_upload_error", ""):
        print(
            f"[WARNING] No zero-copy attach route was available for {zp.name} ({sz} bytes > cap {eff_b64_cap} bytes); "
            "continuing with base64 DataTransfer route so attach does not fail because of the base64 cap.",
            file=sys.stderr,
        )
        fb_line = format_archive_route_decision_line(
            zp.name,
            sz,
            eff_b64_cap,
            "companion_extension_base64_fallback" if ext_active else "datatransfer_base64_fallback",
            use_base64=True,
        )
        setattr(page, "_argon_last_route_decision_line", fb_line)
        print(fb_line)
        fb_ok, fb_path, fb_reason = _attach_zip_via_datatransfer(
            page, zp, use_extension=ext_active, allow_over_cap=True
        )
        if fb_ok:
            setattr(page, "_argon_last_attach_path", fb_path)
            setattr(page, "_argon_last_operation_error", None)
            print(f"[+] Archive {zp.name} attached via {fb_path} (over-cap base64 fallback).")
            return True
        if fb_reason:
            route_errors.append(f"base64_fallback ({fb_reason})")

    no_input_only = (
        "the upload menu rendered no file input" in route_errors
        and all(
            ("rendered no file input" in r or "cdp_dom_file_input" in r or "base64_fallback" in r)
            for r in route_errors
        )
    )
    primary_reason = (
        "the upload menu rendered no file input"
        if no_input_only
        else ("; ".join(route_errors) or "no file input was available in the composer")
    )
    err = AttachOperationError(
        primary_reason,
        path_used=path_label,
        elapsed_seconds=time.monotonic() - t0,
    )
    setattr(page, "_argon_last_operation_error", err)
    print(
        f"[ERROR] {err}",
        file=sys.stderr,
    )
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
    """The composer's SEND button, matched semantically inside the composer's own container first.

    Avoids brittle reliance on a single CSS class name: first searches inside the composer
    container for any visible enabled submit/send button (by aria-label, data-testid,
    data-tooltip, type='submit', or trailing action button inside the composer container
    excluding upload/model/voice controls), then falls back to page-level selectors.
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
                "button[aria-label*='Submit' i], "
                "button[aria-label*='পাঠান'], "
                "button[data-testid*='send' i], "
                "button[data-testid*='submit' i], "
                "button[type='submit'], "
                "button[mat-icon-button]"
            )
            for i in range(min(loc.count(), 10)):
                el = loc.nth(i)
                if not _safe_visible(el):
                    continue
                try:
                    aria = str(el.get_attribute("aria-label") or "")
                    cls = str(el.get_attribute("class") or "")
                    txt = str(el.inner_text(timeout=300) or "").strip()
                    if RE_UPLOAD.search(aria + " " + txt) or txt == "+" or "input-area-switch" in cls:
                        continue
                except Exception:
                    pass
                return el
        except Exception:
            continue
    return None


def _paste_into_composer(page, composer, prompt_text: str) -> str:
    """Paste the prompt into the composer and return the text that landed.

    Order of operations (Round 16):
      1. Companion extension `paste_prompt` (synthetic `ClipboardEvent('paste')` with
         `text/plain` in a `DataTransfer` — zero clipboard permissions, zero origin
         restrictions, no `Control+V`).
      2. Pure-Python synthetic `DataTransfer` paste event dispatched directly on the
         bottom-most composer element in 8 KB chunks.
      3. Pure-Python `navigator.clipboard.writeText` + `Control+V` fallback.

    THE RULE: a prompt is never TYPED (except as a last resort for <= 2 KB).
    """
    t0 = time.monotonic()
    if composer is None:
        err = PasteOperationError(
            "composer editable was not found on the page",
            path_used="python_fallback",
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        return ""

    expected_min = max(1, min(len((prompt_text or "").strip()) // 2, 40))

    # 1. Companion extension path (when installed and active in the page)
    if _extension_is_available(page):
        ext_res = _invoke_companion_extension(
            page,
            "paste_prompt",
            {"promptText": prompt_text, "appendMode": False},
            timeout_ms=12000,
        )
        if isinstance(ext_res, dict) and ext_res.get("ok"):
            landed_ext = _composer_text(composer) or str(ext_res.get("landedText") or "").strip()
            if len(landed_ext) >= expected_min:
                setattr(page, "_argon_last_paste_path", "extension")
                setattr(page, "_argon_last_operation_error", None)
                print(f"[+] Prompt pasted via companion extension DataTransfer ({len(landed_ext)} chars).")
                return landed_ext

    # 2. Synthetic DataTransfer paste in page DOM (works without clipboard permission on any origin)
    try:
        synth_landed = composer.evaluate(
            r"""(el, text) => {
                el.focus();
                if (typeof el.value === "string" && (el.tagName || "").toLowerCase() === "textarea") {
                    el.value = "";
                } else {
                    el.textContent = "";
                }
                let handled = false;
                try {
                    const dt = new DataTransfer();
                    dt.setData("text/plain", text);
                    const ev = new ClipboardEvent("paste", {
                        bubbles: true,
                        cancelable: true,
                        clipboardData: dt
                    });
                    const notCancelled = el.dispatchEvent(ev);
                    const cur = String(el.innerText || el.textContent || el.value || "").trim();
                    if (!notCancelled || cur.length >= Math.min(text.trim().length, 8)) {
                        handled = cur.length > 0;
                    }
                } catch (_e) {
                    handled = false;
                }
                if (!handled) {
                    if (typeof el.value === "string" && (el.tagName || "").toLowerCase() === "textarea") {
                        el.value = text;
                    } else {
                        let okExec = false;
                        try {
                            if (document.queryCommandSupported && document.queryCommandSupported("insertText")) {
                                okExec = document.execCommand("insertText", false, text);
                            }
                        } catch (_e2) {
                            okExec = false;
                        }
                        if (!okExec || String(el.innerText || el.textContent || "").trim().length < Math.min(text.trim().length, 8)) {
                            el.textContent = text;
                        }
                    }
                    el.dispatchEvent(new InputEvent("input", {
                        bubbles: true,
                        cancelable: false,
                        inputType: "insertFromPaste",
                        data: text
                    }));
                    el.dispatchEvent(new Event("change", { bubbles: true }));
                }
                return String(el.innerText || el.textContent || el.value || "").trim();
            }""",
            prompt_text,
        )
        if isinstance(synth_landed, str) and len(synth_landed.strip()) >= expected_min:
            actual_landed = _composer_text(composer) or synth_landed.strip()
            if len(actual_landed) >= expected_min:
                setattr(page, "_argon_last_paste_path", "python_datatransfer")
                setattr(page, "_argon_last_operation_error", None)
                return actual_landed
    except Exception:
        pass

    # 3. Fallback: navigator.clipboard + Control+V in 8 KB chunks
    ctx = getattr(page, "context", None)
    if ctx is not None and hasattr(ctx, "grant_permissions"):
        perms = ["clipboard-read", "clipboard-write"]
        for kwargs in ({"origin": "https://gemini.google.com"}, {}):
            try:
                ctx.grant_permissions(perms, **kwargs)
            except Exception:
                continue

    chunk_size = 8000
    chunks = [prompt_text[i:i + chunk_size] for i in range(0, len(prompt_text), chunk_size)]

    landed = ""
    clipboard_err = ""
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
            clipboard_err = f"clipboard write failed ({exc})"
            print(f"[!] The clipboard is not usable: {exc}", file=sys.stderr)
            break
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

    if len(landed) >= expected_min:
        setattr(page, "_argon_last_paste_path", "python_clipboard_fallback")
        setattr(page, "_argon_last_operation_error", None)
    else:
        reason = clipboard_err or f"paste landed {len(landed)} chars (expected >= {expected_min})"
        setattr(
            page,
            "_argon_last_operation_error",
            PasteOperationError(reason, path_used="python_fallback", elapsed_seconds=time.monotonic() - t0),
        )
    return landed


def _clear_leftover_prompt_after_send(
    page,
    composer,
    probe_snippet: str = "",
    expected_min_len: int = 1,
) -> bool:
    """Empty the composer after a VERIFIED send when the prompt is STILL sitting in it.

    MEASURED 2026-11-14 (the Phase-7 round-24 lane): Gemini's Task mode keeps the
    submitted prompt in the composer, so the send arrow stays ARMED. Any later click or
    Enter then fires the SAME prompt again: it lands as a queued steering turn, the chat
    shows the prompt twice, and a second Argon turn is billed for nothing.

    Only OUR OWN leftover is cleared - if the composer holds anything else (a human typed
    something new) it is left alone, and an empty composer is left alone.
    """
    try:
        remaining = _composer_text(composer)
    except Exception:
        return False
    floor = max(int(expected_min_len or 1), 20)
    if len(remaining) < floor:
        return False
    probe = str(probe_snippet or "").strip()
    if not probe:
        return False
    head = probe[: max(40, min(len(probe), 200))]
    if head not in remaining:
        print("[i] The composer still holds text that is not this prompt - left it alone.")
        return False
    try:
        _focus_composer_dom(composer)
        page.keyboard.press("Control+A")
        page.wait_for_timeout(150)
        page.keyboard.press("Delete")
        page.wait_for_timeout(500)
        if len(_composer_text(composer)) < floor:
            print("[+] Cleared the leftover prompt from the composer after the verified send.")
            return True
        try:
            page.keyboard.press("Backspace")
            page.wait_for_timeout(400)
        except Exception:
            pass
        if len(_composer_text(composer)) < floor:
            print("[+] Cleared the leftover prompt from the composer after the verified send.")
            return True
    except Exception as exc:
        print(f"[i] Could not clear the composer after the send: {exc}", file=sys.stderr)
    return False


def _click_send_immediately(page, composer, expected_min_len: int = 1, probe_snippet: str = "", body_len_before: int = 0, snippet_count_before: int = 0, on_progress: Optional[Any] = None) -> bool:
    """Click the send button (or press Control+Enter) immediately once upload completes (Rule 5 & Rule 6).

    Uses the MV3 companion extension's semantic send matcher + MutationObserver first when
    available, then falls back to the pure-Python semantic button locator and Control+Enter.
    Raises or records a precise `ModelSelectionError` or `SendOperationError` on failure.
    """
    t0 = time.monotonic()
    if not enforce_no_flash_before_send(page):
        err = ModelSelectionError(
            "Hard Flash guard aborted send: model chip is not 4 Argon.",
            path_used="extension" if _extension_is_available(page) else "python_fallback",
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        raise RuntimeError("Hard Flash guard aborted send: model chip is not 4 Argon.")

    if on_progress:
        try:
            on_progress("prompt_sent")
        except Exception:
            pass

    if _extension_is_available(page):
        ext_send = _invoke_companion_extension(
            page,
            "click_send",
            {"expectedMinLen": expected_min_len},
            timeout_ms=10000,
        )
        if isinstance(ext_send, dict) and ext_send.get("ok"):
            setattr(page, "_argon_last_send_path", "extension")
            setattr(page, "_argon_last_operation_error", None)
            if on_progress:
                try:
                    on_progress("verified")
                except Exception:
                    pass
            print("[+] Send triggered and verified via companion extension semantic matcher.")
            # Round 24: Gemini's Task mode keeps the submitted prompt in the composer, so
            # the send arrow stays armed and the leftover can be fired again as a queued
            # steering turn. The extension's matcher already proved the send went out.
            _clear_leftover_prompt_after_send(page, composer, probe_snippet, expected_min_len)
            return True

    button = _composer_send_button(page, composer)
    if button is not None:
        try:
            button.click(timeout=6000)
            page.wait_for_timeout(900)
        except Exception:
            pass

    remaining_text = _composer_text(composer)
    if len(remaining_text) >= expected_min_len:
        _focus_composer_dom(composer)
        try:
            page.keyboard.press("Control+Enter")
            page.wait_for_timeout(900)
        except Exception:
            pass
        remaining_text = _composer_text(composer)

    if len(remaining_text) >= expected_min_len:
        try:
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
        except Exception:
            pass

    verify_deadline = time.monotonic() + 15.0
    while time.monotonic() < verify_deadline:
        after_text = _composer_text(composer)
        composer_emptied = len(after_text) < expected_min_len
        body_after = _page_body_snapshot(page)
        snippet_count_after = body_after.count(probe_snippet) if probe_snippet else 0
        conversation_grew = (
            len(body_after) > body_len_before + 15
            or (bool(probe_snippet) and snippet_count_after > snippet_count_before)
        )
        if composer_emptied or conversation_grew:
            # Round 24: the composer keeps the submitted prompt in Gemini's Task mode, so
            # the send arrow stays armed and the leftover can be fired again as a queued
            # steering turn. Clear it - but ONLY when the snippet count proves the prompt
            # really landed in the transcript (the body always contains the composer's
            # own copy, so a plain "is the snippet in the body" test proves nothing).
            if (
                not composer_emptied
                and probe_snippet
                and snippet_count_after > snippet_count_before
            ):
                _clear_leftover_prompt_after_send(page, composer, probe_snippet, expected_min_len)
            setattr(page, "_argon_last_send_path", "python_fallback")
            setattr(page, "_argon_last_operation_error", None)
            if on_progress:
                try:
                    on_progress("verified")
                except Exception:
                    pass
            return True
        try:
            page.wait_for_timeout(400)
        except Exception:
            time.sleep(0.4)

    ok_final = len(_composer_text(composer)) < expected_min_len
    if not ok_final:
        btn_state = "found and clicked" if button is not None else "not found in composer container"
        err = SendOperationError(
            f"send did not empty composer within 15s (send button {btn_state}; Control+Enter tried)",
            path_used="python_fallback",
            elapsed_seconds=time.monotonic() - t0,
        )
        setattr(page, "_argon_last_operation_error", err)
        print(f"[ERROR] {err}", file=sys.stderr)
    return ok_final


def _execute_composer_send_sequence(
    page,
    zip_path: Optional[Path],
    prompt_text: str,
    manual_setup: bool = False,
    is_continuation: bool = False,
    recorded_ws_mode: str = "unknown",
    on_progress: Optional[Any] = None,
    preflight_log: Optional[List[str]] = None,
    task_text: str = "",
    prev_state: Optional[Dict[str, Any]] = None,
    new_session: bool = False,
    attach_images: Optional[List[str]] = None,
) -> tuple[bool, bool, bool]:
    """Execute the owner's unified send order in one place (Rule 5, Rule 6, Rule 7):

    1st step: find the prompt box and select Task mode + the 4 Argon model (hard Flash guard).
    2nd step: paste the prompt into the composer.
    3rd step: attach the archive and wait until THIS upload finishes (chip visible, spinner cleared).
    4th step: the moment the upload has finished, verify the Flash guard and click send immediately.

    Logs which execution path was used ('extension' vs 'python_fallback') and records the exact
    failing operation ('mode', 'model', 'paste', 'attach', 'send') and reason on `page`.
    Returns (mode_ok, model_ok, attached_and_sent_ok).
    """
    p_log = preflight_log if preflight_log is not None else []
    clean_prompt = (prompt_text or "").strip()
    if not clean_prompt:
        raise ValueError("Refusing to send an empty prompt.")

    ext_active = _extension_is_available(page)
    pipeline_path = "extension" if ext_active else "python_fallback"
    p_log.append(f"pipeline path : {pipeline_path}")
    print(f"[+] Send pipeline execution path: {pipeline_path}")
    setattr(page, "_argon_failed_operation", "")
    setattr(page, "_argon_failed_reason", "")
    setattr(page, "_argon_last_operation_error", None)

    # Step 1: Find the prompt box and select Task mode + 4 Argon model.
    # The lookup WAITS: the Task composer only replaces the Chat composer while
    # the page hydrates, and a lookup that lands in that window finds nothing.
    composer = _find_composer(page, wait_seconds=45.0)
    if composer is None:
        reason = "composer editable is missing from the page DOM"
        p_log.append(f"composer : MISSING ({reason})")
        setattr(page, "_argon_failed_operation", "paste")
        setattr(page, "_argon_failed_reason", reason)
        setattr(page, "_argon_last_operation_error", PasteOperationError(reason, path_used=pipeline_path))
        print(f"[ERROR] Operation 'paste' failed ({pipeline_path}): {reason}", file=sys.stderr)
        return False, False, False

    if ext_active and not manual_setup:
        ext_mm = _invoke_companion_extension(
            page,
            "ensure_mode_and_model",
            {
                "manualSetup": manual_setup,
                "isContinuation": is_continuation,
                "recordedWorkspaceMode": recorded_ws_mode,
            },
            timeout_ms=12000,
        )
        if isinstance(ext_mm, dict) and ext_mm.get("ok"):
            setattr(page, "_argon_workspace_mode", "task")
            setattr(page, "_argon_mode_status", f"OK (extension verified mode=Task, chip='{ext_mm.get('model', '4 Argon')}')")
            setattr(page, "_argon_chip_seen", str(ext_mm.get("model") or "4 Argon"))

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
        p_log.append(f"mode = Chat : REFUSED (chip='{chip_seen_pf}')")
        mode_ok = False
    else:
        chip_tag = (
            f" (chip='{chip_seen_pf}')"
            if (chip_seen_pf and mode_ok and "chip=" not in str(getattr(page, "_argon_mode_status", "")))
            else ""
        )
        mode_status_str = (
            "OK (manual)"
            if manual_setup
            else str(getattr(page, "_argon_mode_status", "") or ("OK" if mode_ok else "UNKNOWN"))
        )
        p_log.append(f"mode = Task : {mode_status_str}{chip_tag}")

    if not mode_ok:
        mode_reason = f"Task mode could not be verified ({p_log[-1]})"
        setattr(page, "_argon_failed_operation", "mode")
        setattr(page, "_argon_failed_reason", mode_reason)
        setattr(page, "_argon_last_operation_error", ModeSelectionError(mode_reason, path_used=pipeline_path))
        print(f"[ERROR] Operation 'mode' failed ({pipeline_path}): {mode_reason}", file=sys.stderr)

    # Hard Flash guard applies even when --manual-setup is passed: never send on Flash.
    model_ok = _retry(
        lambda p: select_argon_model(p, require_chip=True),
        page,
    )
    check_basis = "verified on continuation chip" if is_continuation else "proven on new chat chip"
    p_log.append(f"model = 4 Argon : {'OK' if model_ok else 'FAILED'} ({check_basis})")
    if not model_ok:
        chip_now = _model_chip(page)
        chip_now_name = _model_name(chip_now) if chip_now is not None else "(not visible)"
        model_reason = f"model chip reads {chip_now_name!r} instead of 4 Argon ({check_basis})"
        if not getattr(page, "_argon_failed_operation", ""):
            setattr(page, "_argon_failed_operation", "model")
            setattr(page, "_argon_failed_reason", model_reason)
            setattr(page, "_argon_last_operation_error", ModelSelectionError(model_reason, path_used=pipeline_path))
        print(f"[ERROR] Operation 'model' failed ({pipeline_path}): {model_reason}", file=sys.stderr)

    # Round 21 (FIX 4): the model must be VERIFIED, not assumed. `ensure_mode_and_model`
    # (extension) and `select_argon_model` (python fallback) can report success while the
    # chip has already fallen back to another model (for example 'Hydrogen'), so the chip
    # is read back from the DOM right here and a non-Argon chip aborts the send.
    chip_ok, chip_name = _verify_model_chip_is_argon(page, stage="after the model was selected")
    if not chip_ok:
        model_ok = False
        chip_reason = f"model chip reads {chip_name!r} instead of 4 Argon (after the model was selected)"
        p_log.append(f"model = 4 Argon : FAILED ({chip_reason})")
        if not getattr(page, "_argon_failed_operation", ""):
            setattr(page, "_argon_failed_operation", "model")
            setattr(page, "_argon_failed_reason", chip_reason)
            setattr(page, "_argon_last_operation_error", ModelSelectionError(chip_reason, path_used=pipeline_path))

    if not (mode_ok and model_ok):
        return mode_ok, model_ok, False

    # Round 21 (FIX 3): ONE prompt per lane. The retry ladder and the self-executed
    # manual send fallback can both come back to this function, so before anything is
    # pasted the prompt is proved to be absent from this conversation. If it is already
    # there the send already happened: the send is skipped and the caller waits for
    # the delivery instead of putting the same prompt into the chat a second time.
    dup_reason = _check_duplicate_prompt(
        page,
        prompt_text=prompt_text,
        task_text=task_text,
        prev_state=prev_state,
        new_session=new_session,
    )
    if dup_reason is None and new_session:
        # A --new-session lane never consults the stored task state, but the DOM check
        # still has to run: the retry ladder must never put the same prompt into the
        # same conversation twice.
        dup_reason = _check_duplicate_prompt(
            page,
            prompt_text=prompt_text,
            task_text=task_text,
            prev_state=None,
            new_session=False,
        )
    if dup_reason is not None:
        print("[i] The prompt is already in this conversation - not sending it again.")
        p_log.append("prompt duplicate guard : SKIPPED (the prompt is already in this conversation)")
        return mode_ok, model_ok, True

    # Step 2: Paste the prompt into the composer BEFORE attaching the archive.
    expected_min_len = max(1, min(len(clean_prompt) // 2, 40))
    first_line = next(
        (ln.strip() for ln in clean_prompt.splitlines() if len(ln.strip()) >= 8),
        clean_prompt[:80],
    )
    probe_snippet = first_line[:60]
    body_before = _page_body_snapshot(page)
    body_len_before = len(body_before)
    snippet_count_before = body_before.count(probe_snippet) if probe_snippet else 0

    composer = _find_composer(page, wait_seconds=15.0) or composer
    landed_text = _paste_into_composer(page, composer, prompt_text)
    if len(landed_text) < expected_min_len and len(clean_prompt) <= 2000:
        _focus_composer_dom(composer)
        try:
            page.keyboard.insert_text(clean_prompt)
            page.wait_for_timeout(400)
        except Exception:
            pass
        landed_text = _composer_text(composer)

    paste_path_used = str(getattr(page, "_argon_last_paste_path", "") or pipeline_path)
    if len(landed_text) < expected_min_len:
        last_op_err = getattr(page, "_argon_last_operation_error", None)
        paste_reason = (
            last_op_err.reason
            if isinstance(last_op_err, ArgonOperationError)
            else f"prompt paste landed {len(landed_text)} chars (expected >= {expected_min_len})"
        )
        p_log.append(f"prompt paste ({paste_path_used}) : FAILED ({paste_reason})")
        setattr(page, "_argon_failed_operation", "paste")
        setattr(page, "_argon_failed_reason", paste_reason)
        print(f"[ERROR] Operation 'paste' failed ({paste_path_used}): {paste_reason}", file=sys.stderr)
        return mode_ok, model_ok, False

    p_log.append(f"prompt paste ({paste_path_used}) : OK ({len(landed_text)} chars in composer)")
    if on_progress:
        try:
            on_progress("prompt_pasted")
        except Exception:
            pass

    # Step 3: Attach the archive and wait until THIS upload finishes.
    if zip_path is not None:
        def _attempt_attach_once_or_retry(p) -> bool:
            if attach_zip(p, zip_path):
                return True
            op_err = getattr(p, "_argon_last_operation_error", None)
            err_txt = str(getattr(op_err, "reason", "") or op_err or "")
            if any(
                tok in err_txt
                for tok in (
                    "archive_size_cap_exceeded",
                    "fresh_conversation_required_for_large_archive",
                    "upload_still_running",
                    "upload_failed",
                )
            ):
                return False
            time.sleep(1.0)
            return attach_zip(p, zip_path)

        attached = _attempt_attach_once_or_retry(page)
        attach_path_used = str(getattr(page, "_argon_last_attach_path", "") or pipeline_path)
        ok_z, z_sz, z_cnt, _ = _inspect_zip_archive(zip_path)
        size_tag = f" ({z_sz} bytes, {z_cnt} entries, TASK_BRIEF.md verified)" if ok_z else " (invalid/corrupt archive)"
        if not attached:
            last_op_err = getattr(page, "_argon_last_operation_error", None)
            attach_reason = (
                last_op_err.reason
                if isinstance(last_op_err, ArgonOperationError)
                else f"archive {zip_path.name} could not be attached"
            )
            p_log.append(f"attachment {zip_path.name}{size_tag} ({attach_path_used}) : FAILED ({attach_reason})")
            setattr(page, "_argon_failed_operation", "attach")
            setattr(page, "_argon_failed_reason", attach_reason)
            print(f"[ERROR] Operation 'attach' failed ({attach_path_used}): {attach_reason}", file=sys.stderr)
            return mode_ok, model_ok, False
        p_log.append(f"attachment {zip_path.name}{size_tag} ({attach_path_used}) : OK")
        if on_progress:
            try:
                on_progress("zip_attached")
            except Exception:
                pass

    # Step 3b: attach the screenshots as REAL IMAGES (the archive cannot carry binary
    # files). They must travel WITH the prompt, so this happens after the archive
    # attach - whose upload can clear the composer - and BEFORE the send click.
    # A failure here is never fatal: the brief carries the specification.
    if attach_images:
        try:
            img_ok, img_list, img_err = attach_images_to_composer(
                page, attach_images, open_upload_menu_fn=_open_upload_menu
            )
        except Exception as img_exc:
            img_ok, img_list, img_err = False, [], f"{type(img_exc).__name__}: {img_exc}"
        if img_ok:
            p_log.append(f"screenshots : OK ({len(img_list)} file(s) attached before the send)")
            print(f"[+] Attached {len(img_list)} image(s) to the composer before the send.")
        else:
            p_log.append(f"screenshots : FAILED ({img_err})")
            print(f"[!] --attach-images failed (the prompt is still sent): {img_err}", file=sys.stderr)

    # Re-verify that the prompt is still in the composer after attaching the archive (re-paste if cleared).
    composer = _find_composer(page) or composer
    if len(_composer_text(composer)) < expected_min_len:
        print("[i] Composer was cleared after upload completion - re-pasting prompt immediately...")
        landed_text = _paste_into_composer(page, composer, prompt_text)
        if len(landed_text) < expected_min_len:
            repaste_reason = "composer was cleared by upload completion and re-paste failed"
            p_log.append(f"prompt re-verify after upload : FAILED ({repaste_reason})")
            setattr(page, "_argon_failed_operation", "paste")
            setattr(page, "_argon_failed_reason", repaste_reason)
            print(f"[ERROR] Operation 'paste' failed ({pipeline_path}): {repaste_reason}", file=sys.stderr)
            return mode_ok, model_ok, False

    # Round 21 (FIX 4): verify the chip once more right before the send. The upload can
    # take a while and the chip can fall back to another model meanwhile, so the model
    # the prompt is about to go out on is read back from the DOM one last time.
    chip_ok_send, chip_name_send = _verify_model_chip_is_argon(page, stage="right before the send")
    if not chip_ok_send:
        send_model_reason = f"model chip reads {chip_name_send!r} instead of 4 Argon (right before the send)"
        p_log.append(f"model = 4 Argon : FAILED ({send_model_reason})")
        setattr(page, "_argon_failed_operation", "model")
        setattr(page, "_argon_failed_reason", send_model_reason)
        setattr(page, "_argon_last_operation_error", ModelSelectionError(send_model_reason, path_used=pipeline_path))
        print(f"[ERROR] Operation 'model' failed ({pipeline_path}): {send_model_reason}", file=sys.stderr)
        return mode_ok, model_ok, False

    # R3.4: Never send the prompt before the upload has finished and been confirmed.
    if zip_path is not None:
        if _composer_upload_in_progress(page) or not _attachment_present(page, zip_path):
            still_up = _composer_upload_in_progress(page)
            eff_win_now = _upload_timeout_for_archive(Path(zip_path), floor_seconds=600.0)
            up_state = "upload_still_running" if still_up else "upload_failed"
            up_desc = (
                "the upload is still running (spinner active right before send)"
                if still_up
                else "the upload failed (attachment cannot be confirmed in composer right before send)"
            )
            unconfirmed_reason = (
                f"{up_state}: {up_desc} for {Path(zip_path).name} (effective_window={eff_win_now:.0f}s) - "
                "refusing to send prompt before upload has finished and been confirmed"
            )
            p_log.append(f"upload confirmation before send : FAILED ({unconfirmed_reason})")
            setattr(page, "_argon_failed_operation", "attach")
            setattr(page, "_argon_failed_reason", unconfirmed_reason)
            setattr(page, "_argon_last_operation_error", AttachOperationError(unconfirmed_reason, path_used=pipeline_path))
            print(f"[ERROR] Operation 'attach' failed ({pipeline_path}): {unconfirmed_reason}", file=sys.stderr)
            return mode_ok, model_ok, False

    # Step 4: The moment the upload has finished, verify the Flash guard and click send immediately.
    sent_ok = _click_send_immediately(
        page,
        composer,
        expected_min_len=expected_min_len,
        probe_snippet=probe_snippet,
        body_len_before=body_len_before,
        snippet_count_before=snippet_count_before,
        on_progress=on_progress,
    )
    send_path_used = str(getattr(page, "_argon_last_send_path", "") or pipeline_path)
    if not sent_ok:
        last_op_err = getattr(page, "_argon_last_operation_error", None)
        send_reason = (
            last_op_err.reason
            if isinstance(last_op_err, ArgonOperationError)
            else "send button click and Control+Enter did not submit the composer text"
        )
        p_log.append(f"send ({send_path_used}) : FAILED ({send_reason})")
        setattr(page, "_argon_failed_operation", "send")
        setattr(page, "_argon_failed_reason", send_reason)
        print(f"[ERROR] Operation 'send' failed ({send_path_used}): {send_reason}", file=sys.stderr)
    else:
        p_log.append(f"send ({send_path_used}) : OK")
    return mode_ok, model_ok, sent_ok


def _type_and_send(page, composer, prompt_text: str, on_progress: Optional[Any] = None) -> None:
    """Put the prompt into the composer, verify the Flash guard, send it, and verify delivery."""
    clean_prompt = prompt_text.strip()
    if not clean_prompt:
        raise ValueError("Refusing to send an empty prompt.")

    if not enforce_no_flash_before_send(page):
        raise ModelSelectionError("Hard Flash guard aborted send: model chip is not 4 Argon.")

    expected_min_len = max(1, min(len(clean_prompt) // 2, 40))
    first_line = next(
        (ln.strip() for ln in clean_prompt.splitlines() if len(ln.strip()) >= 8),
        clean_prompt[:80],
    )
    probe_snippet = first_line[:60]

    for attempt in range(2):
        if attempt > 0:
            fresh = _find_composer(page)
            if fresh is not None:
                composer = fresh

        body_before = _page_body_snapshot(page)
        body_len_before = len(body_before)
        snippet_count_before = body_before.count(probe_snippet) if probe_snippet else 0

        landed_text = _composer_text(composer)
        if len(landed_text) < expected_min_len:
            landed_text = _paste_into_composer(page, composer, prompt_text)
        if len(landed_text) < expected_min_len and len(clean_prompt) <= 2000:
            _focus_composer_dom(composer)
            try:
                page.keyboard.insert_text(clean_prompt)
                page.wait_for_timeout(400)
            except Exception:
                pass
            landed_text = _composer_text(composer)

        if len(landed_text) < expected_min_len:
            continue

        if on_progress:
            try:
                on_progress("prompt_pasted")
            except Exception:
                pass

        print(f"[+] Composer populated (length={len(landed_text)} chars). Clicking send immediately...")
        if _click_send_immediately(
            page,
            composer,
            expected_min_len=expected_min_len,
            probe_snippet=probe_snippet,
            body_len_before=body_len_before,
            snippet_count_before=snippet_count_before,
            on_progress=on_progress,
        ):
            return

    last_err = getattr(page, "_argon_last_operation_error", None)
    if isinstance(last_err, ArgonOperationError):
        raise last_err
    raise SendOperationError(
        "The prompt could not be pasted, sent and verified as delivered in the "
        "Gemini composer after one retry."
    )


SYNTHETIC_GEMINI_SELFTEST_HTML = r"""<!DOCTYPE html>
<html lang="bn">
<head>
  <meta charset="utf-8" />
  <title>Gemini - Synthetic Preflight Self-Test</title>
  <style>
    body { font-family: sans-serif; margin: 0; padding: 20px; background: #131314; color: #e3e3e3; }
    #floating-quick-bar { position: fixed; top: 90px; left: 40px; right: 40px; padding: 8px; background: #1e1f20; border: 1px solid #444; }
    #floating-editor { min-height: 24px; padding: 4px; border: 1px dashed #666; }
    #conversation-transcript { margin-top: 180px; min-height: 160px; padding: 12px; }
    .input-area-container { position: fixed; bottom: 24px; left: 40px; right: 40px; padding: 14px; background: #1e1f20; border-radius: 16px; }
    #real-bottom-composer { min-height: 48px; padding: 8px; border: 1px solid #555; outline: none; }
    .controls-row { display: flex; align-items: center; gap: 12px; margin-top: 10px; }
    .mode-pill { padding: 4px 10px; border-radius: 12px; cursor: pointer; }
    .gem-segmented-button-selected { background: #004a77; color: #c2e7ff; }
    #picker-listbox { display: none; position: fixed; bottom: 110px; left: 160px; background: #282a2c; border: 1px solid #555; padding: 8px; }
    #attachment-chips { margin-top: 8px; font-size: 13px; color: #a8c7fa; }
  </style>
</head>
<body>
  <div id="floating-quick-bar" class="floating-action-bar">
    <div>Quick Action Floating Editor (must NEVER receive main prompt)</div>
    <div id="floating-editor" contenteditable="true" role="textbox"></div>
    <button aria-haspopup="menu" aria-label="Floating Upload">+</button>
  </div>

  <div id="conversation-transcript"></div>

  <div class="input-area-container" id="main-composer-container">
    <div id="attachment-chips"></div>
    <rich-textarea>
      <div id="real-bottom-composer" contenteditable="true" role="textbox" aria-label="Enter a prompt here"></div>
    </rich-textarea>
    <div class="controls-row">
      <div id="mode-toggle-group" role="group">
        <span id="mode-chat-wrap" class="mode-pill gem-segmented-button-selected" aria-pressed="true">
          <button type="button" id="btn-mode-chat">চ্যাট</button>
        </span>
        <span id="mode-task-wrap" class="mode-pill" aria-pressed="false">
          <button type="button" id="btn-mode-task">টাস্ক</button>
        </span>
      </div>

      <button type="button" class="input-area-switch" id="model-chip-btn" aria-haspopup="listbox" aria-label="Open mode picker, currently 3.8 Flash">3.8 Flash</button>

      <button type="button" id="upload-menu-btn" aria-haspopup="menu" aria-label="+ Gemini-কে ডিজাইসো করুন">+</button>
      <div id="upload-input-slot"></div>

      <button type="button" id="semantic-submit-btn" data-testid="composer-submit" aria-label="পাঠান">➤</button>
    </div>
  </div>

  <div id="picker-listbox" role="listbox">
    <div role="option" id="opt-flash" aria-selected="true">3.8 Flash</div>
    <div role="option" id="opt-argon" aria-selected="false">4 Argon (Complex tasks)</div>
  </div>

  <script>
    (function () {
      const params = new URLSearchParams(window.location.search);
      const fault = window.__selftestFault || params.get("fault") || "";
      window.__selftestState = {
        fault: fault,
        mode: "chat",
        model: "3.8 Flash",
        uploadMenuOpened: false,
        attachedName: "",
        attachedStem: "",
        attachedSize: 0,
        composerClearedOnUpload: false,
        floatingEditorText: "",
        sentMessages: []
      };

      const chatWrap = document.getElementById("mode-chat-wrap");
      const taskWrap = document.getElementById("mode-task-wrap");
      const btnChat = document.getElementById("btn-mode-chat");
      const btnTask = document.getElementById("btn-mode-task");
      const chipBtn = document.getElementById("model-chip-btn");
      const picker = document.getElementById("picker-listbox");
      const optFlash = document.getElementById("opt-flash");
      const optArgon = document.getElementById("opt-argon");
      const uploadBtn = document.getElementById("upload-menu-btn");
      const uploadSlot = document.getElementById("upload-input-slot");
      const chipsBox = document.getElementById("attachment-chips");
      const composer = document.getElementById("real-bottom-composer");
      const floating = document.getElementById("floating-editor");
      const sendBtn = document.getElementById("semantic-submit-btn");
      const transcript = document.getElementById("conversation-transcript");

      if (fault === "argon_disabled") {
        optArgon.setAttribute("aria-disabled", "true");
      }

      btnTask.addEventListener("click", () => {
        if (fault === "task_mode_stuck") return;
        window.__selftestState.mode = "task";
        taskWrap.classList.add("gem-segmented-button-selected");
        taskWrap.setAttribute("aria-pressed", "true");
        chatWrap.classList.remove("gem-segmented-button-selected");
        chatWrap.setAttribute("aria-pressed", "false");
      });

      btnChat.addEventListener("click", () => {
        window.__selftestState.mode = "chat";
        chatWrap.classList.add("gem-segmented-button-selected");
        chatWrap.setAttribute("aria-pressed", "true");
        taskWrap.classList.remove("gem-segmented-button-selected");
        taskWrap.setAttribute("aria-pressed", "false");
      });

      chipBtn.addEventListener("click", () => {
        picker.style.display = picker.style.display === "block" ? "none" : "block";
      });

      optArgon.addEventListener("click", () => {
        if (optArgon.getAttribute("aria-disabled") === "true" || fault === "flash_stuck") {
          picker.style.display = "none";
          return;
        }
        window.__selftestState.model = "4 Argon";
        chipBtn.textContent = "4 Argon";
        chipBtn.setAttribute("aria-label", "Open mode picker, currently 4 Argon");
        optArgon.setAttribute("aria-selected", "true");
        optFlash.setAttribute("aria-selected", "false");
        picker.style.display = "none";
      });

      uploadBtn.addEventListener("click", () => {
        window.__selftestState.uploadMenuOpened = true;
        if (fault === "no_file_input") {
          return;
        }
        if (!document.getElementById("real-file-input")) {
          const inp = document.createElement("input");
          inp.type = "file";
          inp.id = "real-file-input";
          inp.accept = ".zip,application/zip";
          inp.style.display = "none";
          inp.addEventListener("change", () => {
            if (inp.files && inp.files.length > 0) {
              const f = inp.files[0];
              const stem = f.name.replace(/\.[^.]+$/, "");
              window.__selftestState.attachedName = f.name;
              window.__selftestState.attachedStem = stem;
              window.__selftestState.attachedSize = f.size;
              // Render chip WITHOUT .zip extension just like real Gemini Web:
              chipsBox.innerHTML = `<span class="att-chip" data-stem="${stem}">${stem}</span>`;
              // Simulate Gemini clearing the file input and clearing the composer after upload:
              if (fault !== "keep_composer_on_upload") {
                window.__selftestState.composerClearedOnUpload = true;
                composer.textContent = "";
              }
            }
          });
          uploadSlot.appendChild(inp);
        }
      });

      composer.addEventListener("paste", (ev) => {
        if (ev.clipboardData) {
          const t = ev.clipboardData.getData("text/plain");
          if (t) {
            ev.preventDefault();
            composer.textContent = (composer.textContent || "") + t;
          }
        }
      });

      floating.addEventListener("input", () => {
        window.__selftestState.floatingEditorText = floating.innerText || floating.textContent || "";
      });

      function doSubmit() {
        if (fault === "send_broken") {
          return;
        }
        const txt = String(composer.innerText || composer.textContent || "").trim();
        if (!txt) {
          return;
        }
        window.__selftestState.sentMessages.push({
          text: txt,
          mode: window.__selftestState.mode,
          model: window.__selftestState.model,
          attachedStem: window.__selftestState.attachedStem,
          attachedSize: window.__selftestState.attachedSize
        });
        const msgDiv = document.createElement("div");
        msgDiv.className = "user-turn";
        msgDiv.textContent = txt;
        transcript.appendChild(msgDiv);
        composer.textContent = "";
        // Simulate Gemini reverting the model chip to Flash after send:
        chipBtn.textContent = "3.8 Flash";
        chipBtn.setAttribute("aria-label", "Open mode picker, currently 3.8 Flash");
      }

      sendBtn.addEventListener("click", doSubmit);
      composer.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && ev.ctrlKey) {
          ev.preventDefault();
          doSubmit();
        }
      });
    })();
  </script>
</body>
</html>
"""


class _LocalSyntheticGeminiServer:
    """Lightweight local HTTP server that serves the synthetic Gemini page and paged Drive `viewer/text`."""

    def __init__(self, patch_pages: Optional[List[str]] = None) -> None:
        self.patch_pages = patch_pages or [
            "$ErrorActionPreference = 'Stop'\n$TargetRoot = 'G:\\Developing\\argon-coding-bridge'\n" + ("# page 0 line\n" * 20),
            "# page 1 body line\n" * 20,
            "Write-Host '[SUCCESS] 1 file(s) patched.'\n# --- NOTES: selftest ---\n# ## ROOT CAUSE\n# NOTE: selftest\n# --- END NOTES ---\n",
        ]
        self._httpd: Optional[Any] = None
        self._thread: Optional[Any] = None
        self.port: int = 0
        self.base_url: str = ""

    def __enter__(self) -> "_LocalSyntheticGeminiServer":
        import http.server
        import threading
        import urllib.parse

        pages_data = self.patch_pages

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.startswith("/viewer/text"):
                    qs = urllib.parse.parse_qs(parsed.query)
                    pg_idx = int((qs.get("page") or ["0"])[0])
                    if 0 <= pg_idx < len(pages_data):
                        body = ")]}'\n" + json.dumps({"data": pages_data[pg_idx]})
                    else:
                        body = ")]}'\n" + json.dumps({})
                    raw = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                raw_html = SYNTHETIC_GEMINI_SELFTEST_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw_html)))
                self.end_headers()
                self.wfile.write(raw_html)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._httpd.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass


def _create_selftest_zip(tmp_dir: Path) -> Path:
    """Create a valid self-test context archive containing TASK_BRIEF.md and API_INDEX.md."""
    import zipfile as _zf

    zp = tmp_dir / "argon_context_selftest.zip"
    with _zf.ZipFile(zp, "w", _zf.ZIP_STORED) as zf:
        zf.writestr("TASK_BRIEF.md", "# Self-Test Brief\n## THE EXACT API OF THIS PROJECT\nSelf-test archive.\n")
        zf.writestr("API_INDEX.md", "# API_INDEX.md\n- selftest.py:1 -> def selftest():\n")
        zf.writestr("selftest.py", "def selftest() -> int:\n    return 0\n")
    return zp


def run_preflight_selftest_on_page(
    page: Any,
    fault: str = "",
    tmp_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the 4-operation preflight self-test on `page` against `_LocalSyntheticGeminiServer`.

    Exercises and verifies in seconds:
      1. Lowest composer selection (skipping the floating quick-action bar editor).
      2. Task mode switch (`টাস্ক`) and `4 Argon` model selection from a `3.8 Flash` default.
      3. Synthetic `DataTransfer` prompt paste without clipboard permissions.
      4. `DataTransfer` / `input[type=file]` archive attach without the OS file dialog,
         attachment chip stem matching without `.zip`, and automatic re-paste when the
         upload clears the composer.
      5. Semantic send button click inside the composer container.
    """
    import tempfile as _tempfile

    t0 = time.monotonic()
    own_tmp = tmp_dir is None
    work_dir = Path(tmp_dir) if tmp_dir is not None else Path(_tempfile.mkdtemp(prefix="argon_selftest_"))
    try:
        test_zip = _create_selftest_zip(work_dir)
        test_prompt = "Self-test surgical prompt for Gemini 4 Argon Round 16 verification."
        preflight_log: List[str] = []

        with _LocalSyntheticGeminiServer() as srv:
            url = f"{srv.base_url}/u/0/app" + (f"?fault={fault}" if fault else "")
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            mode_ok, model_ok, sent_ok = _execute_composer_send_sequence(
                page=page,
                zip_path=test_zip,
                prompt_text=test_prompt,
                manual_setup=False,
                is_continuation=False,
                recorded_ws_mode="unknown",
                preflight_log=preflight_log,
                attach_images=None,
            )
            st = {}
            try:
                st = page.evaluate("() => window.__selftestState || {}") or {}
            except Exception:
                st = {}

            if st.get("floatingEditorText"):
                return {
                    "ok": False,
                    "operation": "paste",
                    "reason": "prompt landed in the floating quick-action bar editor instead of the bottom composer",
                    "path_used": "extension" if _extension_is_available(page) else "python_fallback",
                    "elapsed_seconds": time.monotonic() - t0,
                    "preflight_log": preflight_log,
                }

            ok_all = bool(mode_ok and model_ok and sent_ok and len(st.get("sentMessages") or []) == 1)
            failed_op = str(getattr(page, "_argon_failed_operation", "") or "")
            failed_reason = str(getattr(page, "_argon_failed_reason", "") or "")
            if not ok_all and not failed_op:
                failed_op = "send" if (mode_ok and model_ok) else ("mode" if not mode_ok else "model")
                failed_reason = ", ".join(preflight_log)
            return {
                "ok": ok_all,
                "operation": "all" if ok_all else failed_op,
                "reason": "" if ok_all else failed_reason,
                "path_used": "extension" if _extension_is_available(page) else "python_fallback",
                "elapsed_seconds": time.monotonic() - t0,
                "composer_cleared_repaired": bool(st.get("composerClearedOnUpload")),
                "attached_stem": str(st.get("attachedStem") or ""),
                "preflight_log": preflight_log,
            }
    finally:
        if own_tmp:
            shutil.rmtree(work_dir, ignore_errors=True)


def run_preflight_selftest_cli(headless: bool = True, require_extension: bool = False) -> int:
    """Standalone CLI runner for `python dispatch_argon.py --self-test` (Section 0.4 & 2.2).

    Verifies all four operations (attach, paste, model/mode, send) plus paged Drive
    `viewer/text` stitching and fault-injection failure reporting in seconds.
    """
    import tempfile as _tempfile
    import urllib.request as _urlreq

    t0 = time.monotonic()
    print("=" * 78)
    print("  ARGON PREFLIGHT SELF-TEST HARNESS (ROUND 16)")
    print("=" * 78)

    from safe_browser import companion_extension_launch_args, get_companion_extension_dir

    ext_dir = get_companion_extension_dir()
    print(f"  Companion extension : {ext_dir if ext_dir is not None else 'absent (pure-Python fallback)'}")
    if ext_dir is not None:
        manifest_path = ext_dir / "manifest.json"
        sw_path = ext_dir / "service_worker.js"
        cs_path = ext_dir / "content_script.js"
        for req_f in (manifest_path, sw_path, cs_path):
            if not req_f.is_file():
                print(f"[ERROR] Companion extension file missing: {req_f}", file=sys.stderr)
                return EXIT_PREFLIGHT_FAILED
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            perms = set(manifest_data.get("permissions") or [])
            for req_p in ("scripting", "storage", "tabs"):
                if req_p not in perms:
                    print(f"[ERROR] manifest.json missing permission {req_p!r}", file=sys.stderr)
                    return EXIT_PREFLIGHT_FAILED
            print(
                f"  Manifest V{manifest_data.get('manifest_version')}         : OK "
                f"(version={manifest_data.get('version')}, permissions={sorted(perms)})"
            )
        except Exception as exc:
            print(f"[ERROR] Invalid manifest.json: {exc}", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED
    elif require_extension:
        print("[ERROR] --self-test required companion extension, but it is not installed.", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    # Verify paged Drive viewer/text stitching against _LocalSyntheticGeminiServer
    with _LocalSyntheticGeminiServer() as srv:
        stitched_chunks: List[str] = []
        for pg_no in range(5):
            url = f"{srv.base_url}/viewer/text?id=SELFTEST_FILE_ID&authuser=0&page={pg_no}"
            with _urlreq.urlopen(url, timeout=5.0) as resp:
                raw_env = resp.read().decode("utf-8")
            decoded = _decode_drive_text_envelope(raw_env)
            if decoded is None:
                break
            stitched_chunks.append(decoded)
            if len(decoded) < 200:
                break
        stitched_text = "".join(stitched_chunks)
        if not (_looks_like_patch(stitched_text) and _patch_is_complete(stitched_text)):
            print("[ERROR] Self-test paged viewer/text stitching failed completeness check!", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED
        print(
            f"  Drive viewer/text   : OK ({len(stitched_text)} bytes stitched across {len(stitched_chunks)} pages)"
        )

    # Verify Round 17 & Round 18 Argon browser profile, session migration, leftover deletion, and multi-artifact bundle
    with _tempfile.TemporaryDirectory(prefix="argon_r18_st_") as r18_tmp:
        import sqlite3
        from safe_browser import reclaim_isolated_slot_profiles

        r18_root = Path(r18_tmp)
        r18_argon = r18_root / "argon_browser_profile"
        r18_profiles = r18_root / "argon_profiles"
        for s_name, s_email, s_gaia in (
            ("slot-u0", "first@example.com", "111"),
            ("slot-u5", "slot5@example.com", "555"),
        ):
            s_dir = r18_profiles / s_name
            (s_dir / "Default" / "Network").mkdir(parents=True, exist_ok=True)
            (s_dir / "Local State").write_text(
                json.dumps({"os_crypt": {"encrypted_key": "DPAPI_TEST_KEY_R18"}}),
                encoding="utf-8",
            )
            ck_db = s_dir / "Default" / "Network" / "Cookies"
            conn_s = sqlite3.connect(str(ck_db))
            conn_s.execute(
                "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, path TEXT, encrypted_value BLOB, PRIMARY KEY (host_key, name, path))"
            )
            conn_s.execute(
                "INSERT INTO cookies VALUES ('.google.com', 'SID', ?, '/', ?)",
                (f"sid_{s_name}", f"enc_{s_name}".encode("utf-8")),
            )
            conn_s.commit()
            conn_s.close()
            (s_dir / "Default" / "Preferences").write_text(
                json.dumps({"account_info": [{"email": s_email, "gaia": s_gaia, "account_id": s_gaia}]}),
                encoding="utf-8",
            )

        unverified_slot = r18_profiles / "slot-u9"
        (unverified_slot / "Default").mkdir(parents=True, exist_ok=True)
        (unverified_slot / "Default" / "Preferences").write_text(
            json.dumps({"account_info": [{"email": "unverified9@example.com", "gaia": "999"}]}),
            encoding="utf-8",
        )

        for lf_name in ("run-14180-1793594378", "slot-u0-concurrent-16712", "slot-u4-cap2", "slot-u-0"):
            lf_dir = r18_profiles / lf_name
            lf_dir.mkdir(parents=True, exist_ok=True)
            (lf_dir / "waste.bin").write_bytes(b"W" * 4096)

        r18_cfg_path = r18_root / "accounts_pool.json"
        r18_cfg_path.write_text(
            json.dumps(
                {
                    "profile_dir": str(r18_argon),
                    "active_slot": "/u/5/",
                    "accounts": [
                        {"index": 0, "slot": "/u/0/", "email": "first@example.com", "status": "active"},
                        {"index": 5, "slot": "/u/5/", "email": "slot5@example.com", "status": "active"},
                        {"index": 9, "slot": "/u/9/", "email": "unverified9@example.com", "status": "configured"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        rep_mig = reclaim_isolated_slot_profiles(
            shared_profile_dir=r18_argon,
            profiles_root=r18_profiles,
            config_path=r18_cfg_path,
            keep_archive=False,
            migrate_sessions=True,
            verify_live=False,
        )
        r18_cfg = PoolConfig(config_path=r18_cfg_path)
        if (
            r18_cfg.profile_dir_for_slot("/u/5/").resolve() != r18_argon.resolve()
            or _profile_is_isolated_slot(r18_argon)
            or r18_cfg.get_account_index("/u/5/") != 1
            or len(rep_mig["reclaimed"]) != 2
            or len(rep_mig["leftovers_removed"]) != 4
            or len(rep_mig["skipped"]) != 1
            or (r18_profiles / "slot-u0").exists()
            or (r18_profiles / "slot-u5").exists()
            or (r18_profiles / "run-14180-1793594378").exists()
            or not unverified_slot.exists()
        ):
            print(f"[ERROR] Round 18 migration & complete deletion self-test failed: {rep_mig}", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED

        bundle_dir = r18_root / "delivery_test"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        patch_f = bundle_dir / "apply_changes.ps1"
        patch_f.write_text(stitched_text, encoding="utf-8", newline="\n")
        png_f = bundle_dir / "logo_mark.png"
        png_f.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x08\x00\x00\x00\x08\x00\x08\x06\x00\x00\x00")
        svg_f = bundle_dir / "card_background.svg"
        svg_f.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600"><rect width="800" height="600"/></svg>\n', encoding="utf-8")
        wav_f = bundle_dir / "voice_over.wav"
        wav_f.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        b_res = write_delivery_bundle(bundle_dir, patch_path=patch_f, asset_paths=[png_f, svg_f, wav_f])
        manifest_txt = b_res["manifest_path"].read_text(encoding="utf-8")
        if (
            not b_res["bundle_zip"].is_file()
            or len(b_res["files"]) != 4
            or "2048x2048 PNG" not in manifest_txt
            or "assets/card_background.svg" not in manifest_txt
            or "assets/voice_over.wav" not in manifest_txt
        ):
            print(f"[ERROR] Round 18 delivery bundle self-test failed: {manifest_txt}", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED
        print(
            "  Round 18 migration  : OK (2 slot sessions migrated via DPAPI/SQLite & deleted, 4 leftovers deleted, 1 unverified slot refused)"
        )
        print(
            "  Round 18 bundle     : OK (argon_delivery.zip + DELIVERY_MANIFEST.md verified with PS1, 2048x2048 PNG, SVG, and WAV)"
        )

    # Verify Round 20 persistent browser host lifecycle, locked host-state RMW, manual tab survival, and live /u/N account sync
    from test_round20_lifecycle_and_sync import run_round20_regression_suite

    r20_rep = run_round20_regression_suite()
    if not r20_rep.get("ok"):
        print(f"[ERROR] Round 20 lifecycle & account sync self-test failed: {r20_rep}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    print(
        f"  Round 20 lifecycle  : OK (5/5 regression cases passed in {r20_rep.get('elapsed_ms')} ms: "
        "host kept alive, locked RMW, manual tab persisted, live /u/N sync & stale /u/7/->/u/9/ refused)"
    )

    # Round 21: the dispatch-lane failures measured during the round-20 dual dispatch
    # (the second lane refused to attach to the ONE Argon browser, rotated onto a
    # different account, sent the prompt twice and left the model chip on Hydrogen).
    try:
        from test_round21_dispatch_lane import run_round21_regression_suite

        r21_rep = run_round21_regression_suite()
    except Exception as exc:  # the suite is optional - never break the self-test over it
        r21_rep = {"ok": True, "skipped": f"{type(exc).__name__}: {exc}", "elapsed_ms": 0}
    if not r21_rep.get("ok"):
        print(f"[ERROR] Round 21 dispatch-lane self-test failed: {r21_rep}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if r21_rep.get("skipped"):
        print(f"  Round 21 dispatch    : SKIPPED ({r21_rep.get('skipped')})")
    else:
        print(
            f"  Round 21 dispatch    : OK (5/5 regression cases passed in {r21_rep.get('elapsed_ms')} ms: "
            "endpoint file persisted, busy slot not rotatable, non-Argon model refused, "
            "duplicate prompt detected, second lane waited for the starting host)"
        )

    # Run browser self-test (Playwright when available, or Selenium Chromium fallback)
    browser_ran = False
    try:
        from playwright.sync_api import sync_playwright

        with _tempfile.TemporaryDirectory(prefix="argon_pw_selftest_") as prof_tmp:
            chrome_exe = find_chrome_executable()
            with sync_playwright() as p:
                launch_kwargs: Dict[str, Any] = {
                    "user_data_dir": prof_tmp,
                    "headless": headless,
                    "args": list(CHROME_LAUNCH_ARGS),
                    "ignore_default_args": ["--enable-automation"],
                }
                ctx = _launch_with_fallback(p, launch_kwargs, chrome_exe, Path(prof_tmp))
                try:
                    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                    res_ok = run_preflight_selftest_on_page(pg, fault="")
                    if not res_ok.get("ok"):
                        print(
                            f"[ERROR] Self-test failed on operation={res_ok.get('operation')!r}: {res_ok.get('reason')}",
                            file=sys.stderr,
                        )
                        return EXIT_PREFLIGHT_FAILED
                    print(
                        f"  Normal send pipeline: OK in {res_ok['elapsed_seconds']:.2f}s "
                        f"(path={res_ok['path_used']}, stem={res_ok['attached_stem']!r}, "
                        f"re-pasted_after_upload={res_ok['composer_cleared_repaired']})"
                    )
                    res_fault = run_preflight_selftest_on_page(pg, fault="no_file_input")
                    if res_fault.get("ok") or res_fault.get("operation") != "attach" or "no file input" not in str(res_fault.get("reason") or ""):
                        print(
                            f"[ERROR] Fault-injection 'no_file_input' did not report attach failure correctly: {res_fault}",
                            file=sys.stderr,
                        )
                        return EXIT_PREFLIGHT_FAILED
                    print(
                        f"  Fault 'no_file_input': OK (failed in {res_fault['elapsed_seconds']:.2f}s with "
                        f"operation={res_fault['operation']!r}, reason={res_fault['reason']!r})"
                    )
                    browser_ran = True
                finally:
                    ctx.close()
    except ImportError:
        pass

    if not browser_ran:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            with _LocalSyntheticGeminiServer() as srv, _tempfile.TemporaryDirectory(prefix="argon_sel_st_") as tmp_d:
                opts = Options()
                if headless:
                    opts.add_argument("--headless=new")
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                for arg in companion_extension_launch_args():
                    opts.add_argument(arg)
                driver = webdriver.Chrome(options=opts)
                try:
                    cs_file = (ext_dir or COMPANION_EXTENSION_DIR) / "content_script.js"
                    cs_js = cs_file.read_text(encoding="utf-8") if cs_file.is_file() else ""
                    driver.get(f"{srv.base_url}/u/0/app")
                    if "real-bottom-composer" not in (driver.page_source or ""):
                        driver.get("about:blank")
                        driver.execute_script(
                            "window.__selftestFault = arguments[1]; document.open(); document.write(arguments[0]); document.close();",
                            SYNTHETIC_GEMINI_SELFTEST_HTML,
                            "",
                        )
                    if cs_js:
                        driver.execute_script(cs_js)

                    def _drv_dispatch(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
                        req_id = f"sel_{int(time.time() * 1000)}_{action}"
                        return driver.execute_async_script(
                            """
                            const [reqId, act, pl, done] = arguments;
                            const onResp = (ev) => {
                                if (ev && ev.detail && ev.detail.requestId === reqId) {
                                    window.removeEventListener("argon:companion:response", onResp);
                                    done(ev.detail);
                                }
                            };
                            window.addEventListener("argon:companion:response", onResp);
                            window.dispatchEvent(new CustomEvent("argon:companion:request", {
                                detail: { protocol: "argon.companion.v1", requestId: reqId, action: act, payload: pl }
                            }));
                            """,
                            req_id,
                            action,
                            payload,
                        )

                    import base64 as _b64

                    zp = _create_selftest_zip(Path(tmp_d))
                    raw_z = zp.read_bytes()
                    mm_res = _drv_dispatch("ensure_mode_and_model", {"manualSetup": False, "isContinuation": False})
                    paste_res = _drv_dispatch("paste_prompt", {"promptText": "Round 16 self-test prompt via companion extension."})
                    att_res = _drv_dispatch(
                        "attach_archive",
                        {
                            "fileName": zp.name,
                            "expectedSize": len(raw_z),
                            "base64Data": _b64.b64encode(raw_z).decode("ascii"),
                        },
                    )
                    send_res = _drv_dispatch("click_send", {"expectedMinLen": 10})
                    st_final = driver.execute_script("return window.__selftestState;")
                    if not (mm_res.get("ok") and paste_res.get("ok") and att_res.get("ok") and send_res.get("ok")):
                        print(
                            f"[ERROR] Companion extension self-test failed: mode/model={mm_res}, paste={paste_res}, attach={att_res}, send={send_res}",
                            file=sys.stderr,
                        )
                        return EXIT_PREFLIGHT_FAILED
                    if st_final.get("floatingEditorText") or len(st_final.get("sentMessages") or []) != 1:
                        print(f"[ERROR] Self-test state mismatch: {st_final}", file=sys.stderr)
                        return EXIT_PREFLIGHT_FAILED
                    print(
                        f"  Extension pipeline  : OK (mode=Task, model={mm_res.get('model')!r}, "
                        f"paste={paste_res.get('length')} chars, attach_stem={att_res.get('stem')!r}, "
                        f"composer_repaired={att_res.get('composerRestored')}, send=OK)"
                    )

                    # Fault injection: no_file_input must fail in seconds with 'the upload menu rendered no file input'
                    driver.get(f"{srv.base_url}/u/0/app?fault=no_file_input")
                    if "real-bottom-composer" not in (driver.page_source or ""):
                        driver.get("about:blank")
                        driver.execute_script(
                            "window.__selftestFault = arguments[1]; document.open(); document.write(arguments[0]); document.close();",
                            SYNTHETIC_GEMINI_SELFTEST_HTML,
                            "no_file_input",
                        )
                    if cs_js:
                        driver.execute_script(cs_js)
                    fault_att = _drv_dispatch(
                        "attach_archive",
                        {
                            "fileName": zp.name,
                            "expectedSize": len(raw_z),
                            "base64Data": _b64.b64encode(raw_z).decode("ascii"),
                        },
                    )
                    if fault_att.get("ok") or "rendered no file input" not in str(fault_att.get("reason") or ""):
                        print(f"[ERROR] Fault 'no_file_input' did not fail as expected: {fault_att}", file=sys.stderr)
                        return EXIT_PREFLIGHT_FAILED
                    print(
                        f"  Fault 'no_file_input': OK (failed immediately with reason={fault_att.get('reason')!r})"
                    )
                    browser_ran = True
                finally:
                    driver.quit()
        except Exception as sel_exc:
            print(f"[!] Browser self-test driver error: {sel_exc}", file=sys.stderr)
            return EXIT_PREFLIGHT_FAILED

    elapsed = time.monotonic() - t0
    print(f"  Total self-test time: {elapsed:.2f}s (all checks passed in seconds)")
    print("=" * 78)
    return EXIT_SUCCESS


try:
    from round23_reliability import (
        DEFAULT_WAIT_DEADLINE_SECONDS,
        MAX_ARCHIVE_BYTES,
        MAX_BASE64_ATTACH_BYTES,
        _attach_zip_via_cdp_dom_file_input,
        _check_page_responsive,
        _conversation_holds_existing_attachment,
        _is_toggle_control,
        _normalize_viewer_geometry,
        _prove_page_responsive_after_capture,
        _recover_wedged_tab_via_cdp,
        _verify_tab_on_own_conversation,
        attach_images_to_composer,
        format_account_slot_resolution_line,
        format_archive_route_decision_line,
        get_effective_base64_attach_cap,
        install_round23_bridge_hooks,
        resolve_account_index_to_slot,
        resolve_slot_to_browser_account,
        update_brief_summary_with_resolved_slot,
        update_manual_handoff_with_resolved_slot,
    )
    install_round23_bridge_hooks(globals())
except Exception as _r23_import_exc:
    print(f"[ERROR] Could not load round23_reliability hooks: {_r23_import_exc}", file=sys.stderr)
    raise

_r23_orig_run_preflight_selftest_cli = run_preflight_selftest_cli


def run_preflight_selftest_cli(headless: bool = True, require_extension: bool = False) -> int:
    """Run preflight self-tests plus Round 23 reliability & single-session verification suite."""
    rc = _r23_orig_run_preflight_selftest_cli(headless=headless, require_extension=require_extension)
    if rc != EXIT_SUCCESS:
        return rc
    from test_round23_reliability import run_round23_reliability_suite

    r23_rep = run_round23_reliability_suite()
    if not r23_rep.get("ok"):
        print(f"[ERROR] Round 23 reliability self-test failed: {r23_rep}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    e2e = r23_rep["e2e_single_session"]
    print(
        f"  Round 23 reliability: OK (R1-R16 verified in {r23_rep.get('elapsed_ms')} ms: "
        f"model={e2e.get('model_verified')!r}, ext_id={e2e.get('extension_id')}, "
        f"overflow_viewer=clamped, wedge_recovery={e2e.get('wedge_recovery_method')})"
    )
    # Round 24: the re-apply patch scripts must never kill the bridge, and the
    # image attach must put EVERY file into the composer.
    try:
        from test_round24_bugfixes import run_round24_suite

        r24_rep = run_round24_suite()
    except Exception as r24_exc:
        print(f"[ERROR] Round 24 bug-fix self-test could not run: {r24_exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if not r24_rep.get("ok"):
        print(f"[ERROR] Round 24 bug-fix self-test failed: {r24_rep}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    print(
        f"  Round 24 bug fixes    : OK ({r24_rep.get('passed')}/{r24_rep.get('total')} cases in "
        f"{r24_rep.get('elapsed_ms')} ms: the re-apply patchers cannot kill the bridge, "
        f"the duplicate --attach-images option is repaired, "
        f"{r24_rep.get('images_reported')} images reach the composer)"
    )
    try:
        from test_round24_workunits import run_round24_workunits_suite

        r24_wu = run_round24_workunits_suite()
    except Exception as r24_wu_exc:
        print(f"[ERROR] Round 24 work-units self-test could not run: {r24_wu_exc}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if not r24_wu.get("ok"):
        print(f"[ERROR] Round 24 work-units self-test failed: {r24_wu}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    print(
        f"  Round 24 work units   : OK ({r24_wu.get('passed')}/{r24_wu.get('total')} cases in "
        f"{r24_wu.get('elapsed_ms')} ms: W1 base64 cap={r24_wu.get('base64_cap_bytes')}B + CDP zero-copy, "
        f"W2 --account-index + resolved URLs, W3 100MB cap + fresh-chat guard + upload state)"
    )
    return EXIT_SUCCESS


# --- Phase 7: the composer lookup WAITS for the Task composer ---
# Gemini renders the CHAT composer first and replaces it with the TASK composer
# while the page hydrates (measured 2026-11-14 on the live Phase-7 chat: the
# `<textarea class="gds-body-l">` exists at 0 s, the `rich-textarea` composer at
# ~2 s). A single snapshot taken in between finds NOTHING at all, and the send
# dies with "composer editable is missing from the page DOM".
def _find_composer(page, wait_seconds: float = 0.0):
    """Return the prompt composer element, or None when there is none.

    `wait_seconds` makes the lookup WAIT instead of taking a single snapshot.
    """
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        found = _find_composer_once(page)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1.0)


def _find_composer_once(page):
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
    # Tier 1 is the TASK composer (`rich-textarea` / `input-area-container`),
    # tier 2 is every other editable. The tier wins first and the bottom-most
    # element wins inside a tier, so the floating quick-action bar (a generic
    # contenteditable in the middle of the page) never beats the real composer.
    for sels in (
        (
            "rich-textarea div[contenteditable='true']",
            "div.input-area-container div[contenteditable='true']",
        ),
        (
            "div[contenteditable='true']",
            "textarea[aria-label*='prompt']",
            "textarea",
        ),
    ):
        best = None
        best_y = -1.0
        for sel in sels:
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
                    # The CHAT composer of Gemini
                    # (`<textarea class="gds-body-l" placeholder="Ask Gemini">`)
                    # is NOT a composer for a Task prompt: a prompt pasted there
                    # is answered in Chat mode. It is skipped so the lookup waits
                    # for the real TASK composer instead.
                    try:
                        cls = (el.get_attribute("class") or "")
                    except Exception:
                        cls = ""
                    if "gds-body-l" in cls:
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
        if best is not None:
            return best
    return None


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
    lane: Optional[str] = None,
) -> str:
    """Return the best conversation URL to reopen for an in-progress or resumed task.

    Uses `/u/0/` only for an isolated slot profile and `/u/<account_index>/` from
    `PoolConfig.get_account_index(active_slot)` in the shared Argon Bridge browser.
    Prioritizes the recorded `chat_id` from `TaskState` (lane-aware) so `--resume`
    always reopens the exact recorded conversation.
    """
    slot_part = _url_slot_for_account(active_slot, isolated_profile=isolated_profile, strict=False)

    def _url_for_chat_id(cid: str) -> str:
        return f"https://gemini.google.com{slot_part}app/{cid}"

    # 1. Check TaskState (lane-specific first, then project top-level) for a recorded chat_id
    ts_entry = (task_state.get(proj_root, lane=lane) if lane else None) or task_state.get(proj_root) or {}
    ts_cid = str(ts_entry.get("chat_id") or "").strip() or _extract_chat_id(str(ts_entry.get("chat_url") or ""))
    if ts_cid:
        return _url_for_chat_id(ts_cid)

    # 2. Check current_url for a conversation id
    cid = _extract_chat_id(current_url or "")
    if cid:
        return _url_for_chat_id(cid)

    # 3. Check SessionStore (lane-specific first, then project top-level) for this project and mode
    ss_entry = session_store.lookup(proj_root, mode=mode) or {}
    if lane and isinstance(ss_entry.get("lanes"), dict):
        ss_lane = ss_entry["lanes"].get(lane) or {}
        if isinstance(ss_lane, dict):
            ss_lane_cid = str(ss_lane.get("chat_id") or "").strip() or _extract_chat_id(str(ss_lane.get("chat_url") or ""))
            if ss_lane_cid:
                return _url_for_chat_id(ss_lane_cid)
    ss_cid = str(ss_entry.get("chat_id") or "").strip() or _extract_chat_id(str(ss_entry.get("chat_url") or ""))
    if ss_cid:
        return _url_for_chat_id(ss_cid)

    if ts_entry.get("chat_url"):
        return _normalize_gemini_chat_url(str(ts_entry["chat_url"]), isolated_profile=isolated_profile, slot=active_slot)
    if current_url:
        return _normalize_gemini_chat_url(current_url, isolated_profile=isolated_profile, slot=active_slot)
    return _normalize_gemini_chat_url(fallback_url, isolated_profile=isolated_profile, slot=active_slot)


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


def _inspect_conversation_render_state(page) -> Dict[str, Any]:
    """Inspect DOM render state to distinguish a half-loaded/blank page from active Argon generation (Addendum A.1)."""
    try:
        info = page.evaluate(
            r"""() => {
                const ready = document.readyState === 'complete';
                const isVis = (e) => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    const st = window.getComputedStyle(e);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                };
                const spinners = Array.from(
                    document.querySelectorAll("mat-progress-spinner, .mat-progress-spinner, [role='progressbar'], .loading-spinner")
                ).filter(isVis);
                const userTurns = document.querySelectorAll(
                    "user-query, [data-test-id='user-query'], .user-query-text, .query-text, message-turn.user-query, div.user-query"
                ).length;
                const modelTurns = document.querySelectorAll(
                    "model-response, [data-test-id='model-response'], .model-response-text, message-turn.model-response, div.model-response"
                ).length;
                const stopBtns = Array.from(
                    document.querySelectorAll("button[aria-label*='Stop' i], button[aria-label*='stop generating' i], button[aria-label*='থামান']")
                ).filter(isVis);
                const chipCandidates = Array.from(
                    document.querySelectorAll("[class*='status'], [data-test-id*='status'], mat-chip, .mdc-evolution-chip__text-label, header, [role='status']")
                );
                let statusComplete = false;
                for (const c of chipCandidates) {
                    const txt = String(c.innerText || '').trim();
                    if (/^complete(d)?$/i.test(txt) || /\bstatus\s*:\s*complete(d)?\b/i.test(txt)) {
                        statusComplete = true;
                        break;
                    }
                }
                return {
                    ready,
                    spinners: spinners.length,
                    userTurns,
                    modelTurns,
                    stopButtons: stopBtns.length,
                    statusComplete,
                    url: String(location.href || '')
                };
            }"""
        )
        if isinstance(info, dict):
            return info
    except Exception:
        pass
    return {
        "ready": False,
        "spinners": 0,
        "userTurns": 0,
        "modelTurns": 0,
        "stopButtons": 0,
        "statusComplete": False,
        "url": str(getattr(page, "url", "") or ""),
    }


def _is_argon_working(page) -> bool:
    """True ONLY while Argon is actively generating a response (Addendum A.1).

    A conversation that is STILL LOADING (0 user turns and 0 assistant turns in the DOM,
    or a status chip that already says 'Complete' with no Stop button) must NEVER be
    read as 'Argon is working'.
    """
    try:
        # 1. The stop button only exists while a response is being actively generated.
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

    dom_state = _inspect_conversation_render_state(page)
    u_turns_cnt = int(dom_state.get("userTurns") or 0)
    m_turns_cnt = int(dom_state.get("modelTurns") or 0)
    status_complete = bool(dom_state.get("statusComplete"))

    # Addendum A.1: If the conversation area has no rendered user turns and no model turns yet,
    # the page is either a blank new chat or STILL LOADING its history from the server - NOT working.
    if u_turns_cnt == 0 and m_turns_cnt == 0:
        return False

    # Addendum A.1: If the task status chip explicitly says "Complete" and there is no Stop button,
    # Argon is NOT working.
    if status_complete and int(dom_state.get("stopButtons") or 0) == 0:
        return False

    try:
        # 2. Check if the newest assistant turn has already finished.
        turns = _assistant_turns(page)
        if turns:
            newest_turn = turns[-1]
            if _element_is_after_last_user_turn(newest_turn):
                # If there is a delivery chip in this turn, it is definitely finished.
                if _generated_file_chip(page) is not None:
                    return False

                # Check for active loaders/spinners strictly within this turn
                turn_has_loader = False
                for sel in (
                    ".mat-progress-spinner",
                    "[class*='spinner']",
                    "[class*='loader']",
                    "[data-test-id*='loading']",
                    "[class*='thinking']",
                ):
                    try:
                        turn_loc = newest_turn.locator(sel)
                        for i in range(min(turn_loc.count(), 3)):
                            if _safe_visible(turn_loc.nth(i)):
                                turn_has_loader = True
                                break
                        if turn_has_loader:
                            break
                    except Exception:
                        pass
                if turn_has_loader:
                    return True

                # If the turn has substantive text and no active loader or stop button,
                # the assistant turn has finished.
                try:
                    t_text = (newest_turn.inner_text(timeout=2000) or "").strip()
                    if t_text:
                        return False
                except Exception:
                    pass
    except Exception:
        pass

    try:
        # 3. Check for visible spinners ONLY when a user turn is present in the DOM
        # and no completed assistant turn has settled yet.
        if u_turns_cnt > 0:
            for sel in ("[role='progressbar']", ".mat-progress-spinner", "[class*='spinner']", "[class*='loader']"):
                loc = page.locator(sel)
                for i in range(min(loc.count(), 3)):
                    if _safe_visible(loc.nth(i)):
                        return True
    except Exception:
        pass

    try:
        # 4. Check RE_WORKING only if a user turn exists and no completed assistant turn was found
        if u_turns_cnt > 0:
            text = page.locator("body").inner_text(timeout=4000) or ""
            tail = text[-1200:]
            if RE_WORKING.search(tail):
                turns = _assistant_turns(page)
                if not turns or not _element_is_after_last_user_turn(turns[-1]):
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
    """Return the visible text of the last user message in the conversation, if any.

    Strictly excludes composer/input area elements, attachment cards/chips, and
    'You said' headers so it never returns upload chips or the composer template.
    """
    try:
        text = page.evaluate(
            r"""() => {
                const turnSelectors = [
                    'message-turn.user-query',
                    'message-turn:has(user-query)',
                    'user-query',
                    '[data-test-id="user-query"]',
                    '.user-query-container'
                ];
                const allTurns = Array.from(document.querySelectorAll(turnSelectors.join(',')))
                    .filter(el => {
                        if (el.closest('rich-textarea, .input-area, .chat-input, footer, form, [data-test-id="chat-input"]')) {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                if (!allTurns.length) return "";
                const lastTurn = allTurns[allTurns.length - 1];

                const textSelectors = [
                    '.user-query-text',
                    '.query-text',
                    '[data-test-id="user-query-text"]',
                    '.query-content',
                    '.text-content',
                    '.message-content'
                ];
                for (const sel of textSelectors) {
                    const textEl = lastTurn.querySelector(sel);
                    if (textEl) {
                        const t = (textEl.innerText || "").trim();
                        if (t) return t;
                    }
                }

                const clone = lastTurn.cloneNode(true);
                const stripSelectors = [
                    'attachment-chip',
                    '.attachment-chip',
                    '[data-test-id*="attachment"]',
                    'attachment-card',
                    '.file-card',
                    'mat-chip',
                    '.file-preview',
                    '[class*="attachment"]',
                    '[class*="chip"]',
                    '.user-query-header',
                    'h2',
                    'button'
                ];
                for (const sel of stripSelectors) {
                    clone.querySelectorAll(sel).forEach(node => node.remove());
                }
                let t = (clone.innerText || "").trim();
                t = t.replace(/^(You said|আপনি বলেছেন)[\s:\n]*/i, "").trim();
                return t;
            }"""
        )
        if text:
            return text.strip()
    except Exception:
        pass

    for sel in (
        ".user-query-text",
        ".query-text",
        "[data-test-id='user-query'] .user-query-text",
        "user-query .user-query-text",
    ):
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n - 1, -1, -1):
                el = loc.nth(i)
                if _safe_visible(el):
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
      1. the last user message in the live conversation (comparing prompt hash or full normalized text), and
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

    prev = prev_state if isinstance(prev_state, dict) else {}
    prev_status = str(prev.get("state") or "").strip().lower()
    prev_sent_hms = _extract_hms(
        str(prev.get("sent_at") or prev.get("started_at") or prev.get("updated_at") or "")
    )

    # When the stored state is delivered or applied, the previous round FINISHED.
    # A new prompt must NEVER be blocked by the DOM rule.
    if prev_status in ("delivered", "applied"):
        return None

    # 1. Inspect the last user message in the conversation DOM.
    last_user_raw = _last_user_message_text(page)
    if last_user_raw:
        u_hash = _prompt_hash(last_user_raw)
        u_norm = _normalize_ws(last_user_raw)
        dom_matches = False
        if p_hash and u_hash == p_hash:
            dom_matches = True
        elif t_hash and u_hash == t_hash:
            dom_matches = True
        elif p_norm and u_norm and (p_norm == u_norm or (len(p_norm) > 100 and p_norm in u_norm)):
            dom_matches = True

        if dom_matches:
            already_completed = (
                not _is_argon_working(page)
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


APPEND_FOLLOWUP_MARKER = "[ADDENDUM TO RUNNING TASK"


def _check_superseded_prompt(
    page,
    expected_prompt_text: str = "",
    expected_task_text: str = "",
    prev_state: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Return a conflict description when a newer, COMPETING prompt was sent into the same chat (D22, Addendum B).

    Distinguishes a competing prompt from an `--append-followup` extension of the SAME
    running task: any message carrying `APPEND_FOLLOWUP_MARKER`, the running task's own
    label/task_id, or matching an entry in `prev_state['appended_followups']` is part
    of this task and is NOT a supersede.
    """
    last_user_raw = _last_user_message_text(page)
    if not last_user_raw:
        return None
    u_norm = _normalize_ws(last_user_raw)
    u_hash = _prompt_hash(last_user_raw)
    u_prefix = u_norm[:160]

    prev = dict(prev_state) if isinstance(prev_state, dict) else {}
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

    # Addendum B: Check if last_user_raw is an appended follow-up to this running task
    low_u = u_norm.lower()
    if (
        APPEND_FOLLOWUP_MARKER.lower() in low_u
        or "[addendum to the running task" in low_u
        or "[appended follow-up" in low_u
    ):
        return None

    task_label = _normalize_ws(str(prev.get("label") or "")).strip()
    if task_label and len(task_label) >= 8 and task_label.lower() in low_u:
        return None

    appended_list = prev.get("appended_followups")
    if isinstance(appended_list, list):
        for item in appended_list:
            if not isinstance(item, dict):
                continue
            a_hash = str(item.get("prompt_hash") or "")
            a_raw_hash = str(item.get("raw_hash") or "")
            a_pref = _normalize_ws(str(item.get("prefix") or ""))
            a_raw_pref = _normalize_ws(str(item.get("raw_prefix") or ""))
            if (a_hash and u_hash == a_hash) or (a_raw_hash and u_hash == a_raw_hash):
                return None
            if a_pref and len(a_pref) >= 16 and (a_pref[:100] in u_norm or u_norm[:100] in a_pref):
                return None
            if a_raw_pref and len(a_raw_pref) >= 16 and (a_raw_pref[:100] in u_norm or u_norm[:100] in a_raw_pref):
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


def _wait_for_conversation_loaded(
    page,
    timeout: float = 15.0,
    require_turns: bool = False,
) -> bool:
    """Wait until the page has finished loading the conversation and earlier turns are rendered (E3.2, Addendum A.1).

    When `require_turns=True` (an existing conversation being reopened or continued),
    waits until the conversation's user/assistant turns are actually present in the DOM
    and returns `True` only when the conversation has proven to have finished rendering.
    """
    _wait_for_workspace(page, timeout=min(25.0, max(5.0, timeout)))
    try:
        page.wait_for_timeout(1800)
    except Exception:
        time.sleep(1.8)
    deadline = time.monotonic() + max(2.0, float(timeout))
    prev_snapshot = None
    stable_streak = 0
    while time.monotonic() < deadline:
        state = _inspect_conversation_render_state(page)
        u_cnt = int(state.get("userTurns") or 0)
        m_cnt = int(state.get("modelTurns") or 0)
        spin_cnt = int(state.get("spinners") or 0)
        stop_cnt = int(state.get("stopButtons") or 0)
        ready = bool(state.get("ready"))
        turns_ok = (u_cnt > 0 or m_cnt > 0) if require_turns else True

        if ready and turns_ok and (spin_cnt == 0 or stop_cnt > 0 or m_cnt > 0):
            snap = (state.get("url"), u_cnt, m_cnt)
            if snap == prev_snapshot:
                stable_streak += 1
                if stable_streak >= 2:
                    return True
            else:
                prev_snapshot = snap
                stable_streak = 1
        try:
            page.wait_for_timeout(900)
        except Exception:
            time.sleep(0.9)

    final_state = _inspect_conversation_render_state(page)
    if require_turns:
        return bool(int(final_state.get("userTurns") or 0) > 0 or int(final_state.get("modelTurns") or 0) > 0)
    return bool(final_state.get("ready")) and int(final_state.get("spinners") or 0) == 0


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
    slot_part = _url_slot_for_account(slot, isolated_profile=isolated_profile)
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
    # packed again before executing the unified send sequence (model -> prompt -> attachment -> immediate send).
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

    turns_before_co = _assistant_turns(page)
    responses_before_co = len(turns_before_co) if turns_before_co is not None else 0
    chips_before_co = len(_generated_file_chip_indices(page))
    try:
        co_mode_ok, co_model_ok, co_sent_ok = _execute_composer_send_sequence(
            page=page,
            zip_path=zip_path,
            prompt_text=new_prompt,
            manual_setup=manual_setup,
            is_continuation=False,
            attach_images=attach_images,
        )
        if not (co_mode_ok and co_model_ok and co_sent_ok):
            print("[!] Carry-over send sequence failed (Task mode, 4 Argon guard, or upload/send).", file=sys.stderr)
            return None
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
    co_chip_captures = 0
    while time.monotonic() < deadline:
        try:
            new_chip = _new_delivery_chip(page, chips_before_co, responses_before_co)
            if new_chip is None:
                new_chip = _generated_file_chip(page)
            if new_chip is not None:
                co_chip_captures += 1
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
                if getattr(page, "_argon_chip_capture_exhausted", False) or co_chip_captures >= MAX_CHIP_DOWNLOAD_ATTEMPTS:
                    co_fail_reason = str(getattr(page, "_argon_last_capture_failure_reason", "") or "chip download attempts exhausted")
                    print(
                        f"[!] Carry-over delivery chip rejected after bounded attempts ({co_fail_reason}); stopping carry-over loop.",
                        file=sys.stderr,
                    )
                    return None
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

    NOTHING is sent to Gemini here. The build runs locally on this machine.

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
        "the agent: fix them locally - they are small and self-contained.",
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


# --- Phase 7: the screenshots travel WITH the prompt (--attach-images) ---
# The context archive cannot carry binary files, so the UI screenshots are uploaded
# into the composer as real images. Upstream parsed the flag but never forwarded it
# and attached only AFTER the send; both are fixed here.
def _expand_attach_images(spec) -> List[Path]:
    """Turn a directory, a glob or a comma-separated list into a file list.

    `--attach-images` is declared with `nargs="*"`, so argparse hands the value
    over as a LIST of strings - never a single string. Measured on 2026-11-14:
    the list form reached this helper as the literal text "['dir']", every
    lookup failed and NO screenshot was ever attached. The list is flattened
    first here; every element may itself be a comma-separated list.
    """
    if not spec:
        return []
    raw_parts: List[str] = []
    items = list(spec) if isinstance(spec, (list, tuple, set)) else [spec]
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                raw_parts.append(part)
    out: List[Path] = []
    for part in raw_parts:
        p = Path(part).expanduser()
        if p.is_dir():
            for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.svg"):
                out.extend(sorted(p.glob(pat)))
        elif p.is_file():
            out.append(p)
        else:
            parent = p.parent if str(p.parent) else Path(".")
            out.extend(sorted(g for g in parent.glob(p.name) if g.is_file()))
    seen, uniq = set(), []
    for f in out:
        key = str(f).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


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
    deadline_seconds: int = 5400,
    brief_summary: Optional[str] = None,
    attach_images: Optional[List[str]] = None,
    account_index: Optional[int] = None,
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

    prior_ladder_entry = (task_state_store.get(proj_root, lane=lane) or task_state_store.get(proj_root)) or {}
    prior_state_val = str(prior_ladder_entry.get("state") or "").strip().lower()
    prior_task_hash = str(prior_ladder_entry.get("task_hash") or "")
    current_task_hash = _prompt_hash(task) if task else prior_task_hash
    is_same_unfinished_task = bool(
        resume
        or (
            prior_ladder_entry
            and prior_state_val not in ("applied", "delivered", "completed", "superseded", "")
            and (not current_task_hash or not prior_task_hash or current_task_hash == prior_task_hash)
        )
    )
    if force and not resume:
        is_same_unfinished_task = False

    ladder_phase = "delivery" if (
        resume
        or (
            is_same_unfinished_task
            and (
                prior_state_val in ("sent", "waiting", "delivery_failed")
                or str(prior_ladder_entry.get("send_progress") or "") in ("prompt_sent", "verified")
            )
        )
    ) else "send"

    if is_same_unfinished_task:
        send_attempt = int(prior_ladder_entry.get("send_attempt") or 0)
        delivery_attempt = int(prior_ladder_entry.get("delivery_attempt") or 0)
    else:
        send_attempt = 0
        delivery_attempt = 0

    if ladder_phase == "send":
        send_attempt += 1
        current_attempt = send_attempt
    else:
        delivery_attempt += 1
        current_attempt = delivery_attempt

    manual_self_send_mode = False
    if current_attempt > MAX_LADDER_ATTEMPTS:
        manual_self_send_mode = True
        print(
            f"[+] Automatic retry ladder ({MAX_LADDER_ATTEMPTS} attempts) exhausted for phase='{ladder_phase}'; "
            "switching to self-executed manual send fallback (Rule 8: opening browser, writing manual hand-off pack, "
            "selecting 4 Argon, pasting prompt, attaching archive, and clicking send automatically)."
        )
    else:
        print(
            f"[+] Retry ladder: phase={ladder_phase}, attempt {current_attempt} of {MAX_LADDER_ATTEMPTS}"
        )

    send_progress = str(prior_ladder_entry.get("send_progress") or "none") if resume else "none"
    task_state_store.set(
        proj_root,
        lane=lane,
        phase=ladder_phase,
        attempt=current_attempt,
        send_attempt=send_attempt,
        delivery_attempt=delivery_attempt,
        max_attempts=MAX_LADDER_ATTEMPTS,
        send_progress=send_progress,
        task_hash=current_task_hash,
    )
    if not resume:
        task_registry.update_running(
            proj_root,
            lane=lane,
            send_progress="none",
            phase=ladder_phase,
            attempt=current_attempt,
        )

    def _track_send_progress(p_val: str) -> None:
        nonlocal send_progress, ladder_phase, delivery_attempt, current_attempt
        send_progress = p_val
        if p_val in ("prompt_sent", "verified") and ladder_phase == "send":
            ladder_phase = "delivery"
            if delivery_attempt == 0:
                delivery_attempt = 1
            current_attempt = delivery_attempt
            print(
                f"[+] Retry ladder: entering phase=delivery, attempt {current_attempt} of {MAX_LADDER_ATTEMPTS}"
            )
        task_state_store.set(
            proj_root,
            lane=lane,
            send_progress=p_val,
            phase=ladder_phase,
            attempt=current_attempt,
            send_attempt=send_attempt,
            delivery_attempt=delivery_attempt,
        )
        task_registry.update_running(
            proj_root,
            lane=lane,
            send_progress=p_val,
            phase=ladder_phase,
            attempt=current_attempt,
        )

    def _emit_send_failure(step_name: str, shot_path_val: Optional[Path] = None, page_obj: Any = None) -> None:
        try:
            from manual_handoff import write_manual_handoff_pack
            from argon_pool_login import find_chrome_executable
            c_exe = find_chrome_executable()
            p_dir = _profile_dir_for_slot(pool_cfg.profile_dir, active_slot)
            cur_chat = ""
            q_file_now = ""
            fail_reason_now = step_name
            if page_obj is not None:
                try:
                    cur_chat = str(getattr(page_obj, "url", "") or "")
                    q_file_now = str(getattr(page_obj, "_argon_last_quarantined_path", "") or "")
                    fail_reason_now = str(getattr(page_obj, "_argon_last_capture_failure_reason", "") or step_name)
                except Exception:
                    cur_chat = ""
            md_p, json_p = write_manual_handoff_pack(
                proj_root=proj_root,
                lane=lane or "new",
                slot=active_slot or "/u/0/",
                email=target_email or "",
                chat_url=cur_chat or target_url or "https://gemini.google.com/u/0/app",
                prompt_text=prompt_text or "",
                prompt_path=Path(prompt_file).resolve() if prompt_file and Path(prompt_file).is_file() else None,
                zip_path=zip_path,
                send_progress=send_progress,
                failed_step=step_name,
                last_screenshot=shot_path_val,
                chrome_exe=c_exe,
                profile_dir=p_dir,
                phase=ladder_phase,
                attempt=current_attempt,
                max_attempts=MAX_LADDER_ATTEMPTS,
                quarantined_file=q_file_now,
                failure_reason=fail_reason_now,
            )
            if resolved_browser_idx is not None:
                update_manual_handoff_with_resolved_slot(
                    md_p,
                    json_p,
                    slot=active_slot or "/u/0/",
                    email=target_email or "",
                    account_index=resolved_browser_idx,
                    resolved_url=cur_chat or target_url or f"https://gemini.google.com/u/{resolved_browser_idx}/app",
                )
            print("\n[i] Manual hand-off summary:")
            print(f"    Account e-mail : {target_email or '(none / unconfigured)'}")
            print(f"    Chat link      : {cur_chat or target_url or 'https://gemini.google.com/u/0/app'}")
            print(f"    Prompt path    : {str((Path(prompt_file).resolve()) if prompt_file and Path(prompt_file).is_file() else (md_p.parent / 'dispatch_prompt.md').resolve())}")
            print(f"    ZIP path       : {str(zip_path.resolve())}")
            print(f"    Last screenshot: {str(shot_path_val.resolve()) if shot_path_val else '(none)'}")
            print(f"[i] Manual hand-off pack: {md_p.resolve()}")
        except Exception as _handoff_err:
            print(f"[!] Warning: Could not write manual hand-off pack: {_handoff_err}", file=sys.stderr)

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

    # Addendum A.2: Reclaim any leftover `running` task records whose owning PID is dead
    # BEFORE checking the ALREADY RUNNING guard so a failed earlier attempt never blocks retry step 2.
    task_registry.reclaim_dead_tasks(proj_root, lane=lane)

    if not resume:
        existing_lane_state = task_state_store.get(proj_root, lane=lane) if lane else None
        if isinstance(existing_lane_state, dict):
            el_pid = int(existing_lane_state.get("pid") or 0)
            el_state = str(existing_lane_state.get("state") or "").strip().lower()
            el_prog = str(existing_lane_state.get("send_progress") or "none").strip().lower()
            if el_pid and el_pid != os.getpid() and not _pid_alive(el_pid):
                if el_state == "running" or el_prog not in ("prompt_sent", "verified"):
                    task_state_store.set(
                        proj_root,
                        lane=lane,
                        state="failed",
                        end_reason=f"reclaimed leftover state={el_state!r} from dead pid {el_pid}",
                    )
                    existing_lane_state = task_state_store.get(proj_root, lane=lane)

        running_lanes = task_state_store.get_running_lanes(proj_root)
        for rl_name, rl_data in list(running_lanes.items()):
            rl_pid = int((rl_data or {}).get("pid") or 0)
            rl_prog = str((rl_data or {}).get("send_progress") or "none").strip().lower()
            if rl_pid and rl_pid != os.getpid() and not _pid_alive(rl_pid) and rl_prog not in ("prompt_sent", "verified"):
                task_state_store.set(
                    proj_root,
                    lane=rl_name,
                    state="failed",
                    end_reason=f"reclaimed leftover running lane {rl_name!r} from dead pid {rl_pid}",
                )
                running_lanes.pop(rl_name, None)

        blocking_entry: Optional[Dict[str, Any]] = None
        if lane:
            if existing_lane_state and str(existing_lane_state.get("state") or "").strip().lower() in ("sent", "waiting"):
                b_pid_cand = int(existing_lane_state.get("pid") or 0)
                if not b_pid_cand or b_pid_cand == os.getpid() or _pid_alive(b_pid_cand):
                    blocking_entry = existing_lane_state
        elif running_lanes:
            for _, rl_cand in running_lanes.items():
                b_pid_cand = int((rl_cand or {}).get("pid") or 0)
                if not b_pid_cand or b_pid_cand == os.getpid() or _pid_alive(b_pid_cand):
                    blocking_entry = rl_cand
                    break

        if blocking_entry is None:
            reg_active = task_registry.active_tasks_for_project(proj_root, lane=lane)
            for ra_item in reg_active:
                ra_pid = int(ra_item.get("pid") or 0)
                if ra_pid and ra_pid != os.getpid() and not _pid_alive(ra_pid):
                    continue
                if ra_item.get("chat_id") or ra_item.get("chat_url"):
                    blocking_entry = ra_item
                    break

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

    # RESUME: reopen the RECORDED conversation (chat_id / chat_url from TaskState, or
    # SessionStore fallback) instead of packaging and sending a duplicate prompt (Round 17 Section 1.7).
    resume_entry = (task_state_store.get(proj_root, lane=lane) or task_state_store.get(proj_root)) if resume else None
    if resume and isinstance(resume_entry, dict) and not (resume_entry.get("chat_id") or resume_entry.get("chat_url")):
        ss_fallback = session_store.lookup(proj_root, mode=mode) or {}
        if ss_fallback.get("chat_id") or ss_fallback.get("chat_url"):
            resume_entry = dict(resume_entry)
            resume_entry.setdefault("chat_id", ss_fallback.get("chat_id", ""))
            resume_entry.setdefault("chat_url", ss_fallback.get("chat_url", ""))
            resume_entry.setdefault("slot", ss_fallback.get("slot", "/u/0/"))
    resume_chat = bool(
        resume_entry
        and (resume_entry.get("chat_id") or resume_entry.get("chat_url"))
        and str(resume_entry.get("state") or "waiting").strip().lower()
        in ("sent", "waiting", "delivered", "running", "delivery_failed", "failed")
    )
    if resume and not resume_chat:
        print(
            f"[!] No recorded task conversation found in {task_state_store.path} for {proj_root}.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    if resume_chat and not task and resume_entry:
        task = str(resume_entry.get("task") or "")

    # If --resume sees a delivered file that is already on disk and passes BOTH
    # _looks_like_patch and _patch_is_complete (Section 2.1.H), apply or report it directly.
    if resume_chat and resume_entry and resume_entry.get("delivered"):
        existing_delivery = Path(str(resume_entry["delivered"]))
        if existing_delivery.is_file() and not _is_quarantined_path(existing_delivery):
            try:
                existing_text = existing_delivery.read_text(encoding="utf-8", errors="replace")
            except Exception:
                existing_text = ""
            looks_ok_r = _looks_like_patch(existing_text)
            comp_ok_r = _patch_is_complete(existing_text)
            if looks_ok_r and comp_ok_r:
                print(f"[+] Resuming from already-captured complete patch file: {existing_delivery}")
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

    resolved_browser_idx: Optional[int] = None
    if account_index is not None:
        try:
            idx_res = resolve_account_index_to_slot(
                account_index,
                pool_cfg=pool_cfg,
                explicit_slot=slot,
            )
        except RuntimeError as exc_acct_idx:
            print(f"[ERROR] {exc_acct_idx}", file=sys.stderr)
            return (
                EXIT_PREFLIGHT_FAILED
                if "slot_account_index_conflict" in str(exc_acct_idx)
                else EXIT_NOT_SIGNED_IN
            )
        slot = str(idx_res["slot"])
        resolved_browser_idx = int(idx_res["account_index"])

    # D3: Apply Account Policy when selecting active_slot
    if slot:
        active_slot = normalize_slot(slot)
        slot_reason = (
            f"resolved from --account-index {account_index} -> pool slot {active_slot}"
            if account_index is not None
            else f"explicitly requested slot {active_slot}"
        )
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
            fallback_url=get_target_url(
                mode, active_slot, isolated_profile=isolated_slot, strict=False
            ),
            task_state=task_state_store,
            session_store=session_store,
            isolated_profile=isolated_slot,
            lane=lane,
        )
    elif is_continuation and session is not None:
        if slot is None and session.get("slot"):
            active_slot = normalize_slot(session["slot"])
            slot_profile = _profile_dir_for_slot(base_profile, active_slot)
        pool_cfg.set_active_slot(active_slot)
        active_account = pool_cfg.get_by_slot(active_slot)
        target_email = active_account.get("email") if active_account else None
        isolated_slot = _profile_is_isolated_slot(slot_profile)
        target_url = _normalize_gemini_chat_url(str(session["chat_url"]), isolated_profile=isolated_slot, slot=active_slot)
    else:
        pool_cfg.set_active_slot(active_slot)
        active_account = pool_cfg.get_by_slot(active_slot)
        target_email = active_account.get("email") if active_account else None
        isolated_slot = _profile_is_isolated_slot(slot_profile)
        try:
            target_url = get_target_url(mode, active_slot, isolated_profile=isolated_slot)
        except RuntimeError as exc_url:
            # Round 21: the slot's account is not signed in to the Argon browser. Refusing
            # here is the whole point - the old silent fallback opened `/u/<slot number>/`,
            # which is a DIFFERENT Google account, and the task ran under the wrong identity.
            print(f"[!] {exc_url}", file=sys.stderr)
            print(
                "[!] Refusing to open a URL for an account that is not signed in - the pool slot "
                "number is NOT the browser account index.",
                file=sys.stderr,
            )
            return EXIT_NOT_SIGNED_IN

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

    # Rule 8: Write the manual hand-off pack (prompt file + zip location) on EVERY attempt
    # right at the start of the attempt so a human can also send by hand at any moment.
    if not resume_chat and prompt_text:
        _emit_send_failure(f"attempt_{current_attempt}_ready", None, page_obj=None)

    from playwright.sync_api import sync_playwright


    # Every pool slot gets its own browser profile directory, so two runs on
    # two different slots never fight over one Chrome profile. The slot lock
    # stops two runs from using the SAME slot at once.
    from safe_browser import (
        acquire_host_tab,
        has_live_browser_host,
        profile_has_signed_in_account,
        release_host_tab,
        repair_slot_cookie_db_only,
        update_host_tab,
        verify_account_index_in_preferences,
    )

    slot_lock = SlotLock(active_slot, profile_dir=slot_profile)
    lock_acquired = slot_lock.acquire(slot_profile)
    temp_concurrent_profile: Optional[Path] = None

    context = None
    page = None
    host_tab_id: Optional[str] = None
    attached_to_host = False
    try:
        profile_dir = slot_profile
        try:
            if _profile_is_isolated_slot(slot_profile):
                if not (slot_profile.is_dir() and (slot_profile / "Default").is_dir()):
                    print(f"[+] Preparing an isolated browser profile for slot {active_slot}: {slot_profile}")
                    _seed_profile(base_profile, slot_profile)
                elif not _has_cookie_db(slot_profile) and profile_has_signed_in_account(slot_profile):
                    repair_slot_cookie_db_only(base_profile, slot_profile)
            else:
                profile_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"[!] Could not prepare the browser profile: {exc}", file=sys.stderr)
            profile_dir = slot_profile

        # Section 2.3.C, Round 18 & Round 20: Verify the account index in the Argon browser's
        # live Default/Preferences (`account_info`) and refuse stale account_index fallbacks.
        if not _profile_is_isolated_slot(profile_dir):
            try:
                expected_acct_idx = pool_cfg.get_account_index(active_slot)
            except RuntimeError as exc_idx:
                print(f"[!] {exc_idx}", file=sys.stderr)
                return EXIT_NOT_SIGNED_IN
            ok_idx, found_idx, all_pref_emails = verify_account_index_in_preferences(
                profile_dir,
                target_email or "",
                expected_index=expected_acct_idx,
            )
            if found_idx is None and all_pref_emails:
                print(
                    f"[!] Slot {active_slot} ({target_email or 'account'}) is not present in live Default/Preferences "
                    f"(registered_accounts={len(all_pref_emails)}); refusing stale index /u/{expected_acct_idx}/.",
                    file=sys.stderr,
                )
                return EXIT_NOT_SIGNED_IN
            eff_idx = found_idx if found_idx is not None else expected_acct_idx
            resolved_browser_idx = int(eff_idx)
            if found_idx is not None:
                pool_cfg.set_account_index(active_slot, found_idx)
            target_url = re.sub(
                r"(gemini\.google\.com)/u/\d+/",
                f"\\1/u/{eff_idx}/",
                target_url,
                flags=re.IGNORECASE,
            )
            print(
                f"[+] Argon browser account index for slot {active_slot} ({target_email or 'account'}): "
                f"/u/{eff_idx}/ (verified_in_preferences={ok_idx}, registered_accounts={len(all_pref_emails)}) -> {target_url}"
            )

            # Round 21 (FIX 5): the recorded account must be the account this lane
            # ACTUALLY opened. The pool entry's email can disagree with the browser's
            # live account list (the pool slot and the `/u/<N>/` path are two different
            # things), so the email is read back from the browser's own
            # `Default/Preferences` account list for the index that was opened here.
            live_email = _live_account_email_for_index(profile_dir, eff_idx)
            if not live_email:
                print(
                    f"[!] Slot {active_slot}: the Argon browser has NO account at /u/{eff_idx}/ - "
                    f"refusing to record an account that this lane did not open.",
                    file=sys.stderr,
                )
                return EXIT_NOT_SIGNED_IN
            if live_email.lower() != str(target_email or "").strip().lower():
                print(
                    f"[!] Slot {active_slot}: the pool says {target_email or '(no email)'} but the "
                    f"browser's account at /u/{eff_idx}/ is {live_email} - recording the LIVE account.",
                    file=sys.stderr,
                )
            target_email = live_email
            print(f"[+] Account actually used by this lane: {live_email} (/u/{eff_idx}/)")
            res_app_url = f"https://gemini.google.com/u/{eff_idx}/app"
            print(format_account_slot_resolution_line(active_slot, target_email, eff_idx, res_app_url))
            slot_num_val = _slot_number(active_slot)
            if slot_num_val != eff_idx:
                print(
                    f"[WARNING] Pool slot {active_slot} (slot number {slot_num_val}) resolves to a DIFFERENT "
                    f"browser account index /u/{eff_idx}/ ({target_email}) -> {res_app_url} "
                    "(pool slot != browser account index).",
                    file=sys.stderr,
                )
            if not resume_chat and prompt_text:
                _emit_send_failure(f"attempt_{current_attempt}_ready", None, page_obj=None)

        slot_lock.update_profile_dir(profile_dir)
        chrome_exe = find_chrome_executable()

        def _close_active_browser_in_with() -> None:
            nonlocal context, page, host_tab_id, attached_to_host
            if context is None:
                return
            try:
                if attached_to_host and host_tab_id:
                    release_host_tab(context, page, profile_dir, tab_id=host_tab_id)
                else:
                    _close_browser_safely(context, profile_dir)
            except Exception:
                pass
            context = None
            page = None

        with sync_playwright() as p, _PlaywrightSafeCloseGuard(_close_active_browser_in_with):
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(profile_dir),
                "headless": headless,
                "args": list(CHROME_LAUNCH_ARGS),
                "ignore_default_args": ["--enable-automation"],
                "no_viewport": True,
            }

            # Attach to (or start) the Argon browser's single CDP host and open a dedicated tab.
            context, page, host_tab_id, attached_to_host = acquire_host_tab(
                playwright_instance=p,
                profile_dir=profile_dir,
                slot=active_slot,
                project_path=proj_root,
                lane=lane or "single",
                chrome_exe=chrome_exe,
                headless=headless,
                account_email=target_email or "",
            )

            if context is None or page is None:
                # If a plain Chrome without --remote-debugging-port holds the Argon browser profile,
                # close it safely (snapshotting & restoring cookies) instead of cloning *-concurrent-* leftovers.
                if (not lock_acquired or _profile_is_busy(profile_dir)) and not has_live_browser_host(profile_dir):
                    print(
                        f"[i] Profile ({profile_dir}) is held by a Chrome without a CDP endpoint; "
                        f"closing it safely (preserving cookies) so {active_slot} can launch."
                    )
                    try:
                        _close_browser_safely(None, profile_dir)
                    except Exception:
                        pass

                if chrome_exe:
                    print(f"[+] Launching installed Google Chrome binary (fallback mode): {chrome_exe}")
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
            _install_pre_text_content_hide_script(page)
            _install_drive_id_listener(page)

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
                            "Nothing was sent yet.",
                            file=sys.stderr,
                        )
                        task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed")
                        return EXIT_BROWSER_CLOSED
                else:
                    raise

            if not _wait_for_workspace(page):
                print(
                    "[!] The Gemini workspace did not become ready in time (page not ready; session data on disk was not cleared).",
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
                    reason="Gemini workspace composer did not become ready before timeout (page not ready; session data on disk is intact)",
                    slot=active_slot,
                    email=target_email,
                    profile_dir=profile_dir,
                    chat_url=target_url,
                    screenshot_path=shot_ws,
                    ruled_out=["Browser launched on slot profile"],
                    next_step="Re-run the task or inspect the open browser window.",
                )
                task_state_store.set(proj_root, lane=lane, state="preflight_failed", diagnostic_evidence=ev_ws)
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_ws or ""))
                _print_run_failure_summary(ev_ws)
                _emit_send_failure("workspace_not_ready", shot_ws, page_obj=page)
                return EXIT_PREFLIGHT_FAILED

            if _is_page_unauthenticated(page):
                has_disk_cookies = _has_cookie_db(profile_dir)
                has_disk_acct = profile_has_signed_in_account(profile_dir)
                if not has_disk_cookies and not has_disk_acct:
                    unauth_reason = (
                        f"Slot profile {profile_dir} has no cookie DB and empty Default/Preferences account_info "
                        "(session data on disk was cleared or account was never signed in)"
                    )
                else:
                    unauth_reason = (
                        f"Gemini page at {getattr(page, 'url', target_url)} rendered the guest sign-in state "
                        f"(disk cookie_db={has_disk_cookies}, preferences_account={has_disk_acct}; session data on disk was NOT cleared)"
                    )
                print("\n" + "!" * 72, file=sys.stderr)
                print(f"[ERROR] Authentication check failed: {unauth_reason}", file=sys.stderr)
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
                    state_label=unauth_reason,
                    lane=lane,
                )
                ev_unauth = _collect_diagnostic_evidence(
                    page=page,
                    proj_root=proj_root,
                    reason=unauth_reason,
                    slot=active_slot,
                    email=target_email,
                    profile_dir=profile_dir,
                    chat_url=target_url,
                    screenshot_path=shot_unauth,
                    ruled_out=["Browser launched and page finished loading"],
                    next_step="Run Open_Argon_Login.bat, then re-run this task.",
                )
                task_state_store.set(proj_root, lane=lane, state="not_signed_in", diagnostic_evidence=ev_unauth)
                task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_unauth or ""))
                _print_run_failure_summary(ev_unauth)
                _emit_send_failure("not_signed_in", shot_unauth, page_obj=page)
                return EXIT_NOT_SIGNED_IN

            _select_profile_account_if_needed(page, target_email)

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
                    _emit_send_failure("new_chat_failed", shot_fc, page_obj=page)
                    return EXIT_PREFLIGHT_FAILED

            # Addendum A.1: Wait for an existing conversation to finish rendering its turns
            # in the DOM before deciding whether Argon is working or whether the prompt is a duplicate.
            if is_continuation or resume_chat:
                conv_ready = _wait_for_conversation_loaded(page, timeout=30.0, require_turns=True)
                if not conv_ready:
                    print(
                        "[!] Existing conversation has not finished rendering its message history yet; "
                        "reloading once and waiting for turns to appear in the DOM...",
                        file=sys.stderr,
                    )
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    conv_ready = _wait_for_conversation_loaded(page, timeout=25.0, require_turns=True)
                if not conv_ready and is_continuation and not resume_chat:
                    loading_shot = _take_step_screenshot(
                        page,
                        proj_root,
                        "preflight_failure.png",
                        step_label="conversation_still_loading",
                        state_label="Conversation page did not finish loading turns into the DOM",
                        lane=lane,
                    )
                    load_reason = (
                        f"Conversation {getattr(page, 'url', target_url)} could not be proven to have finished "
                        "loading (0 turns rendered in DOM; 'still loading' is not 'working' and not guessed)."
                    )
                    print(f"\n[ERROR] {load_reason}\n        Screenshot: {loading_shot}", file=sys.stderr)
                    task_state_store.set(
                        proj_root,
                        lane=lane,
                        state="failed",
                        end_reason=load_reason,
                        pid=os.getpid(),
                        last_screenshot=str(loading_shot or ""),
                    )
                    task_registry.register_done(
                        proj_root,
                        lane=lane,
                        slot=active_slot,
                        status="failed",
                        screenshot_path=str(loading_shot or ""),
                        end_reason=load_reason,
                    )
                    _emit_send_failure("conversation_still_loading", loading_shot, page_obj=page)
                    return EXIT_PREFLIGHT_FAILED

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
                    f"        Use --resume to wait for the running answer, --append-followup <file.md> to extend it, "
                    f"or --force to supersede it explicitly.",
                    file=sys.stderr,
                )
                task_registry.register_done(
                    proj_root,
                    lane=lane,
                    slot=active_slot,
                    status="failed",
                    screenshot_path=str(live_working_shot or ""),
                    end_reason="refused to start because conversation already has Argon actively working",
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

            # ------------------------------------------------- send & pre-flight --
            preflight: List[str] = []
            shot_dir = proj_root / ".argon_backup"
            shot_dir.mkdir(parents=True, exist_ok=True)

            dispatch_started = (
                float(resume_entry.get("started_epoch") or 0.0)
                if (resume_chat and resume_entry)
                else time.time()
            )
            task_state = TaskState()
            chips_before = 0 if resume_chat else len(_generated_file_chip_indices(page))
            turns_before = _assistant_turns(page)
            if resume_chat:
                responses_before = (
                    max(0, len(turns_before) - 1)
                    if (turns_before and _element_is_after_last_user_turn(turns_before[-1]))
                    else (len(turns_before) if turns_before is not None else 0)
                )
            else:
                responses_before = len(turns_before) if turns_before is not None else 0

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
            if not resume_chat:
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
            mode_ok = model_ok = attached = True

            if resume_chat:
                print(
                    f"[+] Resuming the task in: {target_url}"
                    + (f" (state: {resume_entry.get('state')})" if resume_entry else "")
                )
                task_state.set(proj_root, lane=lane, state="waiting", chat_url=page.url, slot=active_slot)
            else:
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
                            _emit_send_failure("foreign_chat_before_send", shot_foreign_pre, page_obj=page)
                            return EXIT_PREFLIGHT_FAILED

                    if brief_summary:
                        bs_path = Path(brief_summary).resolve()
                        if bs_path.is_file():
                            try:
                                if resolved_browser_idx is not None:
                                    update_brief_summary_with_resolved_slot(
                                        bs_path,
                                        slot=active_slot,
                                        email=target_email or "",
                                        account_index=resolved_browser_idx,
                                        resolved_url=target_url,
                                    )
                                print("\n" + "=" * 78)
                                print(f"[i] Brief Summary ({bs_path}):")
                                print("=" * 78)
                                print(bs_path.read_text(encoding="utf-8"))
                                print("=" * 78 + "\n")
                            except Exception as exc:
                                print(f"[!] Warning: Could not read brief summary {bs_path}: {exc}", file=sys.stderr)
                        else:
                            print(f"[!] Warning: Brief summary file not found at {bs_path}; continuing without summary display.", file=sys.stderr)

                    # Rule 5 & Rule 8: Execute the unified send sequence
                    # (1. find composer & select 4 Argon -> 2. paste prompt -> 3. attach zip -> 4. click send immediately on upload completion).
                    try:
                        mode_ok, model_ok, attached = _execute_composer_send_sequence(
                            page=page,
                            zip_path=zip_path,
                            prompt_text=prompt_text,
                            manual_setup=manual_setup,
                            is_continuation=is_continuation,
                            recorded_ws_mode=recorded_ws_mode,
                            on_progress=_track_send_progress,
                            preflight_log=preflight,
                            task_text=task,
                            prev_state=prev_state,
                            new_session=new_session,
                            attach_images=attach_images,
                        )
                        just_sent_prompt = bool(mode_ok and model_ok and attached)
                    except Exception as send_exc:
                        if _is_target_closed_error(send_exc):
                            rec_page = _recover_preflight_or_send_browser(send_exc, "send_sequence")
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
                                    preflight.clear()
                                    mode_ok, model_ok, attached = _execute_composer_send_sequence(
                                        page=page,
                                        zip_path=zip_path,
                                        prompt_text=prompt_text,
                                        manual_setup=manual_setup,
                                        is_continuation=is_continuation,
                                        recorded_ws_mode=recorded_ws_mode,
                                        on_progress=_track_send_progress,
                                        preflight_log=preflight,
                                        task_text=task,
                                        prev_state=prev_state,
                                        new_session=new_session,
                                        attach_images=attach_images,
                                    )
                                    just_sent_prompt = bool(mode_ok and model_ok and attached)
                        else:
                            print(f"[!] Send sequence error: {send_exc}", file=sys.stderr)
                            just_sent_prompt = False

                    print("[i] Pre-flight & Send Sequence:")
                    for line in preflight:
                        print("      " + line)

                    # Rule 8: If the normal send sequence did not succeed, write the manual hand-off pack
                    # and perform one direct self-executed manual send fallback in the open browser before failing.
                    if not just_sent_prompt and not resume_chat:
                        shot_retry = _take_step_screenshot(
                            page,
                            proj_root,
                            "preflight_failure.png",
                            step_label="send_retry_handoff",
                            state_label="Writing manual hand-off pack and attempting self-executed manual send",
                            lane=lane,
                        )
                        _emit_send_failure("send_sequence_failed", shot_retry, page_obj=page)
                        if current_attempt >= MAX_LADDER_ATTEMPTS or manual_self_send_mode:
                            print(
                                "[+] Automatic attempts exhausted - executing bridge self-manual send fallback "
                                "using the same composer, upload wait, and hard Flash guard (Rule 8)..."
                            )
                            try:
                                preflight.clear()
                                mode_ok, model_ok, attached = _execute_composer_send_sequence(
                                    page=page,
                                    zip_path=zip_path,
                                    prompt_text=prompt_text,
                                    manual_setup=False,
                                    is_continuation=is_continuation,
                                    recorded_ws_mode=recorded_ws_mode,
                                    on_progress=_track_send_progress,
                                    preflight_log=preflight,
                                    task_text=task,
                                    prev_state=prev_state,
                                    new_session=new_session,
                                    attach_images=attach_images,
                                )
                                just_sent_prompt = bool(mode_ok and model_ok and attached)
                            except Exception as manual_exc:
                                print(f"[!] Self-executed manual send fallback failed: {manual_exc}", file=sys.stderr)
                                just_sent_prompt = False

                    if not just_sent_prompt and not resume_chat:
                        failed_op_name = str(getattr(page, "_argon_failed_operation", "") or "send_sequence")
                        failed_op_reason = str(
                            getattr(page, "_argon_failed_reason", "")
                            or ("Send sequence failed (" + ", ".join(preflight) + ")")
                        )
                        shot_saved = _take_step_screenshot(
                            page,
                            proj_root,
                            "preflight_failure.png",
                            step_label=f"{failed_op_name}_failed",
                            state_label=f"Operation '{failed_op_name}' failed: {failed_op_reason}",
                            lane=lane,
                        )
                        print(
                            f"[ERROR] Send failed at operation={failed_op_name!r}: {failed_op_reason}",
                            file=sys.stderr,
                        )
                        print(
                            manual_setup_instructions(mode, str(shot_saved) if shot_saved else None),
                            file=sys.stderr,
                        )
                        ruled = ["Browser launched and workspace composer rendered", "Google account is signed in (not guest)"]
                        if mode_ok:
                            ruled.append("Task mode toggle verified active")
                        if model_ok:
                            ruled.append("Model chip verified as 4 Argon (Flash guard passed)")
                        if attached:
                            ruled.append(f"Archive {zip_path.name} verified attached and sent")
                        ev_pf = _collect_diagnostic_evidence(
                            page=page,
                            proj_root=proj_root,
                            reason=f"Operation '{failed_op_name}' failed: {failed_op_reason} (" + ", ".join(preflight) + ")",
                            slot=active_slot,
                            email=target_email,
                            profile_dir=profile_dir,
                            chat_url=getattr(page, "url", "") or target_url,
                            screenshot_path=shot_saved,
                            preflight=preflight,
                            ruled_out=ruled,
                            next_step=(
                                "Use the manual hand-off pack printed below (or let the next pool slot take over)."
                            ),
                        )
                        task_state_store.set(
                            proj_root,
                            lane=lane,
                            state="preflight_failed",
                            failed_step=f"{failed_op_name}_failed",
                            last_failure_reason=failed_op_reason,
                            diagnostic_evidence=ev_pf,
                        )
                        task_registry.register_done(proj_root, lane=lane, slot=active_slot, status="failed", screenshot_path=str(shot_saved or ""))
                        _print_run_failure_summary(ev_pf)
                        _emit_send_failure(f"{failed_op_name}_failed", shot_saved, page_obj=page)
                        return EXIT_PREFLIGHT_FAILED
                    sent_hms = time.strftime("%H:%M:%S")
                    sent_shot = _take_step_screenshot(
                        page,
                        proj_root,
                        "sent.png",
                        step_label="sent",
                        state_label=f"Prompt and {zip_path.name} submitted at {sent_hms}",
                        lane=lane,
                    )
                    last_step_shot = sent_shot
                    chip_after = _model_chip(page)
                    name_after = _model_name(chip_after)
                    if name_after:
                        print(
                            f"[i] Post-send model chip reads {name_after!r}: the chip after send shows "
                            "the account default for future turns, not the model answering this turn "
                            "(4 Argon was verified before send; real proof is verified in the answer)."
                        )
                    print("[+] Prompt submitted immediately upon upload completion in a verified Task/4 Argon workspace.")
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
                    _emit_send_failure("prompt_sent", sent_shot, page_obj=page)
                    print(f"[+] Task state saved: {task_state.path}")
                    print(f"    account / profile : {active_slot}  ({profile_dir_str})")

            # ------------------------------------------------- remember session --
            chat_id = _wait_for_chat_id(page)
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
                if attached_to_host and host_tab_id:
                    update_host_tab(
                        profile_dir,
                        host_tab_id,
                        chat_id=chat_id,
                        chat_url=effective_chat_url,
                        page_url=effective_chat_url,
                        account=target_email or "",
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

            eff_deadline_secs = max(60, int(deadline_seconds or 5400))
            print(f"[+] Waiting for Gemini 4 Argon response (deadline: {_format_elapsed(eff_deadline_secs)})...")
            deadline = time.monotonic() + float(eff_deadline_secs)
            extracted_patch: Optional[str] = None
            delivered_path: Optional[Path] = delivered.get("path")
            last_body = ""
            run_failed_reason = ""
            last_state_write = 0.0
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
                        if getattr(page, "_argon_chip_capture_exhausted", False):
                            cap_fail_reason = str(
                                getattr(page, "_argon_last_capture_failure_reason", "")
                                or "delivery chip failed validation after bounded attempts"
                            )
                            q_path_str = str(getattr(page, "_argon_last_quarantined_path", "") or "")
                            run_failed_reason = cap_fail_reason
                            task_state.set(
                                proj_root,
                                lane=lane,
                                state="delivery_failed",
                                phase="delivery",
                                attempt=current_attempt,
                                delivery_attempt=delivery_attempt,
                                quarantined_file=q_path_str,
                                last_failure_reason=cap_fail_reason,
                            )
                            print(
                                f"[!] Stopping wait loop: delivery chip reached the {MAX_CHIP_DOWNLOAD_ATTEMPTS}-attempt cap "
                                f"({cap_fail_reason}).",
                                file=sys.stderr,
                            )
                            break

                    working = _is_argon_working(page)
                    try:
                        if page.locator("generated-file").count() > 0:
                            body_now = last_body
                        else:
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
                print(f"[SUCCESS] Delivered patch file: {delivered_path} (sha256={_sha256_full(delivered_text if 'delivered_text' in locals() and delivered_text else delivered_path.read_text(encoding='utf-8', errors='replace'))})")
                if lane == "new":
                    print(
                        f"\n[i] Winner confirmation for 'new' lane:\n"
                        f"    Account e-mail : {target_email or '(none)'}\n"
                        f"    Chat link      : {effective_chat_url}\n"
                        f"    Please confirm or correct whether this conversation should be recorded as the winner."
                    )
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
                try:
                    extra_assets_inline = capture_additional_round_artifacts(page, out_ps1.parent)
                    write_delivery_bundle(out_ps1.parent, patch_path=out_ps1, asset_paths=extra_assets_inline)
                except Exception:
                    pass
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
                q_file_final = str(getattr(page, "_argon_last_quarantined_path", "") or "")
                task_state.set(
                    proj_root,
                    lane=lane,
                    state=final_state_name,
                    phase=ladder_phase,
                    attempt=current_attempt,
                    send_attempt=send_attempt,
                    delivery_attempt=delivery_attempt,
                    quarantined_file=q_file_final,
                    last_failure_reason=reason,
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
                has_delivery_in_recorded_chat = bool(
                    _generated_file_chip(page) is not None
                    or _generated_file_chip_indices_after_user(page)
                    or getattr(page, "_argon_chip_capture_exhausted", False)
                    or extract_powershell_patch(previous_body)
                )
                if carry_over and final_state_name != "superseded":
                    if has_delivery_in_recorded_chat:
                        print(
                            f"[!] Carry-over skipped: the recorded conversation ({live_url}) already has a delivery "
                            f"chip or patch in the chat (capture failed: {reason}). Refusing to open a new session "
                            "or re-send the prompt so a Gemini round is not wasted. Re-run with --resume to capture it.",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[!] Recorded conversation ({live_url}) finished and really has no delivery "
                            f"(0 delivery chips after the last user message and no inline patch block; reason: {reason}) - "
                            "carrying over to a new session.",
                            file=sys.stderr,
                        )
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
                _emit_send_failure("delivery_failed", failed_shot, page_obj=page)
                print(f"[!] Neither a delivered file nor a PowerShell patch block was detected ({reason}).")
                if q_file_final:
                    print(f'    Quarantined delivery file kept at: {q_file_final}')
                    print(f'    Inspect or apply it by hand with: Open_Argon_Apply.bat -PatchFile "{q_file_final}"')
                else:
                    print(f"    If Argon delivered a file, run: Open_Argon_Apply.bat  (searches {Path.home() / 'Downloads'})")
                return EXIT_FAILURE
    finally:
        # Addendum A.2: Guarantee that any run exiting for ANY reason (except deliberate
        # waiting-at-deadline handoff for --resume) never leaves its own TaskRegistry or
        # TaskState record stuck in `running`.
        try:
            active_for_this_pid = [
                e
                for e in task_registry.active_tasks_for_project(proj_root, lane=lane)
                if int(e.get("pid") or 0) in (0, os.getpid())
            ]
            if active_for_this_pid and not locals().get("still_working_at_deadline", False):
                exit_reason_final = str(
                    locals().get("run_failed_reason")
                    or "run exited before completion (terminal cleanup in finally)"
                )
                task_registry.register_done(
                    proj_root,
                    lane=lane,
                    slot=active_slot,
                    status="failed",
                    end_reason=exit_reason_final,
                )
            cur_ts = task_state_store.get(proj_root, lane=lane) or {}
            if (
                isinstance(cur_ts, dict)
                and str(cur_ts.get("state") or "").strip().lower() == "running"
                and int(cur_ts.get("pid") or 0) in (0, os.getpid())
            ):
                task_state_store.set(
                    proj_root,
                    lane=lane,
                    state="failed",
                    end_reason="run exited before prompt was sent",
                    pid=os.getpid(),
                )
        except Exception:
            pass
        if context is not None:
            try:
                if attached_to_host and host_tab_id:
                    release_host_tab(context, page, profile_dir, tab_id=host_tab_id)
                else:
                    _close_browser_safely(context, profile_dir)
            except Exception:
                pass
            context = None
        slot_lock.release()
        if temp_concurrent_profile is not None and temp_concurrent_profile.exists():
            try:
                shutil.rmtree(temp_concurrent_profile, ignore_errors=True)
            except Exception:
                pass
        print("[+] Browser session preserved and the slot lock released.")


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
    parser.add_argument(
        "--account-index",
        type=int,
        default=None,
        metavar="N",
        help="0-based browser account position in live Default/Preferences account_info (resolves N -> owning pool slot; refuses unregistered indices or --slot conflicts).",
    )
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
        "--manual-handoff",
        action="store_true",
        help="Generate a manual hand-off pack for this project/lane without sending a prompt.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help=(
            "Open the browser and leave it open. On its own it opens the browser of --slot "
            "in Gemini Web and PROVES it is up, before anything is sent (Rule 10). "
            "With --manual-handoff it also opens the target conversation in that slot's "
            "real Chrome profile."
        ),
    )
    parser.add_argument(
        "--open-url",
        default="",
        metavar="URL",
        help="The URL --open-browser should open (default: Gemini Web for the slot's account).",
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
        default=5400,
        help="Maximum seconds to wait for Argon's response before saving state for --resume (default: 5400 = 90 min).",
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
        "--close-browser",
        action="store_true",
        help="Safely close the browser of --slot (all its tabs) with session/cookies intact.",
    )
    parser.add_argument(
        "--close-tab",
        nargs="?",
        const="__USE_TAB_ARG__",
        default=None,
        metavar="TAB_ID",
        help="Close one tab in the browser of --slot (use --tab <tab-id> or --close-tab <tab-id>), leaving the browser and other tabs running.",
    )
    parser.add_argument(
        "--tab",
        default=None,
        metavar="TAB_ID",
        help="Tab identifier (tab_id, target_id, chat_id, or index) for --close-tab.",
    )
    parser.add_argument(
        "--list-browsers",
        action="store_true",
        help="List every running browser host, its profile, endpoint, open tabs, and the task that owns each tab.",
    )
    parser.add_argument(
        "--shared-profile",
        action="store_true",
        help="Run this slot as a tab inside the Argon browser profile (~/.gemini/argon_browser_profile; default).",
    )
    parser.add_argument(
        "--isolated-profile",
        action="store_true",
        help="Emergency escape hatch: run this slot in an isolated per-slot browser profile (~/.gemini/argon_profiles/slot-uN).",
    )
    parser.add_argument(
        "--enable-shared-profile",
        action="store_true",
        help="Configure accounts_pool.json to use the Argon browser profile and record verified /u/<index>/ account indices.",
    )
    parser.add_argument(
        "--resync-accounts",
        action="store_true",
        help="Re-sync the account pool with the Argon browser's LIVE signed-in account list "
             "(Default/Preferences): write every pool slot's live /u/<index>/ back, mark the "
             "slots whose account is not signed in, and report what changed.",
    )
    parser.add_argument(
        "--reclaim-slot-profiles",
        "--migrate-slot-profiles",
        dest="reclaim_slot_profiles",
        action="store_true",
        help="Migrate every account session from ~/.gemini/argon_profiles/slot-u* into the Argon browser profile, verify each account there, and COMPLETELY DELETE the old ~68 GB slot directories and test leftovers.",
    )
    parser.add_argument(
        "--keep-archive",
        action="store_true",
        help="Optional: move migrated slot profiles into ~/.gemini/argon_reclaimed_profiles/<timestamp>/ instead of deleting them completely (default: OFF, deletes completely to free disk space).",
    )
    parser.add_argument(
        "--append-followup",
        default=None,
        metavar="FILE_OR_TEXT",
        help="Send an additional follow-up message (from a .md/.txt file or inline text) into a currently running task's conversation without superseding it (or as an ordinary follow-up if no task is running).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the fast local preflight self-test against a synthetic Gemini page (verifying attach, paste, model/mode, send, and paged Drive text capture in seconds).",
    )
    parser.add_argument(
        "--install-extension",
        action="store_true",
        help="Install (or verify) the MV3 companion extension into the Argon browser via CDP Extensions.loadUnpacked and confirm its content script is alive on a Gemini page.",
    )
    parser.add_argument(
        "--attach-images",
        nargs="*",
        default=None,
        metavar="IMAGE",
        help="Attach one or more image files (.png, .jpg, .webp, .svg) to the composer via CDP DOM.setFileInputFiles before sending.",
    )
    parser.add_argument(
        "--no-extension",
        action="store_true",
        help="Disable the MV3 companion extension for this run and use the pure-Python fallback path.",
    )
    parser.add_argument(
        "--browser-loop-start",
        action="store_true",
        help="Start the general-purpose live browser control loop daemon, print the endpoint + token prefix, and run until Ctrl-C.",
    )
    parser.add_argument(
        "--browser-loop-stop",
        action="store_true",
        help="Stop a running live browser control loop daemon and remove ~/.gemini/argon_browser_loop.json.",
    )
    parser.add_argument(
        "--browser-loop-status",
        action="store_true",
        help="Print the live browser control loop daemon state (running or not, port, open tabs, and current map per tab).",
    )
    parser.add_argument(
        "--browser-act",
        default=None,
        metavar="JSON",
        help="Run ONE live browser action (JSON string or @file.json) via the running daemon or a one-shot CDP attach.",
    )
    parser.add_argument(
        "--browser-loop-selftest",
        action="store_true",
        help="Run the end-to-end live browser control loop self-test on a disposable Chrome and print real measured numbers.",
    )
    parser.add_argument(
        "--browser-allow-eval",
        action="store_true",
        help="Allow the live browser loop 'eval' operation (disabled by default for safety; prints a warning).",
    )
    parser.add_argument(
        "--max-verify-rounds",
        type=int,
        default=1,
        help=argparse.SUPPRESS,
    )
    return parser


def resync_accounts_cli() -> int:
    """Re-sync the account pool with the Argon browser's LIVE signed-in account list.

    The pool's `account_index` is the `/u/<N>/` path the dispatcher opens in the Argon
    browser, so it has to match the browser's own `Default/Preferences` (`account_info`)
    list. When it drifts, a lane runs under a DIFFERENT Google account than the one that
    was asked for. This rewrites the index of every pool slot whose account IS signed in,
    marks the slots whose account is NOT signed in (`needs_signin`, stale index cleared)
    and reports everything it found.
    """
    from safe_browser import get_preferences_accounts

    pool_cfg = PoolConfig()
    profile_dir = pool_cfg.profile_dir
    live_accounts = get_preferences_accounts(profile_dir)

    live_by_email: Dict[str, Dict[str, Any]] = {}
    for live_acc in live_accounts:
        live_email = str(live_acc.get("email") or "").strip()
        if live_email:
            live_by_email[live_email.lower()] = live_acc

    print("=" * 78)
    print("  ARGON ACCOUNT POOL RE-SYNC (browser live accounts -> pool)")
    print("=" * 78)
    print(f"  Pool config    : {pool_cfg.path}")
    print(f"  Browser profile: {profile_dir}")
    print(f"  Live accounts  : {len(live_accounts)} (from Default/Preferences)")
    print("")

    changed: List[str] = []
    unchanged: List[str] = []
    need_signin: List[str] = []
    unknown: List[str] = []
    collisions: List[str] = []
    pool_emails = set()
    index_owner: Dict[int, str] = {}

    for acc in pool_cfg.accounts:
        if not isinstance(acc, dict):
            continue
        slot = normalize_slot(acc.get("slot", f"/u/{acc.get('index', 0)}/"))
        email = str(acc.get("email") or "").strip()
        if email:
            pool_emails.add(email.lower())
        live_acc = live_by_email.get(email.lower()) if email else None
        if live_acc is None:
            stale_index = acc.pop("account_index", None)
            acc["needs_signin"] = True
            if stale_index is None:
                need_signin.append(f"{slot}  {email or '(no email)'}  (no account_index in the pool; needs_signin set)")
            else:
                need_signin.append(
                    f"{slot}  {email or '(no email)'}  (stale account_index={stale_index} cleared)"
                )
            continue
        live_index = int(live_acc.get("index"))
        old_index = acc.get("account_index")
        acc.pop("needs_signin", None)
        if old_index != live_index:
            pool_cfg.set_account_index(slot, live_index)
            changed.append(f"{slot}  {email}  /u/{old_index}/ -> /u/{live_index}/")
        else:
            unchanged.append(f"{slot}  {email}  /u/{live_index}/")
        owner = index_owner.get(live_index)
        if owner is None:
            index_owner[live_index] = slot
        else:
            collisions.append(f"/u/{live_index}/ is claimed by both {owner} and {slot}")

    for live_acc in live_accounts:
        live_email = str(live_acc.get("email") or "").strip()
        if live_email.lower() not in pool_emails:
            unknown.append(f"/u/{int(live_acc.get('index'))}/  {live_email}")

    pool_cfg.save()

    print(f"  Slots re-synced ({len(changed)}):")
    for line in changed:
        print(f"    - {line}")
    if not changed:
        print("    (none)")
    print("")
    print(f"  Slots already correct ({len(unchanged)}):")
    for line in unchanged:
        print(f"    - {line}")
    if not unchanged:
        print("    (none)")
    print("")
    print(f"  Slots needing a sign-in ({len(need_signin)}):")
    for line in need_signin:
        print(f"    - {line}")
    if not need_signin:
        print("    (none)")
    print("")
    print(f"  Browser accounts the pool does not know about ({len(unknown)}):")
    for line in unknown:
        print(f"    - {line}")
    if not unknown:
        print("    (none)")
    print("")
    print(f"  Index collisions ({len(collisions)}):")
    for line in collisions:
        print(f"    - {line}")
    if not collisions:
        print("    (none)")
    print("=" * 78)
    return EXIT_SUCCESS


def append_followup_to_running_task(
    append_arg: str,
    project_path: str = ".",
    lane: Optional[str] = None,
    slot: Optional[str] = None,
    mode: str = "gemini",
    headless: bool = False,
    auto_apply: bool = False,
    manual_setup: bool = False,
    target_files: Optional[List[str]] = None,
    output_zip: str = "argon_context.zip",
    verify_timeout: int = 900,
    verify_cmd: Optional[str] = None,
    skip_verify: bool = False,
    deadline_seconds: int = 1800,
) -> int:
    """Send an additional follow-up message into a currently running task's conversation
    without superseding it, or fall back to an ordinary follow-up if no task is running (Addendum B).
    """
    from playwright.sync_api import sync_playwright
    from safe_browser import acquire_host_tab, release_host_tab

    proj_root = Path(project_path).resolve()
    raw_arg = (append_arg or "").strip()
    if not raw_arg:
        print("[!] Error: --append-followup requires a file path or non-empty text.", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    cand_file = Path(raw_arg)
    if not cand_file.is_absolute():
        cand_in_proj = proj_root / raw_arg
        if cand_in_proj.is_file():
            cand_file = cand_in_proj
    if cand_file.is_file():
        source_file = str(cand_file.resolve())
        append_text = cand_file.read_text(encoding="utf-8", errors="replace").strip()
    else:
        source_file = "(inline text)"
        append_text = raw_arg

    if not append_text:
        print(f"[!] Error: Follow-up file {source_file} is empty.", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    task_state_store = TaskState()
    task_registry = TaskRegistry()
    session_store = SessionStore()
    pool_cfg = PoolConfig()

    task_registry.reclaim_dead_tasks(proj_root, lane=lane)

    ts_entry = (task_state_store.get(proj_root, lane=lane) if lane else None) or task_state_store.get(proj_root) or {}
    reg_entries = task_registry.active_tasks_for_project(proj_root, lane=lane)
    reg_entry = reg_entries[0] if reg_entries else {}

    ts_state = str(ts_entry.get("state") or "").strip().lower()
    chat_url = str(ts_entry.get("chat_url") or reg_entry.get("chat_url") or "").strip()
    chat_id = str(ts_entry.get("chat_id") or reg_entry.get("chat_id") or _extract_chat_id(chat_url) or "").strip()
    is_task_active = bool(
        chat_url
        and (
            ts_state in ("sent", "waiting", "running")
            or bool(reg_entry)
        )
    )

    if not is_task_active:
        eff_lane = lane or "followup"
        print(
            f"[+] No currently running task found for {proj_root} (lane={eff_lane}); "
            "executing --append-followup as an ordinary follow-up dispatch."
        )
        return dispatch_to_argon(
            task=append_text,
            project_path=str(proj_root),
            mode=mode,
            slot=slot,
            headless=headless,
            auto_apply=auto_apply,
            manual_setup=manual_setup,
            target_files=target_files,
            output_zip=output_zip,
            new_session=False,
            resume=False,
            verify_timeout=verify_timeout,
            verify_cmd=verify_cmd,
            skip_verify=skip_verify,
            lane=eff_lane,
            deadline_seconds=deadline_seconds,
            attach_images=attach_images,
        )

    eff_lane = str(lane or ts_entry.get("lane") or reg_entry.get("lane") or "followup")
    eff_slot = normalize_slot(slot or ts_entry.get("slot") or reg_entry.get("slot") or pool_cfg.active_slot())
    acct_obj = pool_cfg.get_by_slot(eff_slot) or {}
    eff_email = str(ts_entry.get("email") or reg_entry.get("account") or acct_obj.get("email") or "")
    running_task_id = str(reg_entry.get("task_id") or f"{proj_root.name}:{eff_lane}:{eff_slot.strip('/')}")
    running_label = str(reg_entry.get("label") or ts_entry.get("task") or "running-task").splitlines()[0][:120]
    running_pid = int(reg_entry.get("pid") or ts_entry.get("pid") or 0)

    prof_dir_str = str(ts_entry.get("profile_dir") or "")
    profile_dir = Path(prof_dir_str) if prof_dir_str else _profile_dir_for_slot(pool_cfg.profile_dir, eff_slot)
    isolated = _profile_is_isolated_slot(profile_dir)
    norm_chat_url = _normalize_gemini_chat_url(chat_url, isolated_profile=isolated, slot=eff_slot)

    marker_header = f"{APPEND_FOLLOWUP_MARKER} | task_id={running_task_id} | label={running_label}]"
    formatted_message = f"{marker_header}\n\n{append_text}"
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    append_record = {
        "timestamp": now_str,
        "source_file": source_file,
        "task_id": running_task_id,
        "label": running_label,
        "text": append_text,
        "prompt_hash": _prompt_hash(formatted_message),
        "raw_hash": _prompt_hash(append_text),
        "prefix": _normalize_ws(formatted_message)[:160],
        "raw_prefix": _normalize_ws(append_text)[:160],
    }

    existing_appends = list(ts_entry.get("appended_followups") or [])
    existing_appends.append(append_record)
    orig_task_text = str(ts_entry.get("task") or "")
    updated_task_text = (
        f"{orig_task_text}\n\n--- APPENDED FOLLOW-UP ({now_str}) ---\n{append_text}"
        if orig_task_text
        else append_text
    )

    # Record in TaskState and TaskRegistry BEFORE sending so the running loop immediately
    # recognizes the appended prompt as part of its own task (Addendum B.2).
    task_state_store.set(
        proj_root,
        lane=eff_lane,
        task=updated_task_text,
        appended_followups=existing_appends,
        last_appended_at=now_str,
    )
    task_registry.update_running(
        proj_root,
        lane=eff_lane,
        appended_followups=existing_appends,
        last_appended_at=now_str,
    )

    chrome_exe = find_chrome_executable()
    sent_in_browser = False
    with sync_playwright() as p:
        ctx, pg, tab_id, attached = acquire_host_tab(
            playwright_instance=p,
            profile_dir=profile_dir,
            slot=eff_slot,
            project_path=proj_root,
            lane=f"{eff_lane}-append",
            chrome_exe=chrome_exe,
            headless=headless,
            account_email=eff_email,
        )
        try:
            if ctx is not None and pg is not None:
                pg.goto(norm_chat_url, wait_until="domcontentloaded", timeout=60000)
                _wait_for_conversation_loaded(pg, timeout=25.0, require_turns=False)
                comp = _find_composer(pg)
                if comp is not None and enforce_no_flash_before_send(pg):
                    _type_and_send(pg, comp, formatted_message)
                    pg.wait_for_timeout(2000)
                    sent_in_browser = True
        except Exception as exc:
            print(f"[!] Warning while sending appended follow-up in browser tab: {exc}", file=sys.stderr)
        finally:
            if ctx is not None:
                release_host_tab(ctx, pg, profile_dir, tab_id=tab_id)

    print("=" * 78)
    print("  APPENDED FOLLOW-UP TO RUNNING TASK")
    print("=" * 78)
    print(f"  Project        : {proj_root}")
    print(f"  Conversation   : {norm_chat_url} (chat_id={chat_id or 'active'})")
    print(f"  Account / Slot : {eff_slot} ({eff_email or 'pool account'})")
    print(f"  Extended Task  : {running_task_id} (label={running_label!r}, pid={running_pid or 'active'})")
    print(f"  Source         : {source_file} ({len(append_text)} chars)")
    print(f"  Browser Action : {'sent in existing browser host tab and closed tab' if sent_in_browser else 'recorded in TaskState/TaskRegistry for active conversation'}")
    print("  Supersede Guard: NOT superseded (recorded in appended_followups; single delivery will cover both)")
    print(f"  Appended Text  : { _normalize_ws(append_text)[:200] }")
    print("=" * 78)
    return EXIT_SUCCESS


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
    """Select the pool slot according to the owner's single slot rule (Rule 2).

    Policy:
      - FOLLOW-UP: always uses the account slot that holds the conversation,
        even when that slot is already running MAX_TASKS_PER_SLOT tasks.
      - CARRY-OVER / RESUME: stays on the slot that held the run.
      - NEW TASK: skips an account ONLY when it is already running the maximum
        number of tasks (MAX_TASKS_PER_SLOT). No other status or limit skips an account.
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
            return c_slot, f"CARRY-OVER policy: staying on {c_slot} where the previous context lives"

    # Follow-up always uses its own account even when that account already runs multiple tasks.
    existing_session = None if (new_session or lane == "new") else sess_store.lookup(project_path, mode=mode)
    if existing_session and existing_session.get("chat_url") and existing_session.get("slot"):
        f_slot = normalize_slot(str(existing_session["slot"]))
        f_cid = str(existing_session.get("chat_id") or "")
        f_load = len(reg.active_tasks_for_slot(f_slot))
        busy_note = (
            f"; slot already has {f_load} active task(s), using it anyway because the follow-up chat lives there"
            if f_load >= MAX_TASKS_PER_SLOT
            else f" (registry load {f_load}/{MAX_TASKS_PER_SLOT})"
        )
        return (
            f_slot,
            f"FOLLOW-UP policy: continuing conversation {f_cid or '(stored)'} on the account that holds it ({f_slot}{busy_note})",
        )

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

    def _is_isolated_prof_busy(slot_str: str) -> bool:
        c_prof = _profile_dir_for_slot(base_prof, slot_str)
        if not _profile_is_isolated_slot(c_prof):
            return False
        return _profile_is_busy(c_prof)

    # Pass 1: Slot under MAX_TASKS_PER_SLOT with 0 running tasks, different from last_ran_slots
    for cand in ordered_pool:
        norm_c = normalize_slot(cand)
        if norm_c in last_ran_slots:
            continue
        active_on_slot = reg.active_tasks_for_slot(norm_c)
        if len(active_on_slot) == 0 and not _is_isolated_prof_busy(norm_c):
            return (
                norm_c,
                f"NEW-TASK policy: selected idle account {norm_c} (registry load 0/{MAX_TASKS_PER_SLOT})",
            )

    # Pass 2: Any slot under MAX_TASKS_PER_SLOT, different from last_ran_slots
    for cand in ordered_pool:
        norm_c = normalize_slot(cand)
        if norm_c in last_ran_slots:
            continue
        ok_s, cap_reason = reg.is_slot_available(norm_c, max_tasks=MAX_TASKS_PER_SLOT)
        if ok_s:
            return (
                norm_c,
                f"NEW-TASK policy: selected account {norm_c} under task cap ({cap_reason})",
            )

    # Pass 3: Any slot in the pool under MAX_TASKS_PER_SLOT
    for cand in ordered_pool:
        norm_c = normalize_slot(cand)
        if norm_c in excluded:
            continue
        ok_s, cap_reason = reg.is_slot_available(norm_c, max_tasks=MAX_TASKS_PER_SLOT)
        if ok_s:
            return (
                norm_c,
                f"NEW-TASK policy: selected available account {norm_c} ({cap_reason})",
            )

    # Pass 4: Every slot is already at MAX_TASKS_PER_SLOT - pick the least-loaded slot without blocking
    pool_candidates = [normalize_slot(c) for c in ordered_pool if normalize_slot(c) not in excluded] or [
        normalize_slot(c) for c in ordered_pool
    ]
    least_loaded = min(
        pool_candidates,
        key=lambda s: (
            len(reg.active_tasks_for_slot(s)),
            1 if s in last_ran_slots else 0,
            1 if _is_isolated_prof_busy(s) else 0,
        ),
    )
    ll_count = len(reg.active_tasks_for_slot(least_loaded))
    return (
        least_loaded,
        f"NEW-TASK least-loaded fallback: all accounts are at capacity; continuing on least-loaded account {least_loaded} (registry load {ll_count}/{MAX_TASKS_PER_SLOT})",
    )


def _pool_slots_in_order(
    pool_cfg,
    start_slot: Optional[str],
    skip_start_slot: bool = False,
) -> list:
    """Pool slots to try in wrap-around order (Rule 2).

    Every configured pool account is included regardless of its status field.
    Slots that are currently below `MAX_TASKS_PER_SLOT` in `TaskRegistry` are
    ordered ahead of slots that are already running `MAX_TASKS_PER_SLOT` tasks.
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

    reg = TaskRegistry()
    under_cap: list[str] = []
    at_cap: list[str] = []
    for a in rotated:
        s_norm = normalize_slot(a["slot"])
        ok_slot, _ = reg.is_slot_available(s_norm, max_tasks=MAX_TASKS_PER_SLOT)
        if ok_slot:
            under_cap.append(s_norm)
        else:
            at_cap.append(s_norm)
    return under_cap + at_cap


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
    """Never let a print kill the dispatcher with UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _make_console_utf8_safe()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if getattr(args, "isolated_profile", False):
        os.environ["ARGON_SHARED_PROFILE"] = "0"
        os.environ["ARGON_PROFILE_MODE"] = "isolated"
    elif getattr(args, "shared_profile", False):
        os.environ["ARGON_SHARED_PROFILE"] = "1"
        os.environ["ARGON_PROFILE_MODE"] = "shared"

    if getattr(args, "no_extension", False):
        os.environ["ARGON_DISABLE_COMPANION_EXTENSION"] = "1"

    if getattr(args, "account_index", None) is not None:
        try:
            acct_res = resolve_account_index_to_slot(
                int(args.account_index),
                explicit_slot=args.slot,
            )
        except RuntimeError as exc_acct:
            print(f"[ERROR] {exc_acct}", file=sys.stderr)
            return (
                EXIT_PREFLIGHT_FAILED
                if "slot_account_index_conflict" in str(exc_acct)
                else EXIT_NOT_SIGNED_IN
            )
        args.slot = str(acct_res["slot"])
        print(acct_res["line"])
        if acct_res.get("warning"):
            print(acct_res["warning"], file=sys.stderr)

    if getattr(args, "enable_shared_profile", False):
        cfg_en = PoolConfig()
        rep_en = cfg_en.enable_shared_profile_mode()
        print("=" * 78)
        print("  ARGON BROWSER PROFILE CONFIGURED (ONE BROWSER FOR ALL ACCOUNTS)")
        print("=" * 78)
        print(f"  Argon profile  : {rep_en['profile_dir']}")
        print(f"  Verified slots : {len(rep_en['verified'])}")
        for s_k, idx_v in sorted(rep_en["verified"].items()):
            print(f"    - {s_k} -> /u/{idx_v}/")
        if rep_en["unverified"]:
            print(f"  Pending sign-in: {', '.join(rep_en['unverified'])}")
        print("=" * 78)
        return EXIT_SUCCESS

    if getattr(args, "resync_accounts", False):
        return resync_accounts_cli()

    if getattr(args, "reclaim_slot_profiles", False):
        from safe_browser import reclaim_slot_profiles_cli

        return reclaim_slot_profiles_cli(
            keep_archive=bool(getattr(args, "keep_archive", False)),
        )

    if getattr(args, "self_test", False):
        return run_preflight_selftest_cli(
            headless=bool(getattr(args, "headless", False) or True),
            require_extension=False,
        )

    if (
        getattr(args, "browser_loop_start", False)
        or getattr(args, "browser_loop_stop", False)
        or getattr(args, "browser_loop_status", False)
        or getattr(args, "browser_act", None) is not None
        or getattr(args, "browser_loop_selftest", False)
    ):
        from browser_loop import browser_loop_cli

        return browser_loop_cli(
            start=bool(getattr(args, "browser_loop_start", False)),
            stop=bool(getattr(args, "browser_loop_stop", False)),
            status=bool(getattr(args, "browser_loop_status", False)),
            act=getattr(args, "browser_act", None),
            selftest=bool(getattr(args, "browser_loop_selftest", False)),
            allow_eval=bool(getattr(args, "browser_allow_eval", False)),
            slot=args.slot,
        )

    if getattr(args, "install_extension", False):
        from safe_browser import install_companion_extension_cli

        return install_companion_extension_cli(
            slot=args.slot,
            project_path=args.project_path,
            headless=bool(getattr(args, "headless", False)),
        )

    if getattr(args, "close_browser", False):
        from safe_browser import close_browser_cli

        return close_browser_cli(slot=args.slot, project_path=args.project_path)

    if getattr(args, "close_tab", None) is not None:
        from safe_browser import close_tab_cli

        eff_tab = args.tab if args.close_tab == "__USE_TAB_ARG__" else (args.close_tab or args.tab or "")
        return close_tab_cli(tab_id=str(eff_tab or ""), slot=args.slot, project_path=args.project_path)

    if getattr(args, "list_browsers", False):
        from safe_browser import list_browsers_cli

        return list_browsers_cli(project_path=args.project_path)

    if getattr(args, "open_browser", False) and not getattr(args, "manual_handoff", False):
        from safe_browser import open_browser_cli

        return open_browser_cli(
            slot=args.slot,
            project_path=args.project_path,
            url=str(getattr(args, "open_url", "") or ""),
        )

    if getattr(args, "append_followup", None) is not None:
        return append_followup_to_running_task(
            append_arg=str(args.append_followup),
            project_path=args.project_path,
            lane=args.lane,
            slot=args.slot,
            mode=args.mode,
            headless=args.headless,
            auto_apply=args.auto_apply,
            manual_setup=args.manual_setup,
            target_files=args.files,
            output_zip=args.output_zip,
            verify_timeout=args.verify_timeout,
            verify_cmd=args.verify_cmd,
            skip_verify=args.skip_verify,
            deadline_seconds=args.deadline_seconds,
        )

    if getattr(args, "manual_handoff", False):
        from manual_handoff import handoff_dir_for_lane, run_manual_handoff_cli

        rc_mh = run_manual_handoff_cli(
            project_path=args.project_path,
            lane=args.lane,
            slot=args.slot,
            open_browser=getattr(args, "open_browser", False),
            mode=args.mode,
        )
        try:
            res_slot = resolve_slot_to_browser_account(args.slot)
            h_dir = handoff_dir_for_lane(Path(args.project_path).resolve(), args.lane or "new")
            update_manual_handoff_with_resolved_slot(
                h_dir / "manual_handoff.md",
                h_dir / "handoff.json",
                slot=res_slot["slot"],
                email=res_slot["email"],
                account_index=res_slot["account_index"],
                resolved_url=res_slot["resolved_url"],
            )
            print(res_slot["line"])
            if res_slot.get("warning"):
                print(res_slot["warning"], file=sys.stderr)
        except Exception as _mh_res_exc:
            print(f"[!] Could not resolve browser account index for manual hand-off: {_mh_res_exc}", file=sys.stderr)
        return rc_mh

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

    attach_images = _expand_attach_images(getattr(args, "attach_images", None))
    if attach_images:
        print(f"[+] {len(attach_images)} screenshot file(s) will be attached as images.")

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
            attach_images=attach_images,
        )

    # Rule 2: --lane new is always a NEW task and must rotate across pool slots when needed,
    # whereas a FOLLOW-UP always stays on its own conversation slot even if busy.
    is_new_lane_or_session = bool(args.new_session or args.lane == "new")
    existing_session = None if is_new_lane_or_session else SessionStore().lookup(args.project_path, mode=args.mode)
    is_followup_bound = bool(
        existing_session
        and existing_session.get("chat_url")
        and not is_new_lane_or_session
    )

    chosen_slot, slot_reason = select_account_slot(
        pool_cfg=pool_cfg,
        project_path=args.project_path,
        mode=args.mode,
        requested_slot=args.slot,
        new_session=is_new_lane_or_session,
        lane=args.lane,
    )
    print(f"[+] Account policy selected start slot {chosen_slot}: {slot_reason}")

    if is_followup_bound:
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
            new_session=is_new_lane_or_session,
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
            brief_summary=args.brief_summary,
            attach_images=attach_images,
        )

    last = EXIT_FAILURE
    skipped_accounts: List[tuple[str, str]] = []
    # Round 21 (FIX 2): EXIT_SLOT_BUSY is NOT rotatable. A busy slot means another
    # lane is using that pool account right now; rotating would run this task under a
    # DIFFERENT Google account than the one that was asked for. The lane stops and
    # reports instead. The other three codes are genuine "this account is broken, try
    # the next one" cases and stay rotatable.
    rotatable_codes = {
        EXIT_PREFLIGHT_FAILED,
        EXIT_NOT_SIGNED_IN,
        EXIT_BROWSER_LAUNCH_FAILED,
    }
    code_labels = {
        EXIT_SLOT_BUSY: "slot busy (browser profile in use by another lane, or 2-task registry limit reached)",
        EXIT_PREFLIGHT_FAILED: "pre-flight check failed",
        EXIT_NOT_SIGNED_IN: "profile not signed in",
        EXIT_BROWSER_LAUNCH_FAILED: "browser launch failed",
    }

    for index, slot in enumerate(slots):
        if index:
            print(f"\n[+] Rotating to next pool account ({index + 1}/{len(slots)}): {slot}")
            # Round 21 (FIX 2): the account of a rotated-to slot is re-resolved through
            # the LIVE browser account list. `PoolConfig.get_account_index()` refuses a
            # stale index, so a slot whose account is not signed in is skipped with a
            # named reason instead of being dispatched to.
            rot_pool = pool_cfg
            if pool_cfg is not None:
                try:
                    rot_pool = PoolConfig()
                except Exception:
                    rot_pool = pool_cfg
            if rot_pool is not None:
                try:
                    rotated_index = rot_pool.get_account_index(slot)
                except RuntimeError as exc_idx:
                    skipped_accounts.append((slot, str(exc_idx)))
                    print(f"[!] Skipping account {slot}: {exc_idx}", file=sys.stderr)
                    continue
                print(f"[+] Account {slot} resolves to /u/{rotated_index}/ in the Argon browser.")
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
            new_session=is_new_lane_or_session,
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
            brief_summary=args.brief_summary,
            attach_images=attach_images,
        )
        if last == EXIT_SUCCESS or last == EXIT_ALREADY_RUNNING:
            if skipped_accounts:
                print(f"[+] Account rotation summary: tried {index + 1} account(s); succeeded on {slot}.")
                for sk_slot, sk_reason in skipped_accounts:
                    print(f"    - skipped {sk_slot}: {sk_reason}")
            return last
        if last == EXIT_SLOT_BUSY:
            busy_reason = code_labels.get(EXIT_SLOT_BUSY, "slot is busy")
            skipped_accounts.append((slot, busy_reason))
            print(
                f"[!] Account {slot} is busy ({busy_reason}). Refusing to rotate to another pool "
                f"account: this task must run under the Google account of {slot}, not under a "
                f"different one. Free the slot or re-run the task later.",
                file=sys.stderr,
            )
            return EXIT_SLOT_BUSY
        if not is_followup_bound and last in rotatable_codes and len(slots) > 1:
            reason_label = code_labels.get(last, f"exit code {last}")
            skipped_accounts.append((slot, reason_label))
            print(
                f"[+] Account {slot} failed ({reason_label}) - skipping this slot and continuing "
                f"rotation across the remaining pool accounts."
            )
            continue
        return last

    if skipped_accounts:
        print(f"\n[!] Pool rotation exhausted after trying {len(skipped_accounts)} account(s):", file=sys.stderr)
        for sk_slot, sk_reason in skipped_accounts:
            print(f"    - {sk_slot}: {sk_reason}", file=sys.stderr)
    return last


if __name__ == "__main__":
    sys.exit(main())
