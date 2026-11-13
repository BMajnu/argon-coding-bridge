# ROUND 20 — The Argon browser must STOP dying on its own, and the pool's /u/N must always match the browser

You are the coding model for the **Argon Coding Bridge**. This is round 20.

Round 19 added the live agent↔browser control loop (`live_loop/`, `scripts/browser_loop.py`). That
work stands — **do not redesign or regress it.** This round fixes four defects the owner reported
against it and against the browser lifecycle.

---

## SECTION 0 — WHAT THE OWNER REPORTED

The owner's words:

> "eta fix koro.. browser open korar por kono tool call na thakle somvoboto 30 sec er moddhe close hoye jay;
> ... ar menually login pool er maddhome browser open kore koro kaj korte chaile possible hoy na kenona same vabe
> close hoy jay. r login pool a u/n order brower a login u/n er sathe match nai... eta kivabe always sync rakha
> somvob hobe.."

= *Fix this. After the browser is opened, if there is no tool call, it can close within 30 seconds.
And opening the browser by hand through the login pool so I can work in it is not possible, because it
closes the same way. And the login pool's /u/N order does not match the browser's login /u/N order.
How can that always be kept in sync?*

The owner's screenshots show the two visible symptoms:

1. `Could not save screenshot (preflight_failure.png): Page.screenshot: Target page, context or
   browser has been closed` — the browser died **during** a dispatch's pre-flight.
2. The Pool Login Assistant reporting `Opened Gemini Web (/u/3/ -> https://gemini.google.com/u/3/app)`
   and then the browser closing by itself.

**Everything below was measured on the owner's machine. The root causes are already found — do not
spend this round rediscovering them. Implement the fixes.**

---

## SECTION 1 — ROOT CAUSE 1: `release_host_tab` closes the ENTIRE browser

`skills/argon-coding-bridge/scripts/safe_browser.py:1080-1138`

```python
    remaining_count = len(tabs)
    if remaining_count > 0:
        ... keep the browser open, close only this tab ...
        return {... "mode": "tab_closed_browser_kept" ...}

    print(
        f"[+] Last task tab ({eff_tab_id}) finished on {prof} (active_tabs=0) - "
        "closing browser host gracefully so cookies are flushed."
    )
    remove_host_state(prof)
    return close_browser_safely(context=context, profile_dir=prof, page=page)
```

**The bug:** when the registered task-tab count reaches zero, the whole browser host is closed.
Every dispatch registers a tab in `acquire_host_tab` and releases it in its `finally:` — so **every
finished task takes the browser down with it**, including the owner's manually opened tab.

Measured on the owner's machine, round 20:

| Action | Result |
|---|---|
| `--open-browser` alone, then idle 90 s | the browser **survives** (no task tab is registered) |
| a dispatch / any task-tab lifecycle completes on that profile | **the entire browser closes** |

That is why the owner's manually opened browser vanishes, and why the pre-flight of the *other*
lane dies with `Target page, context or browser has been closed`.

### FIX 1 — the browser never closes because a tab was released

1. `release_host_tab` must close **ONLY its own tab**. It must **NEVER** close the browser host.
2. The host is closed by exactly one thing: the explicit `--close-browser` command, which keeps the
   existing snapshot/restore cookie safety (`close_browser_safely`).
3. To keep the on-disk session fresh without killing the browser, call
   `snapshot_profile_session(prof)` on release — that machinery already exists
   (`safe_browser.py:420`) and it is what actually protects the session. The browser closing was
   never required for the cookies to survive.
4. Keep the function's return dict shape; add `"mode": "tab_closed_browser_kept"` always, and add
   `"browser_closed": False`.
5. If `not is_host or not eff_tab_id` (the direct-launch fallback path), the function currently
   calls `close_browser_safely`. That path owns a browser it launched itself, so closing it is
   correct — but it must still not close a browser that has other task tabs. Keep that branch
   closing only when the host state has no other registered tabs, and print a clear line.

---

