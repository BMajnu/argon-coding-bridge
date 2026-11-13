#!/usr/bin/env python3
"""
Runnable 5-Case Regression Test Suite for Round 20:
Argon Browser Host Persistence, Locked Host-State RMW, Manual Tab Survival,
and Live `/u/<N>/` `Default/Preferences` Account Index Synchronization.

Runs entirely against throwaway temporary directories and mock/headless fixtures
so it never touches `~/.gemini/argon_browser_profile`, `config/accounts_pool.json`,
or any real Google account session.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from argon_pool_login import PoolConfig
from safe_browser import (
    acquire_host_tab,
    read_host_state,
    release_host_tab,
    update_host_state_locked,
    update_host_tab,
    write_host_state,
)


class _FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url
        self._closed = False
        self._argon_tab_id: Optional[str] = None
        self._context: Optional["_FakeContext"] = None

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        if self._context is not None and self in self._context.pages:
            self._context.pages.remove(self)

    def goto(self, url: str, **_kwargs: Any) -> None:
        self.url = url

    def title(self) -> str:
        return "Gemini"

    def evaluate(self, _expr: str, arg: Any = None) -> Any:
        if isinstance(arg, str):
            self._argon_tab_id = arg
        return None


class _FakeCDPSession:
    def __init__(self, target_id: str) -> None:
        self._target_id = target_id

    def send(self, method: str, _params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": self._target_id}}
        return {}

    def detach(self) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.pages: List[_FakePage] = []
        self._lock = threading.Lock()
        self._counter = 0
        self._argon_is_cdp_host = True
        self.browser_closed = False
        self.new_page()

    def new_page(self) -> _FakePage:
        with self._lock:
            self._counter += 1
            pg = _FakePage("about:blank")
            pg._context = self
            self.pages.append(pg)
            return pg

    def new_cdp_session(self, page: _FakePage) -> _FakeCDPSession:
        idx = self.pages.index(page) if page in self.pages else 0
        return _FakeCDPSession(f"TARGET_{idx}_{id(page)}")

    def close(self) -> None:
        self.browser_closed = True


class _FakeBrowser:
    def __init__(self, ctx: _FakeContext) -> None:
        self.contexts = [ctx]


class _FakePlaywright:
    def __init__(self, ctx: _FakeContext) -> None:
        self._browser = _FakeBrowser(ctx)
        self.chromium = self

    def connect_over_cdp(self, _http_ep: str) -> _FakeBrowser:
        return self._browser


def run_round20_regression_suite() -> Dict[str, Any]:
    """Execute all 5 Round-20 regression cases against throwaway fixtures and return measured results."""
    import safe_browser as sb

    t0 = time.monotonic()
    results: Dict[str, Any] = {"ok": True, "cases": {}}

    with tempfile.TemporaryDirectory(prefix="argon_r20_reg_") as tmp_dir:
        root = Path(tmp_dir)
        prof = root / "argon_browser_profile"
        (prof / "Default" / "Network").mkdir(parents=True, exist_ok=True)

        fake_ctx = _FakeContext()
        fake_pw = _FakePlaywright(fake_ctx)

        orig_ensure = sb.ensure_browser_host
        orig_probe = sb.probe_cdp_endpoint
        orig_close_safely = sb.close_browser_safely

        host_alive = {"running": True, "close_calls": 0}

        def _mock_probe(http_endpoint: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
            if not host_alive["running"]:
                return None
            return {
                "http_endpoint": http_endpoint,
                "ws_endpoint": "ws://127.0.0.1:49999/devtools/browser/mock",
                "targets": [{"id": f"t_{i}", "url": pg.url, "type": "page"} for i, pg in enumerate(fake_ctx.pages)],
            }

        def _mock_ensure(
            profile_dir: Any,
            chrome_exe: Optional[str] = None,
            headless: bool = False,
            extra_args: Optional[List[str]] = None,
        ) -> Optional[Dict[str, Any]]:
            host_alive["running"] = True
            st = read_host_state(profile_dir)
            if st and st.get("http_endpoint"):
                return st
            init_st = {
                "profile_dir": str(Path(profile_dir).resolve()),
                "pid": os.getpid(),
                "pids": [os.getpid()],
                "port": 49999,
                "http_endpoint": "http://127.0.0.1:49999",
                "ws_endpoint": "ws://127.0.0.1:49999/devtools/browser/mock",
                "persistent": True,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tabs": {},
            }
            write_host_state(profile_dir, init_st)
            return init_st

        def _mock_close_safely(
            context: Any = None,
            profile_dir: Any = None,
            wait_seconds: float = 8.0,
            page: Any = None,
        ) -> Dict[str, Any]:
            host_alive["close_calls"] += 1
            host_alive["running"] = False
            if profile_dir is not None:
                sb.remove_host_state(profile_dir)
            return {
                "profile_dir": str(profile_dir or prof),
                "was_running": True,
                "mode": "graceful_cdp",
                "snapshot_taken": True,
                "snapshot_restored": False,
                "session_intact": True,
            }

        sb.ensure_browser_host = _mock_ensure  # type: ignore
        sb.probe_cdp_endpoint = _mock_probe  # type: ignore
        sb.close_browser_safely = _mock_close_safely  # type: ignore
        try:
            # ------------------------------------------------------------------
            # CASE 1: Single task tab release keeps the Argon browser host alive
            # ------------------------------------------------------------------
            ctx1, pg1, tid1, att1 = acquire_host_tab(
                playwright_instance=fake_pw,
                profile_dir=prof,
                slot="/u/0/",
                project_path=root,
                lane="single",
                account_email="user0@example.com",
            )
            rel1 = release_host_tab(ctx1, pg1, prof, tab_id=tid1)
            st_after_1 = read_host_state(prof) or {}
            case1_ok = (
                bool(att1)
                and rel1.get("mode") == "tab_closed_browser_kept"
                and host_alive["running"] is True
                and host_alive["close_calls"] == 0
                and len(st_after_1.get("tabs") or {}) == 0
                and len(fake_ctx.pages) >= 1
            )
            results["cases"]["case1_single_release_keeps_host"] = {
                "ok": case1_ok,
                "mode": rel1.get("mode"),
                "host_running": host_alive["running"],
                "close_calls": host_alive["close_calls"],
                "remaining_task_tabs": len(st_after_1.get("tabs") or {}),
                "browser_open_pages": len(fake_ctx.pages),
            }
            if not case1_ok:
                results["ok"] = False

            # ------------------------------------------------------------------
            # CASE 2: Concurrent two-lane acquire/update under lock (zero clobber)
            # ------------------------------------------------------------------
            barrier = threading.Barrier(2)
            acquired_tabs: List[Optional[str]] = [None, None]
            acquired_pages: List[Any] = [None, None]

            def _lane_worker(idx: int, slot_str: str, lane_name: str) -> None:
                barrier.wait(timeout=5.0)
                _, pg_w, tid_w, _ = acquire_host_tab(
                    playwright_instance=fake_pw,
                    profile_dir=prof,
                    slot=slot_str,
                    project_path=root,
                    lane=lane_name,
                    account_email=f"lane{idx}@example.com",
                )
                update_host_tab(prof, tid_w, chat_id=f"chat_{lane_name}", page_url=f"https://gemini.google.com{slot_str}app/chat_{lane_name}")
                acquired_tabs[idx] = tid_w
                acquired_pages[idx] = pg_w

            t_a = threading.Thread(target=_lane_worker, args=(0, "/u/0/", "followup"))
            t_b = threading.Thread(target=_lane_worker, args=(1, "/u/1/", "new"))
            t_a.start()
            t_b.start()
            t_a.join(timeout=10.0)
            t_b.join(timeout=10.0)

            st_both = read_host_state(prof) or {}
            tabs_both = dict(st_both.get("tabs") or {})
            both_present = (
                acquired_tabs[0] is not None
                and acquired_tabs[1] is not None
                and acquired_tabs[0] in tabs_both
                and acquired_tabs[1] in tabs_both
                and len(tabs_both) == 2
            )
            rel_lane0 = release_host_tab(fake_ctx, acquired_pages[0], prof, tab_id=acquired_tabs[0])
            st_after_lane0 = read_host_state(prof) or {}
            lane1_survived = (
                acquired_tabs[1] in (st_after_lane0.get("tabs") or {})
                and host_alive["running"] is True
                and not acquired_pages[1].is_closed()
            )
            rel_lane1 = release_host_tab(fake_ctx, acquired_pages[1], prof, tab_id=acquired_tabs[1])
            case2_ok = bool(both_present and lane1_survived and host_alive["running"] and rel_lane0.get("mode") == "tab_closed_browser_kept" and rel_lane1.get("mode") == "tab_closed_browser_kept")
            results["cases"]["case2_concurrent_locked_rmw"] = {
                "ok": case2_ok,
                "both_tabs_recorded": len(tabs_both),
                "lane1_survived_lane0_release": lane1_survived,
                "host_running_after_both": host_alive["running"],
            }
            if not case2_ok:
                results["ok"] = False

            # ------------------------------------------------------------------
            # CASE 3: Manual "Open in Gemini Web" tab survives task tab release
            # ------------------------------------------------------------------
            manual_pg = fake_ctx.new_page()
            manual_pg.goto("https://gemini.google.com/u/0/app")
            setattr(manual_pg, "_argon_manual_tab", True)
            update_host_state_locked(
                prof,
                lambda st: st.update({"persistent": True, "manual_open": True, "manual_url": manual_pg.url}),
            )
            _, task_pg, task_tid, _ = acquire_host_tab(
                playwright_instance=fake_pw,
                profile_dir=prof,
                slot="/u/2/",
                project_path=root,
                lane="single",
            )
            release_host_tab(fake_ctx, task_pg, prof, tab_id=task_tid)
            st_manual = read_host_state(prof) or {}
            case3_ok = (
                not manual_pg.is_closed()
                and manual_pg in fake_ctx.pages
                and host_alive["running"] is True
                and bool(st_manual.get("persistent"))
                and bool(st_manual.get("manual_open"))
            )
            results["cases"]["case3_manual_tab_persistence"] = {
                "ok": case3_ok,
                "manual_tab_open": not manual_pg.is_closed(),
                "persistent_flag": bool(st_manual.get("persistent")),
                "manual_open_flag": bool(st_manual.get("manual_open")),
                "host_running": host_alive["running"],
            }
            if not case3_ok:
                results["ok"] = False

            # ------------------------------------------------------------------
            # CASE 4: Live /u/N Default/Preferences sync & stale /u/7/ -> /u/9/ refusal
            # ------------------------------------------------------------------
            live_prefs = {
                "account_info": [
                    {"email": "acct0@example.com", "gaia": "g0", "account_id": "g0"},
                    {"email": "acct2_shifted@example.com", "gaia": "g2", "account_id": "g2"},
                    {"email": "acct1_shifted@example.com", "gaia": "g1", "account_id": "g1"},
                ]
            }
            (prof / "Default" / "Preferences").write_text(json.dumps(live_prefs), encoding="utf-8")
            pool_path = root / "accounts_pool.json"
            pool_path.write_text(
                json.dumps(
                    {
                        "profile_dir": str(prof),
                        "active_slot": "/u/0/",
                        "accounts": [
                            {"index": 0, "slot": "/u/0/", "email": "acct0@example.com", "account_index": 0, "status": "active"},
                            {"index": 1, "slot": "/u/1/", "email": "acct1_shifted@example.com", "account_index": 1, "status": "active"},
                            {"index": 2, "slot": "/u/2/", "email": "acct2_shifted@example.com", "account_index": 2, "status": "active"},
                            {"index": 7, "slot": "/u/7/", "email": "missing_slot7@example.com", "account_index": 9, "status": "active"},
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            cfg = PoolConfig(config_path=pool_path, profile_dir_override=prof)
            idx_u1 = cfg.get_account_index("/u/1/")
            idx_u2 = cfg.get_account_index("/u/2/")
            stale_refused = False
            stale_error_msg = ""
            try:
                _ = cfg.get_account_index("/u/7/")
            except RuntimeError as exc:
                stale_refused = True
                stale_error_msg = str(exc)

            sync_rep = cfg.enable_shared_profile_mode()
            acc_u7_after = cfg.get_by_slot("/u/7/") or {}
            case4_ok = (
                idx_u1 == 2
                and idx_u2 == 1
                and stale_refused
                and "account_index" not in acc_u7_after
                and bool(acc_u7_after.get("needs_signin"))
                and "/u/7/" in (sync_rep.get("unverified") or [])
            )
            results["cases"]["case4_live_account_index_sync"] = {
                "ok": case4_ok,
                "u1_synced_index": idx_u1,
                "u2_synced_index": idx_u2,
                "u7_stale_9_refused": stale_refused,
                "u7_error": stale_error_msg,
                "unverified_slots": sync_rep.get("unverified"),
            }
            if not case4_ok:
                results["ok"] = False

            # ------------------------------------------------------------------
            # CASE 5: Explicit close_browser_cli shuts down host when requested
            # ------------------------------------------------------------------
            rc_close = sb.close_browser_cli(slot=None, profile_dir=prof, project_path=str(root))
            case5_ok = (rc_close == 0 and host_alive["running"] is False and host_alive["close_calls"] == 1)
            results["cases"]["case5_explicit_close_browser"] = {
                "ok": case5_ok,
                "exit_code": rc_close,
                "host_running_after_close": host_alive["running"],
                "close_calls": host_alive["close_calls"],
            }
            if not case5_ok:
                results["ok"] = False
        finally:
            sb.ensure_browser_host = orig_ensure  # type: ignore
            sb.probe_cdp_endpoint = orig_probe  # type: ignore
            sb.close_browser_safely = orig_close_safely  # type: ignore

    results["elapsed_ms"] = int(round((time.monotonic() - t0) * 1000))
    return results


def main() -> int:
    rep = run_round20_regression_suite()
    print("=" * 78)
    print("  ARGON ROUND 20 LIFECYCLE & ACCOUNT SYNC REGRESSION SUITE")
    print("=" * 78)
    for name, info in rep["cases"].items():
        status_str = "PASS" if info.get("ok") else "FAIL"
        print(f"  [{status_str}] {name}: {json.dumps(info, ensure_ascii=False)}")
    print(f"  Overall status : {'PASS' if rep['ok'] else 'FAIL'} ({rep['elapsed_ms']} ms)")
    print("=" * 78)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())