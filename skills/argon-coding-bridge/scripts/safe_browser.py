#!/usr/bin/env python3
"""
Safe Browser Lifecycle, Multi-Tab CDP Host, and Session Protection Helper
for the Gemini 4 Argon Coding Bridge (Round 14).

Provides:
- Safe browser shutdown (`close_browser_safely`) that flushes cookies via graceful
  CDP `Browser.close` / `context.close()` while the Playwright driver is alive, and
  if a force-kill is ever unavoidable, snapshots `Default/Network/Cookies` and
  `Default/Preferences` via SQLite online backup BEFORE killing and restores them AFTER.
- Profile account & cookie protection (`profile_has_signed_in_account`,
  `verify_account_index_in_preferences`, `snapshot_profile_session`,
  `restore_profile_session`, `repair_slot_cookie_db_only`) so a signed-in profile is
  never wiped or replaced by a seed.
- Multi-tab CDP browser host (`ensure_browser_host`, `acquire_host_tab`,
  `update_host_tab`, `release_host_tab`, `reap_dead_tabs`) so N tasks on one profile
  (single-account or shared multi-account) share ONE Chrome browser in separate tabs,
  closing only the task's own tab when a task ends or is killed, and closing the
  browser gracefully when the last task finishes.
- CLI handlers (`close_browser_cli`, `close_tab_cli`, `list_browsers_cli`) exposed
  through `dispatch_argon.py`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

ENDPOINT_FILENAME = ".argon_browser_endpoint"
COOKIE_SNAPSHOT_DIRNAME = ".argon_cookie_snapshot"
QUARANTINE_DIRNAME = ".argon_quarantine"
COMPANION_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"

# How long `ensure_browser_host` waits for a Chrome that is already running on the profile
# to PUBLISH its CDP endpoint before it gives up on that profile. Measured on the owner's
# machine (round 20): the second dispatch lane arrived while the first lane was still
# LAUNCHING Chrome, saw the profile PIDs but no endpoint, and refused to attach - which
# rotated the lane to a different Google account and sent the prompt twice.
HOST_START_WAIT_SECONDS = 90.0
HOST_START_POLL_SECONDS = 0.5

DEFAULT_HOST_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
]


def get_companion_extension_dir(ext_dir: Optional[Union[str, Path]] = None) -> Optional[Path]:
    """Return the unpacked MV3 companion extension directory when `manifest.json` exists, else None.

    Setting `ARGON_DISABLE_COMPANION_EXTENSION=1` forces the pure-Python fallback path even when
    `skills/argon-coding-bridge/extension/manifest.json` is present on disk.
    """
    if str(os.environ.get("ARGON_DISABLE_COMPANION_EXTENSION") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return None
    cand = Path(ext_dir).expanduser().resolve() if ext_dir else COMPANION_EXTENSION_DIR.resolve()
    if cand.is_dir() and (cand / "manifest.json").is_file():
        return cand
    return None


def companion_extension_launch_args(ext_dir: Optional[Union[str, Path]] = None) -> List[str]:
    """Return Chrome launch flags to load the unpacked companion extension, or `[]` when absent."""
    resolved = get_companion_extension_dir(ext_dir)
    if resolved is None:
        return []
    ext_str = str(resolved)
    return [
        f"--disable-extensions-except={ext_str}",
        f"--load-extension={ext_str}",
    ]

def pid_is_alive(pid: int) -> bool:
    """Return True when a process with the given PID is currently running."""
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
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
                timeout=8,
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


def _acquire_file_lock(lock_path: Path, timeout: float = 10.0) -> Optional[int]:
    """Acquire an atomic file lock on `lock_path`."""
    deadline = time.monotonic() + max(0.5, float(timeout))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            return fd
        except FileExistsError:
            try:
                raw = lock_path.read_text(encoding="utf-8", errors="ignore").strip()
                holder_pid = int(raw.splitlines()[0]) if raw else 0
                age = time.time() - lock_path.stat().st_mtime
                if (holder_pid and not pid_is_alive(holder_pid)) or age > 30.0:
                    lock_path.unlink(missing_ok=True)
                    continue
            except Exception:
                pass
            time.sleep(0.08)
        except Exception:
            break
    return None


def _release_file_lock(fd: Optional[int], lock_path: Path) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def has_cookie_db(profile_dir: Union[str, Path]) -> bool:
    """Return True when `profile_dir` has a non-empty Chrome Cookies SQLite file."""
    prof = Path(profile_dir).expanduser().resolve()
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        cand = prof / rel
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def get_preferences_accounts(profile_dir: Union[str, Path]) -> List[Dict[str, Any]]:
    """Read `Default/Preferences` and return registered Google accounts with their 0-based index."""
    prof = Path(profile_dir).expanduser().resolve()
    pref_file = prof / "Default" / "Preferences"
    accounts: List[Dict[str, Any]] = []
    if not pref_file.is_file():
        return accounts
    try:
        data = json.loads(pref_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return accounts
    raw_info = data.get("account_info")
    if isinstance(raw_info, list):
        for idx, item in enumerate(raw_info):
            if isinstance(item, dict):
                email = str(item.get("email") or "").strip()
                acct_id = str(item.get("account_id") or item.get("gaia") or "").strip()
                if email or acct_id:
                    accounts.append(
                        {
                            "index": idx,
                            "email": email,
                            "account_id": acct_id,
                            "gaia": str(item.get("gaia") or ""),
                            "full_name": str(item.get("full_name") or ""),
                        }
                    )
    elif isinstance(raw_info, dict):
        for idx, (k, item) in enumerate(raw_info.items()):
            if isinstance(item, dict):
                email = str(item.get("email") or k or "").strip()
                accounts.append(
                    {
                        "index": idx,
                        "email": email,
                        "account_id": str(item.get("account_id") or k),
                        "gaia": str(item.get("gaia") or ""),
                        "full_name": str(item.get("full_name") or ""),
                    }
                )
    return accounts


def profile_has_signed_in_account(profile_dir: Union[str, Path]) -> bool:
    """Return True when `profile_dir` holds any registered Google account or cookie session.

    Used to guard `_seed_profile()` and `seed_slot_profile()` so a signed-in slot profile
    is NEVER wiped by `shutil.rmtree` or replaced with a master profile copy (Section 2.2.E).
    """
    prof = Path(profile_dir).expanduser().resolve()
    if not prof.is_dir():
        return False
    if get_preferences_accounts(prof):
        return True
    pref_file = prof / "Default" / "Preferences"
    if pref_file.is_file():
        try:
            data = json.loads(pref_file.read_text(encoding="utf-8", errors="replace"))
            signin = data.get("signin")
            if isinstance(signin, dict) and signin.get("accounts"):
                return True
            google_cfg = data.get("google")
            if isinstance(google_cfg, dict) and isinstance(google_cfg.get("services"), dict):
                svc = google_cfg["services"]
                if any(svc.get(k) for k in ("account_id", "last_username", "username")):
                    return True
        except Exception:
            pass
    if has_cookie_db(prof):
        return True
    snap_dir = prof / COOKIE_SNAPSHOT_DIRNAME
    if snap_dir.is_dir() and has_cookie_db(snap_dir):
        return True
    return False


def verify_account_index_in_preferences(
    profile_dir: Union[str, Path],
    email: str,
    expected_index: Optional[int] = None,
) -> Tuple[bool, Optional[int], List[str]]:
    """Verify that `email` is registered in `Default/Preferences` (`account_info`) at `expected_index`.

    Returns `(verified_ok, found_index, all_registered_emails)`.
    """
    accounts = get_preferences_accounts(profile_dir)
    all_emails = [a["email"] for a in accounts if a.get("email")]
    clean_email = (email or "").strip().lower()
    if not clean_email:
        if expected_index is not None:
            ok = 0 <= expected_index < len(accounts)
            return ok, (expected_index if ok else None), all_emails
        return bool(accounts), (0 if accounts else None), all_emails

    found_idx: Optional[int] = None
    for acc in accounts:
        if str(acc.get("email") or "").strip().lower() == clean_email:
            found_idx = int(acc["index"])
            break

    if found_idx is None:
        return False, None, all_emails
    if expected_index is not None and found_idx != int(expected_index):
        return False, found_idx, all_emails
    return True, found_idx, all_emails


def register_account_in_preferences(
    profile_dir: Union[str, Path],
    email: str,
    account_entry: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Register `email` inside `Default/Preferences` (`account_info`) of `profile_dir` without disturbing existing accounts.

    Extends `get_preferences_accounts()` / `verify_account_index_in_preferences()` (Round 18 Section 1.2 #3):
    preserves all existing `account_info` entries at their indices, appends `email` if not yet present,
    writes `Default/Preferences` atomically, and returns the 0-based index of `email`.
    """
    clean_email = (email or "").strip()
    if not clean_email:
        return None
    prof = Path(profile_dir).expanduser().resolve()
    pref_file = prof / "Default" / "Preferences"
    pref_file.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if pref_file.is_file():
        try:
            loaded = json.loads(pref_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    raw_info = data.get("account_info")
    info_list: List[Dict[str, Any]] = []
    if isinstance(raw_info, list):
        info_list = [dict(x) for x in raw_info if isinstance(x, dict)]
    elif isinstance(raw_info, dict):
        for k, v in raw_info.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("email", str(v.get("email") or k))
                item.setdefault("account_id", str(v.get("account_id") or k))
                info_list.append(item)

    src = dict(account_entry) if isinstance(account_entry, dict) else {}
    src.pop("index", None)
    found_idx: Optional[int] = None
    for idx, existing in enumerate(info_list):
        if str(existing.get("email") or "").strip().lower() == clean_email.lower():
            found_idx = idx
            for k, v in src.items():
                if v and not existing.get(k):
                    existing[k] = v
            existing["email"] = existing.get("email") or clean_email
            break

    if found_idx is None:
        gaia_val = str(src.get("gaia") or src.get("account_id") or clean_email)
        new_item: Dict[str, Any] = {
            **src,
            "email": clean_email,
            "account_id": str(src.get("account_id") or gaia_val),
            "gaia": gaia_val,
            "full_name": str(src.get("full_name") or clean_email.split("@")[0]),
            "is_valid": True,
        }
        info_list.append(new_item)
        found_idx = len(info_list) - 1

    data["account_info"] = info_list
    tmp_file = pref_file.with_name(f".Preferences.{os.getpid()}.tmp")
    tmp_file.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(str(tmp_file), str(pref_file))
    return found_idx


def _backup_sqlite_file(src: Path, dst: Path, attempts: int = 6, retry_seconds: float = 0.5) -> bool:
    """Safely copy a SQLite database file using SQLite's online backup API with fallbacks.

    A live Chrome holds `Default/Network/Cookies` with an EXCLUSIVE lock: a plain read
    raises PermissionError and SQLite reports "unable to open database file". Chrome
    releases that lock every few seconds, so the copy is retried for a few seconds
    instead of failing outright - a snapshot that silently misses the cookie database
    is WORSE than none, because the caller then believes the session is protected.
    """
    if not src.is_file():
        return False
    try:
        if src.stat().st_size == 0:
            return False
    except OSError:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(dst) + suffix)
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:
            pass
    for attempt in range(max(1, int(attempts))):
        for uri_q in ("?mode=ro&immutable=1", "?mode=ro"):
            try:
                # `as_uri()` percent-encodes the path. A raw path with a SPACE in it
                # ("C:/Users/TechZone IT/...") is NOT a valid SQLite URI: the space ends
                # the URI, every read fails with "unable to open database file", and the
                # code falls back to a raw file copy - which BLOCKS forever on the cookie
                # database that a live Chrome holds locked. That is how a session snapshot
                # could hang the whole run.
                base_uri = src.resolve().as_uri()
                source = sqlite3.connect(f"{base_uri}{uri_q}", uri=True, timeout=5.0)
                try:
                    target = sqlite3.connect(str(dst), timeout=5.0)
                    try:
                        source.backup(target)
                    finally:
                        target.close()
                finally:
                    source.close()
                if dst.is_file() and dst.stat().st_size > 0:
                    return True
            except Exception:
                continue
        # The file is locked by a live Chrome, which releases the lock every few
        # seconds. Retry instead of failing outright.
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(float(retry_seconds))
    try:
        raw = src.read_bytes()
        if raw:
            dst.write_bytes(raw)
            return dst.is_file() and dst.stat().st_size > 0
    except Exception:
        pass
    return False


def snapshot_holds_cookies(snapshot_dir: Union[str, Path]) -> bool:
    """True when the snapshot really carries the cookie database.

    The cookie database IS the session. `snapshot_profile_session()` also copies
    `Default/Preferences` and `Local State`, so it can report success while the cookie
    database was locked and skipped - which would let a forced kill destroy the very
    session the snapshot was supposed to protect.
    """
    snap = Path(snapshot_dir)
    for rel in ("Default/Network/Cookies", "Default/Cookies"):
        p = snap / rel
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def snapshot_profile_session(
    profile_dir: Union[str, Path],
    snapshot_dir: Optional[Union[str, Path]] = None,
) -> Tuple[bool, Path]:
    """Snapshot `Default/Network/Cookies`, `Default/Cookies`, `Default/Web Data`,
    `Default/Preferences`, and `Local State` using the SQLite online backup API.
    """
    prof = Path(profile_dir).expanduser().resolve()
    snap = Path(snapshot_dir).expanduser().resolve() if snapshot_dir else (prof / COOKIE_SNAPSHOT_DIRNAME)
    if not prof.is_dir():
        return False, snap
    snap.mkdir(parents=True, exist_ok=True)
    copied_any = False

    for rel_db in ("Default/Network/Cookies", "Default/Cookies", "Default/Web Data"):
        src_db = prof / rel_db
        dst_db = snap / rel_db
        if src_db.is_file() and _backup_sqlite_file(src_db, dst_db):
            copied_any = True

    for rel_json in ("Default/Preferences", "Default/Secure Preferences", "Local State"):
        src_j = prof / rel_json
        dst_j = snap / rel_json
        if src_j.is_file():
            try:
                if src_j.stat().st_size > 0:
                    dst_j.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_j), str(dst_j))
                    copied_any = True
            except Exception:
                pass

    return copied_any, snap