## SECTION 2 — ROOT CAUSE 2: the tab registry is a lost-update race

`safe_browser.py:972-1019` (`acquire_host_tab`) and `safe_browser.py:1099-1106` (`release_host_tab`)

```python
state = read_host_state(prof) or host      # <-- read with NO lock
tabs = dict(state.get("tabs") or {})
...
tabs[tab_id] = {...}
state["tabs"] = tabs
write_host_state(prof, state)              # <-- write WITH a lock
```

`write_host_state` (`safe_browser.py:544-557`) locks the file, but `read_host_state`
(`safe_browser.py:533-541`) does **not**, and the read-modify-write is not atomic as a whole.

**The bug:** two processes that share the Argon browser profile — `dual_dispatch.py`'s two lanes,
or the dispatcher plus a manual `--browser-act` — both read `tabs = {}`, then both write. **The
second write erases the first lane's tab entry.** The next release sees `remaining_count == 0`
and closes the whole browser while the other lane is still working. That is exactly the owner's
screenshot-1 failure.

### FIX 2 — one locked read-modify-write helper

Add to `safe_browser.py`:

```python
def mutate_host_state(profile_dir, mutator) -> Dict[str, Any]:
    """Read-modify-write the host state under the SAME file lock write_host_state uses.

    `mutator` receives the current state dict and mutates it in place (or returns a new dict).
    The whole read-modify-write happens while the lock is held, so two processes that share the
    browser profile cannot lose each other's tab entries.
    """
```

- Use the existing `_acquire_file_lock` / `_release_file_lock` helpers (`safe_browser.py:113` and
  `:138`) and the same lock path `write_host_state` uses:
  `prof / f"{ENDPOINT_FILENAME}.lock"`.
- Write it with the same atomic `tmp` + `os.replace` pattern so a reader never sees a half file.
- `acquire_host_tab` and `release_host_tab` must both use it for their read-modify-write of the
  `tabs` dict. Do not keep any unlocked read-modify-write of the host state.
- `write_host_state` and `read_host_state` keep their current signatures and behaviour (other
  callers depend on them).

---

## SECTION 3 — ROOT CAUSE 3: a browser opened for manual use must be persistent

The owner opens the browser to work in it by hand — through `--open-browser`, or through the Pool
Login Assistant's **"Open in Gemini Web"** button
(`login_pool_accounts.py:838-848` → `_launch_chrome_url` → `login_pool_accounts.py:779-826` →
`open_browser_cli` → `safe_browser.py:1647-1790`).

With FIX 1 in place the browser survives anyway, but make the intent explicit and durable:

### FIX 3 — a `persistent` flag on the host state

1. The host state (`.argon_browser_endpoint`) gains a `"persistent": true|false` key.
2. `open_browser_cli` sets `persistent: true` on the host state when it opens a browser for
   manual use (it is the "open it so I can work" command).
3. `persistent: true` means: no automatic closer may ever close this browser. It is closed only by
   `--close-browser`.
4. The dispatcher, when it launches a browser for its own round, must **not** set `persistent`
   (or must clear it), so the round-scoped browser can still be reaped by an idle closer if the
   owner enables one (FIX 5).
5. `--list-browsers` must print the `persistent` flag so the owner can see it.

---

## SECTION 4 — ROOT CAUSE 4: the pool's `/u/N` order drifts from the browser's `/u/N` order

This is the owner's third question, and it is the most important correctness fix in this round.

### 4.1 What is true right now (measured on the owner's machine)

The browser's `Default/Preferences` → `account_info` is the **authoritative** `/u/N` order. The
Argon browser currently holds 9 accounts:

```
/u/0/  badiuzzaman.majnu@gmail.com
/u/1/  bmajnu980@gmail.com
/u/2/  bmajnu9811@gmail.com
/u/3/  badiuzzaman.majnu123@gmail.com
/u/4/  msrosyafrin@gmail.com
/u/5/  bmajnu981@gmail.com
/u/6/  badiuzzaman.majnu1@gmail.com
/u/7/  bmajnu786@gmail.com
/u/8/  majnu.charitabari@gmail.com
```

