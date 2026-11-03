#!/usr/bin/env python3
"""
Side-by-side comparison and single-run merge tool for Gemini 4 Argon patches.

Responsibilities (D6, R6, R7):
1. Parse two delivered PowerShell patch scripts (follow-up lane vs new-session lane)
   and extract every target file and every replacement or file-creation block.
2. Classify every block into one of four categories:
   - identical in both
   - only in the follow-up
   - only in the new session
   - CONFLICTING (both changed the same text differently)
3. Write a self-contained side-by-side HTML report (`comparison.html`) per file and
   per block, including the two prompts that produced the patches when available,
   and open it with one click when `--open` is requested.
4. Build a single-run merged `apply_changes.ps1` (`merged_apply_changes.ps1`) from
   explicit per-conflict choices (`--choices`), or output one side alone (`--pick`).
   Every unresolved conflict is reported clearly and NEVER guessed.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

STATUS_IDENTICAL = "identical in both"
STATUS_ONLY_FOLLOWUP = "only in the follow-up"
STATUS_ONLY_NEW = "only in the new session"
STATUS_CONFLICT = "CONFLICTING"

OPEN_SQ = chr(64) + chr(39)
CLOSE_SQ = chr(39) + chr(64)
OPEN_DQ = chr(64) + chr(34)
CLOSE_DQ = chr(34) + chr(64)


def _normalize_rel_path(raw_path: str) -> str:
    cleaned = (raw_path or "").strip().strip("'\"")
    cleaned = cleaned.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _extract_target_root(patch_text: str) -> str:
    if not patch_text:
        return ""
    m = re.search(
        r"\$TargetRoot\s*=\s*['\"]([^'\"]+)['\"]",
        patch_text,
        flags=re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def _extract_notes_block(patch_text: str) -> str:
    if not patch_text:
        return ""
    m = re.search(
        r"(#\s*---\s*NOTES:.*?#\s*---\s*END\s+NOTES\s*---)",
        patch_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    out: List[str] = []
    for raw in patch_text.splitlines():
        s = raw.strip()
        if s.startswith("#") and re.search(r"(?i)\b(ROOT\s+CAUSE|NOTE:|VERIFICATION\s+COMMAND)\b", s):
            out.append(s)
    return "\n".join(out)


def parse_patch_script(patch_text: str) -> Dict[str, Any]:
    """Parse a PowerShell patch script and extract target root, blocks, and notes.

    Supports:
    - `$oldTarget = <here-string>` / `$newReplacement = <here-string>` (and numbered `$old1`/`$new1` variants)
    - hashtable/object replacement entries (`RelPath`/`File`, `Old`, `New`)
    - direct `.Replace(<here-string>, <here-string>)` calls
    - brand-new file writes (`[System.IO.File]::WriteAllText($path, $newContent, ...)`)
    """
    target_root = _extract_target_root(patch_text)
    notes_block = _extract_notes_block(patch_text)
    if not patch_text:
        return {"target_root": target_root, "blocks": [], "notes": notes_block}

    hs_pattern = re.compile(
        r"(" + re.escape(OPEN_SQ) + r"|" + re.escape(OPEN_DQ) + r")[ \t]*\r?\n(.*?)\r?\n("
        + re.escape(CLOSE_SQ) + r"|" + re.escape(CLOSE_DQ) + r")",
        flags=re.DOTALL,
    )
    file_assign_re = re.compile(
        r"(?:\$(?:relPath|rel|relativePath|file|targetRel)(?:_\w+|\d+)?\s*=\s*['\"]([^'\"]+)['\"]|"
        r"Join-Path\s+\$TargetRoot\s+['\"]([^'\"]+)['\"]|"
        r"(?:-RelPath|(?:RelPath|File)\s*=)\s*['\"]([^'\"]+)['\"])",
        flags=re.IGNORECASE,
    )
    var_assign_re = re.compile(
        r"(?:\$([A-Za-z_]\w*)|(OldTarget|Old|Search|NewReplacement|New|Replace|Content|NewContent))\s*=\s*$",
        flags=re.IGNORECASE,
    )

    raw_hs_items: List[Dict[str, Any]] = []
    for m in hs_pattern.finditer(patch_text):
        prefix = patch_text[: m.start()]
        current_file = "<unknown>"
        for fm in file_assign_re.finditer(prefix):
            cand = fm.group(1) or fm.group(2) or fm.group(3) or ""
            norm_cand = _normalize_rel_path(cand)
            if norm_cand and ".argon_backup" not in norm_cand:
                current_file = norm_cand

        prefix_tail = prefix[-300:]
        var_name = ""
        vm_list = list(var_assign_re.finditer(prefix_tail))
        if vm_list:
            last_vm = vm_list[-1]
            var_name = (last_vm.group(1) or last_vm.group(2) or "").strip()

        raw_hs_items.append(
            {
                "file": current_file,
                "var_name": var_name,
                "body": m.group(2),
                "start": m.start(),
                "end": m.end(),
            }
        )

    blocks: List[Dict[str, Any]] = []
    seen_keys: set = set()
    per_file_counts: Dict[str, int] = {}

    def _add_block(rel_file: str, old_txt: str, new_txt: str, is_new: bool = False) -> None:
        norm_f = _normalize_rel_path(rel_file)
        if not norm_f or norm_f == "<unknown>":
            return
        dedup_key = (norm_f, old_txt, new_txt, is_new)
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
        per_file_counts[norm_f] = per_file_counts.get(norm_f, 0) + 1
        blocks.append(
            {
                "file": norm_f,
                "old_text": old_txt,
                "new_text": new_txt,
                "is_new_file": is_new,
                "block_index": len(blocks) + 1,
                "file_block_index": per_file_counts[norm_f],
            }
        )

    pending_old_by_file: Dict[str, List[Tuple[str, str]]] = {}
    i = 0
    while i < len(raw_hs_items):
        item = raw_hs_items[i]
        f_path = item["file"]
        v_name = item["var_name"]
        v_lower = v_name.lower()
        body = item["body"]

        is_old_var = bool(
            v_lower.startswith(("old", "search", "target", "orig", "before"))
            or v_lower in ("old", "oldtarget", "search")
        )
        is_full_content_var = bool(
            v_lower in ("content", "filecontent", "scriptcontent", "text", "fullcontent", "newcontent")
            or v_lower.startswith(("newcontent", "new_content", "newfile", "fullcontent", "filebody"))
        )
        is_new_var = bool(
            not is_full_content_var
            and (
                v_lower.startswith(("new", "replace", "updated", "after"))
                or v_lower in ("new", "newreplacement", "replace")
            )
        )

        if is_old_var:
            suffix = re.sub(r"^(?:old_?target|old|search|target|orig|before)_?", "", v_lower)
            pending_old_by_file.setdefault(f_path, []).append((suffix, body))
            i += 1
            continue

        if is_new_var:
            suffix = re.sub(r"^(?:new_?replacement|new|replace|updated|after)_?", "", v_lower)
            pend_list = pending_old_by_file.get(f_path) or []
            if pend_list:
                matched_idx = 0
                for idx_p, (p_suf, _) in enumerate(pend_list):
                    if p_suf and suffix and p_suf == suffix:
                        matched_idx = idx_p
                        break
                _, old_body = pend_list.pop(matched_idx)
                _add_block(f_path, old_body, body, is_new=False)
            else:
                after_slice = patch_text[item["end"] : item["end"] + 1200]
                if "WriteAllText" in after_slice and ".Replace(" not in after_slice[:300]:
                    _add_block(f_path, "", body, is_new=True)
            i += 1
            continue

        if is_full_content_var:
            pend_list = pending_old_by_file.get(f_path) or []
            if not pend_list:
                _add_block(f_path, "", body, is_new=True)
            i += 1
            continue

        if i + 1 < len(raw_hs_items) and raw_hs_items[i + 1]["file"] == f_path:
            between = patch_text[item["end"] : raw_hs_items[i + 1]["start"]]
            if len(between) < 200 and ("\n" in between or "," in between):
                _add_block(f_path, body, raw_hs_items[i + 1]["body"], is_new=False)
                i += 2
                continue

        after_slice = patch_text[item["end"] : item["end"] + 800]
        if "WriteAllText" in after_slice and ".Replace(" not in after_slice[:250]:
            _add_block(f_path, "", body, is_new=True)
        i += 1

    return {
        "target_root": target_root,
        "blocks": blocks,
        "notes": notes_block,
    }


def _normalize_newlines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _blocks_overlap(
    old_a: str,
    old_b: str,
    file_text: str = "",
) -> bool:
    norm_a = _normalize_newlines(old_a)
    norm_b = _normalize_newlines(old_b)
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b or norm_a in norm_b or norm_b in norm_a:
        return True
    if file_text:
        norm_file = _normalize_newlines(file_text)
        pos_a = norm_file.find(norm_a)
        pos_b = norm_file.find(norm_b)
        if pos_a != -1 and pos_b != -1:
            end_a = pos_a + len(norm_a)
            end_b = pos_b + len(norm_b)
            return max(pos_a, pos_b) < min(end_a, end_b)
    return False


def _file_contains_block(file_text: str, old_text: str) -> bool:
    if not old_text:
        return True
    if not file_text:
        return False
    if old_text in file_text:
        return True
    return _normalize_newlines(old_text) in _normalize_newlines(file_text)


def compare_patches(
    followup_patch_path: Union[str, Path],
    new_patch_path: Union[str, Path],
    project_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Compare two Argon patch scripts per file and per changed block."""
    f_path = Path(followup_patch_path).resolve()
    n_path = Path(new_patch_path).resolve()
    f_text = f_path.read_text(encoding="utf-8", errors="replace") if f_path.is_file() else ""
    n_text = n_path.read_text(encoding="utf-8", errors="replace") if n_path.is_file() else ""

    f_parsed = parse_patch_script(f_text)
    n_parsed = parse_patch_script(n_text)

    proj_root: Optional[Path] = None
    if project_path:
        proj_root = Path(project_path).resolve()
    elif f_parsed["target_root"] and Path(f_parsed["target_root"]).is_dir():
        proj_root = Path(f_parsed["target_root"]).resolve()
    elif n_parsed["target_root"] and Path(n_parsed["target_root"]).is_dir():
        proj_root = Path(n_parsed["target_root"]).resolve()

    target_root = (
        f_parsed["target_root"]
        or n_parsed["target_root"]
        or (str(proj_root) if proj_root else "")
    )

    f_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for b in f_parsed["blocks"]:
        f_by_file.setdefault(b["file"], []).append(b)

    n_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for b in n_parsed["blocks"]:
        n_by_file.setdefault(b["file"], []).append(b)

    all_files = sorted(set(list(f_by_file.keys()) + list(n_by_file.keys())))
    items: List[Dict[str, Any]] = []
    files_report: Dict[str, List[Dict[str, Any]]] = {}
    conflicts: List[Dict[str, Any]] = []

    counts = {
        "total_files": len(all_files),
        "total_blocks": 0,
        "identical": 0,
        "only_followup": 0,
        "only_new": 0,
        "conflicts": 0,
    }

    for rel_file in all_files:
        f_blocks = list(f_by_file.get(rel_file) or [])
        n_blocks = list(n_by_file.get(rel_file) or [])
        used_f: set = set()
        used_n: set = set()
        file_items: List[Dict[str, Any]] = []

        disk_text = ""
        if proj_root is not None:
            cand = proj_root / rel_file
            if cand.is_file():
                try:
                    disk_text = cand.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    disk_text = ""

        # Pass 1: exact match on old_text (or both are new-file creations for rel_file)
        for fi, fb in enumerate(f_blocks):
            norm_f_old = _normalize_newlines(fb["old_text"])
            for ni, nb in enumerate(n_blocks):
                if ni in used_n:
                    continue
                norm_n_old = _normalize_newlines(nb["old_text"])
                if fb["is_new_file"] and nb["is_new_file"]:
                    used_f.add(fi)
                    used_n.add(ni)
                    same_new = _normalize_newlines(fb["new_text"]) == _normalize_newlines(nb["new_text"])
                    status = STATUS_IDENTICAL if same_new else STATUS_CONFLICT
                    file_items.append(
                        {
                            "file": rel_file,
                            "status": status,
                            "is_new_file": True,
                            "old_text": "",
                            "followup_old_text": "",
                            "new_old_text": "",
                            "followup_new_text": fb["new_text"],
                            "new_new_text": nb["new_text"],
                            "followup_matches_tree": True,
                            "new_matches_tree": True,
                        }
                    )
                    break
                if not fb["is_new_file"] and not nb["is_new_file"] and norm_f_old == norm_n_old:
                    used_f.add(fi)
                    used_n.add(ni)
                    same_new = _normalize_newlines(fb["new_text"]) == _normalize_newlines(nb["new_text"])
                    status = STATUS_IDENTICAL if same_new else STATUS_CONFLICT
                    matches_tree = _file_contains_block(disk_text, fb["old_text"]) if disk_text else True
                    file_items.append(
                        {
                            "file": rel_file,
                            "status": status,
                            "is_new_file": False,
                            "old_text": fb["old_text"],
                            "followup_old_text": fb["old_text"],
                            "new_old_text": nb["old_text"],
                            "followup_new_text": fb["new_text"],
                            "new_new_text": nb["new_text"],
                            "followup_matches_tree": matches_tree,
                            "new_matches_tree": matches_tree,
                        }
                    )
                    break

        # Pass 2: overlapping old_text spans in the same file
        for fi, fb in enumerate(f_blocks):
            if fi in used_f:
                continue
            for ni, nb in enumerate(n_blocks):
                if ni in used_n:
                    continue
                if _blocks_overlap(fb["old_text"], nb["old_text"], disk_text):
                    used_f.add(fi)
                    used_n.add(ni)
                    status = STATUS_CONFLICT
                    if disk_text and _file_contains_block(disk_text, fb["old_text"]) and _file_contains_block(disk_text, nb["old_text"]):
                        norm_disk = _normalize_newlines(disk_text)
                        after_f = norm_disk.replace(
                            _normalize_newlines(fb["old_text"]),
                            _normalize_newlines(fb["new_text"]),
                            1,
                        )
                        after_n = norm_disk.replace(
                            _normalize_newlines(nb["old_text"]),
                            _normalize_newlines(nb["new_text"]),
                            1,
                        )
                        if after_f == after_n:
                            status = STATUS_IDENTICAL
                    file_items.append(
                        {
                            "file": rel_file,
                            "status": status,
                            "is_new_file": False,
                            "old_text": fb["old_text"] if len(fb["old_text"]) >= len(nb["old_text"]) else nb["old_text"],
                            "followup_old_text": fb["old_text"],
                            "new_old_text": nb["old_text"],
                            "followup_new_text": fb["new_text"],
                            "new_new_text": nb["new_text"],
                            "followup_matches_tree": _file_contains_block(disk_text, fb["old_text"]) if disk_text else True,
                            "new_matches_tree": _file_contains_block(disk_text, nb["old_text"]) if disk_text else True,
                        }
                    )
                    break

        # Pass 3: remaining unmatched blocks are only in follow-up or only in new session
        for fi, fb in enumerate(f_blocks):
            if fi in used_f:
                continue
            file_items.append(
                {
                    "file": rel_file,
                    "status": STATUS_ONLY_FOLLOWUP,
                    "is_new_file": fb["is_new_file"],
                    "old_text": fb["old_text"],
                    "followup_old_text": fb["old_text"],
                    "new_old_text": "",
                    "followup_new_text": fb["new_text"],
                    "new_new_text": "",
                    "followup_matches_tree": _file_contains_block(disk_text, fb["old_text"]) if (disk_text and not fb["is_new_file"]) else True,
                    "new_matches_tree": False,
                }
            )

        for ni, nb in enumerate(n_blocks):
            if ni in used_n:
                continue
            file_items.append(
                {
                    "file": rel_file,
                    "status": STATUS_ONLY_NEW,
                    "is_new_file": nb["is_new_file"],
                    "old_text": nb["old_text"],
                    "followup_old_text": "",
                    "new_old_text": nb["old_text"],
                    "followup_new_text": "",
                    "new_new_text": nb["new_text"],
                    "followup_matches_tree": False,
                    "new_matches_tree": _file_contains_block(disk_text, nb["old_text"]) if (disk_text and not nb["is_new_file"]) else True,
                }
            )

        for f_idx, entry in enumerate(file_items, start=1):
            counts["total_blocks"] += 1
            entry["item_id"] = counts["total_blocks"]
            entry["file_block_index"] = f_idx
            entry["block_key"] = f"{rel_file}#{f_idx}"
            st = entry["status"]
            if st == STATUS_IDENTICAL:
                counts["identical"] += 1
            elif st == STATUS_ONLY_FOLLOWUP:
                counts["only_followup"] += 1
            elif st == STATUS_ONLY_NEW:
                counts["only_new"] += 1
            elif st == STATUS_CONFLICT:
                counts["conflicts"] += 1
                entry["conflict_id"] = counts["conflicts"]
                conflicts.append(entry)
            items.append(entry)

        files_report[rel_file] = file_items

    return {
        "target_root": target_root,
        "followup_patch_path": str(f_path),
        "new_patch_path": str(n_path),
        "summary": counts,
        "files": files_report,
        "items": items,
        "conflicts": conflicts,
        "followup_notes": f_parsed["notes"],
        "new_notes": n_parsed["notes"],
    }


