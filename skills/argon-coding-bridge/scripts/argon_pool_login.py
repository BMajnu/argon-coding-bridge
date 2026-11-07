#!/usr/bin/env python3
"""
Automatic Google Account-Pool Sign-In Module for Gemini 4 Argon Coding Bridge.

Provides:
- PoolConfig: loads and saves skills/argon-coding-bridge/config/accounts_pool.json
  (UTF-8, 2-space indent, no BOM), expands leading '~' in profile_dir, and exposes
  accounts, next_index(), add_account(email, password, tier="PRO", role="Pool Member"),
  set_status(slot, status), and active_slot().
- notify(title, message): best-effort Windows tray balloon (System.Windows.Forms.NotifyIcon
  through PowerShell) and winsound.MessageBeep(). Never raises and never blocks.
- GoogleLogin: Playwright persistent context on the pool profile directory, headed,
  using the installed Google Chrome binary when it exists (falling back to bundled Chromium),
  launched with --disable-blink-features=AutomationControlled, --no-default-browser-check,
  --no-first-run.
- login(email, password, wait_minutes=6) & login_pool(accounts, ...)
- is_signed_in() helper and the open_gemini() helper.
- CLI: --all, --slot /u/N/, --add EMAIL PASSWORD [--tier T] [--role R],
  --wait-minutes N, --profile-dir PATH.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

logger = logging.getLogger("argon_pool_login")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "accounts_pool.json"

GEMINI_CONTINUE_URL = "https://gemini.google.com/u/0/app"
GOOGLE_ADD_SESSION_URL = (
    "https://accounts.google.com/v3/signin/identifier"
    "?flowName=GlifWebSignIn&flowEntry=AddSession"
    "&continue=https%3A%2F%2Fgemini.google.com%2Fu%2F0%2Fapp"
)
GEMINI_WEB_URL = "https://gemini.google.com{slot}app"


def is_isolated_slot_profile(profile_dir: Optional[Union[str, Path]]) -> bool:
    """Return True when profile_dir is a per-slot profile (holds one account at index 0)."""
    if profile_dir is None:
        return True
    try:
        resolved = Path(profile_dir).expanduser().resolve()
        return resolved.parent.name == "argon_profiles" or resolved.name.startswith("slot-")
    except Exception:
        return False


def build_signin_url(isolated_profile: bool = True) -> str:
    """Return the Google sign-in URL for a slot profile.

    Never derives `authuser` from the slot number: in a per-slot profile the
    account index is always 0, the `continue` target is
    `https://gemini.google.com/u/0/app`, and no `authuser` parameter is sent.
    """
    return GOOGLE_ADD_SESSION_URL


def sanitize_signin_url(url: str, isolated_profile: bool = True) -> str:
    """Strip `authuser` and normalize any `/u/<N>/` continue path to `/u/0/`."""
    if not url:
        return GOOGLE_ADD_SESSION_URL
    cleaned = re.sub(r"([?&])authuser=[^&#]*&?", r"\1", str(url), flags=re.IGNORECASE)
    cleaned = re.sub(r"[?&]$", "", cleaned)
    cleaned = cleaned.replace("?&", "?")
    if isolated_profile:
        cleaned = re.sub(r"/u/\d+/", "/u/0/", cleaned)
        cleaned = re.sub(r"%2Fu%2F\d+%2F", "%2Fu%2F0%2F", cleaned, flags=re.IGNORECASE)
    return cleaned


def reset_slot_profile_accounts(profile_dir: Union[str, Path], clear_cookies: bool = False) -> None:
    """Reset the stored account list (and optionally cookie DB) of a slot profile on disk.

    Must run BEFORE Chrome starts on `profile_dir` so Chrome does not load the
    seeded account from `Default/Preferences` into memory and overwrite disk on exit.
    """
    prof = Path(profile_dir).expanduser().resolve()
    pref = prof / "Default" / "Preferences"
    if pref.is_file():
        try:
            data = json.loads(pref.read_text(encoding="utf-8", errors="replace"))
            changed = False
            if data.get("account_info"):
                data["account_info"] = []
                changed = True
            signin = data.get("signin")
            if isinstance(signin, dict) and signin.get("accounts"):
                signin["accounts"] = []
                changed = True
            google_cfg = data.get("google")
            if isinstance(google_cfg, dict) and isinstance(google_cfg.get("services"), dict):
                svc = google_cfg["services"]
                for k in ("account_id", "last_username", "username", "consented_to_sync"):
                    if k in svc:
                        svc.pop(k, None)
                        changed = True
            if changed:
                tmp = pref.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
                tmp.replace(pref)
        except Exception:
            pass
    if clear_cookies:
        for rel in ("Default/Network/Cookies", "Default/Cookies"):
            base_cookie = prof / rel
            for suffix in ("", "-journal", "-wal", "-shm"):
                cand = Path(str(base_cookie) + suffix)
                try:
                    if cand.is_file():
                        cand.unlink()
                except OSError:
                    pass


CHROME_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-default-browser-check",
    "--no-first-run",
]

STATUS_OK = "OK"
STATUS_NEEDS_HUMAN = "NEEDS_HUMAN"
STATUS_FAILED = "FAILED"


@dataclasses.dataclass
class LoginResult:
    """Result returned by GoogleLogin.login() and GoogleLogin.login_pool()."""

    status: str  # "OK" | "NEEDS_HUMAN" | "FAILED"
    detail: str
    email: str = ""
    slot: str = ""

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> Dict[str, str]:
        return {
            "status": self.status,
            "detail": self.detail,
            "email": self.email,
            "slot": self.slot,
        }


def normalize_slot(slot: Union[str, int]) -> str:
    """Normalize a slot identifier such as 0, '0', '/u/0', or '/u/0/' to '/u/0/'."""
    raw = str(slot).strip()
    if raw.isdigit():
        return f"/u/{int(raw)}/"
    m = re.search(r"/u/(\d+)/?", raw)
    if m:
        return f"/u/{int(m.group(1))}/"
    if not raw.startswith("/"):
        raw = "/" + raw
    if not raw.endswith("/"):
        raw = raw + "/"
    return raw


def slot_number(slot: str) -> Optional[int]:
    """Extract the integer N from a '/u/N/' slot string."""
    m = re.search(r"/u/(\d+)/?", str(slot))
    return int(m.group(1)) if m else None


def slot_key(slot: Optional[Union[str, int]]) -> str:
    """Return the filesystem-safe key for a pool slot (e.g. '/u/3/' -> 'u3')."""
    if slot is None:
        return "u0"
    raw = str(slot).strip()
    if not raw:
        return "u0"
    num = slot_number(raw)
    if num is not None:
        return f"u{num}"
    if raw.isdigit():
        return f"u{int(raw)}"
    cleaned = raw.strip("/").replace("/", "-")
    return cleaned or "u0"


def profile_dir_for_slot(base_profile: Union[str, Path], slot: Optional[Union[str, int]]) -> Path:
    """Return the dedicated browser profile directory for a pool slot (~/.gemini/argon_profiles/slot-uN)."""
    base = Path(base_profile).expanduser().resolve()
    if base.parent.name == "argon_profiles" and base.name.startswith("slot-"):
        root = base.parent
    else:
        root = base.parent / "argon_profiles"
    return root / f"slot-{slot_key(slot)}"


def _pid_is_alive(pid: int) -> bool:
    """Return True when a process with the given PID is still running."""
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


def is_profile_busy(profile_dir: Union[str, Path]) -> bool:
    """Return True when another Chrome instance or active slot lock holds profile_dir."""
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
                        and _pid_is_alive(pid)
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
                if m and not _pid_is_alive(int(m.group(1))):
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


def copy_sqlite_databases(src_root: Path, dst_root: Path) -> bool:
    """Copy Chrome SQLite databases using SQLite's online backup API."""
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
            source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
            try:
                target = sqlite3.connect(str(dst))
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
            if is_cookie_db and (not dst.is_file() or dst.stat().st_size == 0):
                return False
        except Exception as exc:
            if is_cookie_db:
                logger.warning("Could not snapshot SQLite cookie database %s: %s", src, exc)
                return False
            continue
    return True