The pool (`skills/argon-coding-bridge/config/accounts_pool.json`) stores an `account_index` per
slot. Compared against the live browser list:

```
slot     stored  live   email                            verdict
/u/0/    0       0      badiuzzaman.majnu@gmail.com      ok
/u/1/    1       1      bmajnu980@gmail.com              ok
/u/2/    2       2      bmajnu9811@gmail.com             ok
/u/3/    3       3      badiuzzaman.majnu123@gmail.com   ok
/u/4/    4       4      msrosyafrin@gmail.com            ok
/u/5/    5       5      bmajnu981@gmail.com              ok
/u/6/    6       6      badiuzzaman.majnu1@gmail.com     ok
/u/7/    9       NONE   badiuzzamanmajnu786@gmail.com    NOT SIGNED IN -> the stored 9 is a LIE
/u/8/    7       7      bmajnu786@gmail.com              ok
/u/9/    8       8      majnu.charitabari@gmail.com      ok
/u/10/   NONE    NONE   takitahamid1000@gmail.com        NOT SIGNED IN
```

**The browser has 9 accounts (0-8). Slot `/u/7/` points at `/u/9/` — an account that does not
exist.** Opening `/u/7/` therefore lands on a wrong or guest page.

### 4.2 Why the drift happens

`PoolConfig.get_account_index()` (`argon_pool_login.py:624-665`):

- re-verifies **only the ACTIVE slot**, and only when `is_shared_profile_slot(target_slot)`;
- when the email is NOT found in the browser it **silently falls back to the stored
  `account_index`** (the stale 9) or to the slot number.

So a stale index is never detected — it is simply used. And `accounts_pool.json`'s `account_index`
is a **snapshot**: it is only refreshed for one slot at a time, or in bulk by
`enable_shared_profile_mode()` (`argon_pool_login.py:674-711`), which nothing calls routinely.

The GUI's card (`login_pool_accounts.py:838-848`) builds the URL from
`self.pool_config.get_account_index(slot)` — i.e. from that snapshot — so the GUI can open the
wrong account, and it labels the number "(verified)" even when it is not.

### FIX 4 — the browser's `account_info` is the only source of truth, always

1. **Add `PoolConfig.resync_account_indexes()`** in `argon_pool_login.py`:
   - read the live `account_info` via `safe_browser.get_preferences_accounts(profile_dir)`;
   - build an `email → live index` map;
   - for **every** pool account: set `account_index` to the live index for its email; if the email
     is **not** in the browser, **clear** the stale `account_index` and set
     `status = "needs_signin"`;
   - detect two slots that resolve to the same live index and report them;
   - report browser accounts the pool does not know about;
   - save the pool config and return a report dict:
     `{"updated": [...], "needs_signin": [...], "collisions": {...}, "unknown_in_browser": [...]}`.
2. **`get_account_index()` must never fall back to a stale index.** If the account's email is not
   in the browser's `account_info`, it must NOT return the stored number. It must return a clear
   failure the caller can act on — raise a dedicated exception (e.g. `AccountNotSignedInError`)
   carrying the email and the list of signed-in emails, OR return `None` and let the caller
   report. Pick ONE and make every caller handle it. The rule: **never open `/u/N/` for an account
   that is not signed in.**
3. **Every URL builder must resync first, then use the live index.** At minimum:
   - `safe_browser.open_browser_cli` (`safe_browser.py:1694-1696`),
   - `login_pool_accounts.open_gemini_web` (`login_pool_accounts.py:838-848`),
   - the dispatcher's slot → `/u/N/` resolution (`dispatch_argon.py`, the
     `_url_slot_for_account` / `_resolve_reopen_url` area).
   The resync must be cheap (one JSON read of `Default/Preferences`) and must never raise — it
   returns a report.