def parse_conflict_choices(choices_raw: Optional[str]) -> Dict[str, str]:
    """Parse conflict resolution choices such as '1=new,2=followup' or 'src/a.py#1=new'."""
    out: Dict[str, str] = {}
    if not choices_raw:
        return out
    raw = choices_raw.strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    norm_v = _normalize_side_name(str(v))
                    if norm_v:
                        out[str(k).strip()] = norm_v
                return out
        except Exception:
            pass

    for token in re.split(r"[,;]+", raw):
        token = token.strip()
        if not token:
            continue
        if "=" not in token and ":" not in token:
            norm_all = _normalize_side_name(token)
            if norm_all:
                out["all"] = norm_all
            continue
        sep = "=" if "=" in token else ":"
        key, val = token.split(sep, 1)
        norm_val = _normalize_side_name(val)
        if not norm_val:
            raise ValueError(
                f"Invalid conflict choice value {val!r} for {key!r}; expected 'followup' (or 'old') or 'new'."
            )
        out[key.strip()] = norm_val
    return out


def _normalize_side_name(val: Optional[str]) -> Optional[str]:
    cleaned = (val or "").strip().lower()
    if cleaned in ("followup", "follow-up", "follow_up", "old"):
        return "followup"
    if cleaned in ("new", "new-session", "new_session"):
        return "new"
    return None


