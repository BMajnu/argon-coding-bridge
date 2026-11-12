#!/usr/bin/env python3
"""
General-Purpose Live Agent-to-Browser Control Loop Daemon and Action Executor
for the Gemini 4 Argon Coding Bridge (Round 19).

Owns:
- `LIVE_MAP_JS_PATH` and `read_live_map_js()` for the single shared interactive
  element map builder (`skills/argon-coding-bridge/live_loop/live_map.js`).
- `BrowserLoopDaemon`:
  - Lane A (Primary, CDP): Attaches to the Argon browser's `--remote-debugging-port`
    endpoint via Playwright `chromium.connect_over_cdp` (or direct CDP WebSocket
    when Playwright is absent), installs `page.expose_binding("__argonPush", cb)`
    (`Runtime.addBinding`) and `page.add_init_script(live_map.js)` on every tab,
    and subscribes to `context.on("page", ...)` for newly opened tabs.
  - Lane B (Secondary, MV3 Extension): Accepts a local WebSocket connection at
    `ws://127.0.0.1:<port>/live` from `live_loop/extension/live_service_worker.js`.
  - Local HTTP + SSE + WebSocket server bound strictly to `127.0.0.1` with per-run
    64-hex-char token authentication (`X-Argon-Token` header or `?token=` query).
  - Action executor enforcing the stale-element rule (`expect.tag` and `expect.name`
    verified against a freshly built map at action time) and returning the fresh
    interactive map in the same response.
- `browser_loop_cli(...)`: CLI entry point for `--browser-loop-start`,
  `--browser-loop-stop`, `--browser-loop-status`, `--browser-act`, and
  `--browser-loop-selftest`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
import queue
import secrets
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
LIVE_MAP_JS_PATH = SKILL_ROOT / "live_loop" / "live_map.js"
DEFAULT_STATE_FILE = Path.home() / ".gemini" / "argon_browser_loop.json"
PROTOCOL_VERSION = "argon.live.v1"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def read_live_map_js() -> str:
    """Read `LIVE_MAP_JS_PATH` and return the source text. Raise a clear error if missing."""
    if not LIVE_MAP_JS_PATH.is_file():
        raise FileNotFoundError(
            f"live_map.js not found at expected path: {LIVE_MAP_JS_PATH}"
        )
    return LIVE_MAP_JS_PATH.read_text(encoding="utf-8")


def _find_browser_executable() -> Optional[str]:
    """Locate Chrome or Chromium binary for disposable self-test runs."""
    try:
        from argon_pool_login import find_chrome_executable

        found = find_chrome_executable()
        if found and Path(found).is_file():
            return str(found)
    except Exception:
        pass
    for bin_name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        resolved = shutil.which(bin_name)
        if resolved and Path(resolved).is_file():
            return str(resolved)
    return None


def _allocate_loop_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _is_valid_http_https_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _ws_read_frame(sock: socket.socket) -> Optional[Tuple[int, bytes]]:
    """Read a single RFC 6455 WebSocket frame from `sock`."""
    def _recv_exact(n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except Exception:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    hdr = _recv_exact(2)
    if not hdr:
        return None
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = _recv_exact(2)
        if not ext:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _recv_exact(8)
        if not ext:
            return None
        length = struct.unpack("!Q", ext)[0]
    mask_key = b""
    if masked:
        mk = _recv_exact(4)
        if not mk:
            return None
        mask_key = mk
    payload = _recv_exact(length) if length > 0 else b""
    if payload is None:
        return None
    if masked and payload:
        unmasked = bytearray(len(payload))
        for i, byte_val in enumerate(payload):
            unmasked[i] = byte_val ^ mask_key[i % 4]
        payload = bytes(unmasked)
    return opcode, payload


def _ws_write_frame(
    sock: socket.socket,
    payload: bytes,
    opcode: int = 0x1,
    mask: bool = False,
) -> bool:
    """Write a single RFC 6455 WebSocket frame to `sock`."""
    length = len(payload)
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if mask:
        mask_key = os.urandom(4)
        header.extend(mask_key)
        masked_payload = bytearray(length)
        for i, b in enumerate(payload):
            masked_payload[i] = b ^ mask_key[i % 4]
        data = bytes(header) + bytes(masked_payload)
    else:
        data = bytes(header) + payload
    try:
        sock.sendall(data)
        return True
    except Exception:
        return False


class _CDPPageSession:
    """Direct CDP WebSocket client for a single browser page target."""

    def __init__(
        self,
        target_id: str,
        ws_url: str,
        on_binding_push: Callable[[str], None],
    ) -> None:
        self.target_id = target_id
        self.ws_url = ws_url
        self._on_binding_push = on_binding_push
        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, "queue.Queue[Dict[str, Any]]"] = {}
        self._pending_lock = threading.Lock()
        self._closed = False
        self._reader_thread: Optional[threading.Thread] = None
        self.url = ""
        self.title = ""

    def connect(self, timeout: float = 8.0) -> None:
        parsed = urllib.parse.urlparse(self.ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise ConnectionError(f"CDP WebSocket handshake EOF on {self.ws_url}")
            buf += chunk
        status_line = buf.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if "101" not in status_line:
            sock.close()
            raise ConnectionError(f"CDP WebSocket handshake rejected: {status_line}")
        sock.settimeout(None)
        self._sock = sock
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"argon-cdp-reader-{self.target_id[:8]}",
            daemon=True,
        )
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        while not self._closed and self._sock is not None:
            frame = _ws_read_frame(self._sock)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 0x8:
                break
            if opcode == 0x9:
                with self._send_lock:
                    if self._sock is not None:
                        _ws_write_frame(self._sock, payload, opcode=0xA, mask=True)
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg:
                msg_id = int(msg["id"])
                with self._pending_lock:
                    q = self._pending.get(msg_id)
                if q is not None:
                    q.put(msg)
                continue
            method = str(msg.get("method") or "")
            params = msg.get("params") or {}
            if method == "Runtime.bindingCalled" and params.get("name") == "__argonPush":
                raw_payload = str(params.get("payload") or "")
                if raw_payload:
                    try:
                        self._on_binding_push(raw_payload)
                    except Exception:
                        pass
            elif method == "Page.frameNavigated":
                frame_info = params.get("frame") or {}
                if not frame_info.get("parentId"):
                    self.url = str(frame_info.get("url") or self.url)

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        if self._closed or self._sock is None:
            raise ConnectionError("CDP session is closed")
        with self._pending_lock:
            self._next_id += 1
            req_id = self._next_id
            q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1)
            self._pending[req_id] = q
        body = json.dumps({"id": req_id, "method": method, "params": params or {}}).encode("utf-8")
        try:
            with self._send_lock:
                ok = _ws_write_frame(self._sock, body, opcode=0x1, mask=True)
            if not ok:
                raise ConnectionError(f"Failed sending CDP command {method}")
            resp = q.get(timeout=timeout)
        finally:
            with self._pending_lock:
                self._pending.pop(req_id, None)
        if "error" in resp:
            err_obj = resp["error"]
            raise RuntimeError(f"CDP {method} error: {err_obj}")
        return resp.get("result") or {}

    def evaluate(self, expression: str, timeout: float = 10.0) -> Any:
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
            timeout=timeout,
        )
        if "exceptionDetails" in res:
            exc = res["exceptionDetails"]
            raise RuntimeError(f"JS evaluation exception: {exc}")
        result_obj = res.get("result") or {}
        return result_obj.get("value")

    def close(self) -> None:
        self._closed = True
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class BrowserLoopDaemon:
    """Owns the CDP connection, per-tab bindings, map inbox, action executor,
    HTTP+SSE+WebSocket server, and `~/.gemini/argon_browser_loop.json` state file.
    """

    def __init__(
        self,
        http_endpoint: Optional[str] = None,
        state_file: Optional[Union[str, Path]] = None,
        allow_eval: bool = False,
        slot: Optional[str] = None,
        profile_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.cdp_http_endpoint = str(http_endpoint or "").strip()
        self.state_file = Path(state_file).expanduser().resolve() if state_file else DEFAULT_STATE_FILE
        self.allow_eval = bool(allow_eval)
        self.slot = slot
        self.profile_dir = Path(profile_dir).expanduser().resolve() if profile_dir else None

        self.token = secrets.token_hex(32)
        self.port = 0
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.http_endpoint = ""
        self.ws_endpoint = ""

        self._live_map_js = ""
        self._lock = threading.RLock()
        self._maps_by_tab: Dict[int, Dict[str, Any]] = {}
        self._push_history: List[Dict[str, Any]] = []
        self._push_cond = threading.Condition(self._lock)
        self._active_tab: int = 0

        # Playwright worker thread state (when Playwright is installed)
        self._using_playwright = False
        self._pw_thread: Optional[threading.Thread] = None
        self._pw_cmd_queue: "queue.Queue[Tuple[Callable[[], Any], queue.Queue[Tuple[bool, Any]]]]" = queue.Queue()
        self._pw_stop_event = threading.Event()
        self._pw_pages: List[Any] = []
        self._pw_context: Any = None
        self._pw_browser: Any = None

        # Direct CDP fallback state (when Playwright is not installed)
        self._cdp_sessions: List[_CDPPageSession] = []

        # Server state (HTTP + SSE + WebSocket on 127.0.0.1)
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._sse_Queues: List["queue.Queue[Optional[str]]"] = []
        self._ws_clients: List[socket.socket] = []
        self._ws_pending_results: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
        self._stopped = False

    def _resolve_cdp_endpoint(self) -> str:
        if self.cdp_http_endpoint:
            return self.cdp_http_endpoint
        env_ep = str(os.environ.get("ARGON_BROWSER_CDP_ENDPOINT") or "").strip()
        if env_ep:
            self.cdp_http_endpoint = env_ep
            return env_ep
        try:
            from safe_browser import (
                _discover_running_cdp_endpoint,
                _resolve_slot_and_profile,
                find_chrome_pids_for_profile,
                probe_cdp_endpoint,
                read_host_state,
            )

            _, prof = _resolve_slot_and_profile(slot=self.slot, profile_dir=self.profile_dir)
            st = read_host_state(prof)
            if st and st.get("http_endpoint"):
                ep = str(st["http_endpoint"]).strip()
                if probe_cdp_endpoint(ep, timeout=2.0) is not None:
                    self.cdp_http_endpoint = ep
                    return ep
            pids = find_chrome_pids_for_profile(prof)
            if pids:
                disc = _discover_running_cdp_endpoint(prof, existing_state=st, existing_pids=pids)
                if disc is not None:
                    self.cdp_http_endpoint = str(disc[0])
                    return self.cdp_http_endpoint
        except Exception:
            pass
        raise RuntimeError(
            "No running Argon browser CDP endpoint found in .argon_browser_endpoint. "
            "Open the Argon browser first (e.g. --open-browser) or run --browser-loop-selftest."
        )

    def _record_pushed_map(self, tab_idx: int, raw_or_dict: Union[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        recv_ms = time.time() * 1000.0
        try:
            if isinstance(raw_or_dict, str):
                parsed = json.loads(raw_or_dict)
            elif isinstance(raw_or_dict, dict):
                parsed = dict(raw_or_dict)
            else:
                return None
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        sent_at = float(parsed.get("at") or recv_ms)
        latency_ms = max(0.1, round(recv_ms - sent_at, 2))
        parsed["push_latency_ms"] = latency_ms

        envelope = {
            "type": "map",
            "tab": int(tab_idx),
            "map": parsed,
        }
        serialized = json.dumps(envelope, ensure_ascii=False)

        with self._push_cond:
            self._maps_by_tab[int(tab_idx)] = parsed
            self._push_history.append(
                {
                    "tab": int(tab_idx),
                    "gen": int(parsed.get("gen") or 0),
                    "reason": str(parsed.get("reason") or "mutation"),
                    "count": int(parsed.get("count") or 0),
                    "domNodes": int(parsed.get("domNodes") or 0),
                    "latency_ms": latency_ms,
                    "received_at_ms": recv_ms,
                }
            )
            if len(self._push_history) > 200:
                self._push_history = self._push_history[-200:]
            sse_list = list(self._sse_Queues)
            ws_list = list(self._ws_clients)
            self._push_cond.notify_all()

        for q in sse_list:
            try:
                q.put_nowait(serialized)
            except Exception:
                pass
        dead_ws: List[socket.socket] = []
        for ws_sock in ws_list:
            if not _ws_write_frame(ws_sock, serialized.encode("utf-8"), opcode=0x1, mask=False):
                dead_ws.append(ws_sock)
        if dead_ws:
            with self._lock:
                for d in dead_ws:
                    if d in self._ws_clients:
                        self._ws_clients.remove(d)
        return parsed

    def _run_on_pw_thread(self, fn: Callable[[], Any], timeout: float = 30.0) -> Any:
        if threading.current_thread() is self._pw_thread:
            return fn()
        reply_q: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)
        self._pw_cmd_queue.put((fn, reply_q))
        ok, val = reply_q.get(timeout=timeout)
        if not ok:
            if isinstance(val, BaseException):
                raise val
            raise RuntimeError(str(val))
        return val

    def _install_on_playwright_page(self, page: Any, tab_idx: int) -> None:
        if getattr(page, "_argon_live_bound", False):
            return
        setattr(page, "_argon_live_bound", True)

        def _on_push(_source: Any, payload: str) -> None:
            idx = self._pw_pages.index(page) if page in self._pw_pages else tab_idx
            self._record_pushed_map(idx, payload)

        try:
            page.expose_binding("__argonPush", _on_push)
        except Exception:
            pass
        try:
            page.add_init_script(self._live_map_js)
        except Exception:
            pass
        try:
            page.evaluate(self._live_map_js)
        except Exception:
            pass

    def _attach_playwright(self, http_ep: str) -> Dict[str, Any]:
        from playwright.sync_api import sync_playwright

        ready_q: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)

        def _worker() -> None:
            pw_cm = None
            try:
                pw_cm = sync_playwright().start()
                browser = pw_cm.chromium.connect_over_cdp(http_ep)
                context = browser.contexts[0] if browser.contexts else browser.new_context(no_viewport=True)
                self._pw_browser = browser
                self._pw_context = context
                self._pw_pages = list(context.pages)
                if not self._pw_pages:
                    self._pw_pages.append(context.new_page())

                for idx, pg in enumerate(self._pw_pages):
                    self._install_on_playwright_page(pg, idx)

                def _on_new_page(new_pg: Any) -> None:
                    if new_pg not in self._pw_pages:
                        self._pw_pages.append(new_pg)
                    idx_new = self._pw_pages.index(new_pg)
                    self._install_on_playwright_page(new_pg, idx_new)

                context.on("page", _on_new_page)
                ready_q.put((True, {"tabs": len(self._pw_pages), "backend": "playwright_cdp"}))

                while not self._pw_stop_event.is_set():
                    try:
                        fn, reply = self._pw_cmd_queue.get(timeout=0.02)
                        try:
                            res = fn()
                            reply.put((True, res))
                        except BaseException as exc:
                            reply.put((False, exc))
                    except queue.Empty:
                        pass
                    if self._pw_pages:
                        try:
                            self._pw_pages[0].wait_for_timeout(15)
                        except Exception:
                            time.sleep(0.015)
                    else:
                        time.sleep(0.015)
            except BaseException as init_exc:
                if ready_q.empty():
                    ready_q.put((False, init_exc))
            finally:
                if pw_cm is not None:
                    try:
                        pw_cm.stop()
                    except Exception:
                        pass

        self._pw_stop_event.clear()
        self._pw_thread = threading.Thread(target=_worker, name="argon-pw-loop", daemon=True)
        self._pw_thread.start()
        ok, info = ready_q.get(timeout=20.0)
        if not ok:
            if isinstance(info, BaseException):
                raise info
            raise RuntimeError(str(info))
        self._using_playwright = True
        return info

    def _attach_direct_cdp_page(self, target: Dict[str, Any], tab_idx: int) -> _CDPPageSession:
        tid = str(target.get("id") or f"tab_{tab_idx}")
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise RuntimeError(f"Page target {tid} did not expose webSocketDebuggerUrl")

        sess_ref: List[Optional[_CDPPageSession]] = [None]

        def _on_push(raw_json: str) -> None:
            cur_sess = sess_ref[0]
            idx = self._cdp_sessions.index(cur_sess) if (cur_sess and cur_sess in self._cdp_sessions) else tab_idx
            self._record_pushed_map(idx, raw_json)

        sess = _CDPPageSession(tid, ws_url, _on_push)
        sess.url = str(target.get("url") or "")
        sess.title = str(target.get("title") or "")
        sess_ref[0] = sess
        sess.connect()
        sess.call("Runtime.enable")
        sess.call("Page.enable")
        sess.call("Runtime.addBinding", {"name": "__argonPush"})
        sess.call("Page.addScriptToEvaluateOnNewDocument", {"source": self._live_map_js})
        try:
            sess.evaluate(self._live_map_js)
        except Exception:
            pass
        return sess

    def _attach_direct_cdp(self, http_ep: str) -> Dict[str, Any]:
        list_url = f"{http_ep.rstrip('/')}/json/list"
        with urllib.request.urlopen(list_url, timeout=5.0) as resp:
            targets = json.loads(resp.read().decode("utf-8", errors="replace"))
        page_targets = [
            t for t in (targets if isinstance(targets, list) else [])
            if isinstance(t, dict) and t.get("type") == "page" and t.get("webSocketDebuggerUrl")
        ]
        if not page_targets:
            new_url = f"{http_ep.rstrip('/')}/json/new?about:blank"
            req = urllib.request.Request(new_url, method="PUT")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                created = json.loads(resp.read().decode("utf-8", errors="replace"))
            page_targets = [created]

        self._cdp_sessions = []
        for idx, tgt in enumerate(page_targets):
            sess = self._attach_direct_cdp_page(tgt, idx)
            self._cdp_sessions.append(sess)
        self._using_playwright = False
        return {"tabs": len(self._cdp_sessions), "backend": "direct_cdp"}

    def attach(self, http_endpoint: Optional[str] = None) -> Dict[str, Any]:
        """Connect over CDP to the Argon browser endpoint, install `__argonPush` binding
        + `live_map.js` init script on every existing page, and subscribe to new pages.
        """
        if http_endpoint:
            self.cdp_http_endpoint = str(http_endpoint).strip()
        self._live_map_js = read_live_map_js()
        ep = self._resolve_cdp_endpoint()
        try:
            import playwright.sync_api  # noqa: F401

            return self._attach_playwright(ep)
        except ImportError:
            return self._attach_direct_cdp(ep)

    def list_tabs(self) -> List[Dict[str, Any]]:
        if self._using_playwright:
            def _list_pw() -> List[Dict[str, Any]]:
                out: List[Dict[str, Any]] = []
                for idx, pg in enumerate(self._pw_pages):
                    try:
                        u = str(pg.url or "")
                    except Exception:
                        u = ""
                    try:
                        t = str(pg.title() or "")
                    except Exception:
                        t = ""
                    out.append(
                        {
                            "tab": idx,
                            "index": idx,
                            "url": u,
                            "title": t,
                            "active": idx == self._active_tab,
                        }
                    )
                return out

            return self._run_on_pw_thread(_list_pw)

        out_cdp: List[Dict[str, Any]] = []
        for idx, sess in enumerate(self._cdp_sessions):
            last_m = self._maps_by_tab.get(idx) or {}
            out_cdp.append(
                {
                    "tab": idx,
                    "index": idx,
                    "url": str(last_m.get("url") or sess.url or ""),
                    "title": str(last_m.get("title") or sess.title or ""),
                    "active": idx == self._active_tab,
                }
            )
        return out_cdp

    def build_map(self, tab: int = 0) -> Dict[str, Any]:
        """Return the current map of `tab` (rebuilt on demand if needed)."""
        tab_idx = int(tab)
        js_expr = (
            "(() => {"
            " if (!window.__argonLive || typeof window.__argonLive.buildMap !== 'function') {"
            f"   {self._live_map_js}\n"
            " }"
            " return window.__argonLive.buildMap('demand');"
            "})()"
        )
        if self._using_playwright:
            def _build_pw() -> Dict[str, Any]:
                if not self._pw_pages:
                    return {}
                eff_idx = max(0, min(tab_idx, len(self._pw_pages) - 1))
                pg = self._pw_pages[eff_idx]
                m = pg.evaluate(js_expr)
                if isinstance(m, dict):
                    self._record_pushed_map(eff_idx, m)
                    return m
                return self._maps_by_tab.get(eff_idx) or {}

            return self._run_on_pw_thread(_build_pw)

        if self._cdp_sessions:
            eff_idx = max(0, min(tab_idx, len(self._cdp_sessions) - 1))
            sess = self._cdp_sessions[eff_idx]
            m = sess.evaluate(js_expr)
            if isinstance(m, dict):
                self._record_pushed_map(eff_idx, m)
                return m
        return dict(self._maps_by_tab.get(tab_idx) or {})

    def execute(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Run one action, enforce the stale-element rule, and return the result dict with the fresh map."""
        t0 = time.monotonic()
        if not isinstance(action, dict):
            return {
                "ok": False,
                "op": "unknown",
                "tab": self._active_tab,
                "error": "invalid_action",
                "message": "action must be a JSON object",
                "elapsed_ms": 0,
            }

        op = str(action.get("op") or "map").strip()
        tab_idx = int(action.get("tab", self._active_tab) if action.get("tab") is not None else self._active_tab)

        # Security Rule 4: eval is off by default unless --browser-allow-eval was passed.
        if op == "eval" and not self.allow_eval:
            cur_map = self._maps_by_tab.get(tab_idx) or self.build_map(tab_idx)
            return {
                "ok": False,
                "op": "eval",
                "tab": tab_idx,
                "gen": int(cur_map.get("gen") or 0),
                "error": "eval_disabled",
                "message": "eval is disabled by default; pass --browser-allow-eval to enable it",
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "map": cur_map,
            }

        # Security Rule 5: navigate (and newtab with url) accepts ONLY http:// and https://.
        if op == "navigate":
            nav_url = str(action.get("url") or "").strip()
            if not _is_valid_http_https_url(nav_url):
                cur_map = self._maps_by_tab.get(tab_idx) or self.build_map(tab_idx)
                return {
                    "ok": False,
                    "op": "navigate",
                    "tab": tab_idx,
                    "gen": int(cur_map.get("gen") or 0),
                    "error": "invalid_url_scheme",
                    "message": f"navigate accepts only http:// and https:// URLs (refused {nav_url!r})",
                    "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                    "map": cur_map,
                }

        if op == "newtab":
            new_url = str(action.get("url") or "").strip()
            if new_url and not _is_valid_http_https_url(new_url):
                cur_map = self._maps_by_tab.get(tab_idx) or self.build_map(tab_idx)
                return {
                    "ok": False,
                    "op": "newtab",
                    "tab": tab_idx,
                    "gen": int(cur_map.get("gen") or 0),
                    "error": "invalid_url_scheme",
                    "message": f"newtab accepts only http:// and https:// URLs (refused {new_url!r})",
                    "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                    "map": cur_map,
                }

        if op == "wait":
            wait_ms = max(0, int(action.get("ms", 500) if action.get("ms") is not None else 500))
            time.sleep(wait_ms / 1000.0)
            fresh_map = self.build_map(tab_idx)
            return {
                "ok": True,
                "op": "wait",
                "tab": tab_idx,
                "gen": int(fresh_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "note": f"waited {wait_ms} ms",
                "map": fresh_map,
            }

        if op == "tabs":
            tabs_info = self.list_tabs()
            fresh_map = self.build_map(self._active_tab)
            return {
                "ok": True,
                "op": "tabs",
                "tab": self._active_tab,
                "gen": int(fresh_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "tabs": tabs_info,
                "note": f"{len(tabs_info)} open tab(s)",
                "map": fresh_map,
            }

        if op == "switch":
            target_tab = int(action.get("tab", 0))
            if self._using_playwright:
                def _switch_pw() -> Dict[str, Any]:
                    if target_tab < 0 or target_tab >= len(self._pw_pages):
                        raise IndexError(f"tab {target_tab} out of range (0..{len(self._pw_pages) - 1})")
                    self._active_tab = target_tab
                    pg = self._pw_pages[target_tab]
                    try:
                        pg.bring_to_front()
                    except Exception:
                        pass
                    return self.build_map(target_tab)

                try:
                    sw_map = self._run_on_pw_thread(_switch_pw)
                except Exception as exc:
                    return {
                        "ok": False,
                        "op": "switch",
                        "tab": self._active_tab,
                        "error": "invalid_tab",
                        "message": str(exc),
                        "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                    }
            else:
                if target_tab < 0 or target_tab >= len(self._cdp_sessions):
                    return {
                        "ok": False,
                        "op": "switch",
                        "tab": self._active_tab,
                        "error": "invalid_tab",
                        "message": f"tab {target_tab} out of range",
                        "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                    }
                self._active_tab = target_tab
                try:
                    self._cdp_sessions[target_tab].call("Page.bringToFront")
                except Exception:
                    pass
                sw_map = self.build_map(target_tab)
            return {
                "ok": True,
                "op": "switch",
                "tab": self._active_tab,
                "gen": int(sw_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "note": f"switched to tab {self._active_tab}",
                "map": sw_map,
            }

        if op == "newtab":
            new_url = str(action.get("url") or "").strip()
            if self._using_playwright:
                def _newtab_pw() -> Tuple[int, Dict[str, Any]]:
                    pg = self._pw_context.new_page()
                    if pg not in self._pw_pages:
                        self._pw_pages.append(pg)
                    idx = self._pw_pages.index(pg)
                    self._install_on_playwright_page(pg, idx)
                    if new_url:
                        pg.goto(new_url, wait_until="domcontentloaded", timeout=30000)
                        try:
                            pg.evaluate(self._live_map_js)
                        except Exception:
                            pass
                    self._active_tab = idx
                    return idx, self.build_map(idx)

                idx_created, nt_map = self._run_on_pw_thread(_newtab_pw)
            else:
                target_u = new_url if new_url else "about:blank"
                req = urllib.request.Request(
                    f"{self.cdp_http_endpoint.rstrip('/')}/json/new?{urllib.parse.quote(target_u, safe=':/?&=%')}",
                    method="PUT",
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    created = json.loads(resp.read().decode("utf-8", errors="replace"))
                idx_created = len(self._cdp_sessions)
                sess = self._attach_direct_cdp_page(created, idx_created)
                self._cdp_sessions.append(sess)
                self._active_tab = idx_created
                nt_map = self.build_map(idx_created)
            return {
                "ok": True,
                "op": "newtab",
                "tab": idx_created,
                "gen": int(nt_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "note": f"opened tab {idx_created}" + (f" at {new_url}" if new_url else ""),
                "map": nt_map,
            }

        if op == "closeTab":
            close_idx = int(action.get("tab", self._active_tab))
            if self._using_playwright:
                def _closetab_pw() -> Dict[str, Any]:
                    if close_idx < 0 or close_idx >= len(self._pw_pages):
                        raise IndexError(f"tab {close_idx} out of range")
                    pg = self._pw_pages.pop(close_idx)
                    try:
                        pg.close()
                    except Exception:
                        pass
                    self._active_tab = max(0, min(self._active_tab, len(self._pw_pages) - 1))
                    return self.build_map(self._active_tab) if self._pw_pages else {}

                ct_map = self._run_on_pw_thread(_closetab_pw)
            else:
                if close_idx < 0 or close_idx >= len(self._cdp_sessions):
                    return {
                        "ok": False,
                        "op": "closeTab",
                        "tab": self._active_tab,
                        "error": "invalid_tab",
                        "message": f"tab {close_idx} out of range",
                        "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                    }
                sess = self._cdp_sessions.pop(close_idx)
                sess.close()
                try:
                    urllib.request.urlopen(
                        f"{self.cdp_http_endpoint.rstrip('/')}/json/close/{sess.target_id}",
                        timeout=3.0,
                    ).read()
                except Exception:
                    pass
                self._active_tab = max(0, min(self._active_tab, len(self._cdp_sessions) - 1))
                ct_map = self.build_map(self._active_tab) if self._cdp_sessions else {}
            return {
                "ok": True,
                "op": "closeTab",
                "tab": self._active_tab,
                "gen": int(ct_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "note": f"closed tab {close_idx}",
                "map": ct_map,
            }

        if op == "navigate":
            nav_url = str(action.get("url") or "").strip()
            if self._using_playwright:
                def _nav_pw() -> Dict[str, Any]:
                    eff_idx = max(0, min(tab_idx, len(self._pw_pages) - 1))
                    pg = self._pw_pages[eff_idx]
                    pg.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        pg.evaluate(self._live_map_js)
                    except Exception:
                        pass
                    return self.build_map(eff_idx)

                nav_map = self._run_on_pw_thread(_nav_pw)
            else:
                eff_idx = max(0, min(tab_idx, len(self._cdp_sessions) - 1))
                sess = self._cdp_sessions[eff_idx]
                sess.call("Page.navigate", {"url": nav_url})
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    time.sleep(0.08)
                    try:
                        rs = sess.evaluate("document.readyState")
                        if rs in ("interactive", "complete"):
                            break
                    except Exception:
                        pass
                try:
                    sess.evaluate(self._live_map_js)
                except Exception:
                    pass
                nav_map = self.build_map(eff_idx)
            return {
                "ok": True,
                "op": "navigate",
                "tab": tab_idx,
                "gen": int(nav_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "note": f"navigated to {nav_url}",
                "map": nav_map,
            }

        if op == "screenshot":
            shot_dir = Path.cwd() / ".argon_backup" / "screenshots"
            shot_dir.mkdir(parents=True, exist_ok=True)
            shot_path = shot_dir / f"browser_loop_{int(time.time() * 1000)}.png"
            if self._using_playwright:
                def _shot_pw() -> Dict[str, Any]:
                    eff_idx = max(0, min(tab_idx, len(self._pw_pages) - 1))
                    pg = self._pw_pages[eff_idx]
                    pg.screenshot(path=str(shot_path))
                    return self.build_map(eff_idx)

                shot_map = self._run_on_pw_thread(_shot_pw)
            else:
                eff_idx = max(0, min(tab_idx, len(self._cdp_sessions) - 1))
                sess = self._cdp_sessions[eff_idx]
                res_shot = sess.call("Page.captureScreenshot", {"format": "png"})
                b64_png = str(res_shot.get("data") or "")
                shot_path.write_bytes(base64.b64decode(b64_png))
                shot_map = self.build_map(eff_idx)
            return {
                "ok": True,
                "op": "screenshot",
                "tab": tab_idx,
                "path": str(shot_path),
                "gen": int(shot_map.get("gen") or 0),
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
                "note": f"saved screenshot to {shot_path}",
                "map": shot_map,
            }

        # DOM-level operations (map, click, type, press, select, scroll, back, forward, reload, text, eval)
        act_json = json.dumps(action, ensure_ascii=False)
        allow_eval_js = "true" if self.allow_eval else "false"
        dom_expr = (
            "(() => {"
            " if (!window.__argonLive || typeof window.__argonLive.executeDomAction !== 'function') {"
            f"   {self._live_map_js}\n"
            " }"
            f" return window.__argonLive.executeDomAction({act_json}, {allow_eval_js});"
            "})()"
        )

        if self._using_playwright:
            def _dom_pw() -> Dict[str, Any]:
                eff_idx = max(0, min(tab_idx, len(self._pw_pages) - 1))
                pg = self._pw_pages[eff_idx]
                res = pg.evaluate(dom_expr)
                if isinstance(res, dict) and isinstance(res.get("map"), dict):
                    self._record_pushed_map(eff_idx, res["map"])
                return res if isinstance(res, dict) else {"ok": False, "op": op, "error": "invalid_dom_response"}

            dom_res = self._run_on_pw_thread(_dom_pw)
        elif self._cdp_sessions:
            eff_idx = max(0, min(tab_idx, len(self._cdp_sessions) - 1))
            sess = self._cdp_sessions[eff_idx]
            res = sess.evaluate(dom_expr)
            if isinstance(res, dict) and isinstance(res.get("map"), dict):
                self._record_pushed_map(eff_idx, res["map"])
            dom_res = res if isinstance(res, dict) else {"ok": False, "op": op, "error": "invalid_dom_response"}
        else:
            return {
                "ok": False,
                "op": op,
                "tab": tab_idx,
                "error": "not_connected",
                "message": "daemon is not attached to any browser tab",
                "elapsed_ms": int(round((time.monotonic() - t0) * 1000)),
            }

        out = dict(dom_res)
        out["tab"] = tab_idx
        out["elapsed_ms"] = int(round((time.monotonic() - t0) * 1000))
        if isinstance(out.get("map"), dict):
            out["gen"] = int(out["map"].get("gen") or out.get("gen") or 0)
        return out

    def _check_token(self, header_token: Optional[str], query_token: Optional[str]) -> bool:
        cand = str(header_token or query_token or "").strip()
        if not cand or not self.token:
            return False
        return hmac.compare_digest(cand, self.token)

    def start_server(self, port: int = 0) -> Dict[str, Any]:
        """Start the local HTTP+SSE+WebSocket server on `127.0.0.1` and write the state file."""
        daemon = self

        class _LoopRequestHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _authenticate(self, parsed_url: urllib.parse.SplitResult) -> bool:
                hdr_tok = self.headers.get("X-Argon-Token")
                qs = urllib.parse.parse_qs(parsed_url.query)
                q_tok = (qs.get("token") or [""])[0]
                if not daemon._check_token(hdr_tok, q_tok):
                    self._send_json(401, {"ok": False, "error": "unauthorized", "message": "missing or invalid X-Argon-Token"})
                    return False
                return True

            def _handle_websocket_upgrade(self) -> None:
                ws_key = str(self.headers.get("Sec-WebSocket-Key") or "").strip()
                if not ws_key:
                    self._send_json(400, {"ok": False, "error": "bad_websocket_handshake"})
                    return
                accept_val = base64.b64encode(
                    hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()
                ).decode("ascii")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept_val)
                self.end_headers()

                conn = self.connection
                with daemon._lock:
                    daemon._ws_clients.append(conn)

                hello_msg = json.dumps(
                    {
                        "type": "hello",
                        "version": PROTOCOL_VERSION,
                        "tabs": daemon.list_tabs(),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                _ws_write_frame(conn, hello_msg, opcode=0x1, mask=False)

                try:
                    while not daemon._stopped:
                        frame = _ws_read_frame(conn)
                        if frame is None:
                            break
                        opcode, payload = frame
                        if opcode == 0x8:
                            break
                        if opcode == 0x9:
                            _ws_write_frame(conn, payload, opcode=0xA, mask=False)
                            continue
                        if opcode != 0x1:
                            continue
                        try:
                            msg = json.loads(payload.decode("utf-8", errors="replace"))
                        except Exception:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        msg_type = str(msg.get("type") or "")
                        if msg_type == "map" and isinstance(msg.get("map"), dict):
                            t_idx = int(msg.get("tab") or 0)
                            daemon._record_pushed_map(t_idx, msg["map"])
                        elif msg_type == "action" and isinstance(msg.get("action"), dict):
                            res = daemon.execute(msg["action"])
                            resp_env = dict(res)
                            resp_env["type"] = "result"
                            if "requestId" in msg:
                                resp_env["requestId"] = msg["requestId"]
                            _ws_write_frame(
                                conn,
                                json.dumps(resp_env, ensure_ascii=False).encode("utf-8"),
                                opcode=0x1,
                                mask=False,
                            )
                finally:
                    with daemon._lock:
                        if conn in daemon._ws_clients:
                            daemon._ws_clients.remove(conn)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                if not self._authenticate(parsed):
                    return

                upgrade_hdr = str(self.headers.get("Upgrade") or "").lower()
                if parsed.path == "/live" and upgrade_hdr == "websocket":
                    self._handle_websocket_upgrade()
                    return

                if parsed.path == "/status":
                    tabs_info = daemon.list_tabs()
                    with daemon._lock:
                        maps_copy = {str(k): v for k, v in daemon._maps_by_tab.items()}
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "running": True,
                            "pid": os.getpid(),
                            "port": daemon.port,
                            "started_at": daemon.started_at,
                            "http_endpoint": daemon.http_endpoint,
                            "ws_endpoint": daemon.ws_endpoint,
                            "cdp_http_endpoint": daemon.cdp_http_endpoint,
                            "allow_eval": daemon.allow_eval,
                            "tabs": tabs_info,
                            "maps": maps_copy,
                        },
                    )
                    return

                if parsed.path == "/map":
                    qs = urllib.parse.parse_qs(parsed.query)
                    tab_idx = int((qs.get("tab") or [str(daemon._active_tab)])[0])
                    m = daemon._maps_by_tab.get(tab_idx) or daemon.build_map(tab_idx)
                    self._send_json(200, {"ok": True, "tab": tab_idx, "map": m})
                    return

                if parsed.path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    q_sse: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=64)
                    with daemon._lock:
                        daemon._sse_Queues.append(q_sse)
                    try:
                        hello_ev = json.dumps(
                            {"type": "hello", "version": PROTOCOL_VERSION, "tabs": daemon.list_tabs()},
                            ensure_ascii=False,
                        )
                        self.wfile.write(f"data: {hello_ev}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        while not daemon._stopped:
                            try:
                                item = q_sse.get(timeout=5.0)
                                if item is None:
                                    break
                                self.wfile.write(f"data: {item}\n\n".encode("utf-8"))
                                self.wfile.flush()
                            except queue.Empty:
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                    except Exception:
                        pass
                    finally:
                        with daemon._lock:
                            if q_sse in daemon._sse_Queues:
                                daemon._sse_Queues.remove(q_sse)
                    return

                self._send_json(404, {"ok": False, "error": "not_found", "path": parsed.path})

            def do_POST(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                if not self._authenticate(parsed):
                    return
                if parsed.path == "/action":
                    length = int(self.headers.get("Content-Length") or "0")
                    raw_body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
                    try:
                        action_obj = json.loads(raw_body)
                    except Exception as exc:
                        self._send_json(400, {"ok": False, "error": "invalid_json", "message": str(exc)})
                        return
                    res = daemon.execute(action_obj)
                    self._send_json(200, res)
                    return

                if parsed.path == "/stop":
                    self._send_json(200, {"ok": True, "stopping": True})
                    threading.Thread(target=daemon.stop, daemon=True).start()
                    return

                self._send_json(404, {"ok": False, "error": "not_found", "path": parsed.path})

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), _LoopRequestHandler)
        self.port = int(self._httpd.server_address[1])
        self.http_endpoint = f"http://127.0.0.1:{self.port}"
        self.ws_endpoint = f"ws://127.0.0.1:{self.port}/live"

        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="argon-browser-loop-http",
            daemon=True,
        )
        self._server_thread.start()

        state_payload = {
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "http_endpoint": self.http_endpoint,
            "ws_endpoint": self.ws_endpoint,
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(state_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return state_payload

    def stop(self) -> None:
        """Close server, detach from CDP cleanly without touching the browser profile, and remove the state file."""
        self._stopped = True
        with self._push_cond:
            for q in self._sse_Queues:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            self._sse_Queues.clear()
            for ws_sock in self._ws_clients:
                try:
                    ws_sock.close()
                except Exception:
                    pass
            self._ws_clients.clear()
            self._push_cond.notify_all()

        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

        if self._using_playwright:
            self._pw_stop_event.set()
            if self._pw_thread is not None and self._pw_thread.is_alive():
                self._pw_thread.join(timeout=3.0)
            self._pw_thread = None

        for sess in list(self._cdp_sessions):
            sess.close()
        self._cdp_sessions.clear()

        try:
            if self.state_file.is_file():
                data = json.loads(self.state_file.read_text(encoding="utf-8", errors="replace"))
                if int(data.get("pid") or 0) in (0, os.getpid()):
                    self.state_file.unlink(missing_ok=True)
        except Exception:
            try:
                self.state_file.unlink(missing_ok=True)
            except Exception:
                pass


def _read_running_daemon_state(state_file: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    sf = state_file or DEFAULT_STATE_FILE
    if not sf.is_file():
        return None
    try:
        data = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            return None
        pid = int(data.get("pid") or 0)
        from safe_browser import pid_is_alive

        if pid <= 0 or not pid_is_alive(pid):
            sf.unlink(missing_ok=True)
            return None
        return data
    except Exception:
        return None


def _http_request_daemon(
    state: Dict[str, Any],
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
) -> Tuple[int, Dict[str, Any]]:
    base = str(state.get("http_endpoint") or f"http://127.0.0.1:{state.get('port')}").rstrip("/")
    token = str(state.get("token") or "")
    url = f"{base}{path}"
    data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-Argon-Token": token}
    if data_bytes is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), json.loads(raw)
    except urllib.error.HTTPError as he:
        raw_err = he.read().decode("utf-8", errors="replace")
        try:
            parsed_err = json.loads(raw_err)
        except Exception:
            parsed_err = {"ok": False, "error": f"http_{he.code}", "message": raw_err}
        return int(he.code), parsed_err


def _parse_action_arg(raw_arg: str) -> Dict[str, Any]:
    cleaned = (raw_arg or "").strip()
    if cleaned.startswith("@"):
        file_path = Path(cleaned[1:]).expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"Action JSON file not found: {file_path}")
        cleaned = file_path.read_text(encoding="utf-8")
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Action payload must be a JSON object")
    return parsed


def run_browser_loop_selftest(allow_eval: bool = False) -> int:
    """Run the end-to-end live loop self-test against a disposable Chrome instance.

    Proves:
    1. Disposable Chrome launch (isolated temp profile + temp CDP port; never touches Argon browser).
    2. `page.expose_binding("__argonPush", ...)` (`Runtime.addBinding`) + `live_map.js` injection.
    3. Real page load (`https://example.com` when network is reachable, or explicit `data:` URL fallback).
    4. Real spontaneous `MutationObserver` -> `__argonPush` pushes (never polled by the agent) with
       measured browser -> Python latency (`min`, `median`, `max` ms), DOM node count, and element count.
    5. Real `click` action returning the fresh map in the same response.
    6. Stale-element rule: mutating the DOM element at `id: 0` causes a subsequent action with the
       old `expect` to be REFUSED with `error: "stale_element"` and the fresh map attached.
    7. `--browser-act` in both daemon-running mode and daemon-stopped (one-shot CDP attach) mode,
       plus security checks (HTTP 401 without token, non-http(s) `navigate` refusal, `eval` disabled).
    """
    chrome_exe = _find_browser_executable()
    if not chrome_exe:
        print("[ERROR] No Chrome/Chromium binary found for --browser-loop-selftest.", file=sys.stderr)
        return 1

    tmp_profile = Path(tempfile.mkdtemp(prefix="argon_loop_selftest_"))
    tmp_state_file = tmp_profile / "argon_browser_loop_selftest.json"
    cdp_port = _allocate_loop_port()
    cdp_http = f"http://127.0.0.1:{cdp_port}"
    proc: Optional[subprocess.Popen[Any]] = None
    daemon: Optional[BrowserLoopDaemon] = None

    print("=" * 78)
    print("  ARGON LIVE BROWSER CONTROL LOOP SELF-TEST (ROUND 19)")
    print("=" * 78)

    try:
        cmd = [
            str(chrome_exe),
            f"--user-data-dir={tmp_profile}",
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for CDP endpoint to come up
        cdp_ready = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{cdp_http}/json/version", timeout=1.5) as r:
                    ver_info = json.loads(r.read().decode("utf-8", errors="replace"))
                    if ver_info.get("webSocketDebuggerUrl"):
                        cdp_ready = True
                        break
            except Exception:
                time.sleep(0.1)
        if not cdp_ready:
            print(f"[ERROR] Disposable Chrome did not open CDP port {cdp_port}.", file=sys.stderr)
            return 1

        daemon = BrowserLoopDaemon(
            http_endpoint=cdp_http,
            state_file=tmp_state_file,
            allow_eval=allow_eval,
        )
        attach_info = daemon.attach(cdp_http)
        srv_info = daemon.start_server(port=0)
        print(
            f"  Disposable Chrome   : {cdp_http} (temp_profile={tmp_profile.name}, backend={attach_info.get('backend')})"
        )
        print(
            f"  Daemon endpoint     : {srv_info['http_endpoint']} (ws={srv_info['ws_endpoint']}, token={srv_info['token'][:8]}...)"
        )

        # Try loading https://example.com; fall back to a data: URL if network is unavailable.
        used_fallback_data_url = False
        load_t0 = time.monotonic()
        nav_res = daemon.execute({"op": "navigate", "url": "https://example.com/"})
        cur_map = nav_res.get("map") or {}
        has_example_link = any(
            el.get("tag") == "a" for el in (cur_map.get("elements") or [])
        )
        if not nav_res.get("ok") or not has_example_link or "example" not in str(cur_map.get("title") or "").lower():
            used_fallback_data_url = True
            fallback_html = (
                "<!doctype html><html><head><meta charset='utf-8'><title>Example Domain</title></head>"
                "<body><main><header><h1>Example Domain</h1>"
                "<p>This domain is for use in illustrative examples in documents.</p></header>"
                "<section><p><a id='learn-more' href='https://iana.org/help/example-domains' "
                "onclick=\"event.preventDefault(); document.body.setAttribute('data-clicked','Learn more');\">"
                "Learn more</a></p>"
                "<button id='btn-signin' type='button'>Sign in</button>"
                "<input id='search-input' type='text' placeholder='Search docs' value=''/>"
                "<select id='lang-select'><option value='en'>English</option><option value='bn'>Bengali</option></select>"
                "<div id='filler'>"
                + "".join(f"<span><span>node-{i}</span></span>" for i in range(40))
                + "</div></section></main></body></html>"
            )
            data_url = "data:text/html;charset=utf-8," + urllib.parse.quote(fallback_html)
            if daemon._using_playwright:
                daemon._run_on_pw_thread(lambda: daemon._pw_pages[0].goto(data_url, wait_until="domcontentloaded"))
                daemon._run_on_pw_thread(lambda: daemon._pw_pages[0].evaluate(daemon._live_map_js))
            else:
                daemon._cdp_sessions[0].call("Page.navigate", {"url": data_url})
                time.sleep(0.15)
                daemon._cdp_sessions[0].evaluate(daemon._live_map_js)
            cur_map = daemon.build_map(0)
        else:
            # Prevent external navigation on click during the self-test so the page stays on Example Domain for step 6
            prevent_nav_js = (
                "window.addEventListener('click', function(ev) {"
                " ev.preventDefault();"
                " document.body.setAttribute('data-clicked', (ev.target && ev.target.textContent) || 'clicked');"
                "}, true);"
            )
            if daemon._using_playwright:
                daemon._run_on_pw_thread(lambda: daemon._pw_pages[0].evaluate(prevent_nav_js))
            else:
                daemon._cdp_sessions[0].evaluate(prevent_nav_js)

        load_ms = int(round((time.monotonic() - load_t0) * 1000))
        if used_fallback_data_url:
            print(
                f"  Page load           : {load_ms} ms (outbound network unavailable; fell back to data: URL for Example Domain)"
            )
        else:
            print(f"  Page load           : {load_ms} ms (loaded live https://example.com/)")

        # Prove spontaneous push from MutationObserver -> window.__argonPush with ZERO polling from the agent
        with daemon._push_cond:
            before_pushes = len(daemon._push_history)

        # Schedule 5 autonomous DOM mutations inside the page itself; Python does NOT call build_map or execute
        spontaneous_mutation_js = (
            "(() => {"
            " for (let i = 1; i <= 5; i++) {"
            "   setTimeout(() => {"
            "     const b = document.createElement('button');"
            "     b.className = 'spontaneous-btn';"
            "     b.textContent = 'Dynamic Step ' + i;"
            "     document.body.appendChild(b);"
            "   }, i * 35);"
            " }"
            " return true;"
            "})()"
        )
        if daemon._using_playwright:
            daemon._run_on_pw_thread(lambda: daemon._pw_pages[0].evaluate(spontaneous_mutation_js))
        else:
            daemon._cdp_sessions[0].evaluate(spontaneous_mutation_js)

        deadline_push = time.monotonic() + 4.0
        while time.monotonic() < deadline_push:
            with daemon._push_cond:
                mutation_pushes = [
                    p for p in daemon._push_history[before_pushes:]
                    if p.get("reason") == "mutation"
                ]
                if len(mutation_pushes) >= 3:
                    break
                daemon._push_cond.wait(timeout=0.05)

        with daemon._push_cond:
            all_latencies = [float(p["latency_ms"]) for p in daemon._push_history if "latency_ms" in p]
            spontaneous_pushes = [
                p for p in daemon._push_history[before_pushes:]
                if p.get("reason") == "mutation"
            ]
            latest_pushed_map = dict(daemon._maps_by_tab.get(0) or cur_map)

        if not spontaneous_pushes:
            print("[ERROR] Expected spontaneous MutationObserver push via __argonPush, but none arrived!", file=sys.stderr)
            return 1

        min_lat = round(min(all_latencies), 2)
        med_lat = round(statistics.median(all_latencies), 2)
        max_lat = round(max(all_latencies), 2)
        dom_nodes = int(latest_pushed_map.get("domNodes") or 0)
        elem_count = int(latest_pushed_map.get("count") or len(latest_pushed_map.get("elements") or []))
        print(
            f"  Spontaneous pushes  : {len(spontaneous_pushes)} unpolled MutationObserver push(es) "
            f"(min {min_lat} ms / median {med_lat} ms / max {max_lat} ms, n={len(all_latencies)})"
        )
        print(
            f"  DOM compression     : {dom_nodes} total DOM nodes -> {elem_count} interactive elements "
            f"(only interactive elements in map)"
        )
        if elem_count <= 0 or elem_count >= dom_nodes:
            print(
                f"[ERROR] Map compression check failed: domNodes={dom_nodes}, count={elem_count}",
                file=sys.stderr,
            )
            return 1

        # Step 5: Perform a real click action through the HTTP daemon endpoint (--browser-act with daemon running)
        el0 = (latest_pushed_map.get("elements") or [])[0]
        el0_tag = str(el0.get("tag") or "a")
        el0_name = str(el0.get("name") or "Learn more")
        gen_before_click = int(latest_pushed_map.get("gen") or 0)

        status_code, click_res = _http_request_daemon(
            srv_info,
            "POST",
            "/action",
            {
                "op": "click",
                "id": 0,
                "expect": {"tag": el0_tag, "name": el0_name},
                "tab": 0,
            },
        )
        if status_code != 200 or not click_res.get("ok") or not isinstance(click_res.get("map"), dict):
            print(f"[ERROR] Real click action failed: {click_res}", file=sys.stderr)
            return 1
        gen_after_click = int(click_res["map"].get("gen") or 0)
        if gen_after_click <= gen_before_click:
            print(
                f"[ERROR] Click response did not carry a fresh map generation ({gen_before_click} -> {gen_after_click})",
                file=sys.stderr,
            )
            return 1
        fresh_click_map = click_res["map"]
        fresh_els_preview = [
            {"id": e.get("id"), "tag": e.get("tag"), "name": e.get("name")}
            for e in (fresh_click_map.get("elements") or [])[:4]
        ]
        print(
            f"  Action execution    : OK ({click_res.get('note')}, elapsed={click_res.get('elapsed_ms')} ms, "
            f"fresh map gen {gen_before_click} -> {gen_after_click})"
        )
        print(
            f"  Fresh map snapshot  : gen={fresh_click_map.get('gen')}, title={fresh_click_map.get('title')!r}, "
            f"count={fresh_click_map.get('count')}, sample_elements={fresh_els_preview}"
        )

        # Step 6: Prove the stale-element rule by mutating element 0 in the DOM and sending the old expect
        mutate_el0_js = (
            "(() => {"
            " const firstA = document.querySelector('a[href]');"
            " if (firstA) { firstA.textContent = 'Home'; firstA.setAttribute('aria-label', 'Home'); }"
            " return true;"
            "})()"
        )
        if daemon._using_playwright:
            daemon._run_on_pw_thread(lambda: daemon._pw_pages[0].evaluate(mutate_el0_js))
        else:
            daemon._cdp_sessions[0].evaluate(mutate_el0_js)

        _, stale_res = _http_request_daemon(
            srv_info,
            "POST",
            "/action",
            {
                "op": "click",
                "id": 0,
                "expect": {"tag": el0_tag, "name": el0_name},
                "tab": 0,
            },
        )
        if (
            stale_res.get("ok") is not False
            or stale_res.get("error") != "stale_element"
            or not isinstance(stale_res.get("map"), dict)
        ):
            print(f"[ERROR] Stale-element guard did not refuse mismatched expect: {stale_res}", file=sys.stderr)
            return 1
        print(
            f"  Stale-element guard : OK (refused with error='stale_element', message=\"{stale_res.get('message')}\", "
            f"fresh map gen={stale_res['map'].get('gen')})"
        )

        # Security checks:
        # 1) Request without token must return 401
        unauth_req = urllib.request.Request(f"{srv_info['http_endpoint']}/status", method="GET")
        got_401 = False
        try:
            urllib.request.urlopen(unauth_req, timeout=3.0)
        except urllib.error.HTTPError as he:
            got_401 = he.code == 401
        if not got_401:
            print("[ERROR] HTTP API did not reject unauthenticated request with 401!", file=sys.stderr)
            return 1

        # 2) navigate to non-http(s) URL must be refused
        _, bad_nav_res = _http_request_daemon(
            srv_info,
            "POST",
            "/action",
            {"op": "navigate", "url": "javascript:alert(1)"},
        )
        if bad_nav_res.get("ok") is not False or bad_nav_res.get("error") != "invalid_url_scheme":
            print(f"[ERROR] navigate did not refuse javascript: URL: {bad_nav_res}", file=sys.stderr)
            return 1

        # 3) eval must be refused when allow_eval is False
        if not allow_eval:
            _, eval_res = _http_request_daemon(
                srv_info,
                "POST",
                "/action",
                {"op": "eval", "js": "1 + 1"},
            )
            if eval_res.get("ok") is not False or eval_res.get("error") != "eval_disabled":
                print(f"[ERROR] eval was not refused by default: {eval_res}", file=sys.stderr)
                return 1

        print(
            "  Security guards     : OK (HTTP 401 without token, non-http(s) navigate refused, eval refused by default)"
        )

        # Stop the running daemon and verify one-shot --browser-act when daemon is stopped
        daemon.stop()
        daemon = None

        oneshot = BrowserLoopDaemon(
            http_endpoint=cdp_http,
            state_file=tmp_state_file,
            allow_eval=allow_eval,
        )
        oneshot.attach(cdp_http)
        oneshot_res = oneshot.execute({"op": "map", "tab": 0})
        oneshot.stop()
        if not oneshot_res.get("ok") or not isinstance(oneshot_res.get("map"), dict):
            print(f"[ERROR] One-shot action with daemon stopped failed: {oneshot_res}", file=sys.stderr)
            return 1
        print(
            f"  One-shot attach     : OK (--browser-act works with daemon running and daemon stopped, count={oneshot_res['map'].get('count')})"
        )
        print("=" * 78)
        return 0
    finally:
        if daemon is not None:
            try:
                daemon.stop()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        for _ in range(5):
            shutil.rmtree(tmp_profile, ignore_errors=True)
            if not tmp_profile.exists():
                break
            time.sleep(0.1)


def browser_loop_cli(
    start: bool = False,
    stop: bool = False,
    status: bool = False,
    act: Optional[str] = None,
    selftest: bool = False,
    allow_eval: bool = False,
    slot: Optional[str] = None,
    profile_dir: Optional[Union[str, Path]] = None,
    state_file: Optional[Union[str, Path]] = None,
) -> int:
    """CLI entry point for `--browser-loop-start`, `--browser-loop-stop`,
    `--browser-loop-status`, `--browser-act`, and `--browser-loop-selftest`.
    """
    if allow_eval:
        print(
            "[!] WARNING: --browser-allow-eval is enabled; arbitrary JavaScript evaluation ('op': 'eval') is permitted.",
            file=sys.stderr,
        )

    sf = Path(state_file).expanduser().resolve() if state_file else DEFAULT_STATE_FILE

    if selftest:
        return run_browser_loop_selftest(allow_eval=allow_eval)

    if stop:
        st = _read_running_daemon_state(sf)
        if not st:
            sf.unlink(missing_ok=True)
            print("[+] Argon browser loop daemon is not running.")
            return 0
        try:
            _http_request_daemon(st, "POST", "/stop", {})
        except Exception:
            pass
        pid = int(st.get("pid") or 0)
        if pid > 0 and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        sf.unlink(missing_ok=True)
        print(f"[+] Stopped Argon browser loop daemon (pid={pid}, port={st.get('port')}).")
        return 0

    if status:
        st = _read_running_daemon_state(sf)
        if not st:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "running": False,
                        "state_file": str(sf),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        code, payload = _http_request_daemon(st, "GET", "/status")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if code == 200 and payload.get("ok") else 1

    if act is not None:
        try:
            action_obj = _parse_action_arg(act)
        except Exception as exc:
            err_out = {"ok": False, "error": "invalid_action_json", "message": str(exc)}
            print(json.dumps(err_out, indent=2, ensure_ascii=False))
            return 1

        st = _read_running_daemon_state(sf)
        if st:
            code, res = _http_request_daemon(st, "POST", "/action", action_obj)
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if code == 200 and res.get("ok") else 1

        daemon = BrowserLoopDaemon(
            state_file=sf,
            allow_eval=allow_eval,
            slot=slot,
            profile_dir=profile_dir,
        )
        try:
            daemon.attach()
            res = daemon.execute(action_obj)
        except Exception as exc:
            res = {
                "ok": False,
                "op": str(action_obj.get("op") or "map"),
                "error": "attach_failed",
                "message": str(exc),
            }
        finally:
            daemon.stop()
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if start:
        daemon = BrowserLoopDaemon(
            state_file=sf,
            allow_eval=allow_eval,
            slot=slot,
            profile_dir=profile_dir,
        )
        try:
            attach_info = daemon.attach()
            srv = daemon.start_server(port=0)
            print("=" * 78)
            print("  ARGON LIVE BROWSER CONTROL LOOP DAEMON RUNNING")
            print("=" * 78)
            print(f"  HTTP endpoint : {srv['http_endpoint']}")
            print(f"  WS endpoint   : {srv['ws_endpoint']}")
            print(f"  Token prefix  : {srv['token'][:8]}... (stored in {sf})")
            print(f"  Attached tabs : {attach_info.get('tabs')} (backend={attach_info.get('backend')})")
            print("=" * 78)
            while not daemon._stopped:
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\n[+] Stopping Argon browser loop daemon...")
        finally:
            daemon.stop()
        return 0

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Argon Live Browser Control Loop Daemon")
    parser.add_argument("--browser-loop-start", action="store_true", help="Start the live browser control loop daemon.")
    parser.add_argument("--browser-loop-stop", action="store_true", help="Stop a running live browser control loop daemon.")
    parser.add_argument("--browser-loop-status", action="store_true", help="Print the daemon state, tabs, and live maps.")
    parser.add_argument("--browser-act", default=None, metavar="JSON", help="Execute one browser action (JSON string or @path).")
    parser.add_argument("--browser-loop-selftest", action="store_true", help="Run the end-to-end live loop self-test on a disposable Chrome.")
    parser.add_argument("--browser-allow-eval", action="store_true", help="Allow the 'eval' action operation (off by default).")
    parser.add_argument("--slot", default=None, help="Optional Argon account slot.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return browser_loop_cli(
        start=bool(args.browser_loop_start),
        stop=bool(args.browser_loop_stop),
        status=bool(args.browser_loop_status),
        act=args.browser_act,
        selftest=bool(args.browser_loop_selftest),
        allow_eval=bool(args.browser_allow_eval),
        slot=args.slot,
    )


if __name__ == "__main__":
    sys.exit(main())