4. **The GUI card must show the truth.** It must display the **live** verified index, and for a
   slot whose account is not signed in it must say **"NOT SIGNED IN"** instead of a stale number.
   Never label a number "(verified)" unless it was verified against the live list.
5. **Add a `--resync-accounts` CLI flag** to `dispatch_argon.py` that runs the resync and prints
   the report (updated / needs_signin / collisions / unknown_in_browser), so the owner can check
   the sync on demand.

---

## SECTION 5 — The idle closer (opt-in, OFF by default)

The owner does not want the browser to die by itself. But a browser left running forever is also
not ideal, so make the closer exist and make it opt-in.

- Add `--browser-idle-close-seconds N` to `dispatch_argon.py`, **default `0` = never close**.
- When `N > 0`, a watcher closes the browser after it has had **no registered task tabs** for `N`
  seconds — but it must:
  - **never** close a `persistent` host (FIX 3);
  - **never** close while a **non-task tab** is open (a tab the owner is using by hand) — count
    the live CDP targets against the registered task tabs and skip if any extra page is open;
  - close it with `close_browser_safely` so the cookies are flushed and the snapshot/restore
    safety still applies.
- The owner asked for "not within 30 seconds". The default must be **never**, and any non-zero
  value must be **at least 120 seconds** — a smaller value must be rejected with a clear message.

---

## SECTION 6 — FILES

### MODIFY (4)

```
skills/argon-coding-bridge/scripts/safe_browser.py          FIX 1, FIX 2, FIX 3, FIX 4.3, FIX 5
skills/argon-coding-bridge/scripts/argon_pool_login.py       FIX 4.1, FIX 4.2 (resync + no stale fallback)
skills/argon-coding-bridge/scripts/login_pool_accounts.py    FIX 4.4 (the GUI card shows the live index)
skills/argon-coding-bridge/scripts/dispatch_argon.py         FIX 4.5 (--resync-accounts), FIX 5 (idle flag)
```

### CREATE (2)

```
skills/argon-coding-bridge/scripts/test_browser_lifecycle.py   the regression tests (FIX 6)
skills/argon-coding-bridge/references/browser_lifecycle_and_account_sync.md   the rule sheet
```

### DO NOT TOUCH

- `skills/argon-coding-bridge/live_loop/**` and `skills/argon-coding-bridge/scripts/browser_loop.py`
  (round 19 — they work; `browser_loop.py` attaches over CDP directly and never calls
  `acquire_host_tab` / `release_host_tab`, so it does not close the browser).
- `skills/argon-coding-bridge/extension/**`.
- `config/accounts_pool.json` — it is **gitignored and secret-bearing**; the patch must NEVER
  contain it. The resync writes it at RUNTIME, not at patch time.
- The account model, the profile layout, the migration code, the delivery bundle.

---

## SECTION 7 — THE REGRESSION TESTS (a feature that cannot be exercised is not delivered)

`skills/argon-coding-bridge/scripts/test_browser_lifecycle.py` must run standalone with plain
`python` (no pytest dependency) and must prove all of this **without touching the owner's real
profile and without touching any Google account**:

1. **Releasing a task tab does not close the browser.** Use a throwaway profile and a disposable
   Chrome (its own temp profile + its own free port). Register a task tab, release it, and assert
   the CDP endpoint is STILL alive afterwards.
2. **Two concurrent acquisitions keep both tabs.** Spawn two processes that call
   `acquire_host_tab` at the same time against one throwaway profile; assert the host state ends
   with **both** tab ids. This is the lost-update regression test for FIX 2 — it must FAIL on the
   current code and PASS on the fixed code.
3. **The resync maps every signed-in account to its live index**, and flags a slot whose account
   is not signed in with `needs_signin` and NO stale index.
4. **`get_account_index` refuses to return a stale index** for an account that is not in the
   browser's `account_info`.
5. **A `persistent` host is not closed by the idle closer.**

Every test must use a **temporary profile directory** it creates and deletes. It must never touch
`~/.gemini/argon_browser_profile` and must never sign in to anything.

---

## SECTION 8 — VERIFICATION (run these; report the REAL output)

