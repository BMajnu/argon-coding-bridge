#!/usr/bin/env python3
"""
Tkinter GUI Login Assistant for the Gemini 4 Argon Multi-Account Pool.

Features:
- Account card displaying Slot (/u/N/), Role, Tier, and Status badge
- Email and Password fields with Show/Hide toggle and Copy buttons
- Previous / Next account navigation
- "Open Google Add Account Page" and "Open in Gemini Web" buttons
- "Auto Login All" button running GoogleLogin.login_pool in a background thread with
  thread-safe UI updates marshalled via root.after
- "Add Account" dialog (email, password with show/hide, tier, role) that appends the
  credential to accounts_pool.json with the next free slot and logs it in automatically
- In-app modal alert when a human verification challenge or OTP prompt appears; login
  resumes automatically as soon as the user approves on their phone
- Live status line
"""
from __future__ import annotations

import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, List, Optional

from argon_pool_login import (
    CHROME_LAUNCH_ARGS,
    GEMINI_WEB_URL,
    GOOGLE_ADD_SESSION_URL,
    STATUS_OK,
    GoogleLogin,
    LoginResult,
    PoolConfig,
    find_chrome_executable,
    is_profile_busy,
    seed_slot_profile,
)


class AddAccountDialog(tk.Toplevel):
    """Modal dialog to add a new Google Pro account to the pool and auto-login."""

    def __init__(
        self,
        parent: tk.Tk,
        next_slot: str,
        on_submit_callback: Any,
    ) -> None:
        super().__init__(parent)
        self.title(f"Add Account to Pool ({next_slot})")
        self.configure(bg="#0f172a")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._on_submit = on_submit_callback
        self._show_pwd = False

        container = tk.Frame(self, bg="#0f172a", padx=20, pady=18)
        container.pack(fill="both", expand=True)

        header = tk.Label(
            container,
            text=f"Add New Pool Credential (Slot {next_slot})",
            font=("Segoe UI", 12, "bold"),
            fg="#f8fafc",
            bg="#0f172a",
        )
        header.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        tk.Label(
            container,
            text="Google Email:",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#0f172a",
        ).grid(row=1, column=0, sticky="w", pady=6)
        self.email_var = tk.StringVar()
        self.email_entry = tk.Entry(
            container,
            textvariable=self.email_var,
            font=("Consolas", 10),
            width=34,
            bg="#1e293b",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
        )
        self.email_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=6, ipady=4)

        tk.Label(
            container,
            text="Password:",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#0f172a",
        ).grid(row=2, column=0, sticky="w", pady=6)
        self.password_var = tk.StringVar()
        self.password_entry = tk.Entry(
            container,
            textvariable=self.password_var,
            show="*",
            font=("Consolas", 10),
            width=26,
            bg="#1e293b",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
        )
        self.password_entry.grid(row=2, column=1, sticky="ew", padx=(8, 4), pady=6, ipady=4)

        self.toggle_pwd_btn = tk.Button(
            container,
            text="Show",
            command=self._toggle_password,
            font=("Segoe UI", 8, "bold"),
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            activeforeground="#ffffff",
            relief="flat",
            width=6,
            cursor="hand2",
        )
        self.toggle_pwd_btn.grid(row=2, column=2, sticky="e", pady=6, ipady=2)

        tk.Label(
            container,
            text="Tier:",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#0f172a",
        ).grid(row=3, column=0, sticky="w", pady=6)
        self.tier_var = tk.StringVar(value="PRO")
        self.tier_entry = tk.Entry(
            container,
            textvariable=self.tier_var,
            font=("Segoe UI", 10),
            width=34,
            bg="#1e293b",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
        )
        self.tier_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=6, ipady=4)

        tk.Label(
            container,
            text="Role:",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#0f172a",
        ).grid(row=4, column=0, sticky="w", pady=6)
        self.role_var = tk.StringVar(value="Pool Member")
        self.role_entry = tk.Entry(
            container,
            textvariable=self.role_var,
            font=("Segoe UI", 10),
            width=34,
            bg="#1e293b",
            fg="#f8fafc",
            insertbackground="#f8fafc",
            relief="flat",
        )
        self.role_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=6, ipady=4)

        btn_row = tk.Frame(container, bg="#0f172a")
        btn_row.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))

        cancel_btn = tk.Button(
            btn_row,
            text="Cancel",
            command=self.destroy,
            font=("Segoe UI", 9),
            bg="#334155",
            fg="#e2e8f0",
            relief="flat",
            padx=12,
            pady=5,
            cursor="hand2",
        )
        cancel_btn.pack(side="right", padx=(8, 0))

        save_btn = tk.Button(
            btn_row,
            text="Save & Auto Login",
            command=self._submit,
            font=("Segoe UI", 9, "bold"),
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            padx=14,
            pady=5,
            cursor="hand2",
        )
        save_btn.pack(side="right")

        self.email_entry.focus_set()

    def _toggle_password(self) -> None:
        self._show_pwd = not self._show_pwd
        self.password_entry.configure(show="" if self._show_pwd else "*")
        self.toggle_pwd_btn.configure(text="Hide" if self._show_pwd else "Show")

    def _submit(self) -> None:
        email = self.email_var.get().strip()
        password = self.password_var.get()
        tier = self.tier_var.get().strip() or "PRO"
        role = self.role_var.get().strip() or "Pool Member"

        if not email or "@" not in email:
            messagebox.showerror("Invalid Email", "Please enter a valid Google email address.", parent=self)
            return
        if not password:
            messagebox.showerror("Missing Password", "Please enter the account password.", parent=self)
            return

        self.destroy()
        self._on_submit(email, password, tier, role)