def _resolve_conflict_side(item: Dict[str, Any], choices: Dict[str, str]) -> Optional[str]:
    if not choices:
        return None
    candidates = [
        str(item.get("conflict_id", "")),
        f"conflict_{item.get('conflict_id', '')}",
        f"item_{item.get('item_id', '')}",
        str(item.get("item_id", "")),
        str(item.get("block_key", "")),
        str(item.get("file", "")),
        "all",
        "*",
    ]
    for key in candidates:
        if key and key in choices:
            return choices[key]
    return None


def _combine_notes_blocks(followup_notes: str, new_notes: str, mode_label: str) -> str:
    lines: List[str] = [
        "# --- NOTES: inconsistencies and suggestions found while reading the project ---",
        "# ## ROOT CAUSE",
    ]
    seen: set = set()
    for src_label, block in (("followup", followup_notes), ("new", new_notes)):
        for raw_ln in (block or "").splitlines():
            s = raw_ln.strip()
            if not s.startswith("#"):
                continue
            if "--- NOTES:" in s or "--- END NOTES ---" in s or "## ROOT CAUSE" in s:
                continue
            if s not in seen:
                seen.add(s)
                lines.append(s)
    if len(lines) == 2:
        lines.append(f"# NOTE: {mode_label} generated by compare_argon_patches.py from dual-session patches.")
    lines.append("# --- END NOTES ---")
    return "\n".join(lines)