```powershell
# 1. syntax
python -m py_compile skills\argon-coding-bridge\scripts\safe_browser.py `
                      skills\argon-coding-bridge\scripts\argon_pool_login.py `
                      skills\argon-coding-bridge\scripts\login_pool_accounts.py `
                      skills\argon-coding-bridge\scripts\dispatch_argon.py

# 2. the new regression tests (must print PASS for every case and exit 0)
python skills\argon-coding-bridge\scripts\test_browser_lifecycle.py

# 3. the bridge's own self-test must still pass
python skills\argon-coding-bridge\scripts\dispatch_argon.py --self-test

# 4. round 19's loop must still pass
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-loop-selftest

# 5. the account resync must print a real report
python skills\argon-coding-bridge\scripts\dispatch_argon.py --resync-accounts
```

Put these exact commands in the NOTES block as `VERIFICATION COMMAND`, and paste the REAL output
of `test_browser_lifecycle.py` and `--resync-accounts` into the chat reply. **Never report a
measurement you did not measure.**

---

## SECTION 9 — DELIVERY CONTRACT

Deliver **ONE** `apply_changes.ps1` containing every change, exactly as the task prompt template
describes.

1. **No new files are needed here** — all six paths in Section 6 exist or are MODIFY targets
   except `scripts/test_browser_lifecycle.py` and
   `references/browser_lifecycle_and_account_sync.md`, which are CREATED. A file entry with a
   `New` block and **no `Old` block** creates the file; the apply script must make the parent
   directory, write UTF-8 without BOM, then re-read and verify.
2. **Existing files** use a `Blocks` entry with BOTH `Old` and `New`, where `Old` is the **exact
   current text** of that region (byte for byte, from the archive you received — the archive IS
   the current tree).
3. The two-phase rule still applies: **Phase 1 checks every `Old` block of every file before the
   first file is written**; a missing target throws while the project is untouched. Phase 2 backs
   up, writes, and re-reads to verify.
4. Use `[System.StringComparison]::Ordinal` + `[System.Text.StringBuilder]`, single-quoted
   here-strings (`@' ... '@`), and UTF-8 without BOM. Never `.Replace()` on a large needle.
5. The NOTES block must carry a `## ROOT CAUSE` section naming **all four** root causes with
   their `file:line`, and a table of every change.
6. **Do not** delete anything, do not touch `config/accounts_pool.json`, do not change the account
   model, the profile layout, the migration code, the delivery bundle, or any existing CLI flag.
7. The chat reply stays short: what changed, a table of the files, how to verify, and the pointer
   to the attached `apply_changes.ps1`.

---

## SECTION 10 — ACCEPTANCE CRITERIA

- [ ] `release_host_tab` no longer closes the browser host; only `--close-browser` does.
- [ ] A browser opened for manual use (`--open-browser`, the GUI's "Open in Gemini Web") stays
      open indefinitely — no 30-second death, no death after the next task finishes.
- [ ] The owner's manually opened tab is NOT closed when another task releases its tab.
- [ ] Two concurrent task-tab acquisitions on one profile keep BOTH tabs (no lost update).
- [ ] The pool's `/u/N` order is resolved from the browser's live `account_info` every time.
- [ ] Slot `/u/7/` (not signed in) does NOT open `/u/9/`; it reports "NOT SIGNED IN" loudly.
- [ ] `--resync-accounts` prints a real report and fixes the stored indexes.
- [ ] The GUI card shows the live index and says "NOT SIGNED IN" instead of a stale number.
- [ ] The idle closer is OFF by default and refuses to close a persistent host or a host with a
      manual tab open.
- [ ] `test_browser_lifecycle.py` passes all 5 cases and exits 0.
- [ ] `--self-test` and `--browser-loop-selftest` still pass.
- [ ] The 6 existing roots' behaviour is unchanged; no existing CLI flag changed.
- [ ] `config/accounts_pool.json` appears nowhere in the patch and no credential leaks into it.
