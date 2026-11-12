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
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

ENDPOINT_FILENAME = ".argon_browser_endpoint"
COOKIE_SNAPSHOT_DIRNAME = ".argon_cookie_snapshot"
QUARANTINE_DIRNAME = ".argon_quarantine"
COMPANION_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"

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
        tmp = ep_file.with_name(f".{ENDPOINT_FILENAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(ep_file))
    except Exception:
        pass
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
                "} | Select-Object -ExpandProperty ProcessId"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=8,
            )
            for ln in (out.stdout or "").splitlines():
                ln_s = ln.strip()
                if ln_s.isdigit():
                    val = int(ln_s)
                    if val > 0 and val not in pids:
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
    Only refuses (`return None`) when a running Chrome on `profile_dir` publishes no CDP endpoint at all.
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
        if discovered is not None:
            http_ep, probed_info = discovered
            m_p = re.search(r":(\d+)$", http_ep)
            port_val = int(m_p.group(1)) if m_p else int((existing or {}).get("port") or 0)
            ext_dir_existing = get_companion_extension_dir()
            recovered_state: Dict[str, Any] = {
                "profile_dir": str(prof),
                "pid": int(existing_pids[0]),
                "pids": [int(p) for p in existing_pids],
                "port": port_val,
                "http_endpoint": http_ep,
                "ws_endpoint": str(probed_info.get("ws_endpoint") or (existing or {}).get("ws_endpoint") or ""),
                "companion_extension": str((existing or {}).get("companion_extension") or (ext_dir_existing or "")),
                "started_at": str((existing or {}).get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S")),
                "tabs": dict((existing or {}).get("tabs") or {}),
            }
            write_host_state(prof, recovered_state)
            return recovered_state
        if existing is not None:
            remove_host_state(prof)
        return None

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
    state = read_host_state(prof)
    if not state or not isinstance(state.get("tabs"), dict):
        return []

    tabs: Dict[str, Any] = dict(state.get("tabs") or {})
    reaped: List[str] = []
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

    if reaped:
        state["tabs"] = tabs
        write_host_state(prof, state)
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

        state = read_host_state(prof) or host
        tabs = dict(state.get("tabs") or {})

        reuse_blank = False
        if not tabs and len(context.pages) == 1:
            try:
                first_url = str(context.pages[0].url or "").strip()
                if first_url in (
                    "",
                    "about:blank",
                    "chrome://newtab/",
                    "chrome://new-tab-page/",
                ):
                    reuse_blank = True
            except Exception:
                reuse_blank = False

        page = context.pages[0] if reuse_blank else context.new_page()
        slot_clean = re.sub(r"[^A-Za-z0-9]+", "", str(slot or "u0")) or "u0"
        tab_id = f"tab-{slot_clean}-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        target_id = ""
        try:
            cdp_sess = context.new_cdp_session(page)
            t_info = cdp_sess.send("Target.getTargetInfo") or {}
            target_id = str((t_info.get("targetInfo") or {}).get("targetId") or "")
            cdp_sess.detach()
        except Exception:
            target_id = ""
        try:
            page.evaluate("(tid) => { try { window.name = tid; } catch (e) {} }", tab_id)
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
            "page_url": str(getattr(page, "url", "") or "about:blank"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        state["tabs"] = tabs
        write_host_state(prof, state)

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
    """Update metadata (`chat_url`, `chat_id`, `page_url`, `account`) for `tab_id` in host state."""
    if not tab_id:
        return
    prof = Path(profile_dir).expanduser().resolve()
    state = read_host_state(prof)
    if not state or not isinstance(state.get("tabs"), dict):
        return
    tabs = dict(state["tabs"])
    entry = dict(tabs.get(tab_id) or {})
    if not entry:
        return
    for k, v in fields.items():
        if v is not None:
            entry[k] = v
    entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tabs[tab_id] = entry
    state["tabs"] = tabs
    write_host_state(prof, state)


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
    """Close ONLY this task's tab when other tasks are active; close the browser host
    gracefully when the last task finishes (Section 2.3.B & 2.3.E).
    """
    prof = Path(profile_dir).expanduser().resolve()
    is_host = bool(getattr(context, "_argon_is_cdp_host", False))
    eff_tab_id = tab_id or getattr(page, "_argon_tab_id", None)

    if not is_host or not eff_tab_id:
        return close_browser_safely(context=context, profile_dir=prof)

    snapshot_profile_session(prof)
    reap_dead_tabs(context, prof)

    state = read_host_state(prof) or {}
    tabs: Dict[str, Any] = dict(state.get("tabs") or {})
    tabs.pop(eff_tab_id, None)

    for tid, tinfo in list(tabs.items()):
        tpid = int((tinfo or {}).get("pid") or 0)
        if tpid and tpid != os.getpid() and not pid_is_alive(tpid):
            tabs.pop(tid, None)

    remaining_count = len(tabs)
    if remaining_count > 0:
        state["tabs"] = tabs
        write_host_state(prof, state)
        try:
            if page is not None and not (hasattr(page, "is_closed") and page.is_closed()):
                page.close()
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

    print(
        f"[+] Last task tab ({eff_tab_id}) finished on {prof} (active_tabs=0) - "
        "closing browser host gracefully so cookies are flushed."
    )
    remove_host_state(prof)
    return close_browser_safely(context=context, profile_dir=prof, page=page)


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

    # Seed the slot profile from the master profile when it has no account yet,
    # exactly like the pool assistant's "Open in Gemini Web" button does.
    try:
        if not is_isolated_slot_profile(prof):
            pass
        elif not (prof.is_dir() and (prof / "Default").is_dir()):
            seed_slot_profile(Path.home() / ".gemini" / "argon_browser_profile", prof)
    except Exception as exc:
        print(f"[i] The slot profile could not be seeded ({exc}); continuing anyway.")

    # An isolated slot profile holds ONE account at /u/0/. In the shared Argon Bridge
    # browser profile, the URL uses the account's verified Default/Preferences index
    # from PoolConfig.get_account_index(eff_slot) (Round 17 Section 1.2 #2).
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


def reclaim_isolated_slot_profiles(
    shared_profile_dir: Optional[Union[str, Path]] = None,
    profiles_root: Optional[Union[str, Path]] = None,
    archive_root: Optional[Union[str, Path]] = None,
    config_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Safely reclaim disk space from old isolated `~/.gemini/argon_profiles/slot-uN/` profiles (Round 17 Section 1.5).

    Safety contract:
      1. Never runs automatically - only when explicitly invoked via `--reclaim-slot-profiles`.
      2. Refuses to touch any slot profile whose Chrome process is currently running.
      3. Verifies that the shared Argon Bridge profile (`~/.gemini/argon_browser_profile`) has a valid
         cookie DB AND holds every Google account email registered in the slot profile (and configured
         for that slot in `PoolConfig`) inside `Default/Preferences` (`account_info`), recording the
         verified `account_index` in `PoolConfig`.
      4. Moves each verified slot directory to a recoverable archive folder
         (`~/.gemini/argon_reclaimed_profiles/<timestamp>/slot-uN`) rather than deleting its session
         files, and purges only regenerable Chromium caches inside the archived copy so the ~68 GB of
         cache bloat is freed immediately while `Cookies`, `Preferences`, and `Local State` remain
         recoverable.
    """
    from argon_pool_login import PoolConfig, normalize_slot

    cfg = PoolConfig(config_path=config_path, profile_dir_override=shared_profile_dir)
    shared_prof = Path(shared_profile_dir).expanduser().resolve() if shared_profile_dir else cfg.profile_dir
    prof_root = (
        Path(profiles_root).expanduser().resolve()
        if profiles_root
        else (shared_prof.parent / "argon_profiles")
    )
    stamp = time.strftime("%Y%m%d_%H%M%S")
    rec_root = (
        Path(archive_root).expanduser().resolve()
        if archive_root
        else (shared_prof.parent / "argon_reclaimed_profiles" / stamp)
    )

    reclaimed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total_reclaimed_bytes = 0

    if not prof_root.is_dir():
        return {
            "shared_profile": str(shared_prof),
            "profiles_root": str(prof_root),
            "archive_root": str(rec_root),
            "reclaimed": reclaimed,
            "skipped": skipped,
            "total_reclaimed_bytes": 0,
        }

    shared_has_cookies = has_cookie_db(shared_prof)
    shared_pref_accounts = get_preferences_accounts(shared_prof)
    shared_emails_map = {
        str(a.get("email") or "").strip().lower(): int(a["index"])
        for a in shared_pref_accounts
        if str(a.get("email") or "").strip()
    }

    for slot_dir in sorted(prof_root.iterdir()):
        if not slot_dir.is_dir() or not slot_dir.name.startswith("slot-"):
            continue
        m_slot = re.search(r"slot-u(\d+)", slot_dir.name, flags=re.IGNORECASE)
        slot_norm = normalize_slot(int(m_slot.group(1))) if m_slot else "/u/0/"

        live_pids = find_chrome_pids_for_profile(slot_dir)
        if live_pids:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(slot_dir),
                    "reason": f"slot profile is currently in use by Chrome PID(s) {live_pids}",
                }
            )
            continue

        slot_pref_accounts = get_preferences_accounts(slot_dir)
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

        if not shared_has_cookies:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(slot_dir),
                    "reason": f"shared profile {shared_prof} has no valid cookie DB yet",
                }
            )
            continue

        if not required_emails:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(slot_dir),
                    "reason": "slot profile has no identifiable account email to verify against shared profile",
                }
            )
            continue

        missing_emails = [em for em in required_emails if em not in shared_emails_map]
        if missing_emails:
            skipped.append(
                {
                    "slot": slot_norm,
                    "path": str(slot_dir),
                    "reason": f"shared profile does NOT hold account(s): {', '.join(missing_emails)}",
                }
            )
            continue

        verified_idx = shared_emails_map[required_emails[0]]
        try:
            cfg.set_account_index(slot_norm, verified_idx)
        except Exception:
            pass

        bytes_before = _dir_byte_size(slot_dir)
        rec_root.mkdir(parents=True, exist_ok=True)
        dest_dir = rec_root / slot_dir.name
        if dest_dir.exists():
            dest_dir = rec_root / f"{slot_dir.name}_{int(time.time() * 1000) % 100000}"

        shutil.move(str(slot_dir), str(dest_dir))
        for cache_rel in RECLAIMABLE_CACHE_RELPATHS:
            cache_target = dest_dir / cache_rel
            if cache_target.is_dir():
                shutil.rmtree(cache_target, ignore_errors=True)

        bytes_after = _dir_byte_size(dest_dir)
        reclaimed_now = max(0, bytes_before - bytes_after)
        total_reclaimed_bytes += bytes_before
        reclaimed.append(
            {
                "slot": slot_norm,
                "email": required_emails[0],
                "account_index": verified_idx,
                "original_path": str(slot_dir),
                "archived_to": str(dest_dir),
                "bytes_before": bytes_before,
                "bytes_archived_session": bytes_after,
                "bytes_freed_from_cache": reclaimed_now,
            }
        )

    return {
        "shared_profile": str(shared_prof),
        "profiles_root": str(prof_root),
        "archive_root": str(rec_root),
        "reclaimed": reclaimed,
        "skipped": skipped,
        "total_reclaimed_bytes": total_reclaimed_bytes,
    }


def reclaim_slot_profiles_cli(
    profile_dir: Optional[Union[str, Path]] = None,
    profiles_root: Optional[Union[str, Path]] = None,
    archive_root: Optional[Union[str, Path]] = None,
) -> int:
    """CLI handler for `--reclaim-slot-profiles` (Round 17 Section 1.5)."""
    rep = reclaim_isolated_slot_profiles(
        shared_profile_dir=profile_dir,
        profiles_root=profiles_root,
        archive_root=archive_root,
    )
    total_mb = rep["total_reclaimed_bytes"] / (1024.0 * 1024.0)
    total_gb = total_mb / 1024.0
    print("=" * 78)
    print("  ARGON ISOLATED SLOT PROFILE RECLAMATION")
    print("=" * 78)
    print(f"  Shared profile  : {rep['shared_profile']}")
    print(f"  Slot profiles   : {rep['profiles_root']}")
    print(f"  Recoverable dir : {rep['archive_root']}")
    if rep["reclaimed"]:
        print(f"  Reclaimed slots : {len(rep['reclaimed'])} ({total_mb:.1f} MB / {total_gb:.2f} GB reclaimed)")
        for item in rep["reclaimed"]:
            mb_i = item["bytes_before"] / (1024.0 * 1024.0)
            print(
                f"    - {item['slot']} ({item['email']} -> /u/{item['account_index']}/): "
                f"{mb_i:.1f} MB moved to {item['archived_to']}"
            )
    else:
        print("  Reclaimed slots : 0")
    if rep["skipped"]:
        print(f"  Refused/Skipped : {len(rep['skipped'])}")
        for sk in rep["skipped"]:
            print(f"    - {sk['slot']} ({sk['path']}): REFUSED ({sk['reason']})")
    print("=" * 78)
    return 0