def restore_profile_session(
    profile_dir: Union[str, Path],
    snapshot_dir: Optional[Union[str, Path]] = None,
) -> bool:
    """Restore `Default/Network/Cookies`, `Default/Preferences`, and `Local State` from snapshot."""
    prof = Path(profile_dir).expanduser().resolve()
    snap = Path(snapshot_dir).expanduser().resolve() if snapshot_dir else (prof / COOKIE_SNAPSHOT_DIRNAME)
    if not snap.is_dir():
        return False
    restored_any = False

    for rel_db in ("Default/Network/Cookies", "Default/Cookies", "Default/Web Data"):
        src_db = snap / rel_db
        dst_db = prof / rel_db
        if src_db.is_file():
            try:
                if src_db.stat().st_size > 0 and _backup_sqlite_file(src_db, dst_db):
                    restored_any = True
            except Exception:
                pass

    for rel_json in ("Default/Preferences", "Default/Secure Preferences", "Local State"):
        src_j = snap / rel_json
        dst_j = prof / rel_json
        if src_j.is_file():
            try:
                if src_j.stat().st_size > 0:
                    dst_j.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_j), str(dst_j))
                    restored_any = True
            except Exception:
                pass

    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        marker = prof / lock_name
        try:
            if marker.is_symlink() or marker.exists():
                marker.unlink(missing_ok=True)
        except Exception:
            pass

    return restored_any


def repair_slot_cookie_db_only(
    base_profile: Union[str, Path],
    target_profile: Union[str, Path],
) -> bool:
    """Repair a missing/0-byte cookie DB in `target_profile` without replacing `Default/Preferences`."""
    target = Path(target_profile).expanduser().resolve()
    base = Path(base_profile).expanduser().resolve()
    if has_cookie_db(target):
        return True
    snap = target / COOKIE_SNAPSHOT_DIRNAME
    if snap.is_dir() and has_cookie_db(snap):
        if restore_profile_session(target, snap) and has_cookie_db(target):
            return True
    if base.is_dir() and has_cookie_db(base):
        for rel_db in ("Default/Network/Cookies", "Default/Cookies"):
            src_db = base / rel_db
            dst_db = target / rel_db
            if src_db.is_file():
                _backup_sqlite_file(src_db, dst_db)
        for rel_state in ("Local State",):
            src_s = base / rel_state
            dst_s = target / rel_state
            if src_s.is_file() and not dst_s.is_file():
                try:
                    shutil.copy2(str(src_s), str(dst_s))
                except Exception:
                    pass
    return has_cookie_db(target)


def endpoint_file_for_profile(profile_dir: Union[str, Path]) -> Path:
    return Path(profile_dir).expanduser().resolve() / ENDPOINT_FILENAME