def build_merged_patch(
    comparison: Dict[str, Any],
    output_path: Union[str, Path],
    choices: Optional[Dict[str, str]] = None,
    pick_side: Optional[str] = None,
    target_root_override: Optional[str] = None,
) -> Path:
    """Build a single-run PowerShell patch script (`apply_changes.ps1`) from the comparison.

    Enforces R7 & D6:
    - `$TargetRoot` pinned at the top,
    - every target block verified before any file is written,
    - backups into `.argon_backup/<yyyyMMdd_HHmmss>/<relPath>`,
    - UTF-8 without BOM,
    - every written file re-read from disk and verified (`[Verified] <path>` / `[FAILED VERIFY] <path>`),
    - trailing `# --- NOTES ... # --- END NOTES ---` block,
    - `[SUCCESS] N file(s) patched.`
    - Any unresolved conflict raises ValueError and is NEVER guessed.
    """
    norm_pick = _normalize_side_name(pick_side) if pick_side else None
    if pick_side and not norm_pick:
        raise ValueError(f"Invalid --pick value {pick_side!r}; expected 'followup' (or 'old') or 'new'.")

    resolved_choices = dict(choices or {})
    selected_per_file: Dict[str, List[Dict[str, Any]]] = {}
    unresolved: List[str] = []

    for item in comparison.get("items") or []:
        st = item["status"]
        rel_file = item["file"]

        if norm_pick == "followup":
            if st == STATUS_ONLY_NEW:
                continue
            selected_per_file.setdefault(rel_file, []).append(
                {
                    "file": rel_file,
                    "is_new_file": item["is_new_file"],
                    "old_text": item["followup_old_text"],
                    "new_text": item["followup_new_text"],
                    "source": "followup",
                }
            )
            continue

        if norm_pick == "new":
            if st == STATUS_ONLY_FOLLOWUP:
                continue
            selected_per_file.setdefault(rel_file, []).append(
                {
                    "file": rel_file,
                    "is_new_file": item["is_new_file"],
                    "old_text": item["new_old_text"],
                    "new_text": item["new_new_text"],
                    "source": "new",
                }
            )
            continue

        # Full merge mode (both lanes combined)
        if st in (STATUS_IDENTICAL, STATUS_ONLY_FOLLOWUP):
            selected_per_file.setdefault(rel_file, []).append(
                {
                    "file": rel_file,
                    "is_new_file": item["is_new_file"],
                    "old_text": item["followup_old_text"],
                    "new_text": item["followup_new_text"],
                    "source": "both" if st == STATUS_IDENTICAL else "followup",
                }
            )
        elif st == STATUS_ONLY_NEW:
            selected_per_file.setdefault(rel_file, []).append(
                {
                    "file": rel_file,
                    "is_new_file": item["is_new_file"],
                    "old_text": item["new_old_text"],
                    "new_text": item["new_new_text"],
                    "source": "new",
                }
            )
        elif st == STATUS_CONFLICT:
            chosen = _resolve_conflict_side(item, resolved_choices)
            if chosen not in ("followup", "new"):
                cid = item.get("conflict_id", "?")
                bkey = item.get("block_key", rel_file)
                unresolved.append(f"conflict #{cid} ({bkey})")
                continue
            old_chosen = item["followup_old_text"] if chosen == "followup" else item["new_old_text"]
            new_chosen = item["followup_new_text"] if chosen == "followup" else item["new_new_text"]
            selected_per_file.setdefault(rel_file, []).append(
                {
                    "file": rel_file,
                    "is_new_file": item["is_new_file"],
                    "old_text": old_chosen,
                    "new_text": new_chosen,
                    "source": chosen,
                }
            )

    if unresolved:
        raise ValueError(
            "Refusing to guess unresolved conflict(s): "
            + ", ".join(unresolved)
            + ". Pass explicit choices via --choices (for example: --choices \"1=new,2=followup\")."
        )

    if not selected_per_file:
        # An empty merge would write a script that patches nothing and still
        # prints "[SUCCESS] 0 file(s) patched." - the user would believe the
        # merge applied while not a single line changed.
        raise ValueError(
            "No patch blocks were parsed from either side - refusing to build an "
            "empty merged patch. Check that both patches use the expected "
            "here-string shape."
        )

    target_root = (
        target_root_override
        or comparison.get("target_root")
        or ""
    )
    if not target_root:
        raise ValueError(
            "The target root is unknown - refusing to build a merged patch that "
            "could write to the wrong project. Pass --project-path."
        )
    escaped_root = str(target_root).replace("'", "''")

    ps_lines: List[str] = [
        "$ErrorActionPreference = 'Stop'",
        f"$TargetRoot = '{escaped_root}'",
        "$utf8NoBom = [System.Text.UTF8Encoding]::new($false)",
        "$backupSession = Join-Path (Join-Path $TargetRoot '.argon_backup') (Get-Date -Format 'yyyyMMdd_HHmmss')",
        "$patchedCount = 0",
        "",
        "# --- PHASE 1: VALIDATE EVERY TARGET BLOCK BEFORE WRITING ANY FILE ---",
    ]

    file_list = sorted(selected_per_file.keys())
    for f_idx, rel_file in enumerate(file_list, start=1):
        esc_rel = rel_file.replace("'", "''")
        blocks = selected_per_file[rel_file]
        has_replacements = any(not b["is_new_file"] for b in blocks)
        new_file_blocks = [b for b in blocks if b["is_new_file"]]
        if new_file_blocks and (len(new_file_blocks) > 1 or has_replacements):
            # A file cannot be written whole AND patched in blocks in the same
            # merged patch - the two intents contradict each other.
            raise ValueError(
                f"{rel_file}: a file is created whole and patched in blocks at the "
                "same time. Pick one side for this file, or apply them separately."
            )
        ps_lines.append(f"$relPath = '{esc_rel}'")
        ps_lines.append(f"$relPath_{f_idx} = $relPath")
        ps_lines.append(f"$path_{f_idx} = Join-Path $TargetRoot '{esc_rel}'")
        if new_file_blocks:
            # The file is created whole, so it must not exist yet.
            ps_lines.append(f"if (Test-Path -LiteralPath $path_{f_idx}) {{")
            ps_lines.append(
                f"    throw \"Target file already exists, refusing to overwrite it: $relPath_{f_idx}\""
            )
            ps_lines.append("}")
            ps_lines.append(f"$content_{f_idx} = $new_{f_idx}_1")
        elif has_replacements:
            ps_lines.append(f"if (-not (Test-Path -LiteralPath $path_{f_idx})) {{")
            ps_lines.append(f"    throw \"Target file not found: $relPath_{f_idx}. Aborting before any file is written.\"")
            ps_lines.append("}")
            ps_lines.append(f"$content_{f_idx} = [System.IO.File]::ReadAllText($path_{f_idx}, [System.Text.Encoding]::UTF8)")
        for b_idx, blk in enumerate(blocks, start=1):
            if blk["is_new_file"]:
                ps_lines.append(f"$new_{f_idx}_{b_idx} = {OPEN_SQ}")
                ps_lines.append(blk["new_text"])
                ps_lines.append(CLOSE_SQ)
            else:
                ps_lines.append(f"$old_{f_idx}_{b_idx} = {OPEN_SQ}")
                ps_lines.append(blk["old_text"])
                ps_lines.append(CLOSE_SQ)
                ps_lines.append(f"$new_{f_idx}_{b_idx} = {OPEN_SQ}")
                ps_lines.append(blk["new_text"])
                ps_lines.append(CLOSE_SQ)
                ps_lines.append(f"if (-not $content_{f_idx}.Contains($old_{f_idx}_{b_idx})) {{")
                ps_lines.append(f"    $oldCrlf = ($old_{f_idx}_{b_idx} -replace \"`r?`n\", \"`r`n\")")
                ps_lines.append(f"    $oldLf   = ($old_{f_idx}_{b_idx} -replace \"`r?`n\", \"`n\")")
                ps_lines.append(f"    if ($content_{f_idx}.Contains($oldCrlf)) {{")
                ps_lines.append(f"        $old_{f_idx}_{b_idx} = $oldCrlf")
                ps_lines.append(f"        $new_{f_idx}_{b_idx} = ($new_{f_idx}_{b_idx} -replace \"`r?`n\", \"`r`n\")")
                ps_lines.append(f"    }} elseif ($content_{f_idx}.Contains($oldLf)) {{")
                ps_lines.append(f"        $old_{f_idx}_{b_idx} = $oldLf")
                ps_lines.append(f"        $new_{f_idx}_{b_idx} = ($new_{f_idx}_{b_idx} -replace \"`r?`n\", \"`n\")")
                ps_lines.append("    }")
                ps_lines.append("}")
                ps_lines.append(f"if (-not $content_{f_idx}.Contains($old_{f_idx}_{b_idx})) {{")
                ps_lines.append(
                    f"    throw \"Target block #{b_idx} not found in $relPath_{f_idx}. Aborting before any file is written.\""
                )
                ps_lines.append("}")
        ps_lines.append("")

    ps_lines.append("# --- PHASE 2: BACK UP, APPLY, AND VERIFY EVERY FILE ---")
    for f_idx, rel_file in enumerate(file_list, start=1):
        blocks = selected_per_file[rel_file]
        ps_lines.append(f"$relPath = $relPath_{f_idx}")
        ps_lines.append(f"$path = $path_{f_idx}")
        ps_lines.append("if (Test-Path -LiteralPath $path) {")
        ps_lines.append("    $backupDest = Join-Path $backupSession $relPath")
        ps_lines.append("    $backupParent = [System.IO.Path]::GetDirectoryName($backupDest)")
        ps_lines.append("    if ($backupParent -and -not (Test-Path $backupParent)) {")
        ps_lines.append("        New-Item -ItemType Directory -Path $backupParent -Force | Out-Null")
        ps_lines.append("    }")
        ps_lines.append("    Copy-Item -LiteralPath $path -Destination $backupDest -Force")
        ps_lines.append("}")
        ps_lines.append("$parentDir = [System.IO.Path]::GetDirectoryName($path)")
        ps_lines.append("if ($parentDir -and -not (Test-Path $parentDir)) {")
        ps_lines.append("    New-Item -ItemType Directory -Path $parentDir -Force | Out-Null")
        ps_lines.append("}")

        if new_file_blocks:
            # The whole file is written, so the accumulated content IS the new
            # content. It must never be overwritten by a later block.
            ps_lines.append(f"$content = $content_{f_idx}")
            written_blocks = [1]
        else:
            ps_lines.append(f"$content = $content_{f_idx}")
            for b_idx, blk in enumerate(blocks, start=1):
                ps_lines.append(f"$oldTarget = $old_{f_idx}_{b_idx}")
                ps_lines.append(f"$newReplacement = $new_{f_idx}_{b_idx}")
                ps_lines.append("if (-not $content.Contains($oldTarget)) {")
                ps_lines.append("    throw \"Target block not found in $relPath. Aborting before any file is written.\"")
                ps_lines.append("}")
                ps_lines.append("$content = $content.Replace($oldTarget, $newReplacement)")
            written_blocks = list(range(1, len(blocks) + 1))

        ps_lines.append("[System.IO.File]::WriteAllText($path, $content, $utf8NoBom)")
        ps_lines.append("Write-Host \"[Patched] $relPath\" -ForegroundColor Green")
        ps_lines.append("$patchedCount++")
        ps_lines.append("$verifyContent = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)")
        for b_idx in written_blocks:
            ps_lines.append(f"if (-not $verifyContent.Contains($new_{f_idx}_{b_idx})) {{")
            ps_lines.append("    Write-Host \"[FAILED VERIFY] $relPath\" -ForegroundColor Red")
            ps_lines.append("    exit 1")
            ps_lines.append("}")
        ps_lines.append("Write-Host \"[Verified] $relPath\" -ForegroundColor Cyan")
        ps_lines.append("")

    ps_lines.append("Write-Host \"[SUCCESS] $patchedCount file(s) patched.\" -ForegroundColor Green")
    ps_lines.append(
        "$rollbackScript = Join-Path $TargetRoot 'skills/argon-coding-bridge/scripts/Apply-ArgonPatch.ps1'"
    )
    ps_lines.append(
        "Write-Host \"[+] Rollback command: pwsh -NoProfile -ExecutionPolicy Bypass -File `\"$rollbackScript`\" -ProjectPath `\"$TargetRoot`\" -Rollback\" -ForegroundColor Yellow"
    )
    ps_lines.append("")
    ps_lines.append(
        _combine_notes_blocks(
            comparison.get("followup_notes") or "",
            comparison.get("new_notes") or "",
            mode_label=f"pick={norm_pick}" if norm_pick else "merged patch",
        )
    )
    ps_lines.append("")

    out_p = Path(output_path).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text("\n".join(ps_lines), encoding="utf-8")
    return out_p