class ChallengeModalAlert(tk.Toplevel):
    """
    In-app modal alert shown when Google presents a 2FA / phone verification / OTP challenge.
    Because it is marshalled onto the Tkinter main thread via root.after while Playwright polls
    in the background thread, the login resumes automatically as soon as the user approves.
    """

    def __init__(self, parent: tk.Tk, email: str, reason: str) -> None:
        super().__init__(parent)
        self.title("Phone / 2FA Verification Required")
        self.configure(bg="#1e1b4b")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = tk.Frame(self, bg="#1e1b4b", padx=22, pady=18)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="Human Verification Required",
            font=("Segoe UI", 12, "bold"),
            fg="#fbbf24",
            bg="#1e1b4b",
        ).pack(anchor="w", pady=(0, 8))

        msg = (
            f"Account: {email}\n"
            f"Detected: {reason}\n\n"
            "Please check your phone (or the open Chrome window) and approve the Google "
            "verification prompt or enter the OTP code.\n\n"
            "Keep the Chrome window open — the automatic login is polling in the background "
            "and will resume by itself as soon as you approve."
        )
        tk.Label(
            frame,
            text=msg,
            font=("Segoe UI", 9),
            fg="#f8fafc",
            bg="#1e1b4b",
            justify="left",
            wraplength=440,
        ).pack(anchor="w", pady=(0, 14))

        ok_btn = tk.Button(
            frame,
            text="OK, Approving on Phone",
            command=self.destroy,
            font=("Segoe UI", 9, "bold"),
            bg="#f59e0b",
            fg="#0f172a",
            activebackground="#d97706",
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
        )
        ok_btn.pack(anchor="e")