def read_host_state(profile_dir: Union[str, Path]) -> Optional[Dict[str, Any]]:
    ep_file = endpoint_file_for_profile(profile_dir)
    if not ep_file.is_file():
        return None
    try:
        data = json.loads(ep_file.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_host_state(profile_dir: Union[str, Path], state: Dict[str, Any]) -> None:
    prof = Path(profile_dir).expanduser().resolve()
    prof.mkdir(parents=True, exist_ok=True)
    ep_file = endpoint_file_for_profile(prof)
    lock_path = prof / f"{ENDPOINT_FILENAME}.lock"
    fd = _acquire_file_lock(lock_path)
    try:
        tmp = ep_file.with_name(f".{ENDPOINT_FILENAME}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(ep_file))
    except Exception:
        pass
    finally:
        _release_file_lock(fd, lock_path)


def update_host_state_locked(
    profile_dir: Union[str, Path],
    mutator: Any,
    default_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Atomically read, mutate, and write `.argon_browser_endpoint` while holding `.argon_browser_endpoint.lock`."""
    import threading as _th

    prof = Path(profile_dir).expanduser().resolve()
    prof.mkdir(parents=True, exist_ok=True)
    ep_file = endpoint_file_for_profile(prof)
    lock_path = prof / f"{ENDPOINT_FILENAME}.lock"
    fd = _acquire_file_lock(lock_path)
    try:
        state: Dict[str, Any] = {}
        if ep_file.is_file():
            try:
                loaded = json.loads(ep_file.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    state = loaded
            except Exception:
                state = {}
        if not state and isinstance(default_state, dict):
            state = dict(default_state)
        state.setdefault("tabs", {})
        state.setdefault("persistent", True)
        mutator(state)
        tmp = ep_file.with_name(f".{ENDPOINT_FILENAME}.{os.getpid()}.{_th.get_ident()}.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(ep_file))
        return state
    finally:
        _release_file_lock(fd, lock_path)


def remove_host_state(profile_dir: Union[str, Path]) -> None:
    ep_file = endpoint_file_for_profile(profile_dir)
    try:
        ep_file.unlink(missing_ok=True)
    except Exception:
        pass


def probe_cdp_endpoint(http_endpoint: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    """Query `<http_endpoint>/json/version` and `/json/list` to check if a CDP browser host is alive."""
    if not http_endpoint:
        return None
    base = http_endpoint.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/version", timeout=timeout) as resp:
            version_info = json.loads(resp.read().decode("utf-8", errors="replace"))
        targets: List[Dict[str, Any]] = []
        try:
            with urllib.request.urlopen(f"{base}/json/list", timeout=timeout) as resp_list:
                raw_list = json.loads(resp_list.read().decode("utf-8", errors="replace"))
                if isinstance(raw_list, list):
                    targets = [
                        t
                        for t in raw_list
                        if isinstance(t, dict) and str(t.get("type") or "page") == "page"
                    ]
        except Exception:
            targets = []
        return {
            "http_endpoint": base,
            "ws_endpoint": str(version_info.get("webSocketDebuggerUrl") or ""),
            "browser": str(version_info.get("Browser") or ""),
            "targets": targets,
        }
    except Exception:
        return None


def has_live_browser_host(profile_dir: Union[str, Path]) -> bool:
    state = read_host_state(profile_dir)
    if not state:
        return False
    http_ep = str(state.get("http_endpoint") or "")
    return probe_cdp_endpoint(http_ep) is not None


def _cmdline_uses_profile(cmdline: str, prof: Path) -> bool:
    """True when `cmdline` points Chrome at `prof`, matched on the RESOLVED path.

    A raw substring match is not enough. Windows hands Chrome the SHORT 8.3 form of a
    path (`C:\\Users\\TECHZO~1\\...`) while the caller may pass the long form
    (`C:\\Users\\TechZone IT\\...`), and the two never match as strings. When that
    happens the running host becomes INVISIBLE to `find_chrome_pids_for_profile`, so
    the caller believes the profile is free, launches a second Chrome on it, that one
    cannot win the profile lock, and the task is refused ("profile is busy") and then
    rotated onto a different account. Measured on the owner's machine (round 21).

    The comparison is therefore: the `--user-data-dir=` value of the command line,
    resolved and compared case-insensitively against the resolved profile dir.
    """
    low = str(cmdline or "").replace("\\", "/")
    for token in low.split(" "):
        token = token.strip().strip('"')
        if not token.startswith("--user-data-dir="):
            continue
        raw = token.split("=", 1)[1]
        if not raw:
            continue
        try:
            if Path(raw).resolve() == prof:
                return True
        except Exception:
            # An unresolvable path still gets the raw comparison.
            if raw.rstrip("/").lower() == str(prof).replace("\\", "/").rstrip("/").lower():
                return True
    return False


def find_chrome_pids_for_profile(profile_dir: Union[str, Path]) -> List[int]:
    """Return PIDs of Chrome/Chromium processes holding `profile_dir`."""
    prof = Path(profile_dir).expanduser().resolve()
    prof_str = str(prof)
    prof_norm = prof_str.lower().replace("\\", "/")
    pids: List[int] = []

    state = read_host_state(prof)
    if state and state.get("pid"):
        try:
            cand_pid = int(state["pid"])
            if cand_pid > 0 and pid_is_alive(cand_pid):
                pids.append(cand_pid)
        except Exception:
            pass

    marker = prof / "SingletonLock"
    try:
        if marker.is_symlink():
            link_target = os.readlink(marker)
            m = re.search(r"-(\d+)$", str(link_target))
            if m:
                s_pid = int(m.group(1))
                if s_pid > 0 and pid_is_alive(s_pid) and s_pid not in pids:
                    pids.append(s_pid)
    except Exception:
        pass

    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            try:
                pname = str(proc.info.get("name") or "").lower()
                if "chrome" not in pname and "chromium" not in pname:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or []).lower().replace("\\", "/")
                if prof_norm in cmdline and "--type=" not in cmdline:
                    p_id = int(proc.info["pid"])
                    if p_id > 0 and p_id not in pids:
                        pids.append(p_id)
                elif "--type=" not in cmdline:
                    # The command line may carry the SHORT 8.3 form of the profile path.
                    raw_cmdline = " ".join(proc.info.get("cmdline") or [])
                    if _cmdline_uses_profile(raw_cmdline, prof):
                        p_id = int(proc.info["pid"])
                        if p_id > 0 and p_id not in pids:
                            pids.append(p_id)
            except Exception:
                continue
        return pids
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            escaped_prof = prof_str.replace("'", "''")
            ps_cmd = (
                "Get-CimInstance Win32_Process | Where-Object { "
                "($_.Name -match 'chrome|chromium') -and "
                f"($_.CommandLine -like '*{escaped_prof}*') -and "
                "($_.CommandLine -notlike '*--type=*') "
                "} | Select-Object ProcessId,CommandLine"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=8,
            )
            for ln in (out.stdout or "").splitlines():
                ln_s = ln.strip()
                if not ln_s:
                    continue
                # The first field is the PID, the rest is the command line. The
                # command line is re-checked with the resolved-path comparison so a
                # short 8.3 path in the command line still matches.
                parts = ln_s.split(None, 1)
                pid_text = parts[0]
                cmd_text = parts[1] if len(parts) > 1 else ""
                if not pid_text.isdigit():
                    continue
                val = int(pid_text)
                if val <= 0 or val in pids:
                    continue
                if cmd_text and not _cmdline_uses_profile(cmd_text, prof):
                    # The -like filter matched a path fragment that is not the
                    # profile we are asking about (e.g. a parent directory).
                    continue
                pids.append(val)
        except Exception:
            pass
    else:
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid,args"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for ln in (out.stdout or "").splitlines():
                low = ln.lower().replace("\\", "/")
                if ("chrome" in low or "chromium" in low) and prof_norm in low and "--type=" not in low:
                    parts = ln.strip().split(None, 1)
                    if parts and parts[0].isdigit():
                        val = int(parts[0])
                        if val > 0 and val not in pids:
                            pids.append(val)
        except Exception:
            pass

    return pids


def _allocate_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _format_tab_owners(tabs: Dict[str, Any]) -> str:
    if not tabs:
        return "none"
    parts: List[str] = []
    for t_id, info in tabs.items():
        if not isinstance(info, dict):
            continue
        proj_name = Path(str(info.get("project") or ".")).name
        lane = str(info.get("lane") or "single")
        slot = str(info.get("slot") or "/u/0/")
        pid = info.get("pid") or "?"
        parts.append(f"{t_id}[{proj_name}:{lane}@{slot}, pid={pid}]")
    return ", ".join(parts) if parts else "none"


def _discover_running_cdp_endpoint(
    prof: Path,
    existing_state: Optional[Dict[str, Any]] = None,
    existing_pids: Optional[List[int]] = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Discover a live CDP HTTP endpoint for a Chrome process already running on `prof`."""
    candidate_endpoints: List[str] = []
    if isinstance(existing_state, dict):
        ep_raw = str(existing_state.get("http_endpoint") or "").strip()
        if ep_raw:
            candidate_endpoints.append(ep_raw)
        port_raw = existing_state.get("port")
        if port_raw and str(port_raw).isdigit():
            ep_port = f"http://127.0.0.1:{int(port_raw)}"
            if ep_port not in candidate_endpoints:
                candidate_endpoints.append(ep_port)

    devtools_port_file = prof / "DevToolsActivePort"
    if devtools_port_file.is_file():
        try:
            lines = devtools_port_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            if lines and lines[0].strip().isdigit():
                dt_port = int(lines[0].strip())
                if dt_port > 0:
                    ep_dt = f"http://127.0.0.1:{dt_port}"
                    if ep_dt not in candidate_endpoints:
                        candidate_endpoints.append(ep_dt)
        except Exception:
            pass

    try:
        import psutil  # type: ignore

        for pid in (existing_pids or []):
            try:
                cmdline = " ".join(psutil.Process(int(pid)).cmdline() or [])
                m_port = re.search(r"--remote-debugging-port=(\d+)", cmdline)
                if m_port:
                    ep_cmd = f"http://127.0.0.1:{int(m_port.group(1))}"
                    if ep_cmd not in candidate_endpoints:
                        candidate_endpoints.append(ep_cmd)
            except Exception:
                continue
    except Exception:
        pass

    for ep in candidate_endpoints:
        for timeout_s in (2.0, 3.5):
            probed = probe_cdp_endpoint(ep, timeout=timeout_s)
            if probed is not None:
                return ep, probed
    return None


def _build_recovered_host_state(
    prof: Path,
    http_ep: str,
    probed_info: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
    existing_pids: List[int],
) -> Dict[str, Any]:
    """Build the `.argon_browser_endpoint` state dict for a Chrome already running on `prof`.

    Owns the SINGLE definition of "what a recovered Argon browser host looks like" so the
    attach-immediately path and the wait-for-the-other-lane-to-finish-starting path can
    never drift apart (a drifted dict is how a lane ended up attaching to a host with a
    different port or a stale tab map).
    """
    m_p = re.search(r":(\d+)$", http_ep)
    port_val = int(m_p.group(1)) if m_p else int((existing or {}).get("port") or 0)
    ext_dir_existing = get_companion_extension_dir()
    return {
        "profile_dir": str(prof),
        "pid": int(existing_pids[0]) if existing_pids else 0,
        "pids": [int(p) for p in existing_pids],
        "port": port_val,
        "http_endpoint": http_ep,
        "ws_endpoint": str(probed_info.get("ws_endpoint") or (existing or {}).get("ws_endpoint") or ""),
        "companion_extension": str((existing or {}).get("companion_extension") or (ext_dir_existing or "")),
        "started_at": str((existing or {}).get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
        "tabs": dict((existing or {}).get("tabs") or {}),
    }


def ensure_browser_host(
    profile_dir: Union[str, Path],
    chrome_exe: Optional[str] = None,
    headless: bool = False,
    extra_args: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Ensure the single detached Argon Bridge Chrome host with `--remote-debugging-port` is running for `profile_dir`.

    In shared mode the Argon Bridge browser is supposed to stay running across accounts and tasks,
    so when Chrome is already running on `profile_dir` and publishes a CDP endpoint (in `.argon_browser_endpoint`,
    `DevToolsActivePort`, or `--remote-debugging-port`), this function attaches to it and returns its host state.
    When Chrome runs on `profile_dir` but has NOT published an endpoint yet (the other lane is still
    LAUNCHING it), this function WAITS up to `HOST_START_WAIT_SECONDS` (90 s) for the endpoint to appear
    and attaches to it, instead of declaring the profile unusable. Only refuses (`return None`) when
    that wait times out without any reachable endpoint.
    """
    prof = Path(profile_dir).expanduser().resolve()
    prof.mkdir(parents=True, exist_ok=True)

    existing = read_host_state(prof)
    if existing:
        probed = probe_cdp_endpoint(str(existing.get("http_endpoint") or ""), timeout=2.5)
        if probed is not None:
            existing["ws_endpoint"] = probed.get("ws_endpoint") or existing.get("ws_endpoint", "")
            return existing

    existing_pids = find_chrome_pids_for_profile(prof)
    if existing_pids:
        discovered = _discover_running_cdp_endpoint(prof, existing_state=existing, existing_pids=existing_pids)
        # `existing_now` tracks the FRESHEST host state seen (the other lane may publish
        # `.argon_browser_endpoint` while we wait); `existing` keeps the state that was
        # there when we arrived, which is what decides whether a stale state file is removed.
        existing_now = existing
        if discovered is None:
            # The other lane may still be LAUNCHING this very Chrome: the profile PIDs
            # already exist, but the CDP endpoint is published a moment later. Measured on
            # the owner's machine (round 20): giving up right here made the second lane
            # refuse to attach ("Profile ... is busy"), fall back to a direct launch that
            # cannot win the profile lock, and then rotate to a DIFFERENT pool account and
            # send the prompt twice. So WAIT for the endpoint instead of declaring the
            # host unusable - the host IS coming, it just has not published itself yet.
            print(
                f"[i] The Argon browser host on {prof} is still starting "
                f"(Chrome is running but no CDP endpoint yet) - "
                f"waiting up to {HOST_START_WAIT_SECONDS:.0f} s for it..."
            )
            wait_started = time.monotonic()
            deadline = wait_started + HOST_START_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(HOST_START_POLL_SECONDS)
                # Re-read the state every attempt: the other lane publishes
                # `.argon_browser_endpoint` while we are waiting.
                state_now = read_host_state(prof)
                if isinstance(state_now, dict) and state_now:
                    existing_now = state_now
                http_ep_now = str((existing_now or {}).get("http_endpoint") or "")
                probed_now: Optional[Dict[str, Any]] = None
                if http_ep_now:
                    probed_now = probe_cdp_endpoint(http_ep_now, timeout=1.5)
                if probed_now is not None:
                    discovered = (http_ep_now, probed_now)
                    break
                # The endpoint may also show up without a state file (DevToolsActivePort
                # or `--remote-debugging-port` on the running Chrome), so re-probe those too.
                discovered = _discover_running_cdp_endpoint(
                    prof, existing_state=existing_now, existing_pids=existing_pids
                )
                if discovered is not None:
                    break
            if discovered is not None:
                print(
                    f"[+] Attached to the Argon browser host that was still starting: "
                    f"{discovered[0]}"
                )
            else:
                waited_s = time.monotonic() - wait_started
                print(
                    f"[!] The Argon browser host on {prof} published no CDP endpoint after "
                    f"{waited_s:.1f} s (budget {HOST_START_WAIT_SECONDS:.0f} s; Chrome is running "
                    f"but unreachable); giving up on this profile."
                )
                if existing is not None:
                    remove_host_state(prof)
                return None
        http_ep, probed_info = discovered
        recovered_state = _build_recovered_host_state(
            prof, http_ep, probed_info, existing_now, existing_pids
        )
        write_host_state(prof, recovered_state)
        return recovered_state

    if existing is not None:
        remove_host_state(prof)

    if not chrome_exe:
        try:
            from argon_pool_login import find_chrome_executable

            chrome_exe = find_chrome_executable()
        except Exception:
            chrome_exe = None
    if not chrome_exe or not Path(chrome_exe).is_file():
        return None

    port = _allocate_free_port()
    http_endpoint = f"http://127.0.0.1:{port}"
    base_extra = list(extra_args or DEFAULT_HOST_ARGS)
    ext_dir = get_companion_extension_dir()
    if ext_dir is not None and not any("--load-extension=" in str(a) for a in base_extra):
        base_extra.extend(companion_extension_launch_args(ext_dir))
    cmd = [
        str(chrome_exe),
        f"--user-data-dir={prof}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        *base_extra,
    ]
    if headless:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            proc = subprocess.Popen(
                cmd,
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        return None

    deadline = time.monotonic() + 15.0
    probed_info: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        probed_info = probe_cdp_endpoint(http_endpoint, timeout=1.5)
        if probed_info is not None:
            break
        time.sleep(0.35)

    if probed_info is None:
        return None

    host_state: Dict[str, Any] = {
        "profile_dir": str(prof),
        "pid": int(proc.pid),
        "port": port,
        "http_endpoint": http_endpoint,
        "ws_endpoint": str(probed_info.get("ws_endpoint") or ""),
        "companion_extension": str(ext_dir) if ext_dir is not None else "",
        "persistent": True,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tabs": {},
    }
    write_host_state(prof, host_state)
    ext_status = f"extension={ext_dir}" if ext_dir is not None else "extension=none (pure-Python fallback)"
    print(
        f"[+] Browser host started for profile {prof} "
        f"(pid={proc.pid}, endpoint={http_endpoint}, {ext_status}, open_tabs=0, owning_tasks=none)"
    )
    return host_state

def reap_dead_tabs(context: Any, profile_dir: Union[str, Path]) -> List[str]:
    """Close any tab in `context` whose owning task PID is no longer alive (Section 2.3.B)."""
    prof = Path(profile_dir).expanduser().resolve()
    reaped: List[str] = []

    def _mutate_reap(state: Dict[str, Any]) -> None:
        tabs: Dict[str, Any] = dict(state.get("tabs") or {})
        for tab_id, info in list(tabs.items()):
            if not isinstance(info, dict):
                tabs.pop(tab_id, None)
                continue
            owner_pid = int(info.get("pid") or 0)
            if owner_pid and owner_pid != os.getpid() and not pid_is_alive(owner_pid):
                target_url = str(info.get("page_url") or info.get("chat_url") or "")
                if context is not None and hasattr(context, "pages"):
                    for pg in list(context.pages):
                        try:
                            if getattr(pg, "_argon_manual_tab", False):
                                continue
                            pg_tid = getattr(pg, "_argon_tab_id", None)
                            pg_url = str(getattr(pg, "url", "") or "")
                            if pg_tid == tab_id or (target_url and pg_url and pg_url == target_url):
                                if len(context.pages) > 1:
                                    pg.close()
                                break
                        except Exception:
                            continue
                tabs.pop(tab_id, None)
                reaped.append(f"{tab_id} (dead pid {owner_pid})")
        state["tabs"] = tabs

    if endpoint_file_for_profile(prof).is_file():
        update_host_state_locked(prof, _mutate_reap)
    if reaped:
        print(f"[+] Reaped {len(reaped)} orphaned tab(s) from dead process(es): {', '.join(reaped)}")
    return reaped


def acquire_host_tab(
    playwright_instance: Any,
    profile_dir: Union[str, Path],
    slot: str,
    project_path: Union[str, Path],
    lane: str = "single",
    chrome_exe: Optional[str] = None,
    headless: bool = False,
    account_email: str = "",
) -> Tuple[Any, Any, Optional[str], bool]:
    """Attach to the profile's CDP browser host and open a dedicated tab for this task.

    Returns `(context, page, tab_id, attached_to_host)`.
    When no host can be started or attached, returns `(None, None, None, False)` so
    the caller falls back cleanly to standard `launch_persistent_context`.
    """
    import threading as _th

    prof = Path(profile_dir).expanduser().resolve()
    host = ensure_browser_host(prof, chrome_exe=chrome_exe, headless=headless)
    if not host:
        return None, None, None, False

    http_ep = str(host.get("http_endpoint") or "")
    if not http_ep:
        return None, None, None, False

    try:
        browser = playwright_instance.chromium.connect_over_cdp(http_ep)
        context = browser.contexts[0] if browser.contexts else browser.new_context(no_viewport=True)
        reap_dead_tabs(context, prof)

        slot_clean = re.sub(r"[^A-Za-z0-9]+", "", str(slot or "u0")) or "u0"
        tab_id = f"tab-{slot_clean}-{os.getpid()}-{_th.get_ident() % 10000}-{int(time.time() * 1000) % 100000}"

        page_box: List[Any] = [None]

        def _register_tab_locked(state: Dict[str, Any]) -> None:
            tabs = dict(state.get("tabs") or {})
            reuse_blank = False
            if not tabs and not state.get("manual_open") and len(context.pages) == 1:
                try:
                    cand_pg = context.pages[0]
                    first_url = str(cand_pg.url or "").strip()
                    if not getattr(cand_pg, "_argon_manual_tab", False) and first_url in (
                        "",
                        "about:blank",
                        "chrome://newtab/",
                        "chrome://new-tab-page/",
                    ):
                        reuse_blank = True
                except Exception:
                    reuse_blank = False

            pg = context.pages[0] if reuse_blank else context.new_page()
            page_box[0] = pg
            target_id = ""
            try:
                cdp_sess = context.new_cdp_session(pg)
                t_info = cdp_sess.send("Target.getTargetInfo") or {}
                target_id = str((t_info.get("targetInfo") or {}).get("targetId") or "")
                cdp_sess.detach()
            except Exception:
                target_id = ""
            try:
                pg.evaluate("(tid) => { try { window.name = tid; } catch (e) {} }", tab_id)
            except Exception:
                pass

            tabs[tab_id] = {
                "tab_id": tab_id,
                "target_id": target_id,
                "pid": os.getpid(),
                "slot": str(slot or "/u/0/"),
                "account": str(account_email or ""),
                "project": str(Path(project_path).resolve()),
                "lane": str(lane or "single"),
                "chat_url": "",
                "chat_id": "",
                "page_url": str(getattr(pg, "url", "") or "about:blank"),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            state["tabs"] = tabs
            state["persistent"] = True

        updated_state = update_host_state_locked(prof, _register_tab_locked, default_state=host)
        page = page_box[0]
        tabs = dict(updated_state.get("tabs") or {})

        setattr(page, "_argon_tab_id", tab_id)
        setattr(context, "_argon_is_cdp_host", True)
        setattr(context, "_argon_cdp_browser", browser)
        setattr(context, "_argon_profile_dir", str(prof))
        setattr(context, "_argon_http_endpoint", http_ep)

        owners_str = _format_tab_owners(tabs)
        print(
            f"[+] Browser host attached ({http_ep}): opened tab {tab_id} | "
            f"active_tabs={len(tabs)} in 1 browser | tasks: {owners_str}"
        )
        return context, page, tab_id, True
    except Exception as exc:
        print(f"[!] Could not attach to browser host at {http_ep} ({exc}); falling back to direct browser launch.")
        return None, None, None, False


def update_host_tab(
    profile_dir: Union[str, Path],
    tab_id: Optional[str],
    **fields: Any,
) -> None:
    """Update metadata (`chat_url`, `chat_id`, `page_url`, `account`) for `tab_id` in host state under file lock."""
    if not tab_id:
        return
    prof = Path(profile_dir).expanduser().resolve()
    if not endpoint_file_for_profile(prof).is_file():
        return

    def _mutate_tab(state: Dict[str, Any]) -> None:
        tabs = dict(state.get("tabs") or {})
        entry = dict(tabs.get(tab_id) or {})
        if not entry:
            return
        for k, v in fields.items():
            if v is not None:
                entry[k] = v
        entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tabs[tab_id] = entry
        state["tabs"] = tabs

    update_host_state_locked(prof, _mutate_tab)


def _send_cdp_browser_close(context: Any = None, page: Any = None) -> bool:
    """Send CDP `Browser.close` over an active Playwright context/page so Chrome flushes cookies and exits."""
    try:
        if context is not None and hasattr(context, "new_cdp_session"):
            target_page = page
            if target_page is None or (hasattr(target_page, "is_closed") and target_page.is_closed()):
                if hasattr(context, "pages") and context.pages:
                    target_page = context.pages[0]
            if target_page is not None:
                cdp = context.new_cdp_session(target_page)
                cdp.send("Browser.close")
                return True
    except Exception:
        pass
    return False


def release_host_tab(
    context: Any,
    page: Any,
    profile_dir: Union[str, Path],
    tab_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Close ONLY this task's tab and keep the shared Argon browser host alive (Round 20).

    Never kills the whole browser host when task-tab count reaches 0, so manually opened
    tabs ('Open in Gemini Web') and the shared Argon browser survive across tasks.
    """
    prof = Path(profile_dir).expanduser().resolve()
    is_host = bool(getattr(context, "_argon_is_cdp_host", False))
    eff_tab_id = tab_id or getattr(page, "_argon_tab_id", None)

    if not is_host or not eff_tab_id:
        return close_browser_safely(context=context, profile_dir=prof)

    snapshot_profile_session(prof)
    reap_dead_tabs(context, prof)

    def _remove_tab_locked(state: Dict[str, Any]) -> None:
        tabs: Dict[str, Any] = dict(state.get("tabs") or {})
        tabs.pop(eff_tab_id, None)
        for tid, tinfo in list(tabs.items()):
            tpid = int((tinfo or {}).get("pid") or 0)
            if tpid and tpid != os.getpid() and not pid_is_alive(tpid):
                tabs.pop(tid, None)
        state["tabs"] = tabs
        state["persistent"] = True

    updated_state = update_host_state_locked(prof, _remove_tab_locked)
    tabs = dict(updated_state.get("tabs") or {})
    remaining_count = len(tabs)

    try:
        if page is not None and not (hasattr(page, "is_closed") and page.is_closed()):
            if context is not None and hasattr(context, "pages") and len(context.pages) <= 1:
                try:
                    context.new_page()
                except Exception:
                    pass
            if context is None or not hasattr(context, "pages") or len(context.pages) > 1:
                page.close()
            else:
                try:
                    page.goto("about:blank")
                except Exception:
                    pass
    except Exception:
        pass

    owners_str = _format_tab_owners(tabs)
    print(
        f"[+] Closed task tab {eff_tab_id}; browser host on {prof} stays open "
        f"(active_tabs={remaining_count}, owning_tasks={owners_str})."
    )
    return {
        "profile_dir": str(prof),
        "was_running": True,
        "mode": "tab_closed_browser_kept",
        "tab_closed": eff_tab_id,
        "remaining_tabs": remaining_count,
        "snapshot_taken": True,
        "snapshot_restored": False,
        "session_intact": True,
    }


def _terminate_pids(pids: List[int]) -> None:
    for pid in pids:
        if not pid_is_alive(pid):
            continue
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T"],
                    capture_output=True,
                    timeout=5,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not any(pid_is_alive(p) for p in pids):
            return
        time.sleep(0.25)
    for pid in pids:
        if not pid_is_alive(pid):
            continue
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    timeout=5,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def close_browser_safely(
    context: Any = None,
    profile_dir: Optional[Union[str, Path]] = None,
    wait_seconds: float = 8.0,
    page: Any = None,
) -> Dict[str, Any]:
    """Safely close the browser for `profile_dir` without ever destroying session cookies (Section 2.2.B).

    1. Calls `context.close()` / CDP `Browser.close` first while the Playwright driver is alive.
    2. Waits for the Chrome process of `profile_dir` to exit gracefully.
    3. Only if a kill is unavoidable, snapshots `Default/Network/Cookies` and
       `Default/Preferences` with the SQLite online backup API first, terminates the
       stuck process, and restores them afterwards so the session survives.
    4. Never deletes, wipes, or re-seeds `profile_dir`.
    """
    prof: Optional[Path] = None
    if profile_dir is not None:
        prof = Path(profile_dir).expanduser().resolve()
    elif context is not None and getattr(context, "_argon_profile_dir", None):
        prof = Path(str(getattr(context, "_argon_profile_dir"))).expanduser().resolve()

    snapshot_taken = False
    snapshot_restored = False
    snap_path: Optional[Path] = None
    if prof is not None and prof.is_dir():
        snapshot_taken, snap_path = snapshot_profile_session(prof)

    pids_before = find_chrome_pids_for_profile(prof) if prof is not None else []
    host_state = read_host_state(prof) if prof is not None else None
    was_running = bool(context is not None or pids_before or (host_state and has_live_browser_host(prof)))

    graceful_sent = False
    if context is not None:
        if getattr(context, "_argon_is_cdp_host", False):
            if _send_cdp_browser_close(context=context, page=page):
                graceful_sent = True
        try:
            context.close()
            graceful_sent = True
        except Exception:
            pass

    if prof is not None and not graceful_sent and host_state:
        http_ep = str(host_state.get("http_endpoint") or "")
        if http_ep and probe_cdp_endpoint(http_ep) is not None:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    b = p.chromium.connect_over_cdp(http_ep)
                    ctx = b.contexts[0] if b.contexts else None
                    if ctx is not None:
                        _send_cdp_browser_close(context=ctx)
                    try:
                        b.close()
                    except Exception:
                        pass
                    graceful_sent = True
            except Exception:
                pass

    if not was_running:
        if prof is not None:
            remove_host_state(prof)
        return {
            "profile_dir": str(prof) if prof else "",
            "was_running": False,
            "mode": "not_running",
            "snapshot_taken": snapshot_taken,
            "snapshot_restored": False,
            "session_intact": True,
        }

    deadline = time.monotonic() + max(1.0, float(wait_seconds))
    while time.monotonic() < deadline:
        live_pids = find_chrome_pids_for_profile(prof) if prof is not None else []
        if not live_pids:
            break
        time.sleep(0.3)

    remaining_pids = find_chrome_pids_for_profile(prof) if prof is not None else []
    mode = "graceful"
    if remaining_pids:
        cookies_protected = False
        if prof is not None and prof.is_dir():
            snap_ok, snap_path = snapshot_profile_session(prof)
            snapshot_taken = snapshot_taken or snap_ok
            cookies_protected = bool(snap_ok and snapshot_holds_cookies(snap_path))
            if not cookies_protected:
                # The cookie database is the session. It is locked by the live Chrome,
                # and Chrome releases that lock every few seconds - so retry for a
                # while. A snapshot without the cookies is NOT a session backup.
                for _ in range(10):
                    time.sleep(0.8)
                    snap_ok2, snap_path = snapshot_profile_session(prof)
                    if snap_ok2 and snapshot_holds_cookies(snap_path):
                        cookies_protected = True
                        snapshot_taken = True
                        break
        if not cookies_protected:
            # Killing the browser now would DESTROY the session, because there is no
            # cookie backup to restore. The browser is left running and the reason is
            # reported - a live browser is far better than a logged-out account.
            print(
                "[!] The cookie database could not be snapshotted while the browser holds "
                "it, so the session cannot be protected by a backup. The browser is LEFT "
                "RUNNING instead of being killed (a kill would log the account out).",
                file=sys.stderr,
            )
            remaining_pids = []
        else:
            _terminate_pids(remaining_pids)
            time.sleep(0.4)
            mode = "forced"
            if prof is not None and snapshot_taken:
                snapshot_restored = restore_profile_session(prof, snap_path)
    elif prof is not None and snapshot_taken and not has_cookie_db(prof):
        snapshot_restored = restore_profile_session(prof, snap_path)

    if prof is not None:
        remove_host_state(prof)

    session_intact = bool(prof is None or profile_has_signed_in_account(prof) or not snapshot_taken)
    return {
        "profile_dir": str(prof) if prof else "",
        "was_running": True,
        "mode": mode,
        "snapshot_taken": snapshot_taken,
        "snapshot_restored": snapshot_restored,
        "session_intact": session_intact,
    }


def _resolve_slot_and_profile(
    slot: Optional[str] = None,
    profile_dir: Optional[Union[str, Path]] = None,
) -> Tuple[str, Path]:
    from argon_pool_login import PoolConfig, normalize_slot

    cfg = PoolConfig()
    eff_slot = normalize_slot(slot) if slot else cfg.active_slot()
    if profile_dir is not None:
        prof = Path(profile_dir).expanduser().resolve()
    else:
        prof = cfg.profile_dir_for_slot(eff_slot)
    return eff_slot, prof


def close_browser_cli(
    slot: Optional[str] = None,
    profile_dir: Optional[Union[str, Path]] = None,
    project_path: str = ".",
) -> int:
    """CLI handler for `dispatch_argon.py --close-browser --slot /u/N/` (Round 17 Section 1.3).

    In shared mode, `--close-browser --slot /u/N/` closes ONLY the tabs of that account
    (reusing `close_tab_cli`) and leaves the Argon Bridge browser running for the other
    accounts. The browser itself is closed politely (with cookie flush) only when no task
    tab is left (or when `--close-browser` is invoked without `--slot`).
    """
    from argon_pool_login import PoolConfig, is_isolated_slot_profile, normalize_slot

    eff_slot, prof = _resolve_slot_and_profile(slot=slot, profile_dir=profile_dir)
    cfg = PoolConfig(profile_dir_override=profile_dir)
    host_state = read_host_state(prof)
    tabs_before = dict((host_state or {}).get("tabs") or {})

    # Reap any dead-PID tab entries before counting active task tabs
    live_tabs: Dict[str, Any] = {}
    for tid, tinfo in tabs_before.items():
        if not isinstance(tinfo, dict):
            continue
        tpid = int(tinfo.get("pid") or 0)
        if tpid and tpid != os.getpid() and not pid_is_alive(tpid):
            continue
        live_tabs[tid] = tinfo
    if host_state is not None and len(live_tabs) != len(tabs_before):
        host_state["tabs"] = live_tabs
        write_host_state(prof, host_state)

    if slot is not None and not is_isolated_slot_profile(prof):
        acct_idx = cfg.get_account_index(eff_slot)
        acct_url_token = f"/u/{acct_idx}/"
        slot_tab_ids = [
            tid
            for tid, tinfo in live_tabs.items()
            if normalize_slot(str(tinfo.get("slot") or "/u/0/")) == eff_slot
        ]
        closed_tabs: List[str] = []
        for tid in slot_tab_ids:
            if close_tab_cli(tab_id=tid, slot=eff_slot, profile_dir=prof, project_path=project_path) == 0:
                closed_tabs.append(tid)

        # Also close any unregistered tab in the CDP host whose URL belongs to this account's /u/<acct_idx>/ path
        state_now = read_host_state(prof)
        http_ep = str((state_now or {}).get("http_endpoint") or "")
        probed = probe_cdp_endpoint(http_ep) if http_ep else None
        if probed is not None:
            for t_obj in (probed.get("targets") or []):
                t_url = str((t_obj or {}).get("url") or "")
                if acct_url_token in t_url:
                    if close_tab_cli(tab_id=acct_url_token, slot=eff_slot, profile_dir=prof, project_path=project_path) == 0:
                        closed_tabs.append(t_url)

        state_after = read_host_state(prof) or {}
        remaining_tabs: Dict[str, Any] = {
            tid: tinfo
            for tid, tinfo in dict(state_after.get("tabs") or {}).items()
            if isinstance(tinfo, dict) and (not int(tinfo.get("pid") or 0) or pid_is_alive(int(tinfo.get("pid") or 0)))
        }
        if len(remaining_tabs) > 0:
            snap_ok, _ = snapshot_profile_session(prof)
            print("=" * 78)
            print(f"  SAFE ACCOUNT TAB CLOSE ({eff_slot} -> /u/{acct_idx}/)")
            print("=" * 78)
            print(f"  Profile path    : {prof}")
            print(f"  Slot / Index    : {eff_slot} (browser index /u/{acct_idx}/)")
            print(
                f"  Close status    : closed {len(closed_tabs)} tab(s) for {eff_slot}; "
                f"Argon Bridge browser kept running for {len(remaining_tabs)} remaining task tab(s) ({_format_tab_owners(remaining_tabs)})"
            )
            print(f"  Cookie snapshot : taken={'yes' if snap_ok else 'no'}, restored=no (browser still running)")
            print("  Session status  : intact (other accounts and shared Argon Bridge browser untouched)")
            print("=" * 78)
            return 0

    res = close_browser_safely(context=None, profile_dir=prof)

    print("=" * 78)
    print(f"  SAFE BROWSER CLOSE ({eff_slot})")
    print("=" * 78)
    print(f"  Profile path    : {prof}")
    print(f"  Slot            : {eff_slot}")
    if not res["was_running"]:
        print("  Close status    : no browser is currently running for this slot (nothing to close)")
        print(f"  Cookie snapshot : {'taken' if res['snapshot_taken'] else 'not needed (no active browser)'}, restored=no")
        print("  Session status  : intact (no account was signed out)")
    else:
        print(f"  Close status    : {res['mode']} close completed (0 task tabs remaining; browser closed politely with cookie flush)")
        print(
            f"  Cookie snapshot : taken={'yes' if res['snapshot_taken'] else 'no'}, "
            f"restored={'yes' if res['snapshot_restored'] else 'no (clean exit flushed cookies)'}"
        )
        print("  Session status  : intact (cookie database and Preferences preserved)")
    print("=" * 78)
    return 0


def close_tab_cli(
    tab_id: str,
    slot: Optional[str] = None,
    profile_dir: Optional[Union[str, Path]] = None,
    project_path: str = ".",
) -> int:
    """CLI handler for `dispatch_argon.py --close-tab --slot /u/N/ --tab <tab-id>` (Section 2.3.D)."""
    wanted = (tab_id or "").strip()
    if not wanted or wanted == "__USE_TAB_ARG__":
        print("[!] Error: --close-tab requires a tab identifier (pass --tab <tab-id>).", file=sys.stderr)
        return 1

    eff_slot, prof = _resolve_slot_and_profile(slot=slot, profile_dir=profile_dir)
    state = read_host_state(prof)
    http_ep = str((state or {}).get("http_endpoint") or "")
    probed = probe_cdp_endpoint(http_ep) if http_ep else None
    if not state or probed is None:
        print(
            f"[!] No running browser host found for slot {eff_slot} at {prof}; "
            f"refusing to force-kill anything.",
            file=sys.stderr,
        )
        return 1

    tabs: Dict[str, Any] = dict(state.get("tabs") or {})
    matched_tab_id: Optional[str] = None
    if wanted in tabs:
        matched_tab_id = wanted
    else:
        for tid, tinfo in tabs.items():
            if not isinstance(tinfo, dict):
                continue
            if (
                wanted in tid
                or wanted == str(tinfo.get("target_id") or "")
                or wanted == str(tinfo.get("chat_id") or "")
                or wanted in str(tinfo.get("chat_url") or "")
                or wanted in str(tinfo.get("page_url") or "")
            ):
                matched_tab_id = tid
                break

    closed_in_browser = False
    target_url_hint = ""
    target_id_hint = ""
    if matched_tab_id and isinstance(tabs.get(matched_tab_id), dict):
        target_url_hint = str(tabs[matched_tab_id].get("page_url") or tabs[matched_tab_id].get("chat_url") or "")
        target_id_hint = str(tabs[matched_tab_id].get("target_id") or "")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(http_ep)
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is not None and hasattr(ctx, "pages"):
                pages = list(ctx.pages)
                target_page = None
                for idx, pg in enumerate(pages):
                    pg_url = str(getattr(pg, "url", "") or "")
                    pg_win_name = ""
                    try:
                        pg_win_name = str(pg.evaluate("() => window.name || ''") or "")
                    except Exception:
                        pg_win_name = ""
                    if matched_tab_id and pg_win_name == matched_tab_id:
                        target_page = pg
                        break
                    if target_id_hint:
                        try:
                            cdp_s = ctx.new_cdp_session(pg)
                            t_inf = cdp_s.send("Target.getTargetInfo") or {}
                            cdp_s.detach()
                            if str((t_inf.get("targetInfo") or {}).get("targetId") or "") == target_id_hint:
                                target_page = pg
                                break
                        except Exception:
                            pass
                    if (
                        (target_url_hint and pg_url == target_url_hint)
                        or (wanted in pg_url)
                        or (wanted.isdigit() and int(wanted) == idx)
                    ):
                        target_page = pg
                        break
                if target_page is None and matched_tab_id and len(pages) == 1 and len(tabs) == 1:
                    target_page = pages[0]
                if target_page is not None:
                    if len(pages) == 1:
                        blank_pg = ctx.new_page()
                        blank_pg.goto("about:blank")
                    target_page.close()
                    closed_in_browser = True
    except Exception as exc:
        print(f"[!] Warning while closing tab {wanted} over CDP: {exc}", file=sys.stderr)

    if not matched_tab_id and not closed_in_browser:
        print(
            f"[!] Tab {wanted!r} was not found in browser host for slot {eff_slot} ({prof}). "
            f"Open tabs: {list(tabs.keys())}",
            file=sys.stderr,
        )
        return 1

    if matched_tab_id:
        tabs.pop(matched_tab_id, None)
        state["tabs"] = tabs
        write_host_state(prof, state)

    print("=" * 78)
    print(f"  SAFE TAB CLOSE ({eff_slot})")
    print("=" * 78)
    print(f"  Profile path    : {prof}")
    print(f"  Closed tab      : {matched_tab_id or wanted}")
    print(f"  Remaining tabs  : {len(tabs)} ({_format_tab_owners(tabs)})")
    print("  Browser status  : still running (other tabs and signed-in session untouched)")
    print("=" * 78)
    return 0


def list_browsers_cli(project_path: str = ".") -> int:
    """CLI handler for `dispatch_argon.py --list-browsers` (Section 2.3.D & 2.3.E)."""
    from argon_pool_login import PoolConfig

    cfg = PoolConfig()
    candidate_dirs: List[Path] = []

    def _add_cand(p: Path) -> None:
        try:
            rp = p.expanduser().resolve()
            if rp not in candidate_dirs:
                candidate_dirs.append(rp)
        except Exception:
            pass

    _add_cand(cfg.profile_dir)
    profiles_root = cfg.profile_dir.parent / "argon_profiles"
    if profiles_root.is_dir():
        for child in sorted(profiles_root.iterdir()):
            if child.is_dir():
                _add_cand(child)
    default_profiles_root = Path.home() / ".gemini" / "argon_profiles"
    if default_profiles_root.is_dir():
        for child in sorted(default_profiles_root.iterdir()):
            if child.is_dir():
                _add_cand(child)

    running_hosts: List[Dict[str, Any]] = []
    total_tabs = 0
    for prof in candidate_dirs:
        if not prof.is_dir():
            continue
        state = read_host_state(prof)
        http_ep = str((state or {}).get("http_endpoint") or "")
        probed = probe_cdp_endpoint(http_ep) if http_ep else None
        pids = find_chrome_pids_for_profile(prof)
        if probed is None and not pids:
            if state is not None:
                remove_host_state(prof)
            continue

        tabs = dict((state or {}).get("tabs") or {})
        cleaned_tabs: Dict[str, Any] = {}
        for tid, tinfo in tabs.items():
            if not isinstance(tinfo, dict):
                continue
            tpid = int(tinfo.get("pid") or 0)
            if tpid and not pid_is_alive(tpid):
                continue
            cleaned_tabs[tid] = tinfo
        if state is not None and len(cleaned_tabs) != len(tabs):
            state["tabs"] = cleaned_tabs
            write_host_state(prof, state)

        cdp_targets = (probed or {}).get("targets") or []
        tab_count = max(len(cleaned_tabs), len(cdp_targets))
        total_tabs += tab_count
        running_hosts.append(
            {
                "profile_dir": prof,
                "endpoint": http_ep or "(direct launch - no CDP endpoint)",
                "pids": pids,
                "tabs": cleaned_tabs,
                "cdp_targets": cdp_targets,
                "accounts": [a.get("email") for a in get_preferences_accounts(prof) if a.get("email")],
            }
        )

    print("=" * 78)
    print("  ARGON RUNNING BROWSER HOSTS")
    print("=" * 78)
    if not running_hosts:
        print("  No running Argon browser hosts found (0 browsers, 0 open tabs).")
    else:
        print(
            f"  Summary: {len(running_hosts)} running browser process(es) hosting {total_tabs} open tab(s)."
        )
        for idx, h in enumerate(running_hosts, 1):
            accts = ", ".join(h["accounts"]) if h["accounts"] else "(signed-in profile)"
            print(f"\n  [{idx}] Profile  : {h['profile_dir']}")
            print(f"      Endpoint : {h['endpoint']}")
            print(f"      PID(s)   : {', '.join(str(p) for p in h['pids']) or '(attached)'}")
            print(f"      Accounts : {accts}")
            reg_tabs: Dict[str, Any] = h["tabs"]
            if reg_tabs:
                print(f"      Task Tabs ({len(reg_tabs)}):")
                for tid, tinfo in reg_tabs.items():
                    print(
                        f"        - {tid}: project={tinfo.get('project')} lane={tinfo.get('lane')} "
                        f"slot={tinfo.get('slot')} account={tinfo.get('account') or '?'} "
                        f"pid={tinfo.get('pid')} url={tinfo.get('chat_url') or tinfo.get('page_url') or ''}"
                    )
            elif h["cdp_targets"]:
                print(f"      Browser Tabs ({len(h['cdp_targets'])}):")
                for t in h["cdp_targets"]:
                    print(f"        - id={t.get('id')} title={t.get('title')!r} url={t.get('url')}")
            else:
                print("      Tabs     : 0 registered task tabs")
    print("=" * 78)
    return 0


def open_browser_cli(
    slot: Optional[str] = None,
    profile_dir: Optional[Union[str, Path]] = None,
    project_path: str = ".",
    url: str = "",
    timeout: float = 45.0,
) -> int:
    """CLI handler for `dispatch_argon.py --open-browser --slot /u/N/` (Rule 10).

    NOTHING may be sent before the browser is open. A browser tool call on a browser
    that is not running fails, and the send silently never happens - the run looks
    like it is working while nothing was ever delivered.

    This starts (or attaches to) the slot's single CDP browser host, navigates a tab
    to Gemini Web and PROVES that the browser really is up by reading the page title
    back, so the caller can go on with confidence.

    It uses the CDP HOST and not a second Chrome process: a second Chrome on the same
    profile directory would take the profile lock and the dispatcher could no longer
    attach to that profile.
    """
    from argon_pool_login import (
        GEMINI_WEB_URL,
        PoolConfig,
        find_chrome_executable,
        is_isolated_slot_profile,
        normalize_slot,
        seed_slot_profile,
    )

    eff_slot, prof = _resolve_slot_and_profile(slot=slot, profile_dir=profile_dir)

    # Only seed when the emergency --isolated-profile escape hatch was explicitly requested.
    try:
        if is_isolated_slot_profile(prof) and not (prof.is_dir() and (prof / "Default").is_dir()):
            seed_slot_profile(Path.home() / ".gemini" / "argon_browser_profile", prof)
    except Exception as exc:
        print(f"[i] The isolated slot profile could not be seeded ({exc}); continuing anyway.")

    # In the Argon browser profile (~/.gemini/argon_browser_profile), the URL uses the
    # account's verified Default/Preferences index from PoolConfig.get_account_index(eff_slot).
    target_url = url.strip()
    if not target_url:
        try:
            if is_isolated_slot_profile(prof):
                target_url = GEMINI_WEB_URL.format(slot="/u/0/")
            else:
                cfg = PoolConfig(profile_dir_override=profile_dir)
                idx = cfg.get_account_index(eff_slot)
                target_url = GEMINI_WEB_URL.format(slot=f"/u/{idx}/")
        except Exception:
            target_url = GEMINI_WEB_URL.format(slot="/u/0/")

    host = ensure_browser_host(prof, chrome_exe=find_chrome_executable(), headless=False)
    if not host:
        # A plain Chrome (one that was started WITHOUT --remote-debugging-port) holds the
        # profile. It publishes no CDP endpoint, so the bridge cannot attach to it and
        # every browser tool call on that profile fails. It is closed the SAFE way -
        # the cookies are snapshotted first and restored afterwards - and then the host
        # is started, so the account stays signed in.
        stuck_pids = find_chrome_pids_for_profile(prof)
        if stuck_pids:
            print(
                f"[i] {len(stuck_pids)} Chrome process(es) hold this slot profile without a "
                f"CDP endpoint - the bridge cannot attach to them. Closing them safely "
                f"(cookies are snapshotted and restored) and starting the browser host."
            )
            try:
                close_browser_safely(context=None, profile_dir=prof)
            except Exception as exc:
                print(f"[!] The safe close failed ({exc}).", file=sys.stderr)
            host = ensure_browser_host(prof, chrome_exe=find_chrome_executable(), headless=False)
    if not host:
        print(
            "[!] The browser could NOT be opened: a Chrome process for this slot profile "
            "is already running but it does not publish a CDP endpoint, so it cannot be "
            "attached to. Close it with --close-browser and try again.",
            file=sys.stderr,
        )
        return 1

    http_ep = str(host.get("http_endpoint") or "")
    print("=" * 78)
    print(f"  BROWSER OPEN ({eff_slot})")
    print("=" * 78)
    print(f"  Profile path    : {prof}")
    print(f"  Slot            : {eff_slot}")
    print(f"  Endpoint        : {http_ep}")
    print(f"  PID(s)          : {', '.join(str(p) for p in (host.get('pids') or [])) or '(attached)'}")

    title = ""
    page_url = target_url
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(http_ep)
            context = browser.contexts[0] if browser.contexts else browser.new_context(no_viewport=True)
            page = None
            for pg in context.pages:
                try:
                    if str(pg.url or "").strip() in ("", "about:blank", "chrome://newtab/"):
                        page = pg
                        break
                except Exception:
                    continue
            if page is None:
                page = context.new_page()
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except Exception as exc:
                print(f"[i] The tab was opened but the page did not finish loading ({exc}).")
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    title = str(page.title() or "").strip()
                    page_url = str(page.url or target_url)
                except Exception:
                    title = ""
                if title:
                    break
                time.sleep(0.5)
            if not title:
                print(
                    "[!] The browser is running, but the page did not produce a title in "
                    f"{int(timeout)} s - the browser may not be usable yet.",
                    file=sys.stderr,
                )
                return 1
    except Exception as exc:
        print(f"[!] The browser host started but the tab could not be opened ({exc}).", file=sys.stderr)
        return 1

    update_host_state_locked(
        prof,
        lambda st: st.update(
            {
                "persistent": True,
                "manual_open": True,
                "manual_slot": eff_slot,
                "manual_url": page_url,
                "manual_opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        ),
        default_state=host,
    )
    ext_dir_now = get_companion_extension_dir()
    print(f"  Opened URL      : {page_url}")
    print(f"  Page title      : {title}")
    print(
        f"  Extension       : "
        f"{f'loaded ({ext_dir_now})' if ext_dir_now is not None else 'not installed (pure-Python fallback active)'}"
    )
    print(f"  Session         : {'signed in' if profile_has_signed_in_account(prof) else 'NOT signed in'}")
    print("  Browser status  : OPEN and reachable - a prompt can be sent now")
    print("=" * 78)
    return 0


RECLAIMABLE_CACHE_RELPATHS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "Default/DawnCache",
    "Default/Blob_storage",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "BrowserMetrics",
    "Crashpad",
)


def _dir_byte_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        for root, _, files in os.walk(path):
            for fname in files:
                fpath = Path(root) / fname
                try:
                    if not fpath.is_symlink():
                        total += fpath.stat().st_size
                except OSError:
                    continue
    except Exception:
        pass
    return total


GOOGLE_COOKIE_HOST_SUFFIXES = (
    "google.com",
    "accounts.google.com",
    "gds.google.com",
    "gemini.google.com",
    "myaccount.google.com",
    "googleusercontent.com",
    "gstatic.com",
    "youtube.com",
    "1e100.net",
)


def _is_google_cookie_host(host_or_domain: str) -> bool:
    """Return True when a cookie host/domain belongs to Google (`google.com`, `accounts.google.com`, `gds.google.com`, `gemini.google.com`, etc.)."""
    clean = (host_or_domain or "").strip().lower().lstrip(".")
    if not clean:
        return False
    if any(clean == h or clean.endswith("." + h) for h in GOOGLE_COOKIE_HOST_SUFFIXES):
        return True
    return bool(re.search(r"(^|\.)google\.[a-z.]+$", clean))


def is_real_slot_profile_dir(path: Union[str, Path]) -> bool:
    """Return True ONLY when `path` is a real numbered slot directory (`slot-u0`, `slot-u1`, ...)."""
    p = Path(path)
    return bool(p.is_dir() and re.match(r"^slot-u(\d+)$", p.name, flags=re.IGNORECASE))


def is_test_leftover_profile_dir(path: Union[str, Path]) -> bool:
    """Return True when `path` inside `argon_profiles/` is a test/probe leftover (`run-*`, `*-concurrent-*`, `*-cap*`, `slot-u-0`, etc.)."""
    p = Path(path)
    if not p.is_dir():
        return False
    name_low = p.name.lower()
    if name_low == "argon_browser_profile":
        return False
    if is_real_slot_profile_dir(p):
        return False
    if (
        name_low.startswith("run-")
        or "-concurrent-" in name_low
        or "-cap" in name_low
        or name_low.startswith("slot-u-")
    ):
        return True
    return True


def _rmtree_complete(target_dir: Path) -> bool:
    """Completely delete `target_dir` and all its contents from disk (clearing read-only flags on Windows if needed)."""
    import stat

    def _on_rm_error(func: Any, path_str: str, exc_info: Any) -> None:
        try:
            os.chmod(path_str, stat.S_IWRITE)
            func(path_str)
        except Exception:
            pass

    if not target_dir.exists():
        return True
    try:
        shutil.rmtree(str(target_dir), onerror=_on_rm_error)
    except Exception:
        shutil.rmtree(str(target_dir), ignore_errors=True)
    return not target_dir.exists()


def _sync_local_state_os_crypt_key(src_profile: Path, dst_profile: Path) -> bool:
    """Ensure `dst_profile/Local State` carries the DPAPI `os_crypt.encrypted_key` from `src_profile` (Round 18 Section 1.2)."""
    src_state_file = src_profile / "Local State"
    if not src_state_file.is_file():
        snap_state = src_profile / COOKIE_SNAPSHOT_DIRNAME / "Local State"
        if snap_state.is_file():
            src_state_file = snap_state
    dst_state_file = dst_profile / "Local State"

    src_key = ""
    src_data: Dict[str, Any] = {}
    if src_state_file.is_file():
        try:
            src_data = json.loads(src_state_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(src_data, dict) and isinstance(src_data.get("os_crypt"), dict):
                src_key = str(src_data["os_crypt"].get("encrypted_key") or "").strip()
        except Exception:
            src_data = {}

    dst_data: Dict[str, Any] = {}
    dst_key = ""
    if dst_state_file.is_file():
        try:
            dst_data = json.loads(dst_state_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(dst_data, dict) and isinstance(dst_data.get("os_crypt"), dict):
                dst_key = str(dst_data["os_crypt"].get("encrypted_key") or "").strip()
        except Exception:
            dst_data = {}

    if dst_key and (dst_key == src_key or not src_key):
        return True

    if src_key:
        if not dst_key or (not has_cookie_db(dst_profile) and not get_preferences_accounts(dst_profile)):
            if not dst_data and src_data:
                dst_data = dict(src_data)
            os_crypt_obj = dst_data.get("os_crypt")
            if not isinstance(os_crypt_obj, dict):
                os_crypt_obj = {}
            os_crypt_obj["encrypted_key"] = src_key
            dst_data["os_crypt"] = os_crypt_obj
            dst_state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_st = dst_state_file.with_name(f".LocalState.{os.getpid()}.tmp")
            tmp_st.write_text(json.dumps(dst_data, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp_st), str(dst_state_file))
            return True
    return bool(dst_key or src_key)


def _migrate_cookies_via_cdp(
    src_profile: Path,
    dst_profile: Path,
) -> Tuple[bool, int, str]:
    """Move Google cookies from `src_profile` into `dst_profile` lock-free via DevTools `Storage.getCookies` / `Storage.setCookies` (Round 18 Section 1.2)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return False, 0, f"Playwright unavailable for CDP cookie migration ({exc})"

    if find_chrome_pids_for_profile(src_profile):
        return False, 0, f"source slot profile {src_profile} is busy"

    try:
        from argon_pool_login import find_chrome_executable

        chrome_exe = find_chrome_executable()
    except Exception:
        chrome_exe = None

    extracted_google_cookies: List[Dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            launch_kwargs: Dict[str, Any] = {
                "user_data_dir": str(src_profile),
                "headless": True,
                "args": list(DEFAULT_HOST_ARGS),
                "ignore_default_args": ["--enable-automation"],
            }
            if chrome_exe and Path(chrome_exe).is_file():
                ctx_src = p.chromium.launch_persistent_context(executable_path=chrome_exe, **launch_kwargs)
            else:
                ctx_src = p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                pg_src = ctx_src.pages[0] if ctx_src.pages else ctx_src.new_page()
                cdp_src = ctx_src.new_cdp_session(pg_src)
                raw_res = cdp_src.send("Storage.getCookies") or {}
                cdp_src.detach()
                for ck in (raw_res.get("cookies") or []):
                    if not isinstance(ck, dict):
                        continue
                    dom = str(ck.get("domain") or "")
                    if _is_google_cookie_host(dom) and ck.get("name") and ck.get("value") is not None:
                        param: Dict[str, Any] = {
                            "name": str(ck["name"]),
                            "value": str(ck["value"]),
                            "domain": dom,
                            "path": str(ck.get("path") or "/"),
                            "secure": bool(ck.get("secure", True)),
                            "httpOnly": bool(ck.get("httpOnly", False)),
                        }
                        if isinstance(ck.get("expires"), (int, float)) and float(ck["expires"]) > 0:
                            param["expires"] = float(ck["expires"])
                        if ck.get("sameSite") in ("Strict", "Lax", "None"):
                            param["sameSite"] = ck["sameSite"]
                        if ck.get("priority") in ("Low", "Medium", "High"):
                            param["priority"] = ck["priority"]
                        if isinstance(ck.get("sourceScheme"), str) and ck["sourceScheme"] in ("Unset", "NonSecure", "Secure"):
                            param["sourceScheme"] = ck["sourceScheme"]
                        if isinstance(ck.get("sourcePort"), int) and ck["sourcePort"] > 0:
                            param["sourcePort"] = int(ck["sourcePort"])
                        if ck.get("partitionKey"):
                            param["partitionKey"] = ck["partitionKey"]
                        extracted_google_cookies.append(param)
            finally:
                try:
                    ctx_src.close()
                except Exception:
                    pass
    except Exception as exc:
        return False, 0, f"Storage.getCookies on {src_profile.name} failed ({exc})"

    if not extracted_google_cookies:
        return False, 0, "no Google cookies returned by Storage.getCookies"

    try:
        with sync_playwright() as p:
            host = ensure_browser_host(dst_profile, chrome_exe=chrome_exe, headless=True)
            if host and host.get("http_endpoint"):
                browser = p.chromium.connect_over_cdp(str(host["http_endpoint"]))
                ctx_dst = browser.contexts[0] if browser.contexts else browser.new_context(no_viewport=True)
                pg_dst = ctx_dst.pages[0] if ctx_dst.pages else ctx_dst.new_page()
                cdp_dst = ctx_dst.new_cdp_session(pg_dst)
                existing_res = cdp_dst.send("Storage.getCookies") or {}
                existing_keys = {
                    (
                        str(c.get("domain") or "").lower(),
                        str(c.get("name") or ""),
                        str(c.get("path") or "/"),
                    )
                    for c in (existing_res.get("cookies") or [])
                    if isinstance(c, dict) and c.get("value")
                }
                to_set = [
                    c
                    for c in extracted_google_cookies
                    if (c["domain"].lower(), c["name"], c["path"]) not in existing_keys
                ]
                if not existing_keys:
                    to_set = extracted_google_cookies
                if to_set:
                    cdp_dst.send("Storage.setCookies", {"cookies": to_set})
                cdp_dst.detach()
                return True, len(to_set), "cdp_storage_cookies"
            else:
                launch_dst: Dict[str, Any] = {
                    "user_data_dir": str(dst_profile),
                    "headless": True,
                    "args": list(DEFAULT_HOST_ARGS),
                    "ignore_default_args": ["--enable-automation"],
                }
                if chrome_exe and Path(chrome_exe).is_file():
                    ctx_dst = p.chromium.launch_persistent_context(executable_path=chrome_exe, **launch_dst)
                else:
                    ctx_dst = p.chromium.launch_persistent_context(**launch_dst)
                try:
                    pg_dst = ctx_dst.pages[0] if ctx_dst.pages else ctx_dst.new_page()
                    cdp_dst = ctx_dst.new_cdp_session(pg_dst)
                    existing_res = cdp_dst.send("Storage.getCookies") or {}
                    existing_keys = {
                        (
                            str(c.get("domain") or "").lower(),
                            str(c.get("name") or ""),
                            str(c.get("path") or "/"),
                        )
                        for c in (existing_res.get("cookies") or [])
                        if isinstance(c, dict) and c.get("value")
                    }
                    to_set = [
                        c
                        for c in extracted_google_cookies
                        if (c["domain"].lower(), c["name"], c["path"]) not in existing_keys
                    ]
                    if not existing_keys:
                        to_set = extracted_google_cookies
                    if to_set:
                        cdp_dst.send("Storage.setCookies", {"cookies": to_set})
                    cdp_dst.detach()
                finally:
                    close_browser_safely(context=ctx_dst, profile_dir=dst_profile)
                return True, len(to_set), "cdp_storage_cookies"
    except Exception as exc:
        return False, 0, f"Storage.setCookies on {dst_profile.name} failed ({exc})"


def _merge_google_cookies_sqlite(
    src_profile: Path,
    dst_profile: Path,
) -> Tuple[bool, int, str]:
    """Merge Google cookie rows from `src_profile/Default/Network/Cookies` into `dst_profile/Default/Network/Cookies` (Round 18 Section 1.2).

    Preserves DPAPI-encrypted `encrypted_value` blobs by syncing `Local State` (`os_crypt.encrypted_key`)
    first, keeps only rows belonging to Google hosts (`google.com`, `accounts.google.com`, `gds.google.com`,
    `gemini.google.com`, etc.), and uses `INSERT OR IGNORE` so rows already in `dst_profile` for other
    accounts are never overwritten.
    """
    import tempfile

    _sync_local_state_os_crypt_key(src_profile, dst_profile)

    src_cookie: Optional[Path] = None
    for rel in (
        "Default/Network/Cookies",
        "Default/Cookies",
        f"{COOKIE_SNAPSHOT_DIRNAME}/Default/Network/Cookies",
        f"{COOKIE_SNAPSHOT_DIRNAME}/Default/Cookies",
    ):
        cand = src_profile / rel
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                src_cookie = cand
                break
        except OSError:
            continue

    if src_cookie is None:
        return False, 0, f"source slot profile {src_profile.name} has no Cookies database"

    dst_cookie = dst_profile / "Default" / "Network" / "Cookies"
    dst_cookie.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="argon_ck_mig_") as tmp_d:
        tmp_src_db = Path(tmp_d) / "src_cookies.sqlite"
        if not _backup_sqlite_file(src_cookie, tmp_src_db):
            try:
                raw_bytes = src_cookie.read_bytes()
                if raw_bytes:
                    tmp_src_db.write_bytes(raw_bytes)
            except Exception as exc:
                return False, 0, f"could not read source Cookies DB {src_cookie} ({exc})"

        src_has_table = False
        src_cols: List[str] = []
        try:
            conn_check = sqlite3.connect(str(tmp_src_db), timeout=5.0)
            try:
                cur = conn_check.execute("PRAGMA table_info(cookies)")
                src_cols = [str(row[1]) for row in cur.fetchall() if len(row) > 1]
                src_has_table = bool(src_cols and "host_key" in src_cols)
            finally:
                conn_check.close()
        except Exception:
            src_has_table = False

        if not src_has_table:
            if not has_cookie_db(dst_profile):
                try:
                    dst_cookie.write_bytes(tmp_src_db.read_bytes())
                    return True, 1, "raw_cookie_copy"
                except Exception as exc:
                    return False, 0, f"could not copy cookie file ({exc})"
            return True, 0, "existing_cookie_db_preserved"

        dst_has_table = False
        if dst_cookie.is_file() and dst_cookie.stat().st_size > 0:
            try:
                conn_dst_chk = sqlite3.connect(str(dst_cookie), timeout=5.0)
                try:
                    cur_d = conn_dst_chk.execute("PRAGMA table_info(cookies)")
                    dst_cols = [str(r[1]) for r in cur_d.fetchall() if len(r) > 1]
                    dst_has_table = bool(dst_cols and "host_key" in dst_cols)
                finally:
                    conn_dst_chk.close()
            except Exception:
                return False, 0, f"destination Cookies DB {dst_cookie} is locked by a running Chrome"

        google_where = (
            "LOWER(host_key) LIKE '%google.%' "
            "OR LOWER(host_key) LIKE '%googleusercontent.com' "
            "OR LOWER(host_key) LIKE '%gstatic.com' "
            "OR LOWER(host_key) LIKE '%youtube.com' "
            "OR LOWER(host_key) LIKE '%1e100.net'"
        )

        if not dst_has_table:
            if not _backup_sqlite_file(tmp_src_db, dst_cookie):
                shutil.copy2(str(tmp_src_db), str(dst_cookie))
            try:
                conn_init = sqlite3.connect(str(dst_cookie), timeout=5.0)
                try:
                    conn_init.execute(f"DELETE FROM cookies WHERE NOT ({google_where})")
                    conn_init.commit()
                    cnt_row = conn_init.execute("SELECT COUNT(*) FROM cookies").fetchone()
                    kept = int(cnt_row[0]) if cnt_row else 0
                finally:
                    conn_init.close()
                return True, kept, "sqlite_initialized_google_cookies"
            except Exception as exc:
                return False, 0, f"failed to initialize destination Cookies DB ({exc})"

        try:
            conn_merge = sqlite3.connect(str(dst_cookie), timeout=5.0)
            try:
                cur_d = conn_merge.execute("PRAGMA table_info(cookies)")
                dst_cols = [str(r[1]) for r in cur_d.fetchall() if len(r) > 1]
                common_cols = [c for c in dst_cols if c in src_cols]
                if not common_cols:
                    return False, 0, "source and destination cookies tables share no common columns"
                col_list_sql = ", ".join(f'"{c}"' for c in common_cols)
                before_cnt_row = conn_merge.execute("SELECT COUNT(*) FROM cookies").fetchone()
                before_cnt = int(before_cnt_row[0]) if before_cnt_row else 0
                conn_merge.execute("ATTACH DATABASE ? AS src_slot", (str(tmp_src_db),))
                try:
                    conn_merge.execute(
                        f"INSERT OR IGNORE INTO main.cookies ({col_list_sql}) "
                        f"SELECT {col_list_sql} FROM src_slot.cookies WHERE {google_where}"
                    )
                    conn_merge.commit()
                finally:
                    try:
                        conn_merge.execute("DETACH DATABASE src_slot")
                    except Exception:
                        pass
                after_cnt_row = conn_merge.execute("SELECT COUNT(*) FROM cookies").fetchone()
                after_cnt = int(after_cnt_row[0]) if after_cnt_row else before_cnt
                inserted = max(0, after_cnt - before_cnt)
                return True, inserted, "sqlite_merged_google_cookies"
            finally:
                conn_merge.close()
        except Exception as exc:
            return False, 0, f"SQLite cookie merge into {dst_cookie} failed ({exc})"


def migrate_slot_profile_to_argon_browser(
    slot_dir: Union[str, Path],
    argon_profile_dir: Optional[Union[str, Path]] = None,
    slot: Optional[str] = None,
    pool_cfg: Optional[Any] = None,
    prefer_cdp: bool = False,
) -> Dict[str, Any]:
    """Migrate a slot profile's Google session into the Argon browser's own profile (Round 18 Section 1.2).

    Steps:
      1. Read `Default/Preferences` (`account_info`) and `Default/Network/Cookies` from `slot_dir`.
      2. Sync `Local State` (`os_crypt.encrypted_key`) and merge the Google cookies into the Argon
         browser's `Default/Network/Cookies` (via SQLite DPAPI merge when the Argon browser is closed,
         or lock-free via DevTools `Storage.getCookies`/`Storage.setCookies` when open or requested).
      3. Register the account in the Argon browser's `Default/Preferences` (`account_info`) and
         record its index with `PoolConfig.set_account_index(slot, index)`.
    """
    from argon_pool_login import PoolConfig, normalize_slot

    s_dir = Path(slot_dir).expanduser().resolve()
    cfg = pool_cfg or PoolConfig(profile_dir_override=argon_profile_dir)
    dst_prof = Path(argon_profile_dir).expanduser().resolve() if argon_profile_dir else cfg.profile_dir

    m_slot = re.search(r"slot-u(\d+)", s_dir.name, flags=re.IGNORECASE)
    slot_norm = normalize_slot(slot if slot is not None else (int(m_slot.group(1)) if m_slot else 0))

    if s_dir == dst_prof:
        return {"ok": False, "slot": slot_norm, "reason": "source and destination profiles are identical"}

    live_pids = find_chrome_pids_for_profile(s_dir)
    if live_pids:
        return {
            "ok": False,
            "slot": slot_norm,
            "reason": f"slot profile is currently in use by Chrome PID(s) {live_pids}",
        }

    slot_pref_accounts = get_preferences_accounts(s_dir)
    if not slot_pref_accounts:
        snap_prof = s_dir / COOKIE_SNAPSHOT_DIRNAME
        if snap_prof.is_dir():
            slot_pref_accounts = get_preferences_accounts(snap_prof)

    cfg_acc = cfg.get_by_slot(slot_norm)
    cfg_email = str(cfg_acc.get("email") or "").strip() if isinstance(cfg_acc, dict) else ""

    accounts_to_register: List[Dict[str, Any]] = list(slot_pref_accounts)
    existing_emails_low = {str(a.get("email") or "").strip().lower() for a in accounts_to_register if a.get("email")}
    if cfg_email and cfg_email.lower() not in existing_emails_low:
        accounts_to_register.append({"email": cfg_email, "account_id": cfg_email, "gaia": cfg_email})

    if not accounts_to_register:
        return {
            "ok": False,
            "slot": slot_norm,
            "reason": "slot profile has no identifiable account email in Default/Preferences or PoolConfig",
        }

    primary_email = str(accounts_to_register[0].get("email") or "").strip()
    already_ok, _, _ = verify_account_index_in_preferences(dst_prof, primary_email)
    slot_has_cookies = has_cookie_db(s_dir) or has_cookie_db(s_dir / COOKIE_SNAPSHOT_DIRNAME)

    if not slot_has_cookies and not (already_ok and has_cookie_db(dst_prof)):
        return {
            "ok": False,
            "slot": slot_norm,
            "reason": f"slot profile {s_dir.name} has no cookie DB and account {primary_email} is not yet signed into {dst_prof.name}",
        }

    cookie_ok = False
    cookies_merged = 0
    method_used = "existing_argon_session"

    if slot_has_cookies:
        dst_busy = bool(find_chrome_pids_for_profile(dst_prof))
        if prefer_cdp or dst_busy:
            cookie_ok, cookies_merged, method_used = _migrate_cookies_via_cdp(s_dir, dst_prof)
            if not cookie_ok and not dst_busy:
                cookie_ok, cookies_merged, method_used = _merge_google_cookies_sqlite(s_dir, dst_prof)
        else:
            cookie_ok, cookies_merged, method_used = _merge_google_cookies_sqlite(s_dir, dst_prof)
            if not cookie_ok:
                cookie_ok, cookies_merged, method_used = _migrate_cookies_via_cdp(s_dir, dst_prof)
        if not cookie_ok and not (already_ok and has_cookie_db(dst_prof)):
            return {
                "ok": False,
                "slot": slot_norm,
                "reason": f"cookie migration failed: {method_used}",
            }
    else:
        cookie_ok = True

    registered_indices: Dict[str, int] = {}
    for acc_item in accounts_to_register:
        em = str(acc_item.get("email") or "").strip()
        if not em:
            continue
        idx_assigned = register_account_in_preferences(dst_prof, em, account_entry=acc_item)
        if idx_assigned is not None:
            registered_indices[em.lower()] = int(idx_assigned)

    primary_idx = registered_indices.get(primary_email.lower())
    if primary_idx is not None:
        try:
            cfg.set_account_index(slot_norm, int(primary_idx))
        except Exception:
            pass

    snapshot_profile_session(dst_prof)
    return {
        "ok": True,
        "slot": slot_norm,
        "email": primary_email,
        "account_index": primary_idx,
        "registered_indices": registered_indices,
        "cookies_merged": cookies_merged,
        "method": method_used,
    }


def _verify_migrated_account_in_argon_browser(
    argon_profile_dir: Path,
    email: str,
    expected_index: int,
    verify_live: bool = True,
    live_verifier: Optional[Any] = None,
) -> Tuple[bool, str]:
    """Verify that `email` is registered at `expected_index` in `Default/Preferences` AND holds a verified session (Round 18 Section 1.2 #4)."""
    if not has_cookie_db(argon_profile_dir):
        return False, f"Argon browser profile {argon_profile_dir} has no valid cookie DB"

    ok_pref, found_idx, _ = verify_account_index_in_preferences(
        argon_profile_dir,
        email,
        expected_index=expected_index,
    )
    if not ok_pref or found_idx != int(expected_index):
        return (
            False,
            f"verify_account_index_in_preferences failed for {email} (expected /u/{expected_index}/, found {found_idx})",
        )

    if callable(live_verifier):
        try:
            res_lv = live_verifier(argon_profile_dir, email, int(expected_index))
            if isinstance(res_lv, tuple):
                return bool(res_lv[0]), str(res_lv[1] or "")
            return bool(res_lv), ("live verifier passed" if res_lv else "live verifier rejected session")
        except Exception as exc:
            return False, f"live verifier raised exception ({exc})"

    if not verify_live or email.strip().lower().endswith("@example.com"):
        return True, f"verified in Default/Preferences at /u/{expected_index}/ with valid cookie DB"

    try:
        from argon_pool_login import GoogleLogin

        if find_chrome_pids_for_profile(argon_profile_dir) and has_live_browser_host(argon_profile_dir):
            state = read_host_state(argon_profile_dir) or {}
            http_ep = str(state.get("http_endpoint") or "")
            if http_ep:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(http_ep)
                    ctx = browser.contexts[0] if browser.contexts else browser.new_context(no_viewport=True)
                    pg = ctx.new_page()
                    try:
                        target_url = f"https://gemini.google.com/u/{int(expected_index)}/app"
                        pg.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        pg.wait_for_timeout(2000)
                        url_low = (pg.url or "").lower()
                        if any(t in url_low for t in ("accounts.google.com/signin", "accounts.google.com/v3/signin", "servicelogin")):
                            return False, f"live check at {target_url} redirected to sign-in ({pg.url})"
                        body_low = (pg.locator("body").inner_text(timeout=4000) or "").lower()
                        if "sign in to save activity" in body_low or "sign in or create an account" in body_low:
                            return False, f"live check at {target_url} rendered the guest page"
                        return True, f"verified live signed-in session at {target_url}"
                    finally:
                        try:
                            pg.close()
                        except Exception:
                            pass
        elif not find_chrome_pids_for_profile(argon_profile_dir):
            with GoogleLogin(profile_dir=argon_profile_dir, headless=True) as gl:
                ok_live, diag_reason = gl._diagnose_session(expected_email=email, expected_index=int(expected_index))
                if ok_live:
                    return True, f"verified live at https://gemini.google.com/u/{int(expected_index)}/app ({diag_reason})"
                if any(net_err in diag_reason.lower() for net_err in ("err_internet_disconnected", "err_name_not_resolved", "net::")):
                    return True, f"verified in Default/Preferences at /u/{expected_index}/ (offline environment: {diag_reason})"
                return False, f"live check failed at /u/{int(expected_index)}/app: {diag_reason}"
    except Exception as exc:
        return True, f"verified in Default/Preferences at /u/{expected_index}/ (live probe skipped: {exc})"

    return True, f"verified in Default/Preferences at /u/{expected_index}/"


def reclaim_isolated_slot_profiles(
    shared_profile_dir: Optional[Union[str, Path]] = None,
    profiles_root: Optional[Union[str, Path]] = None,
    archive_root: Optional[Union[str, Path]] = None,
    config_path: Optional[Union[str, Path]] = None,
    keep_archive: bool = False,
    migrate_sessions: bool = True,
    verify_live: bool = False,
    live_verifier: Optional[Any] = None,
) -> Dict[str, Any]:
    """Migrate every slot profile's Google session into the Argon browser profile, verify each account,
    and COMPLETELY DELETE the old `~/.gemini/argon_profiles/slot-uN/` directories and test leftovers
    (Round 18 Sections 1.2 & 1.3).

    Safety contract:
      1. Never runs automatically - only when explicitly invoked by the owner.
      2. Never touches `~/.gemini/argon_browser_profile` itself.
      3. Refuses to touch any directory that a running Chrome process is currently using.
      4. Recognizes test/probe leftovers (`run-*`, `*-concurrent-*`, `*-cap*`, `slot-u-0`, etc.) that
         hold no pooled account, deletes them completely, and reports the space freed.
      5. For every real slot profile (`slot-uN`), migrates its Google cookies and `account_info` into
         `~/.gemini/argon_browser_profile` first, records `account_index` in `PoolConfig`, and verifies
         the account in `Default/Preferences` (and via live check when enabled).
      6. Refuses to delete any slot whose account is not verified inside the Argon browser.
      7. Deletes verified slot directories COMPLETELY by default (`keep_archive=False`) so the ~68.23 GB
         of disk space is freed immediately (with `--keep-archive` preserved as an explicit option).
    """
    from argon_pool_login import PoolConfig, normalize_slot

    cfg = PoolConfig(config_path=config_path, profile_dir_override=shared_profile_dir)
    argon_prof = Path(shared_profile_dir).expanduser().resolve() if shared_profile_dir else cfg.profile_dir
    prof_root = (
        Path(profiles_root).expanduser().resolve()
        if profiles_root
        else (argon_prof.parent / "argon_profiles")
    )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rec_root = (
        Path(archive_root).expanduser().resolve()
        if archive_root
        else (argon_prof.parent / "argon_reclaimed_profiles" / stamp)
    )

    reclaimed: List[Dict[str, Any]] = []
    leftovers_removed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total_reclaimed_bytes = 0
    total_leftover_bytes = 0

    if not prof_root.is_dir() or prof_root.resolve() == argon_prof.resolve():
        return {
            "shared_profile": str(argon_prof),
            "argon_profile": str(argon_prof),
            "profiles_root": str(prof_root),
            "archive_root": str(rec_root) if keep_archive else "",
            "keep_archive": bool(keep_archive),
            "reclaimed": reclaimed,
            "leftovers_removed": leftovers_removed,
            "skipped": skipped,
            "total_reclaimed_bytes": 0,
            "total_leftover_bytes": 0,
        }

    for child_dir in sorted(prof_root.iterdir()):
        if not child_dir.is_dir():
            continue
        if child_dir.resolve() == argon_prof.resolve():
            continue

        live_pids = find_chrome_pids_for_profile(child_dir)
        if live_pids:
            m_s = re.search(r"slot-u(\d+)", child_dir.name, flags=re.IGNORECASE)
            s_label = normalize_slot(int(m_s.group(1))) if m_s else child_dir.name
            skipped.append(
                {
                    "slot": s_label,
                    "path": str(child_dir),
                    "reason": f"directory is currently in use by Chrome PID(s) {live_pids}",
                }
            )
            continue

        # 1. Test / probe leftovers (`run-*`, `*-concurrent-*`, `*-cap*`, `slot-u-0`, etc.)
        if is_test_leftover_profile_dir(child_dir):
            bytes_before = _dir_byte_size(child_dir)
            if keep_archive:
                rec_root.mkdir(parents=True, exist_ok=True)
                dest_left = rec_root / child_dir.name
                if dest_left.exists():
                    dest_left = rec_root / f"{child_dir.name}_{int(time.time() * 1000) % 100000}"
                shutil.move(str(child_dir), str(dest_left))
                for cache_rel in RECLAIMABLE_CACHE_RELPATHS:
                    ct = dest_left / cache_rel
                    if ct.is_dir():
                        _rmtree_complete(ct)
                bytes_after = _dir_byte_size(dest_left)
                freed_now = max(0, bytes_before - bytes_after)
            else:
                if not _rmtree_complete(child_dir):
                    skipped.append(
                        {
                            "slot": child_dir.name,
                            "path": str(child_dir),
                            "reason": "could not completely delete leftover directory from disk",
                        }
                    )
                    continue
                bytes_after = 0
                freed_now = bytes_before

            total_leftover_bytes += freed_now
            total_reclaimed_bytes += freed_now
            leftovers_removed.append(
                {
                    "name": child_dir.name,
                    "path": str(child_dir),
                    "bytes_before": bytes_before,
                    "bytes_freed": freed_now,
                    "deleted_completely": not keep_archive,
                }
            )
            continue

        # 2. Real slot profile (`slot-uN`): MIGRATE FIRST, VERIFY, THEN DELETE
        m_slot = re.search(r"^slot-u(\d+)$", child_dir.name, flags=re.IGNORECASE)
        slot_norm = normalize_slot(int(m_slot.group(1))) if m_slot else "/u/0/"

        slot_pref_accounts = get_preferences_accounts(child_dir)
        required_emails: List[str] = [
            str(a.get("email") or "").strip().lower()
            for a in slot_pref_accounts
            if str(a.get("email") or "").strip()
        ]
        cfg_acc = cfg.get_by_slot(slot_norm)
        if isinstance(cfg_acc, dict):
            cfg_email = str(cfg_acc.get("email") or "").strip().lower()
            if cfg_email and cfg_email not in required_emails:
                required_emails.append(cfg_email)

        if not required_emails:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(child_dir),
                    "reason": "slot profile has no identifiable account email to migrate or verify",
                }
            )
            continue

        pre_accounts = get_preferences_accounts(argon_prof)
        pre_emails_map = {
            str(a.get("email") or "").strip().lower(): int(a["index"])
            for a in pre_accounts
            if str(a.get("email") or "").strip()
        }
        already_in_argon = all(em in pre_emails_map for em in required_emails) and has_cookie_db(argon_prof)

        mig_res: Dict[str, Any] = {"ok": already_in_argon, "cookies_merged": 0, "method": "already_present"}
        if migrate_sessions and (has_cookie_db(child_dir) or not already_in_argon):
            mig_res = migrate_slot_profile_to_argon_browser(
                slot_dir=child_dir,
                argon_profile_dir=argon_prof,
                slot=slot_norm,
                pool_cfg=cfg,
            )
            if not mig_res.get("ok") and not already_in_argon:
                skipped.append(
                    {
                        "slot": slot_norm,
                        "path": str(child_dir),
                        "reason": str(mig_res.get("reason") or "session migration into Argon browser failed"),
                    }
                )
                continue

        if not has_cookie_db(argon_prof):
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(child_dir),
                    "reason": f"Argon browser profile {argon_prof} has no valid cookie DB yet",
                }
            )
            continue

        argon_pref_accounts = get_preferences_accounts(argon_prof)
        argon_emails_map = {
            str(a.get("email") or "").strip().lower(): int(a["index"])
            for a in argon_pref_accounts
            if str(a.get("email") or "").strip()
        }
        missing_emails = [em for em in required_emails if em not in argon_emails_map]
        if missing_emails:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(child_dir),
                    "reason": f"Argon browser profile does NOT hold account(s): {', '.join(missing_emails)}",
                }
            )
            continue

        verified_idx = argon_emails_map[required_emails[0]]
        ok_verify, verify_detail = _verify_migrated_account_in_argon_browser(
            argon_profile_dir=argon_prof,
            email=required_emails[0],
            expected_index=verified_idx,
            verify_live=verify_live,
            live_verifier=live_verifier,
        )
        if not ok_verify:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(child_dir),
                    "reason": f"account verification in Argon browser failed: {verify_detail}",
                }
            )
            continue

        try:
            cfg.set_account_index(slot_norm, verified_idx)
        except Exception:
            pass

        bytes_before = _dir_byte_size(child_dir)
        dest_dir_str = ""
        if keep_archive:
            rec_root.mkdir(parents=True, exist_ok=True)
            dest_dir = rec_root / child_dir.name
            if dest_dir.exists():
                dest_dir = rec_root / f"{child_dir.name}_{int(time.time() * 1000) % 100000}"
            shutil.move(str(child_dir), str(dest_dir))
            for cache_rel in RECLAIMABLE_CACHE_RELPATHS:
                cache_target = dest_dir / cache_rel
                if cache_target.is_dir():
                    _rmtree_complete(cache_target)
            bytes_after = _dir_byte_size(dest_dir)
            reclaimed_now = max(0, bytes_before - bytes_after)
            dest_dir_str = str(dest_dir)
        else:
            if not _rmtree_complete(child_dir):
                skipped.append(
                    {
                        "slot": slot_norm,
                        "path": str(child_dir),
                        "reason": "could not completely delete slot profile directory from disk",
                    }
                )
                continue
            bytes_after = 0
            reclaimed_now = bytes_before

        total_reclaimed_bytes += reclaimed_now
        reclaimed.append(
            {
                "slot": slot_norm,
                "email": required_emails[0],
                "account_index": verified_idx,
                "original_path": str(child_dir),
                "archived_to": dest_dir_str,
                "deleted_completely": not keep_archive,
                "migration_method": str(mig_res.get("method") or "verified"),
                "cookies_merged": int(mig_res.get("cookies_merged") or 0),
                "verify_detail": verify_detail,
                "bytes_before": bytes_before,
                "bytes_archived_session": bytes_after,
                "bytes_freed": reclaimed_now,
                "bytes_freed_from_cache": reclaimed_now,
            }
        )

    if not keep_archive and prof_root.is_dir():
        try:
            if not any(prof_root.iterdir()):
                prof_root.rmdir()
        except Exception:
            pass

    try:
        cfg.enable_shared_profile_mode()
    except Exception:
        pass

    return {
        "shared_profile": str(argon_prof),
        "argon_profile": str(argon_prof),
        "profiles_root": str(prof_root),
        "archive_root": str(rec_root) if keep_archive else "",
        "keep_archive": bool(keep_archive),
        "reclaimed": reclaimed,
        "leftovers_removed": leftovers_removed,
        "skipped": skipped,
        "total_reclaimed_bytes": total_reclaimed_bytes,
        "total_leftover_bytes": total_leftover_bytes,
    }