def render_comparison_html(
    comparison: Dict[str, Any],
    output_html: Union[str, Path],
    followup_prompt_path: Optional[Union[str, Path]] = None,
    new_prompt_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Render the side-by-side HTML report (`comparison.html`) per file and per block."""
    out_path = Path(output_html).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    f_prompt_text = ""
    n_prompt_text = ""
    if followup_prompt_path and Path(followup_prompt_path).is_file():
        f_prompt_text = Path(followup_prompt_path).read_text(encoding="utf-8", errors="replace")
    elif (out_path.parent / "followup_prompt.md").is_file():
        f_prompt_text = (out_path.parent / "followup_prompt.md").read_text(encoding="utf-8", errors="replace")

    if new_prompt_path and Path(new_prompt_path).is_file():
        n_prompt_text = Path(new_prompt_path).read_text(encoding="utf-8", errors="replace")
    elif (out_path.parent / "new_prompt.md").is_file():
        n_prompt_text = (out_path.parent / "new_prompt.md").read_text(encoding="utf-8", errors="replace")

    summary = comparison["summary"]
    target_root = comparison.get("target_root") or ""
    f_patch = comparison.get("followup_patch_path") or ""
    n_patch = comparison.get("new_patch_path") or ""

    badge_colors = {
        STATUS_IDENTICAL: ("#065f46", "#d1fae5"),
        STATUS_ONLY_FOLLOWUP: ("#1e40af", "#dbeafe"),
        STATUS_ONLY_NEW: ("#6b21a8", "#f3e8ff"),
        STATUS_CONFLICT: ("#991b1b", "#fee2e2"),
    }

    file_sections: List[str] = []
    for rel_file, f_items in (comparison.get("files") or {}).items():
        block_cards: List[str] = []
        for item in f_items:
            st = item["status"]
            fg, bg = badge_colors.get(st, ("#1f2937", "#f3f4f6"))
            cid = item.get("conflict_id")
            conflict_picker = ""
            if cid is not None:
                conflict_picker = (
                    f"<div class='conflict-controls' data-conflict-id='{cid}'>"
                    f"<strong>Resolve Conflict #{cid}:</strong> "
                    f"<label><input type='radio' name='conflict_{cid}' value='followup' onchange='updateMergeCmd()'> Use Follow-up</label> "
                    f"<label><input type='radio' name='conflict_{cid}' value='new' onchange='updateMergeCmd()'> Use New Session</label>"
                    f"</div>"
                )
            f_match_badge = (
                "<span class='tree-ok'>matches tree</span>"
                if item.get("followup_matches_tree")
                else "<span class='tree-miss'>not in lane / tree</span>"
            )
            n_match_badge = (
                "<span class='tree-ok'>matches tree</span>"
                if item.get("new_matches_tree")
                else "<span class='tree-miss'>not in lane / tree</span>"
            )
            old_disp = html.escape(item.get("old_text") or "(new file creation)")
            f_new_disp = html.escape(item.get("followup_new_text") or "(not touched in follow-up)")
            n_new_disp = html.escape(item.get("new_new_text") or "(not touched in new session)")

            block_cards.append(
                f"<div class='block-card'>"
                f"<div class='block-header'>"
                f"<span><strong>Block #{item['item_id']}</strong> ({html.escape(item['block_key'])})</span>"
                f"<span class='badge' style='color:{fg};background:{bg};'>{html.escape(st)}</span>"
                f"</div>"
                f"{conflict_picker}"
                f"<div class='panes'>"
                f"<div class='pane'><div class='pane-title'>Original Target ($oldTarget)</div><pre>{old_disp}</pre></div>"
                f"<div class='pane'><div class='pane-title'>Follow-up Lane {f_match_badge}</div><pre>{f_new_disp}</pre></div>"
                f"<div class='pane'><div class='pane-title'>New-Session Lane {n_match_badge}</div><pre>{n_new_disp}</pre></div>"
                f"</div>"
                f"</div>"
            )
        file_sections.append(
            f"<section class='file-section'>"
            f"<h2>File: <code>{html.escape(rel_file)}</code> ({len(f_items)} block(s))</h2>"
            + "\n".join(block_cards)
            + "</section>"
        )

    f_first = (f_prompt_text.splitlines()[0] if f_prompt_text.splitlines() else "(not provided)")
    n_first = (n_prompt_text.splitlines()[0] if n_prompt_text.splitlines() else "(not provided)")

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argon Dual-Session Patch Comparison</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 24px; background: #0f172a; color: #e2e8f0; }}
  h1, h2, h3 {{ margin-top: 0; color: #f8fafc; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 14px; }}
  .card .num {{ font-size: 24px; font-weight: 700; color: #38bdf8; }}
  .actions-bar {{ background: #1e293b; border: 1px solid #475569; border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
  .cmd-box {{ background: #020617; color: #a7f3d0; padding: 10px 12px; border-radius: 6px; font-family: monospace; font-size: 13px; overflow-x: auto; margin: 6px 0 12px 0; }}
  .file-section {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
  .block-card {{ background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 12px; margin-top: 12px; }}
  .block-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .badge {{ padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; text-transform: uppercase; }}
  .conflict-controls {{ background: #450a0a; border: 1px solid #dc2626; padding: 8px 12px; border-radius: 6px; margin-bottom: 10px; }}
  .panes {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }}
  .pane {{ background: #020617; border: 1px solid #1e293b; border-radius: 6px; padding: 8px; overflow-x: auto; }}
  .pane-title {{ font-size: 12px; font-weight: 600; color: #94a3b8; margin-bottom: 6px; }}
  .tree-ok {{ color: #4ade80; font-size: 11px; margin-left: 6px; }}
  .tree-miss {{ color: #f87171; font-size: 11px; margin-left: 6px; }}
  pre {{ margin: 0; font-size: 12px; line-height: 1.45; white-space: pre-wrap; word-break: break-word; color: #e2e8f0; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #38bdf8; font-weight: 600; }}
</style>
</head>
<body>
  <h1>Argon Dual-Session Side-by-Side Comparison</h1>
  <p>Target Root: <code>{html.escape(target_root)}</code></p>

  <div class="summary-grid">
    <div class="card"><div>Files Touched</div><div class="num">{summary['total_files']}</div></div>
    <div class="card"><div>Total Blocks</div><div class="num">{summary['total_blocks']}</div></div>
    <div class="card"><div>Identical in Both</div><div class="num">{summary['identical']}</div></div>
    <div class="card"><div>Only in Follow-up</div><div class="num">{summary['only_followup']}</div></div>
    <div class="card"><div>Only in New Session</div><div class="num">{summary['only_new']}</div></div>
    <div class="card"><div>Conflicting</div><div class="num" style="color:#f87171;">{summary['conflicts']}</div></div>
  </div>

  <div class="actions-bar">
    <h3>One-Click Pick / Merge / Record Winner Commands</h3>
    <div><strong>1. Pick Follow-up Winner Alone &amp; Record Winner:</strong></div>
    <div class="cmd-box">python skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path "{html.escape(target_root)}" --record-winner followup &amp;&amp; python skills/argon-coding-bridge/scripts/compare_argon_patches.py --followup-patch "{html.escape(f_patch)}" --new-patch "{html.escape(n_patch)}" --pick followup --apply</div>
    <div><strong>2. Pick New-Session Winner Alone &amp; Record Winner:</strong></div>
    <div class="cmd-box">python skills/argon-coding-bridge/scripts/dispatch_argon.py --project-path "{html.escape(target_root)}" --record-winner new &amp;&amp; python skills/argon-coding-bridge/scripts/compare_argon_patches.py --followup-patch "{html.escape(f_patch)}" --new-patch "{html.escape(n_patch)}" --pick new --apply</div>
    <div><strong>3. Build &amp; Apply Single-Run Merged Patch:</strong></div>
    <div class="cmd-box" id="merge-cmd">python skills/argon-coding-bridge/scripts/compare_argon_patches.py --followup-patch "{html.escape(f_patch)}" --new-patch "{html.escape(n_patch)}" --merge --apply</div>
  </div>

  <div class="file-section">
    <h2>Dispatched Prompts Comparison</h2>
    <p><strong>Follow-up Prompt:</strong> {len(f_prompt_text)} chars | First line: <code>{html.escape(f_first)}</code></p>
    <details><summary>View Full Follow-up Prompt</summary><pre>{html.escape(f_prompt_text)}</pre></details>
    <p style="margin-top:12px;"><strong>New-Session Prompt:</strong> {len(n_prompt_text)} chars | First line: <code>{html.escape(n_first)}</code></p>
    <details><summary>View Full New-Session Prompt</summary><pre>{html.escape(n_prompt_text)}</pre></details>
  </div>

  {"".join(file_sections)}

<script>
function updateMergeCmd() {{
  const controls = document.querySelectorAll('.conflict-controls');
  const parts = [];
  controls.forEach(c => {{
    const cid = c.getAttribute('data-conflict-id');
    const checked = c.querySelector('input[type="radio"]:checked');
    if (checked) {{
      parts.push(cid + '=' + checked.value);
    }}
  }});
  const base = 'python skills/argon-coding-bridge/scripts/compare_argon_patches.py --followup-patch "{html.escape(f_patch)}" --new-patch "{html.escape(n_patch)}" --merge';
  const choiceArg = parts.length > 0 ? ' --choices "' + parts.join(',') + '"' : '';
  document.getElementById('merge-cmd').textContent = base + choiceArg + ' --apply';
}}
</script>
</body>
</html>
"""
    out_path.write_text(html_doc, encoding="utf-8")
    return out_path


def _discover_latest_dual_round_dir(project_path: Path) -> Optional[Path]:
    dual_root = Path(project_path).resolve() / "downloads" / "dual"
    if not dual_root.is_dir():
        return None
    candidates = [d for d in dual_root.iterdir() if d.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return candidates[0]


def _apply_patch_file(project_path: Path, patch_path: Path) -> int:
    from dispatch_argon import apply_delivered_patch, refuse_invalid_patch

    patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    if not refuse_invalid_patch(patch_text, project_path=project_path, patch_name=patch_path.name):
        return 1
    ok = apply_delivered_patch(project_path, patch_path)
    return 0 if ok else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare two Gemini 4 Argon patches side by side and build a single-run merged patch."
    )
    parser.add_argument("--followup-patch", default=None, help="Path to the follow-up lane apply_changes.ps1")
    parser.add_argument("--new-patch", default=None, help="Path to the new-session lane apply_changes.ps1")
    parser.add_argument("--followup-prompt", default=None, help="Optional path to followup_prompt.md")
    parser.add_argument("--new-prompt", default=None, help="Optional path to new_prompt.md")
    parser.add_argument("--round-dir", default=None, help="Optional dual round directory (downloads/dual/<round-id>)")
    parser.add_argument("--project-path", default=".", help="Path to the target project root")
    parser.add_argument("--output-html", default=None, help="Output path for comparison.html")
    parser.add_argument("--output-merged", default=None, help="Output path for merged_apply_changes.ps1")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Build merged_apply_changes.ps1 combining both patches (requires explicit --choices when conflicts exist)",
    )
    parser.add_argument(
        "--pick",
        choices=["followup", "new", "old"],
        default=None,
        help="Build/apply only the chosen side ('followup'/'old' or 'new')",
    )
    parser.add_argument(
        "--choices",
        default=None,
        help="Explicit conflict choices, e.g. '1=new,2=followup' or 'skills/foo.py#1=new'",
    )
    parser.add_argument("--open", action="store_true", help="Open comparison.html in the default browser")
    parser.add_argument("--apply", action="store_true", help="Apply the resulting merged or picked patch to --project-path")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    proj_root = Path(args.project_path).resolve()
    round_dir: Optional[Path] = Path(args.round_dir).resolve() if args.round_dir else None
    if round_dir is None and (not args.followup_patch or not args.new_patch):
        round_dir = _discover_latest_dual_round_dir(proj_root)

    followup_patch = Path(args.followup_patch).resolve() if args.followup_patch else (
        (round_dir / "followup" / "apply_changes.ps1") if round_dir else None
    )
    new_patch = Path(args.new_patch).resolve() if args.new_patch else (
        (round_dir / "new" / "apply_changes.ps1") if round_dir else None
    )

    if followup_patch is None or not followup_patch.is_file():
        print(f"[ERROR] Follow-up patch file not found: {followup_patch}", file=sys.stderr)
        return 1
    if new_patch is None or not new_patch.is_file():
        print(f"[ERROR] New-session patch file not found: {new_patch}", file=sys.stderr)
        return 1

    base_out_dir = round_dir if round_dir is not None else followup_patch.parent
    output_html = Path(args.output_html).resolve() if args.output_html else (base_out_dir / "comparison.html")
    output_merged = Path(args.output_merged).resolve() if args.output_merged else (base_out_dir / "merged_apply_changes.ps1")

    followup_prompt = Path(args.followup_prompt).resolve() if args.followup_prompt else (base_out_dir / "followup_prompt.md")
    new_prompt = Path(args.new_prompt).resolve() if args.new_prompt else (base_out_dir / "new_prompt.md")

    comparison = compare_patches(followup_patch, new_patch, project_path=proj_root)
    html_path = render_comparison_html(
        comparison,
        output_html=output_html,
        followup_prompt_path=followup_prompt if followup_prompt.is_file() else None,
        new_prompt_path=new_prompt if new_prompt.is_file() else None,
    )

    s = comparison["summary"]
    print("=" * 78)
    print("  ARGON DUAL-SESSION PATCH COMPARISON")
    print("=" * 78)
    print(f"  follow-up patch    : {followup_patch}")
    print(f"  new-session patch  : {new_patch}")
    print(f"  files touched      : {s['total_files']}")
    print(f"  total blocks       : {s['total_blocks']}")
    print(f"  identical in both  : {s['identical']}")
    print(f"  only in follow-up  : {s['only_followup']}")
    print(f"  only in new session: {s['only_new']}")
    print(f"  CONFLICTING        : {s['conflicts']}")
    print(f"  comparison report  : {html_path}")
    print("-" * 78)
    for item in comparison["items"]:
        cid_tag = f" [CONFLICT #{item['conflict_id']}]" if item.get("conflict_id") else ""
        print(f"  [block #{item['item_id']:02d}] {item['file']} (#{item['file_block_index']}) -> {item['status']}{cid_tag}")
    print("=" * 78)

    if args.open:
        try:
            webbrowser.open(html_path.as_uri())
            print(f"[+] Opened comparison report in default browser: {html_path}")
        except Exception as exc:
            print(f"[!] Could not open browser automatically: {exc}", file=sys.stderr)

    target_patch_to_apply: Optional[Path] = None
    if args.pick or args.merge or args.output_merged:
        try:
            parsed_choices = parse_conflict_choices(args.choices)
            merged_path = build_merged_patch(
                comparison,
                output_path=output_merged,
                choices=parsed_choices,
                pick_side=args.pick,
                target_root_override=str(proj_root) if args.project_path != "." else None,
            )
            print(f"[+] Wrote single-run patch script: {merged_path}")
            target_patch_to_apply = merged_path
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 2

    if args.apply:
        if target_patch_to_apply is None:
            print(
                "[ERROR] --apply requires either --pick <followup|new> or --merge.",
                file=sys.stderr,
            )
            return 1
        rc = _apply_patch_file(proj_root, target_patch_to_apply)
        return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