class PoolLoginAssistantApp:
    """Main Tkinter GUI for managing and signing into the Gemini 4 Argon Account Pool."""

    def __init__(self, root: tk.Tk, config_path: Optional[str] = None) -> None:
        self.root = root
        self.pool_config = PoolConfig(config_path=config_path)
        self.current_index = 0
        self.password_visible = False
        self._worker_running = False
        self._active_challenge_modal: Optional[ChallengeModalAlert] = None

        self.root.title("Gemini 4 Argon — Multi-Account Pool Login Assistant")
        self.root.geometry("650x540")
        self.root.configure(bg="#0f172a")
        self.root.resizable(False, False)

        self._build_ui()
        self._refresh_account_card()

    def _build_ui(self) -> None:
        header_frame = tk.Frame(self.root, bg="#0f172a", padx=22, pady=14)
        header_frame.pack(fill="x")

        tk.Label(
            header_frame,
            text="Gemini 4 Argon Account Pool Assistant",
            font=("Segoe UI", 14, "bold"),
            fg="#f8fafc",
            bg="#0f172a",
        ).pack(anchor="w")

        self.profile_dir_label = tk.Label(
            header_frame,
            text=f"Slot Profile: {self.pool_config.profile_dir_for_slot()}",
            font=("Consolas", 8),
            fg="#64748b",
            bg="#0f172a",
        )
        self.profile_dir_label.pack(anchor="w", pady=(2, 0))

        card = tk.Frame(
            self.root,
            bg="#1e293b",
            padx=20,
            pady=16,
            highlightbackground="#334155",
            highlightthickness=1,
        )
        card.pack(fill="x", padx=22, pady=(2, 10))

        badge_row = tk.Frame(card, bg="#1e293b")
        badge_row.pack(fill="x", pady=(0, 12))

        self.slot_label = tk.Label(
            badge_row,
            text="Slot /u/0/",
            font=("Segoe UI", 11, "bold"),
            fg="#38bdf8",
            bg="#1e293b",
        )
        self.slot_label.pack(side="left")

        self.tier_label = tk.Label(
            badge_row,
            text="PRO",
            font=("Segoe UI", 9, "bold"),
            fg="#0f172a",
            bg="#34d399",
            padx=8,
            pady=2,
        )
        self.tier_label.pack(side="left", padx=(10, 6))

        self.role_label = tk.Label(
            badge_row,
            text="Active Primary",
            font=("Segoe UI", 9),
            fg="#cbd5e1",
            bg="#334155",
            padx=8,
            pady=2,
        )
        self.role_label.pack(side="left")

        self.counter_label = tk.Label(
            badge_row,
            text="1 / 7",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#1e293b",
        )
        self.counter_label.pack(side="right")

        email_row = tk.Frame(card, bg="#1e293b")
        email_row.pack(fill="x", pady=5)

        tk.Label(
            email_row,
            text="Email:",
            width=10,
            anchor="w",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#1e293b",
        ).pack(side="left")

        self.email_var = tk.StringVar()
        self.email_entry = tk.Entry(
            email_row,
            textvariable=self.email_var,
            font=("Consolas", 10),
            bg="#0f172a",
            fg="#f8fafc",
            readonlybackground="#0f172a",
            relief="flat",
            state="readonly",
        )
        self.email_entry.pack(side="left", fill="x", expand=True, padx=(4, 8), ipady=5)

        tk.Button(
            email_row,
            text="Copy Email",
            command=self.copy_email,
            font=("Segoe UI", 8, "bold"),
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            relief="flat",
            width=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right")

        pwd_row = tk.Frame(card, bg="#1e293b")
        pwd_row.pack(fill="x", pady=5)

        tk.Label(
            pwd_row,
            text="Password:",
            width=10,
            anchor="w",
            font=("Segoe UI", 9, "bold"),
            fg="#94a3b8",
            bg="#1e293b",
        ).pack(side="left")

        self.password_var = tk.StringVar()
        self.password_entry = tk.Entry(
            pwd_row,
            textvariable=self.password_var,
            show="*",
            font=("Consolas", 10),
            bg="#0f172a",
            fg="#f8fafc",
            readonlybackground="#0f172a",
            relief="flat",
            state="readonly",
        )
        self.password_entry.pack(side="left", fill="x", expand=True, padx=(4, 8), ipady=5)

        tk.Button(
            pwd_row,
            text="Copy Password",
            command=self.copy_password,
            font=("Segoe UI", 8, "bold"),
            bg="#334155",
            fg="#f8fafc",
            activebackground="#475569",
            relief="flat",
            width=12,
            pady=4,
            cursor="hand2",
        ).pack(side="right")

        self.show_pwd_btn = tk.Button(
            pwd_row,
            text="Show",
            command=self.toggle_password,
            font=("Segoe UI", 8, "bold"),
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            relief="flat",
            width=7,
            pady=4,
            cursor="hand2",
        )
        self.show_pwd_btn.pack(side="right", padx=(0, 6))

        nav_frame = tk.Frame(self.root, bg="#0f172a", padx=22)
        nav_frame.pack(fill="x", pady=(2, 8))

        self.prev_btn = tk.Button(
            nav_frame,
            text="◀ Previous",
            command=self.prev_account,
            font=("Segoe UI", 9, "bold"),
            bg="#1e293b",
            fg="#e2e8f0",
            activebackground="#334155",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.prev_btn.pack(side="left")

        self.next_btn = tk.Button(
            nav_frame,
            text="Next ▶",
            command=self.next_account,
            font=("Segoe UI", 9, "bold"),
            bg="#1e293b",
            fg="#e2e8f0",
            activebackground="#334155",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        )
        self.next_btn.pack(side="left", padx=(8, 0))

        self.add_account_btn = tk.Button(
            nav_frame,
            text="＋ Add Account",
            command=self.open_add_account_dialog,
            font=("Segoe UI", 9, "bold"),
            bg="#0284c7",
            fg="#ffffff",
            activebackground="#0369a1",
            relief="flat",
            padx=14,
            pady=6,
            cursor="hand2",
        )
        self.add_account_btn.pack(side="right")

        self.auto_login_all_btn = tk.Button(
            nav_frame,
            text="⚡ Auto Login All",
            command=self.start_auto_login_all,
            font=("Segoe UI", 9, "bold"),
            bg="#16a34a",
            fg="#ffffff",
            activebackground="#15803d",
            relief="flat",
            padx=16,
            pady=6,
            cursor="hand2",
        )
        self.auto_login_all_btn.pack(side="right", padx=(0, 8))

        launch_frame = tk.Frame(self.root, bg="#0f172a", padx=22)
        launch_frame.pack(fill="x", pady=(4, 8))

        tk.Button(
            launch_frame,
            text="Open Google Add Account Page",
            command=self.open_add_account_page,
            font=("Segoe UI", 9, "bold"),
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            relief="flat",
            pady=8,
            cursor="hand2",
        ).pack(fill="x", pady=(0, 6))

        sub_launch = tk.Frame(launch_frame, bg="#0f172a")
        sub_launch.pack(fill="x")

        tk.Button(
            sub_launch,
            text="Open in Gemini Web",
            command=self.open_gemini_web,
            font=("Segoe UI", 9, "bold"),
            bg="#4f46e5",
            fg="#ffffff",
            activebackground="#4338ca",
            relief="flat",
            pady=7,
            cursor="hand2",
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))

        status_frame = tk.Frame(self.root, bg="#0f172a", padx=22, pady=8)
        status_frame.pack(fill="x", side="bottom")

        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            status_frame,
            variable=self.progress_var,
            maximum=100.0,
            mode="determinate",
        )
        self.progress_bar.pack(fill="x", pady=(0, 6))

        self.status_var = tk.StringVar(value="Ready. Select an account or click 'Auto Login All'.")
        self.status_label = tk.Label(
            status_frame,
            textvariable=self.status_var,
            font=("Segoe UI", 9),
            fg="#38bdf8",
            bg="#0f172a",
            anchor="w",
        )
        self.status_label.pack(fill="x")

    def set_status(self, message: str, color: str = "#38bdf8") -> None:
        """Update the live status line (must be called on the Tkinter main thread)."""
        self.status_var.set(message)
        self.status_label.configure(fg=color)

    def set_status_threadsafe(self, message: str, color: str = "#38bdf8") -> None:
        """Marshal a status update onto the Tkinter main thread via root.after."""
        self.root.after(0, lambda: self.set_status(message, color))

    def _current_account(self) -> Optional[Dict[str, Any]]:
        accounts = self.pool_config.accounts
        if not accounts:
            return None
        self.current_index = max(0, min(self.current_index, len(accounts) - 1))
        return accounts[self.current_index]

    def _refresh_account_card(self) -> None:
        acc = self._current_account()
        total = len(self.pool_config.accounts)
        if not acc:
            self.slot_label.configure(text="No Accounts")
            self.tier_label.configure(text="-")
            self.role_label.configure(text="-")
            self.counter_label.configure(text="0 / 0")
            self.email_var.set("")
            self.password_var.set("")
            return

        slot = acc.get("slot", f"/u/{self.current_index}/")
        tier = acc.get("tier", "PRO")
        role = acc.get("role", "Pool Member")
        status = acc.get("status", "configured")

        self.slot_label.configure(text=f"Slot {slot}")
        self.tier_label.configure(text=tier)
        self.role_label.configure(text=f"{role}  •  {status}")
        self.counter_label.configure(text=f"Account {self.current_index + 1} of {total}")
        self.email_var.set(acc.get("email", ""))
        self.password_var.set(acc.get("password", ""))
        try:
            self.profile_dir_label.configure(
                text=f"Slot Profile: {self.pool_config.profile_dir_for_slot(slot)}"
            )
        except Exception:
            pass

    def toggle_password(self) -> None:
        self.password_visible = not self.password_visible
        self.password_entry.configure(show="" if self.password_visible else "*")
        self.show_pwd_btn.configure(text="Hide" if self.password_visible else "Show")

    def copy_email(self) -> None:
        acc = self._current_account()
        if not acc:
            return
        email = acc.get("email", "")
        self.root.clipboard_clear()
        self.root.clipboard_append(email)
        self.set_status(f"Copied email for {acc.get('slot', '')}: {email}", "#34d399")

    def copy_password(self) -> None:
        acc = self._current_account()
        if not acc:
            return
        pwd = acc.get("password", "")
        self.root.clipboard_clear()
        self.root.clipboard_append(pwd)
        self.set_status(f"Copied password for {acc.get('email', '')} to clipboard.", "#34d399")

    def prev_account(self) -> None:
        if not self.pool_config.accounts:
            return
        self.current_index = (self.current_index - 1) % len(self.pool_config.accounts)
        self._refresh_account_card()
        acc = self._current_account()
        if acc:
            self.set_status(f"Viewing {acc.get('slot')} ({acc.get('email')}).")

    def next_account(self) -> None:
        if not self.pool_config.accounts:
            return
        self.current_index = (self.current_index + 1) % len(self.pool_config.accounts)
        self._refresh_account_card()
        acc = self._current_account()
        if acc:
            self.set_status(f"Viewing {acc.get('slot')} ({acc.get('email')}).")

    def _launch_chrome_url(self, url: str, description: str, slot: Optional[str] = None) -> None:
        acc = self._current_account()
        target_slot = slot or (acc.get("slot", "/u/0/") if acc else self.pool_config.active_slot())
        profile_dir = self.pool_config.profile_dir_for_slot(target_slot)
        if is_profile_busy(profile_dir):
            self.set_status(
                f"Slot {target_slot} profile ({profile_dir}) is busy in another run.",
                "#fbbf24",
            )
            return
        if not (profile_dir.is_dir() and (profile_dir / "Default").is_dir()):
            seed_slot_profile(self.pool_config.profile_dir, profile_dir)
        profile_dir.mkdir(parents=True, exist_ok=True)
        chrome_exe = find_chrome_executable()
        if chrome_exe:
            try:
                subprocess.Popen(
                    [
                        chrome_exe,
                        f"--user-data-dir={profile_dir}",
                        *CHROME_LAUNCH_ARGS,
                        url,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.set_status(
                    f"Opened {description} in Google Chrome (slot profile {profile_dir.name}).",
                    "#34d399",
                )
                return
            except Exception as exc:
                self.set_status(f"Failed to launch Chrome directly: {exc}", "#f87171")

        def _bg() -> None:
            try:
                gl = GoogleLogin(profile_dir=profile_dir, pool_config=self.pool_config, slot=target_slot)
                page = gl._active_page()
                page.goto(url, wait_until="domcontentloaded")
                self.set_status_threadsafe(
                    f"Opened {description} in Playwright Chromium ({profile_dir.name}).",
                    "#34d399",
                )
            except Exception as exc:
                self.set_status_threadsafe(f"Error opening browser: {exc}", "#f87171")

        threading.Thread(target=_bg, daemon=True).start()

    def open_add_account_page(self) -> None:
        acc = self._current_account()
        slot = acc.get("slot", "/u/0/") if acc else self.pool_config.active_slot()
        if acc:
            self.copy_email()
        self._launch_chrome_url(GOOGLE_ADD_SESSION_URL, "Google Add Account Page", slot=slot)

    def open_gemini_web(self) -> None:
        acc = self._current_account()
        slot = self.pool_config.set_active_slot(acc.get("slot", "/u/0/")) if acc else self.pool_config.active_slot()
        url = GEMINI_WEB_URL.format(slot=slot)
        self._launch_chrome_url(url, f"Gemini Web ({url})", slot=slot)

    def open_ai_studio(self) -> None:
        """Removed: Google AI Studio is no longer supported.

        AI Studio consistently fails on agentic work, so the whole workflow was
        removed. Use "Open in Gemini Web" instead.
        """
        messagebox.showinfo(
            "Google AI Studio removed",
            "Google AI Studio is no longer supported - it consistently fails on "
            "agentic work.\n\nUse 'Open in Gemini Web' instead.",
            parent=self.root,
        )

    def _show_challenge_modal_on_ui(self, email: str, reason: str) -> None:
        """Called on main thread via root.after when a challenge or OTP prompt is detected."""
        self.set_status(
            f"Verification required for {email} ({reason}). Approve on your phone to continue...",
            "#fbbf24",
        )
        try:
            if self._active_challenge_modal is not None and self._active_challenge_modal.winfo_exists():
                self._active_challenge_modal.destroy()
        except Exception:
            pass
        self._active_challenge_modal = ChallengeModalAlert(self.root, email, reason)

    def _close_challenge_modal_on_ui(self) -> None:
        try:
            if self._active_challenge_modal is not None and self._active_challenge_modal.winfo_exists():
                self._active_challenge_modal.destroy()
        except Exception:
            pass
        self._active_challenge_modal = None

    def start_auto_login_all(self) -> None:
        """Run GoogleLogin.login_pool across all pool accounts in a background thread."""
        if self._worker_running:
            self.set_status("An automatic sign-in task is already running...", "#fbbf24")
            return

        accounts = list(self.pool_config.accounts)
        if not accounts:
            self.set_status("No accounts configured in accounts_pool.json.", "#f87171")
            return

        self._run_accounts_login_in_background(accounts, label="Auto Login All")

    def open_add_account_dialog(self) -> None:
        """Show the Add Account dialog, append to pool config, and auto-login the new account."""
        if self._worker_running:
            self.set_status("Please wait for the current login task to finish first.", "#fbbf24")
            return
        next_idx = self.pool_config.next_index()
        next_slot = f"/u/{next_idx}/"
        AddAccountDialog(self.root, next_slot=next_slot, on_submit_callback=self._on_account_added)

    def _on_account_added(self, email: str, password: str, tier: str, role: str) -> None:
        new_entry = self.pool_config.add_account(email=email, password=password, tier=tier, role=role)
        self.current_index = len(self.pool_config.accounts) - 1
        self._refresh_account_card()
        self.set_status(
            f"Added {new_entry['email']} at {new_entry['slot']}. Starting automatic sign-in...",
            "#34d399",
        )
        self._run_accounts_login_in_background([new_entry], label=f"Login {new_entry['slot']}")

    def _run_accounts_login_in_background(
        self,
        accounts_to_login: List[Dict[str, Any]],
        label: str = "Auto Login",
    ) -> None:
        self._worker_running = True
        self.auto_login_all_btn.configure(state="disabled")
        self.add_account_btn.configure(state="disabled")
        self.progress_var.set(0.0)

        def on_progress(
            idx: int,
            count: int,
            acc: Dict[str, Any],
            result: Optional[LoginResult],
        ) -> None:
            email = acc.get("email", "")
            slot = acc.get("slot", f"/u/{idx}/")
            if result is None:
                pct = (idx / max(count, 1)) * 100.0
                msg = f"[{idx + 1}/{count}] Signing in {slot} ({email})..."

                def _ui_start() -> None:
                    self.progress_var.set(pct)
                    self.set_status(msg, "#38bdf8")
                    for i, pool_acc in enumerate(self.pool_config.accounts):
                        if pool_acc.get("slot") == slot:
                            self.current_index = i
                            self._refresh_account_card()
                            break

                self.root.after(0, _ui_start)
            else:
                pct = ((idx + 1) / max(count, 1)) * 100.0
                color = "#34d399" if result.status == STATUS_OK else "#fbbf24"
                msg = f"[{idx + 1}/{count}] {slot} ({email}): {result.status} — {result.detail}"

                def _ui_done() -> None:
                    self._close_challenge_modal_on_ui()
                    self.progress_var.set(pct)
                    self._refresh_account_card()
                    self.set_status(msg, color)

                self.root.after(0, _ui_done)

        def on_challenge(email: str, reason: str) -> None:
            self.root.after(0, lambda: self._show_challenge_modal_on_ui(email, reason))

        def on_status(msg: str) -> None:
            self.root.after(0, lambda: self.set_status(msg, "#38bdf8"))

        def worker() -> None:
            results: List[LoginResult] = []
            first_slot = (
                accounts_to_login[0].get("slot", "/u/0/")
                if accounts_to_login
                else self.pool_config.active_slot()
            )
            gl = GoogleLogin(
                profile_dir=self.pool_config.profile_dir_for_slot(first_slot),
                pool_config=self.pool_config,
                slot=first_slot,
            )
            try:
                results = gl.login_pool(
                    accounts=accounts_to_login,
                    wait_minutes=6.0,
                    on_progress=on_progress,
                    on_challenge=on_challenge,
                    on_status=on_status,
                )
            except Exception as exc:
                err_msg = f"{label} encountered an error: {exc}"
                self.root.after(0, lambda: self.set_status(err_msg, "#f87171"))
            finally:
                gl.close()

                def _finish() -> None:
                    self._close_challenge_modal_on_ui()
                    self._worker_running = False
                    self.auto_login_all_btn.configure(state="normal")
                    self.add_account_btn.configure(state="normal")
                    self._refresh_account_card()
                    if results:
                        ok_count = sum(1 for r in results if r.status == STATUS_OK)
                        summary_color = "#34d399" if ok_count == len(results) else "#fbbf24"
                        self.set_status(
                            f"{label} complete: {ok_count}/{len(results)} account(s) signed in.",
                            summary_color,
                        )

                self.root.after(0, _finish)

        threading.Thread(target=worker, daemon=True).start()


def main() -> None:
    root = tk.Tk()
    PoolLoginAssistantApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