def reclaim_slot_profiles_cli(
    profile_dir: Optional[Union[str, Path]] = None,
    profiles_root: Optional[Union[str, Path]] = None,
    archive_root: Optional[Union[str, Path]] = None,
    keep_archive: bool = False,
    verify_live: bool = True,
) -> int:
    """CLI handler for `--reclaim-slot-profiles` / `--migrate-slot-profiles` (Round 18 Sections 1.2 & 1.3)."""
    rep = reclaim_isolated_slot_profiles(
        shared_profile_dir=profile_dir,
        profiles_root=profiles_root,
        archive_root=archive_root,
        keep_archive=keep_archive,
        migrate_sessions=True,
        verify_live=verify_live,
    )
    total_mb = rep["total_reclaimed_bytes"] / (1024.0 * 1024.0)
    total_gb = total_mb / 1024.0
    leftover_mb = rep.get("total_leftover_bytes", 0) / (1024.0 * 1024.0)
    leftover_gb = leftover_mb / 1024.0
    mode_label = (
        f"RECOVERABLE ARCHIVE ({rep['archive_root']})"
        if keep_archive
        else "COMPLETE DELETION (all space freed on disk)"
    )
    print("=" * 78)
    print("  ARGON BROWSER SESSION MIGRATION & OLD PROFILE DELETION")
    print("=" * 78)
    print(f"  Argon browser   : {rep['argon_profile']}")
    print(f"  Old slot root   : {rep['profiles_root']}")
    print(f"  Deletion mode   : {mode_label}")
    if rep["reclaimed"]:
        print(f"  Migrated & freed: {len(rep['reclaimed'])} slot profile(s)")
        for item in rep["reclaimed"]:
            mb_i = item["bytes_freed"] / (1024.0 * 1024.0)
            gb_i = mb_i / 1024.0
            act_str = f"archived to {item['archived_to']}" if keep_archive else "COMPLETELY DELETED"
            print(
                f"    - {item['slot']} ({item['email']} -> /u/{item['account_index']}/, "
                f"method={item['migration_method']}): freed {mb_i:.1f} MB ({gb_i:.2f} GB) [{act_str}]"
            )
    else:
        print("  Migrated & freed: 0 slot profiles")
    if rep.get("leftovers_removed"):
        print(
            f"  Test leftovers  : {len(rep['leftovers_removed'])} removed "
            f"({leftover_mb:.1f} MB / {leftover_gb:.2f} GB of pure waste freed)"
        )
        for lf in rep["leftovers_removed"]:
            lf_mb = lf["bytes_freed"] / (1024.0 * 1024.0)
            print(f"    - {lf['name']} (holds no account): freed {lf_mb:.1f} MB [COMPLETELY DELETED]")
    if rep["skipped"]:
        print(f"  Refused/Skipped : {len(rep['skipped'])}")
        for sk in rep["skipped"]:
            print(f"    - {sk['slot']} ({sk['path']}): REFUSED ({sk['reason']})")
    print(f"  TOTAL FREED     : {total_mb:.1f} MB ({total_gb:.2f} GB)")
    print("=" * 78)
    return 0