def seed_slot_profile(base_profile: Union[str, Path], target_profile: Union[str, Path]) -> bool:
    """Seed a per-slot profile directory from the master profile when the master is free."""
    base = Path(base_profile).expanduser().resolve()
    target = Path(target_profile).expanduser().resolve()
    if target.is_dir() and (target / "Default").is_dir():
        if not _has_cookie_db(base) or _has_cookie_db(target):
            return True
    if not base.is_dir() or not (base / "Default").is_dir():
        logger.info("Master profile %s has no Default profile to seed %s from yet.", base, target)
        return False
    if is_profile_busy(base):
        logger.warning(
            "Cannot seed slot profile %s: master profile %s is currently in use by another run.",
            target,
            base,
        )
        return False

    def _safe_copy(src: str, dst: str) -> str:
        try:
            return shutil.copy2(src, dst)
        except Exception:
            return dst

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(
            base,
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
            return False
        if not copy_sqlite_databases(base, target):
            shutil.rmtree(target, ignore_errors=True)
            return False
        return True
    except Exception as exc:
        logger.warning("Could not seed slot profile %s from %s: %s", target, base, exc)
        return False


class PoolConfig:
    """Loads and saves skills/argon-coding-bridge/config/accounts_pool.json (UTF-8, 2-space indent, no BOM)."""

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        profile_dir_override: Optional[Union[str, Path]] = None,
    ) -> None:
        self.path: Path = Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH
        self._profile_dir_override: Optional[Path] = (
            Path(profile_dir_override).expanduser().resolve() if profile_dir_override else None
        )
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        if self.path.exists():
            raw_text = self.path.read_text(encoding="utf-8-sig")
            self.data = json.loads(raw_text)
        else:
            self.data = {
                "version": "1.3.0",
                "description": (
                    "Multi-Account Pool configuration for the Gemini 4 Argon Dispatcher. "
                    "One persistent Chrome profile holds every Google account, so the dispatcher "
                    "can rotate between them for quota."
                ),
                "profile_dir": "~/.gemini/argon_browser_profile",
                "active_slot": "/u/0/",
                "accounts": [],
            }
        if "accounts" not in self.data or not isinstance(self.data["accounts"], list):
            self.data["accounts"] = []
        if "profile_dir" not in self.data or not self.data["profile_dir"]:
            self.data["profile_dir"] = "~/.gemini/argon_browser_profile"
        if "active_slot" not in self.data or not self.data["active_slot"]:
            self.data["active_slot"] = "/u/0/"
        return self.data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Validate unique slots and non-empty passwords before writing
        seen_slots = set()
        for acc in self.data.get("accounts") or []:
            if not isinstance(acc, dict):
                continue
            s_norm = normalize_slot(acc.get("slot", f"/u/{acc.get('index', 0)}/"))
            if s_norm in seen_slots:
                idx = self.next_index()
                s_norm = f"/u/{idx}/"
                acc["index"] = idx
            acc["slot"] = s_norm
            seen_slots.add(s_norm)
        serialized = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        tmp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_bytes(serialized.encode("utf-8"))
        os.replace(str(tmp_path), str(self.path))

    @property
    def profile_dir(self) -> Path:
        """Return the expanded Path to the persistent browser profile directory."""
        if self._profile_dir_override is not None:
            return self._profile_dir_override
        raw = str(self.data.get("profile_dir", "~/.gemini/argon_browser_profile"))
        return Path(raw).expanduser().resolve()

    @profile_dir.setter
    def profile_dir(self, value: Union[str, Path]) -> None:
        self.data["profile_dir"] = str(value)
        self._profile_dir_override = Path(value).expanduser().resolve()

    @property
    def accounts(self) -> List[Dict[str, Any]]:
        return self.data.setdefault("accounts", [])

    def profile_dir_for_slot(self, slot: Optional[Union[str, int]] = None) -> Path:
        """Return the per-slot browser profile directory (~/.gemini/argon_profiles/slot-uN)."""
        target_slot = normalize_slot(slot) if slot is not None and str(slot).strip() else self.active_slot()
        return profile_dir_for_slot(self.profile_dir, target_slot)

    def active_slot(self) -> str:
        """Return the currently configured active '/u/N/' slot."""
        slot = self.data.get("active_slot")
        if slot:
            return normalize_slot(slot)
        if self.accounts:
            return normalize_slot(self.accounts[0].get("slot", "/u/0/"))
        return "/u/0/"

    def set_active_slot(self, slot: Union[str, int]) -> str:
        norm = normalize_slot(slot)
        self.data["active_slot"] = norm
        self.save()
        return norm

    def next_index(self) -> int:
        """Return the next free integer slot index across all configured accounts."""
        used = set()
        for acc in self.accounts:
            idx = acc.get("index")
            if isinstance(idx, int):
                used.add(idx)
            s_num = slot_number(acc.get("slot", ""))
            if s_num is not None:
                used.add(s_num)
        candidate = 0
        while candidate in used:
            candidate += 1
        return candidate

    def add_account(
        self,
        email: str,
        password: str,
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Append a new account with the next free '/u/N/' slot and persist to disk."""
        clean_email = email.strip()
        if not clean_email:
            raise ValueError("Email must not be empty.")
        idx = self.next_index()
        slot = f"/u/{idx}/"
        entry: Dict[str, Any] = {
            "index": idx,
            "slot": slot,
            "email": clean_email,
            "password": password,
            "tier": (tier or "PRO").strip(),
            "role": (role or "Pool Member").strip(),
            "status": "configured",
        }
        self.accounts.append(entry)
        self.save()
        return entry

    def set_status(self, slot: Union[str, int], status: str) -> Optional[Dict[str, Any]]:
        """Update the status field for the account matching slot (or email) and persist."""
        raw_target = str(slot).strip()
        norm_target = normalize_slot(raw_target) if (raw_target.isdigit() or "/u/" in raw_target) else raw_target
        matched: Optional[Dict[str, Any]] = None
        for acc in self.accounts:
            acc_slot = normalize_slot(acc.get("slot", f"/u/{acc.get('index', 0)}/"))
            if acc_slot == norm_target or acc.get("email", "").lower() == raw_target.lower():
                acc["status"] = status
                matched = acc
                break
        if matched is not None:
            self.save()
        return matched

    def get_by_slot(self, slot: Union[str, int]) -> Optional[Dict[str, Any]]:
        norm = normalize_slot(slot)
        for acc in self.accounts:
            if normalize_slot(acc.get("slot", f"/u/{acc.get('index', 0)}/")) == norm:
                return acc
        return None

    def import_accounts_text(
        self,
        raw_text: str,
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Safely parse and import multi-line 'email:password' credentials (D7.1, D7.4).

        Blank lines and '#' comment lines are ignored. Every line is validated on
        its own so one malformed line never corrupts accounts_pool.json. Existing
        emails have their password updated in place; new emails get a unique slot.
        """
        added: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        accounts_to_login: List[Dict[str, Any]] = []

        email_re = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")
        for line_no, raw_line in enumerate((raw_text or "").splitlines(), 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                failed.append(
                    {
                        "line_no": line_no,
                        "line": stripped[:40],
                        "reason": "missing ':' separator (expected email:password)",
                    }
                )
                continue
            email_part, pwd_part = stripped.split(":", 1)
            clean_email = email_part.strip()
            clean_pwd = pwd_part.strip()
            if not clean_email or not email_re.match(clean_email):
                failed.append(
                    {
                        "line_no": line_no,
                        "line": clean_email or "(empty email)",
                        "reason": f"invalid email address: {clean_email!r}",
                    }
                )
                continue
            if not clean_pwd:
                failed.append(
                    {
                        "line_no": line_no,
                        "line": clean_email,
                        "reason": f"empty password for {clean_email}",
                    }
                )
                continue

            existing_acc: Optional[Dict[str, Any]] = None
            for acc in self.accounts:
                if str(acc.get("email") or "").strip().lower() == clean_email.lower():
                    existing_acc = acc
                    break

            if existing_acc is not None:
                existing_acc["email"] = clean_email
                existing_acc["password"] = clean_pwd
                if tier:
                    existing_acc["tier"] = tier.strip()
                if role:
                    existing_acc["role"] = role.strip()
                if existing_acc.get("status") in ("not_signed_in", "failed", "quota_limited"):
                    existing_acc["status"] = "configured"
                updated.append(existing_acc)
                if existing_acc not in accounts_to_login:
                    accounts_to_login.append(existing_acc)
            else:
                idx = self.next_index()
                slot = f"/u/{idx}/"
                entry: Dict[str, Any] = {
                    "index": idx,
                    "slot": slot,
                    "email": clean_email,
                    "password": clean_pwd,
                    "tier": (tier or "PRO").strip(),
                    "role": (role or "Pool Member").strip(),
                    "status": "configured",
                }
                self.accounts.append(entry)
                added.append(entry)
                accounts_to_login.append(entry)

        if added or updated:
            self.save()

        return {
            "added": added,
            "updated": updated,
            "failed": failed,
            "accounts_to_login": accounts_to_login,
        }

    def import_accounts_file(
        self,
        file_path: Union[str, Path],
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Load a .txt file of email:password lines and import them into accounts_pool.json."""
        p = Path(file_path).expanduser().resolve()
        raw_text = p.read_text(encoding="utf-8-sig", errors="replace")
        return self.import_accounts_text(raw_text, tier=tier, role=role)

    def import_accounts_text(
        self,
        raw_text: str,
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Safely parse and import multi-line 'email:password' credentials (D7.1, D7.4).

        Blank lines and '#' comment lines are ignored. Every line is validated on
        its own so one malformed line never corrupts accounts_pool.json. Existing
        emails have their password updated in place; new emails get a unique slot.
        """
        added: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        accounts_to_login: List[Dict[str, Any]] = []

        email_re = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")
        for line_no, raw_line in enumerate((raw_text or "").splitlines(), 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                failed.append(
                    {
                        "line_no": line_no,
                        "line": stripped[:40],
                        "reason": "missing ':' separator (expected email:password)",
                    }
                )
                continue
            email_part, pwd_part = stripped.split(":", 1)
            clean_email = email_part.strip()
            clean_pwd = pwd_part.strip()
            if not clean_email or not email_re.match(clean_email):
                failed.append(
                    {
                        "line_no": line_no,
                        "line": clean_email or "(empty email)",
                        "reason": f"invalid email address: {clean_email!r}",
                    }
                )
                continue
            if not clean_pwd:
                failed.append(
                    {
                        "line_no": line_no,
                        "line": clean_email,
                        "reason": f"empty password for {clean_email}",
                    }
                )
                continue

            existing_acc: Optional[Dict[str, Any]] = None
            for acc in self.accounts:
                if str(acc.get("email") or "").strip().lower() == clean_email.lower():
                    existing_acc = acc
                    break

            if existing_acc is not None:
                existing_acc["email"] = clean_email
                existing_acc["password"] = clean_pwd
                if tier:
                    existing_acc["tier"] = tier.strip()
                if role:
                    existing_acc["role"] = role.strip()
                if existing_acc.get("status") in ("not_signed_in", "failed", "quota_limited"):
                    existing_acc["status"] = "configured"
                updated.append(existing_acc)
                if existing_acc not in accounts_to_login:
                    accounts_to_login.append(existing_acc)
            else:
                idx = self.next_index()
                slot = f"/u/{idx}/"
                entry: Dict[str, Any] = {
                    "index": idx,
                    "slot": slot,
                    "email": clean_email,
                    "password": clean_pwd,
                    "tier": (tier or "PRO").strip(),
                    "role": (role or "Pool Member").strip(),
                    "status": "configured",
                }
                self.accounts.append(entry)
                added.append(entry)
                accounts_to_login.append(entry)

        if added or updated:
            self.save()

        return {
            "added": added,
            "updated": updated,
            "failed": failed,
            "accounts_to_login": accounts_to_login,
        }

    def import_accounts_file(
        self,
        file_path: Union[str, Path],
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Load a .txt file of email:password lines and import them into accounts_pool.json."""
        p = Path(file_path).expanduser().resolve()
        raw_text = p.read_text(encoding="utf-8-sig", errors="replace")
        return self.import_accounts_text(raw_text, tier=tier, role=role)

    def import_accounts_text(
        self,
        raw_text: str,
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Safely parse and import multi-line 'email:password' credentials (D7.1, D7.4).

        Blank lines and '#' comment lines are ignored. Every line is validated on
        its own so one malformed line never corrupts accounts_pool.json. Existing
        emails have their password updated in place; new emails get a unique slot.
        """
        added: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        accounts_to_login: List[Dict[str, Any]] = []

        email_re = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")
        for line_no, raw_line in enumerate((raw_text or "").splitlines(), 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in stripped:
                failed.append(
                    {
                        "line_no": line_no,
                        "line": stripped[:40],
                        "reason": "missing ':' separator (expected email:password)",
                    }
                )
                continue
            email_part, pwd_part = stripped.split(":", 1)
            clean_email = email_part.strip()
            clean_pwd = pwd_part.strip()
            if not clean_email or not email_re.match(clean_email):
                failed.append(
                    {
                        "line_no": line_no,
                        "line": clean_email or "(empty email)",
                        "reason": f"invalid email address: {clean_email!r}",
                    }
                )
                continue
            if not clean_pwd:
                failed.append(
                    {
                        "line_no": line_no,
                        "line": clean_email,
                        "reason": f"empty password for {clean_email}",
                    }
                )
                continue

            existing_acc: Optional[Dict[str, Any]] = None
            for acc in self.accounts:
                if str(acc.get("email") or "").strip().lower() == clean_email.lower():
                    existing_acc = acc
                    break

            if existing_acc is not None:
                existing_acc["email"] = clean_email
                existing_acc["password"] = clean_pwd
                if tier:
                    existing_acc["tier"] = tier.strip()
                if role:
                    existing_acc["role"] = role.strip()
                if existing_acc.get("status") in ("not_signed_in", "failed", "quota_limited"):
                    existing_acc["status"] = "configured"
                updated.append(existing_acc)
                if existing_acc not in accounts_to_login:
                    accounts_to_login.append(existing_acc)
            else:
                idx = self.next_index()
                slot = f"/u/{idx}/"
                entry: Dict[str, Any] = {
                    "index": idx,
                    "slot": slot,
                    "email": clean_email,
                    "password": clean_pwd,
                    "tier": (tier or "PRO").strip(),
                    "role": (role or "Pool Member").strip(),
                    "status": "configured",
                }
                self.accounts.append(entry)
                added.append(entry)
                accounts_to_login.append(entry)

        if added or updated:
            self.save()

        return {
            "added": added,
            "updated": updated,
            "failed": failed,
            "accounts_to_login": accounts_to_login,
        }

    def import_accounts_file(
        self,
        file_path: Union[str, Path],
        tier: str = "PRO",
        role: str = "Pool Member",
    ) -> Dict[str, Any]:
        """Load a .txt file of email:password lines and import them into accounts_pool.json."""
        p = Path(file_path).expanduser().resolve()
        raw_text = p.read_text(encoding="utf-8-sig", errors="replace")
        return self.import_accounts_text(raw_text, tier=tier, role=role)


def notify(title: str, message: str) -> None:
    """
    Show a Windows tray balloon (System.Windows.Forms.NotifyIcon through PowerShell)
    and play winsound.MessageBeep().
    Best effort only: it must never raise and must never block the login flow.
    """
    try:
        import winsound  # type: ignore

        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            winsound.MessageBeep()
    except Exception:
        pass

    if sys.platform != "win32":
        return

    try:
        safe_title = str(title).replace("'", "''").replace("\r", " ").replace("\n", " ")[:63]
        safe_msg = str(message).replace("'", "''").replace("\r", " ").replace("\n", " ")[:250]
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
            "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning; "
            f"$n.BalloonTipTitle = '{safe_title}'; "
            f"$n.BalloonTipText = '{safe_msg}'; "
            "$n.Visible = $true; "
            "$n.ShowBalloonTip(10000); "
            "Start-Sleep -Seconds 6; "
            "$n.Dispose();"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception:
        pass


def find_chrome_executable() -> Optional[str]:
    """Return the path to the installed Google Chrome binary if present, else None."""
    env_override = os.environ.get("CHROME_PATH") or os.environ.get("GOOGLE_CHROME_SHIM")
    if env_override and Path(env_override).is_file():
        return str(Path(env_override))

    candidates: List[str] = []
    if sys.platform == "win32":
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_var)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        candidates.extend(
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        )
    elif sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        for bin_name in ("google-chrome", "google-chrome-stable", "chrome"):
            resolved = shutil.which(bin_name)
            if resolved:
                candidates.append(resolved)

    for path in candidates:
        if path and Path(path).is_file():
            return str(Path(path))

    return None


class GoogleLogin:
    """
    Playwright persistent context on the pool profile directory, headed by default,
    using the installed Google Chrome binary when available (falling back to bundled Chromium).
    """

    CHALLENGE_TEXT_PATTERNS = (
        "verify it's you",
        "verify it\u2019s you",
        "unusual traffic",
        "check your phone",
        "two-step verification",
        "2-step verification",
        "tap yes on your phone",
        "open the gmail app",
        "get a verification code",
        "enter the code",
        "confirm your recovery",
    )

    PASSWORD_ERROR_PATTERNS = (
        "wrong password",
        "couldn't find your google account",
        "couldn\u2019t find your google account",
        "enter a valid email or phone number",
        "account has been disabled",
    )

    def __init__(
        self,
        profile_dir: Optional[Union[str, Path]] = None,
        headless: bool = False,
        pool_config: Optional[PoolConfig] = None,
        slot: Optional[Union[str, int]] = None,
    ) -> None:
        self.pool_config = pool_config or PoolConfig(profile_dir_override=profile_dir)
        if slot is not None:
            self.profile_dir: Path = self.pool_config.profile_dir_for_slot(slot)
        elif profile_dir is not None:
            self.profile_dir = Path(profile_dir).expanduser().resolve()
        else:
            self.profile_dir = self.pool_config.profile_dir
        self.headless = headless
        self._playwright: Any = None
        self.context: Any = None

    def switch_to_slot_profile(self, slot: Union[str, int], reset_accounts: bool = False) -> Path:
        """Switch this GoogleLogin instance to the given slot's isolated profile directory."""
        norm = normalize_slot(slot)
        target_dir = self.pool_config.profile_dir_for_slot(norm)
        base_dir = self.pool_config.profile_dir
        if not (target_dir.is_dir() and (target_dir / "Default").is_dir()):
            if base_dir.is_dir() and (base_dir / "Default").is_dir() and target_dir.resolve() != base_dir.resolve():
                seed_slot_profile(base_dir, target_dir)
        if self.context is not None and (reset_accounts or self.profile_dir.resolve() != target_dir.resolve()):
            self.close()
        self.profile_dir = target_dir
        if reset_accounts:
            reset_slot_profile_accounts(target_dir, clear_cookies=True)
        self.start()
        if reset_accounts:
            self._clear_google_session()
        return target_dir

    def __enter__(self) -> "GoogleLogin":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def start(self) -> Any:
        """Start the Playwright persistent context on self.profile_dir."""
        if self.context is not None:
            return self.context

        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        chrome_exe = find_chrome_executable()

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "args": list(CHROME_LAUNCH_ARGS),
            "ignore_default_args": ["--enable-automation"],
            "no_viewport": True,
        }

        if chrome_exe:
            logger.info("Launching installed Google Chrome binary: %s", chrome_exe)
            try:
                self.context = self._playwright.chromium.launch_persistent_context(
                    executable_path=chrome_exe,
                    **launch_kwargs,
                )
                return self.context
            except Exception as exc:
                logger.warning(
                    "Failed to launch installed Chrome (%s); falling back to bundled Chromium.",
                    exc,
                )

        logger.info("Launching Playwright bundled Chromium with profile: %s", self.profile_dir)
        self.context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        return self.context

    def close(self) -> None:
        """Close the Playwright browser context and stop Playwright cleanly."""
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _active_page(self) -> Any:
        ctx = self.start()
        if ctx.pages:
            return ctx.pages[0]
        return ctx.new_page()

    def is_signed_in(self, page: Optional[Any] = None) -> bool:
        """
        Return True if the browser context holds an authenticated Google session
        and the current page is not stuck on a sign-in or challenge screen.
        """
        ctx = self.start()
        try:
            cookies = ctx.cookies("https://accounts.google.com")
        except Exception:
            cookies = []

        auth_cookie_names = {
            "SID",
            "HSID",
            "SSID",
            "APISID",
            "SAPISID",
            "__Secure-1PSID",
            "__Secure-3PSID",
        }
        has_auth_cookie = any(c.get("name") in auth_cookie_names and c.get("value") for c in cookies)

        if page is None:
            return has_auth_cookie

        url = (page.url or "").lower()
        if any(
            token in url
            for token in (
                "/signin/identifier",
                "/signin/v2/identifier",
                "/servicelogin",
                "/challenge/",
                "/signin/rejected",
            )
        ):
            return False

        if any(
            host in url
            for host in (
                "myaccount.google.com",
                "accounts.google.com/manageaccount",
                "accounts.google.com/b/",
                "gemini.google.com",
                "www.google.com",
                "gds.google.com",
            )
        ):
            return has_auth_cookie

        return has_auth_cookie

    def _reset_profile_accounts(self) -> None:
        """Forget the accounts this profile used to hold.

        A slot profile must hold exactly ONE account. The profile it was
        seeded from can still carry the previous account's account-list
        (Preferences) - and Google keeps THAT account active, so a fresh
        sign-in is only "added" and Gemini still shows the guest page. Clearing
        the list before the browser starts makes the account we sign in the
        only one.
        """
        was_running = self.context is not None
        if was_running:
            self.close()
        reset_slot_profile_accounts(self.profile_dir, clear_cookies=True)
        if was_running:
            self.start()

    def _clear_google_session(self) -> None:
        """Drop every cookie so the next sign-in becomes the ACTIVE session.

        Without this the jar keeps the seeded account's SID next to the new
        one, and Google keeps honouring the old (dead or
        verification-pending) session.
        """
        try:
            ctx = self.start()
            ctx.clear_cookies()
        except Exception:
            pass

    def verify_session(self) -> bool:
        """Check that this profile really holds a LIVE Google session.

        The presence of the auth cookies is NOT enough: a copied or rotated
        cookie jar keeps SID / SSID / __Secure-1PSID around while Google has
        already invalidated the session. Opening Gemini then shows the
        anonymous guest page ("Sign in to save activity", model Flash-Lite),
        which looks like a working browser but is not signed in.

        The profile holds exactly ONE account and /u/N/ is an index into the
        account list of THIS browser, so the check opens /u/0/.
        """
        try:
            page = self._active_page()
        except Exception:
            return False
        try:
            page.goto(
                "https://gemini.google.com/u/0/app",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            page.wait_for_timeout(6000)
            body = (page.locator("body").inner_text(timeout=15000) or "").lower()
        except Exception:
            return False
        url_now = (page.url or "").lower()
        if any(
            tok in url_now
            for tok in (
                "accounts.google.com/v3/signin",
                "accounts.google.com/signin",
                "accounts.google.com/servicelogin",
                "/signin/rejected",
                "/challenge/",
            )
        ):
            return False
        try:
            sign_in_links = page.locator("a[href*='ServiceLogin'], a[aria-label*='Sign in']")
            if sign_in_links.count() > 0 and sign_in_links.first.is_visible():
                return False
        except Exception:
            pass
        # "Where should we start?" is normal copy on a signed-in Gemini home
        # page - only the guest sidebar item counts.
        if "sign in to save activity" in body or "sign in or create an account" in body:
            return False
        try:
            if page.locator(
                "a[aria-label*='Google Account'], button[aria-label*='Google Account']"
            ).count() > 0:
                return True
        except Exception:
            pass
        return "sign in to save activity" not in body and "sign in or create an account" not in body

    def _detect_challenge(self, page: Any) -> Optional[str]:
        """
        Inspect the current page for a human verification challenge:
        - a .../challenge/<x> URL other than pwd
        - a visible input#ca captcha box
        - a visible two-step code box
        - page text such as "Verify it's you", "unusual traffic", "Check your phone", "two-step verification"
        Returns a short human-readable description if detected, else None.
        """
        try:
            url = page.url or ""
        except Exception:
            url = ""

        # 1. URL pattern: .../challenge/<x> where <x> is not pwd
        m = re.search(r"/challenge/([^/?#]+)", url, flags=re.IGNORECASE)
        if m:
            challenge_kind = m.group(1).strip().lower()
            if challenge_kind and challenge_kind != "pwd":
                return f"Challenge URL (/challenge/{challenge_kind})"

        # 2. Visible input#ca captcha box
        try:
            ca_loc = page.locator("input#ca")
            if ca_loc.count() > 0 and ca_loc.first.is_visible():
                return "CAPTCHA challenge (input#ca)"
        except Exception:
            pass

        # 3. Visible two-step verification / OTP code input box
        otp_selectors = (
            "input#idvPin",
            "input[name='totpPin']",
            "input[name='Pin']",
            "input[name='idvPin']",
            "input[autocomplete='one-time-code']",
            "input#backupCodePin",
        )
        for sel in otp_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return f"Two-step verification code prompt ({sel})"
            except Exception:
                continue

        # 4. Page text indicators
        try:
            body_text = (page.locator("body").inner_text(timeout=1500) or "").lower()
        except Exception:
            body_text = ""

        for phrase in self.CHALLENGE_TEXT_PATTERNS:
            if phrase in body_text:
                return f"Verification prompt ('{phrase}')"

        return None

    def _detect_credential_error(self, page: Any) -> Optional[str]:
        """Check whether Google displayed an explicit invalid email/password error."""
        try:
            body_text = (page.locator("body").inner_text(timeout=1000) or "").lower()
        except Exception:
            return None
        for err in self.PASSWORD_ERROR_PATTERNS:
            if err in body_text:
                return err
        return None

    def _dismiss_post_login_prompts(self, page: Any) -> None:
        """Click through optional post-login 'Not now' / 'Skip' / 'I agree' speedbump screens if present."""
        try:
            url = (page.url or "").lower()
            if "speedbump" in url or "gds.google.com" in url or "consent" in url:
                for label in ("Not now", "Skip", "Cancel", "I agree", "Continue"):
                    btn = page.get_by_role("button", name=label)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click(timeout=2000)
                        page.wait_for_timeout(800)
                        break
        except Exception:
            pass

    def login(
        self,
        email: str,
        password: str,
        wait_minutes: float = 6,
        on_challenge: Optional[Callable[[str, str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> LoginResult:
        """
        Perform the complete automatic Google sign-in for a single account:
        1. Open https://accounts.google.com/v3/signin/identifier?flowName=GlifWebSignIn&flowEntry=AddSession
        2. Fill input#identifierId and press Enter.
        3. Fill password field (input#c1, fallback input[name='Passwd'], input[name='Password'])
           and press Enter. Prefer Chrome's autofilled value if already populated.
        4. Press Escape before submitting so an autofill dropdown cannot swallow Enter.
        5. Detect human challenges (/challenge/<x> != pwd, input#ca, 2SV code box, or verification text).
        6. On challenge: call notify(), log clear instruction naming the account, keep browser open,
           and poll until approved for up to wait_minutes. Never solve, guess, or bypass the challenge.
        7. Return LoginResult(status in OK / NEEDS_HUMAN / FAILED, detail=...).
        """
        def _emit(msg: str) -> None:
            logger.info("[%s] %s", email, msg)
            if on_status:
                try:
                    on_status(msg)
                except Exception:
                    pass

        if self.context is None and is_isolated_slot_profile(self.profile_dir):
            reset_slot_profile_accounts(self.profile_dir, clear_cookies=True)
        page = self._active_page()

        try:
            signin_url = build_signin_url(isolated_profile=is_isolated_slot_profile(self.profile_dir))
            _emit(f"Opening Google sign-in page ({signin_url})...")
            page.goto(signin_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(800)
            cur_url = (page.url or "").lower()
            if "/signin/rejected" in cur_url or "authuser=" in cur_url:
                _emit("Detected rejected or authuser-bound sign-in URL; resetting session and retrying without authuser...")
                self._clear_google_session()
                page.goto(sanitize_signin_url(signin_url, isolated_profile=True), wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(800)

            # If Google displays an account chooser first, click "Use another account"
            ident_locator = page.locator("input#identifierId, input[type='email']")
            if ident_locator.count() == 0 or not ident_locator.first.is_visible():
                for text_cand in ("Use another account", "Add account"):
                    cand = page.get_by_text(text_cand, exact=False)
                    if cand.count() > 0 and cand.first.is_visible():
                        cand.first.click(timeout=5000)
                        page.wait_for_timeout(800)
                        break

            # Step 2: Fill input#identifierId and press Enter
            _emit("Entering account email into input#identifierId...")
            ident_input = page.locator("input#identifierId").first
            ident_input.wait_for(state="visible", timeout=20000)
            ident_input.click()
            ident_input.fill(email.strip())
            page.wait_for_timeout(200)
            ident_input.press("Escape")
            page.wait_for_timeout(100)
            ident_input.press("Enter")

            # Wait for transition to password field or challenge
            password_selectors = [
                "input#c1",
                "input[name='Passwd']",
                "input[name='Password']",
                "input[type='password']",
            ]
            combined_pwd_selector = ", ".join(password_selectors)

            pwd_field = None
            deadline_pwd = time.monotonic() + 20.0
            while time.monotonic() < deadline_pwd:
                early_challenge = self._detect_challenge(page)
                if early_challenge:
                    break

                err = self._detect_credential_error(page)
                if err:
                    return LoginResult(
                        status=STATUS_FAILED,
                        detail=f"Google rejected identifier for {email}: {err}",
                        email=email,
                    )

                for sel in password_selectors:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        pwd_field = loc.first
                        break
                if pwd_field is not None:
                    break
                page.wait_for_timeout(400)

            # Step 3 & 4: Fill password (respecting Chrome autofill) -> Escape -> Enter
            if pwd_field is not None:
                # Allow Chrome password manager a moment to autofill
                page.wait_for_timeout(900)
                try:
                    autofilled_val = pwd_field.input_value(timeout=2000)
                except Exception:
                    autofilled_val = ""

                if autofilled_val and len(autofilled_val.strip()) > 0:
                    _emit("Using password already autofilled by Chrome password manager.")
                    pwd_field.click()
                else:
                    _emit("Filling password field...")
                    pwd_field.click()
                    pwd_field.fill(password)

                page.wait_for_timeout(200)
                # Step 4: Press Escape before submitting so an autofill dropdown cannot swallow Enter
                pwd_field.press("Escape")
                page.wait_for_timeout(150)
                pwd_field.press("Enter")
                page.wait_for_timeout(1500)

                # Fallback click on #passwordNext if still on the password step and button is visible
                try:
                    if "/challenge/pwd" in (page.url or "") and pwd_field.is_visible():
                        next_btn = page.locator("#passwordNext")
                        if next_btn.count() > 0 and next_btn.first.is_visible():
                            next_btn.first.click(timeout=3000)
                            page.wait_for_timeout(1200)
                except Exception:
                    pass
            elif not self._detect_challenge(page):
                return LoginResult(
                    status=STATUS_FAILED,
                    detail=f"Password field ({combined_pwd_selector}) did not appear for {email}.",
                    email=email,
                )

            # Wait briefly for post-password navigation or challenge
            post_pwd_deadline = time.monotonic() + 12.0
            while time.monotonic() < post_pwd_deadline:
                self._dismiss_post_login_prompts(page)
                if self.is_signed_in(page):
                    _emit(f"Successfully signed in {email}.")
                    return LoginResult(
                        status=STATUS_OK,
                        detail=f"Signed in {email} automatically.",
                        email=email,
                    )
                if self._detect_challenge(page):
                    break
                cred_err = self._detect_credential_error(page)
                if cred_err:
                    return LoginResult(
                        status=STATUS_FAILED,
                        detail=f"Google rejected credentials for {email}: {cred_err}",
                        email=email,
                    )
                page.wait_for_timeout(500)

            # Step 5 & 6: Detect human challenge, notify, and poll up to wait_minutes
            challenge_reason = self._detect_challenge(page)
            if challenge_reason:
                instruction = (
                    f"Action required for {email}: Google presented {challenge_reason}. "
                    f"Please approve the verification prompt on your phone or complete it in the "
                    f"open browser window (waiting up to {wait_minutes} minutes)."
                )
                _emit(instruction)
                notify(
                    "Argon Pool Login: Verify Account",
                    f"{email} needs verification ({challenge_reason}). Approve on your phone.",
                )
                if on_challenge is not None:
                    try:
                        on_challenge(email, challenge_reason)
                    except Exception:
                        pass

                wait_seconds = max(10.0, float(wait_minutes) * 60.0)
                poll_deadline = time.monotonic() + wait_seconds
                while time.monotonic() < poll_deadline:
                    self._dismiss_post_login_prompts(page)
                    if self.is_signed_in(page) and not self._detect_challenge(page):
                        _emit(f"Verification approved! {email} is now signed in.")
                        return LoginResult(
                            status=STATUS_OK,
                            detail=f"Signed in {email} after human verification approval.",
                            email=email,
                        )
                    page.wait_for_timeout(2000)

                return LoginResult(
                    status=STATUS_NEEDS_HUMAN,
                    detail=(
                        f"Verification still pending for {email} ({challenge_reason}) "
                        f"after waiting {wait_minutes} minutes."
                    ),
                    email=email,
                )

            # Final check if signed in
            self._dismiss_post_login_prompts(page)
            if self.is_signed_in(page):
                return LoginResult(
                    status=STATUS_OK,
                    detail=f"Signed in {email}.",
                    email=email,
                )

            cred_err = self._detect_credential_error(page)
            if cred_err:
                return LoginResult(
                    status=STATUS_FAILED,
                    detail=f"Sign-in failed for {email}: {cred_err}",
                    email=email,
                )

            return LoginResult(
                status=STATUS_FAILED,
                detail=f"Sign-in did not reach an authenticated page for {email} (url={page.url}).",
                email=email,
            )

        except Exception as exc:
            logger.exception("Unexpected error during login for %s", email)
            return LoginResult(
                status=STATUS_FAILED,
                detail=f"Exception while signing in {email}: {exc}",
                email=email,
            )

    def login_pool(
        self,
        accounts: Optional[Iterable[Dict[str, Any]]] = None,
        wait_minutes: float = 6,
        on_progress: Optional[Callable[[int, int, Dict[str, Any], Optional[LoginResult]], None]] = None,
        on_challenge: Optional[Callable[[str, str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> List[LoginResult]:
        """
        Sign into a sequence of pool accounts, placing each account into its own
        per-slot browser profile (~/.gemini/argon_profiles/slot-uN/) so parallel
        runs on different slots never collide and each slot profile holds a real
        authenticated Google session.
        """
        acct_list = list(accounts if accounts is not None else self.pool_config.accounts)
        results: List[LoginResult] = []
        total = len(acct_list)
        base_dir = self.pool_config.profile_dir
        use_slot_profiles = (
            self.profile_dir.resolve() == base_dir.resolve()
            or self.profile_dir.parent.name == "argon_profiles"
        )

        for idx, acc in enumerate(acct_list):
            email = str(acc.get("email", "")).strip()
            password = str(acc.get("password", ""))
            slot = normalize_slot(acc.get("slot", f"/u/{acc.get('index', idx)}/"))

            if on_progress:
                try:
                    on_progress(idx, total, acc, None)
                except Exception:
                    pass

            if not email:
                res = LoginResult(
                    status=STATUS_FAILED,
                    detail=f"Account at slot {slot} has no email configured.",
                    email="",
                    slot=slot,
                )
            else:
                if use_slot_profiles:
                    slot_profile = self.pool_config.profile_dir_for_slot(slot)
                    if is_profile_busy(slot_profile):
                        res = LoginResult(
                            status=STATUS_FAILED,
                            detail=f"Slot profile {slot_profile} for {slot} is currently in use by another browser.",
                            email=email,
                            slot=slot,
                        )
                    else:
                        # A slot profile holds exactly ONE account. The profile it was
                        # seeded from can still carry the previous account's session and
                        # a pending "verify it's you" challenge, and Google then keeps
                        # THAT account active - the new sign-in is only "added", so
                        # Gemini keeps showing the guest page. Reset Preferences and
                        # cookies BEFORE starting Chrome so the account we sign in
                        # becomes the active one at index 0.
                        self.switch_to_slot_profile(slot, reset_accounts=True)
                        res = self.login(
                            email=email,
                            password=password,
                            wait_minutes=wait_minutes,
                            on_challenge=on_challenge,
                            on_status=on_status,
                        )
                        res.slot = slot
                        # The sign-in can succeed while the session is still not usable
                        # (a stale cookie jar, a guest page). Do not report success until
                        # the session really works.
                        if res.status == STATUS_OK and not self.verify_session():
                            res.status = STATUS_FAILED
                            res.detail = (
                                (res.detail + " | " if res.detail else "")
                                + "the profile still shows Gemini's guest page - "
                                  "the session is not usable"
                            )
                else:
                    self.start()
                    res = self.login(
                        email=email,
                        password=password,
                        wait_minutes=wait_minutes,
                        on_challenge=on_challenge,
                        on_status=on_status,
                    )
                    res.slot = slot

            status_map = {
                STATUS_OK: "active",
                STATUS_NEEDS_HUMAN: "needs_human",
                STATUS_FAILED: "failed",
            }
            try:
                self.pool_config.set_status(slot, status_map.get(res.status, res.status.lower()))
            except Exception:
                pass

            results.append(res)

            if on_progress:
                try:
                    on_progress(idx, total, acc, res)
                except Exception:
                    pass

        return results

    def open_gemini(self, slot: Optional[str] = None) -> Any:
        """Open Gemini Web (https://gemini.google.com/u/0/app) in the slot's persistent profile."""
        if slot:
            try:
                self.pool_config.set_active_slot(slot)
            except Exception:
                pass
        slot_part = normalize_slot(slot) if slot else self.pool_config.active_slot()
        self.switch_to_slot_profile(slot_part)
        page = self._active_page()
        url_slot = "/u/0/" if is_isolated_slot_profile(self.profile_dir) else slot_part
        page.goto(GEMINI_WEB_URL.format(slot=url_slot), wait_until="domcontentloaded", timeout=45000)
        return page

    def open_aistudio(self, slot: Optional[str] = None) -> Any:
        """Removed: Google AI Studio is no longer supported.

        AI Studio consistently fails on agentic work, so the whole workflow was
        removed. Use `open_gemini` (Gemini Web) instead.
        """
        raise RuntimeError(
            "Google AI Studio is no longer supported - it consistently fails on "
            "agentic work. Use open_gemini() (Gemini Web) instead."
        )


def is_signed_in(profile_dir: Optional[Union[str, Path]] = None, slot: Optional[str] = None) -> bool:
    """Module-level helper to check whether the slot (or base) pool profile is signed into Google."""
    cfg = PoolConfig(profile_dir_override=profile_dir)
    if profile_dir is not None:
        target_profile = Path(profile_dir).expanduser().resolve()
    else:
        slot_prof = cfg.profile_dir_for_slot(slot)
        target_profile = slot_prof if slot_prof.is_dir() else cfg.profile_dir
    with GoogleLogin(profile_dir=target_profile, pool_config=cfg) as gl:
        return gl.is_signed_in()


def open_gemini(profile_dir: Optional[Union[str, Path]] = None, slot: Optional[str] = None) -> None:
    """Launch Chrome on the slot profile directory directly at https://gemini.google.com/u/<slot>/app."""
    cfg = PoolConfig(profile_dir_override=profile_dir)
    slot_part = cfg.set_active_slot(slot) if slot else cfg.active_slot()
    if profile_dir is not None:
        target_profile = Path(profile_dir).expanduser().resolve()
    else:
        target_profile = cfg.profile_dir_for_slot(slot_part)
        if not (target_profile.is_dir() and (target_profile / "Default").is_dir()):
            seed_slot_profile(cfg.profile_dir, target_profile)
    # The slot profile holds exactly ONE Google account, and /u/N/ is an index
    # into the account list of THIS browser - so it must be /u/0/. Opening
    # /u/<n>/ here lands on the anonymous guest page.
    _launch_detached_browser(target_profile, GEMINI_WEB_URL.format(slot="/u/0/"))


def open_aistudio(profile_dir: Optional[Union[str, Path]] = None, slot: Optional[str] = None) -> None:
    """Removed: Google AI Studio is no longer supported.

    AI Studio consistently fails on agentic work, so the whole workflow was
    removed. Use `open_gemini` (Gemini Web) instead.
    """
    raise RuntimeError(
        "Google AI Studio is no longer supported - it consistently fails on "
        "agentic work. Use open_gemini() (Gemini Web) instead."
    )


def _launch_detached_browser(profile_dir: Path, url: str) -> None:
    """Launch installed Google Chrome (or Playwright Chromium) with the persistent profile at url."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    chrome_exe = find_chrome_executable()
    if chrome_exe:
        cmd = [
            chrome_exe,
            f"--user-data-dir={profile_dir}",
            *CHROME_LAUNCH_ARGS,
            url,
        ]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return

    with GoogleLogin(profile_dir=profile_dir) as gl:
        page = gl._active_page()
        page.goto(url, wait_until="domcontentloaded")


def login_pool(
    accounts: Optional[Iterable[Dict[str, Any]]] = None,
    profile_dir: Optional[Union[str, Path]] = None,
    wait_minutes: float = 6,
    pool_config: Optional[PoolConfig] = None,
    on_progress: Optional[Callable[[int, int, Dict[str, Any], Optional[LoginResult]], None]] = None,
    on_challenge: Optional[Callable[[str, str], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> List[LoginResult]:
    """Module-level convenience wrapper for GoogleLogin.login_pool."""
    cfg = pool_config or PoolConfig(profile_dir_override=profile_dir)
    acct_list = list(accounts if accounts is not None else cfg.accounts)
    first_slot = acct_list[0].get("slot") if acct_list else cfg.active_slot()
    initial_profile = (
        Path(profile_dir).expanduser().resolve()
        if profile_dir is not None
        else cfg.profile_dir_for_slot(first_slot)
    )
    gl = GoogleLogin(profile_dir=initial_profile, pool_config=cfg)
    try:
        return gl.login_pool(
            accounts=acct_list,
            wait_minutes=wait_minutes,
            on_progress=on_progress,
            on_challenge=on_challenge,
            on_status=on_status,
        )
    finally:
        gl.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automatic Google Account-Pool Sign-In for Gemini 4 Argon Coding Bridge"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Automatically sign in all accounts in accounts_pool.json sequentially.",
    )
    parser.add_argument(
        "--slot",
        type=str,
        default=None,
        help="Sign in only the account matching the given slot (e.g. /u/0/ or 0).",
    )
    parser.add_argument(
        "--add",
        nargs=2,
        metavar=("EMAIL", "PASSWORD"),
        default=None,
        help="Add a new account credential to accounts_pool.json and immediately sign it in.",
    )
    parser.add_argument(
        "--import-file",
        type=str,
        default=None,
        metavar="TXT_PATH",
        help="Import bulk email:password lines from a .txt file into accounts_pool.json and auto-login.",
    )
    parser.add_argument(
        "--tier",
        type=str,
        default="PRO",
        help="Account tier when using --add (default: PRO).",
    )
    parser.add_argument(
        "--role",
        type=str,
        default="Pool Member",
        help="Account role when using --add (default: Pool Member).",
    )
    parser.add_argument(
        "--wait-minutes",
        type=float,
        default=6.0,
        help="Minutes to wait for human approval when a challenge/2FA prompt is detected (default: 6).",
    )
    parser.add_argument(
        "--profile-dir",
        type=str,
        default=None,
        help="Override the persistent browser profile directory path.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Optional path to accounts_pool.json.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    pool_cfg = PoolConfig(config_path=args.config, profile_dir_override=args.profile_dir)

    target_accounts: List[Dict[str, Any]] = []

    if args.import_file:
        rep = pool_cfg.import_accounts_file(args.import_file, tier=args.tier, role=args.role)
        logger.info(
            "Bulk import from %s: %d added, %d updated, %d failed",
            args.import_file,
            len(rep["added"]),
            len(rep["updated"]),
            len(rep["failed"]),
        )
        for fail in rep["failed"]:
            logger.warning("  [Line %s] %s - %s", fail["line_no"], fail["line"], fail["reason"])
        for acc in rep["accounts_to_login"]:
            if acc not in target_accounts:
                target_accounts.append(acc)

    if args.import_file:
        rep = pool_cfg.import_accounts_file(args.import_file, tier=args.tier, role=args.role)
        logger.info(
            "Bulk import from %s: %d added, %d updated, %d failed",
            args.import_file,
            len(rep["added"]),
            len(rep["updated"]),
            len(rep["failed"]),
        )
        for fail in rep["failed"]:
            logger.warning("  [Line %s] %s - %s", fail["line_no"], fail["line"], fail["reason"])
        for acc in rep["accounts_to_login"]:
            if acc not in target_accounts:
                target_accounts.append(acc)

    if args.import_file:
        rep = pool_cfg.import_accounts_file(args.import_file, tier=args.tier, role=args.role)
        logger.info(
            "Bulk import from %s: %d added, %d updated, %d failed",
            args.import_file,
            len(rep["added"]),
            len(rep["updated"]),
            len(rep["failed"]),
        )
        for fail in rep["failed"]:
            logger.warning("  [Line %s] %s - %s", fail["line_no"], fail["line"], fail["reason"])
        for acc in rep["accounts_to_login"]:
            if acc not in target_accounts:
                target_accounts.append(acc)

    if args.add:
        email, password = args.add
        new_acc = pool_cfg.add_account(email=email, password=password, tier=args.tier, role=args.role)
        logger.info(
            "Added account %s at slot %s (tier=%s, role=%s) to %s",
            new_acc["email"],
            new_acc["slot"],
            new_acc["tier"],
            new_acc["role"],
            pool_cfg.path,
        )
        target_accounts.append(new_acc)

    if args.slot:
        acc = pool_cfg.get_by_slot(args.slot)
        if not acc:
            logger.error("No account found in %s for slot %s", pool_cfg.path, args.slot)
            return 1
        if acc not in target_accounts:
            target_accounts.append(acc)

    if args.all or (not target_accounts and not args.add and not args.slot):
        if not args.all and not args.add and not args.slot:
            parser.print_help()
            return 0
        target_accounts = list(pool_cfg.accounts)

    first_slot = target_accounts[0].get("slot") if target_accounts else pool_cfg.active_slot()
    initial_profile = (
        Path(args.profile_dir).expanduser().resolve()
        if args.profile_dir
        else pool_cfg.profile_dir_for_slot(first_slot)
    )
    gl = GoogleLogin(profile_dir=initial_profile, pool_config=pool_cfg)
    try:
        results = gl.login_pool(accounts=target_accounts, wait_minutes=args.wait_minutes)
    finally:
        gl.close()

    print("\n" + "=" * 66)
    print("  Gemini 4 Argon Account Pool Sign-In Summary")
    print("=" * 66)
    for res in results:
        print(f"  [{res.status:<11}] {res.slot or '-'} {res.email}: {res.detail}")
    print("=" * 66)

    any_failed = any(r.status != STATUS_OK for r in results)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
