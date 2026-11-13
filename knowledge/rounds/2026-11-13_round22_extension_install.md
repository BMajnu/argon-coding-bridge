# ROUND 22 — The companion extension must be INSTALLED (one click), VERIFIED, and ALWAYS there

You are the coding model for the **Argon Coding Bridge**. This is round 22.

Rounds 19-21 are in the tree and must not be regressed: the live browser control loop
(`live_loop/`, `scripts/browser_loop.py`), the browser-lifecycle guarantees (the browser never
closes because a tab was released), and the always-in-sync account pool. Keep all of it.

This round fixes the one thing the owner keeps hitting: **the companion MV3 extension is never
actually installed, so every attach/upload silently falls back to the pure-Python path and often
hangs or fails.**

---

## SECTION 0 — WHAT THE OWNER REPORTED

> "আমাদের এখানে যে ব্রাউজার এক্সটেনশন টি রয়েছে এটি জাস্ট ফার্স্ট টাইম 1 ক্লিকে এই যে আর্গন browser টি রয়েছে সেটির মধ্যে
> ইন্সটল হয়ে যাবে এবং পরবর্তীতে আর ইন্সটল করা করার প্রয়োজন পড়বে না কেননা এটি অলরেডি এখানে থাকবে কিন্তু বর্তমানে যে এটি হচ্ছে
> যে প্রতিবার হয়তো ইন্সটল করার প্রয়োজন পড়তেছে এবং আছে কিনা অথবা এটি ব্রাউজার মত কখনো ইন্সটলই হচ্ছে না যে কারণে এখনো যে কোন
> এজেন্ট কোন ধরনের একটু অর্থাৎ ইমেজ আপলোড অথবা ডিসপ্যাচ ফাইলটি আপলোড করতে অনেক সময় নিচ্ছে অনেক সময় পুরোপুরি ব্যর্থ হছে পাঠানোর জন্য
> অচ্ছে এই সমস্যাগুলো সলভ করার জন্যই এক্সটেনশন থেকে বের করা হয়েছিল জেটির মাধ্যমে এই টোটাল প্রসেসটি খুব ইজি ওয়েতে খুব দ্রুতভাবে করা
> সম্ভব হবে কিন্তু বর্তমানে এটি হচ্ছে না ... ব্রাউজারে থ্রি ডট মেনু থেকে যখন এক্সটেনশন মেটে যাচ্ছি তখন কোন ধরনের এক্সটেনশন সেখানে একজাস্ট
> করছে না বা দেখাচ্ছেনা যেখানে কোন এক্সটেনশন রয়েছে তো এটিও ফিক্স করতে হবে ... এটি যেহেতু ফ্রি ইন্সটল থাকবে এখানে তো অলরেডি সেখানে ...
> ১০০% ভাবে এটা নিশ্চিত করতে হবে।"

= *The browser extension we have should install into the Argon browser with a single click on the
first time, and afterwards there should be no need to install it again, because it will already be
there. Right now it seems an install is needed every time, and it may not even be installed - which
is why uploading an image or the dispatch file often takes forever or fails outright. The extension
was built to solve exactly those problems and to make the whole process easy and fast, but right now
it is not happening. When I look in the browser's three-dot menu, no extension shows up - that has
to be fixed too. It should stay installed there for free, and the handling of these problems has to
be fixed 100%.*

---

## SECTION 1 — THE ROOT CAUSE, MEASURED ON THE OWNER'S MACHINE

Everything below was measured on the owner's machine with Chrome **157.0.8103.78** before this round.

### 1.1 The flag that stopped working

The bridge launches the browser with:

```
--load-extension=<dir>  --disable-extensions-except=<dir>
```

**Chrome removed `--load-extension` from the branded build (Chrome 137+).** It is silently ignored -
no error, no extension, nothing.

The authoritative check is the DevTools Protocol's `Extensions.getExtensions` command, which lists
the unpacked extensions Chrome really has. Measured matrix, each row a fresh profile:

```
HEADED + --load-extension + --disable-extensions-except   -> getExtensions: []   (the flag does NOTHING)
HEADED + --load-extension only                            -> getExtensions: []
HEADED + --disable-extensions-except only                 -> getExtensions: []
HEADED + neither                                          -> getExtensions: []
```

And on the owner's **live** Argon browser, right now:

```
command line : --load-extension=...argon-coding-bridge\extension  (the flag IS there)
getExtensions: []                     <- nothing is installed
chrome://extensions : 0 extensions
content script marker on every Gemini page : None   <- the content script is NOT running
```

So the bridge was **claiming the extension was loaded while it was not installed at all**:

```python
# dispatch_argon.py:4604 - this is the line that lies
print(f"[+] Companion extension loaded from: {ext_dir}")
```

It only proved the DIRECTORY exists and that it appended a dead flag. Because
`_extension_is_available(page)` (dispatch_argon.py:6818) checks the content-script marker, and the
marker was never set, **every** attach, paste, send and delivery-capture fell back to the
pure-Python path. That is the owner's upload failure.

### 1.2 The flag is not only dead, it actively BLOCKS the fix

`--disable-extensions-except=<dir>` disables every extension except that one path. Measured on a
browser started with it, `Extensions.loadUnpacked` returned an id but the extension **never
appeared** (`getExtensions: []`, no service-worker target, no content script). The same call on a
browser started WITHOUT the flag installed it perfectly.

### 1.3 The fix that works, measured end to end

The DevTools Protocol has an `Extensions` domain in this Chrome:

```
Extensions.loadUnpacked    - Installs an unpacked extension from the filesystem similar to
                            --load-extension CLI flags. Returns extension ID once installed.
Extensions.getExtensions   - Gets a list of all unpacked extensions.
Extensions.uninstall       - Uninstalls an unpacked extension from the profile.
```

Measured, on a fresh profile, Chrome 157, headed:

```
Extensions.loadUnpacked -> id=pgankdiaanldfgnaobabnojjnmakgena   in 0.03 s
Extensions.getExtensions -> 1 entry: id=pgankdiaanldfgnaobabnojjnmakgena  path=<our extension dir>
CDP target  -> service_worker  chrome-extension://pgankdiaanldfgnaobabnojjnmakgena/service_worker.js
chrome://extensions -> "Argon Coding Bridge Companion  1.16.0"  is LISTED
the content script marker on a Gemini page -> '1.16.0'   <- THE CONTENT SCRIPT REALLY RUNS
```

So the one-click install **works**, it **is visible** in `chrome://extensions`, and the extension
becomes fully functional - which is what puts the fast attach/upload path back.

### 1.4 The honest limit: Chrome will not keep it across a restart

Measured three ways, all on the owner's machine:

```
restart after a HARD kill          -> getExtensions: []   the content script: None
restart after a GRACEFUL close     -> getExtensions: []   the content script: None
restart + developer_mode=true     -> getExtensions: []   the content script: None
```

The profile DOES record it, in `Secure Preferences`:
`extensions.settings.pgankdiaanldfgnaobabnojjnmakgena  location=4  path=<our dir>` - but Chrome 157
discards that record on the next start.

**So the extension cannot be made to persist natively. The BRIDGE must guarantee it.** That is
what "it will already be there" has to mean in practice: the user installs it once (or never - the
bridge does it), and from then on it is **always present**, because the bridge re-installs it
automatically at every browser start. Re-installing costs **0.03 s** and is idempotent.

---

## SECTION 2 — WHAT TO BUILD

### 2.1 `ensure_companion_extension_installed()` - the guarantee

New function in `skills/argon-coding-bridge/scripts/safe_browser.py`:

```python
def ensure_companion_extension_installed(
    browser_or_cdp_session: Any,
    ext_dir: Optional[Union[str, Path]] = None,
    timeout_s: float = 20.0,
) -> Dict[str, Any]:
    """Make sure the companion MV3 extension is REALLY installed in the running browser.

    Returns {"installed": bool, "id": str, "path": str, "method": str, "detail": str}.
    """
```

- `ext_dir` defaults to `get_companion_extension_dir()`; if that is None (the extension is absent
  or `ARGON_DISABLE_COMPANION_EXTENSION=1`) return `{"installed": False, "method": "disabled"}`.
- Open a **browser-level** CDP session (`context.browser.new_browser_cdp_session()` in Playwright,
  or `browser.new_browser_cdp_session()` on a `Browser`).
- Call `Extensions.getExtensions`. If an entry's `path` resolves to our `ext_dir`, it is ALREADY
  installed: return `{"installed": True, "id": <id>, "method": "already_installed"}` **without
  touching anything**. This is the idempotent fast path and it must not re-install.
- Otherwise call `Extensions.loadUnpacked` with `{"path": str(ext_dir)}`. It returns
  `{"id": "<extension id>"}`.
- **Verify**: wait up to `timeout_s` for the extension to appear in `Extensions.getExtensions`
  (poll every 0.25 s) AND for its service-worker target
  (`chrome-extension://<id>/service_worker.js`) to show up in the CDP target list. Only then report
  `installed: True`. If it does not appear, report `installed: False` with the reason - **never
  claim success you did not see**.
- If the CDP `Extensions` domain is not available (Chrome older than 133, or the domain is
  disabled), return `{"installed": False, "method": "unsupported"}` and let the caller fall back to
  the `--load-extension` launch flag (which still works on those older Chromes) and say so.
- Catch every exception. This function must NEVER take the bridge down.

### 2.2 Remove the dead (and blocking) launch flags

- `safe_browser.py`: `DEFAULT_HOST_ARGS` and `companion_extension_launch_args()` currently add
  `--load-extension=<dir>` and `--disable-extensions-except=<dir>`. **Stop adding
  `--disable-extensions-except`** - it is what blocks `Extensions.loadUnpacked` (Section 1.2).
- Keep `--load-extension` **only** for the fallback path of Section 2.1's `unsupported` case, and
  make it conditional: only pass it when the CDP `Extensions` domain is not available. On Chrome
  137+ it is dead weight.
- If `companion_extension_launch_args()` becomes empty, keep the function and return an empty list
  rather than deleting it - other code calls it.

### 2.3 Call the guarantee everywhere the browser comes up

- In `ensure_browser_host()`: right after the host is up and the state is written, call
  `ensure_companion_extension_installed()` and **print the VERIFIED result**:
  ```
  [+] Companion extension: INSTALLED (id=pgankdia..., verified by Extensions.getExtensions)
  ```
  or
  ```
  [!] Companion extension: NOT installed (<reason>) - the pure-Python fallback path will be used
  ```
- In `open_browser_cli()` (the `--open-browser` command and the Pool Login Assistant's
  "Open in Gemini Web"): same call, same verified line.
- In the send pipeline (`dispatch_argon.py`, where `pipeline_path` is decided at line ~7536): if the
  extension is NOT active on the page, **try to install it once and re-check** before falling back
  to `python_fallback`. A page that was already open when the extension was installed will not have
  the content script until it is **reloaded** - so after a successful install, reload the page
  (or open a fresh tab) and re-check `_extension_is_available(page)`.

### 2.4 The one click the owner asked for

- New CLI flag on `dispatch_argon.py`: `--install-extension`.
  - It attaches to the running Argon browser (starting it if it is not running), installs the
    extension with `Extensions.loadUnpacked`, verifies it, opens a Gemini tab, **reloads it** and
    proves the content script is alive (`data-argon-companion-extension` is set), and prints the
    extension id.
  - Exit code 0 only when the extension is installed AND the content script is confirmed alive on a
    real page. Otherwise a non-zero exit and a clear reason.
  - It must be safe to run again at any time (idempotent).
- A one-click launcher next to the existing ones: `Open_Argon_Install_Extension.bat` in the repo
  root, following the style of `Open_Argon_Apply.bat` / `Open_Argon_Login.bat`.
- `--list-browsers` must show, per browser, whether the companion extension is installed and its id.

### 2.5 Stop lying, everywhere

- Replace the lie at `dispatch_argon.py:4604` (`"[+] Companion extension loaded from: ..."`) with
  the verified line. The word "loaded" may only be used when `Extensions.getExtensions` (or the
  content-script marker) actually proves it.
- Every place that prints a pipeline path (`extension` vs `python_fallback`) is already honest -
  keep it that way.

### 2.6 Make the extension path the fast path again

The extension exists to make attach/paste/send/capture fast and reliable. With it installed,
`_extension_is_available(page)` is True and the bridge uses:

- `attach_archive` - `DataTransfer` injection into `input[type=file]`, **no Windows file dialog**,
- `paste_prompt` - a synthetic `ClipboardEvent('paste')`, no clipboard permission,
- `click_send` - a semantic model/mode/send match instead of brittle CSS,
- `capture_delivery` - the paged Drive `viewer/text` walker.

**Do not rewrite the extension.** The extension is fine (version 1.16.0, it loads and runs). The
bug was that it was never installed. Only make the extension's own `manifest.json` more permissive
if - and only if - a measurement proves it is needed.

One thing that IS worth changing in the extension: give it a **stable extension id** by adding a
`"key"` to `skills/argon-coding-bridge/extension/manifest.json` (the base64 public key that
produces the id `pgankdiaanldfgnaobabnojjnmakgena`). Then the id never changes, whatever path the
plugin is installed from, and the bridge can assert the exact id it expects.

---

## SECTION 3 — THE FILES

### MODIFY

```
skills/argon-coding-bridge/scripts/safe_browser.py       2.1, 2.2, 2.3
skills/argon-coding-bridge/scripts/dispatch_argon.py     2.3, 2.4, 2.5
skills/argon-coding-bridge/extension/manifest.json       2.6 (the stable "key")
```

### CREATE

```
skills/argon-coding-bridge/scripts/test_round22_extension_install.py   the regression suite
Open_Argon_Install_Extension.bat                                        the one-click launcher
```

### DO NOT TOUCH

- `skills/argon-coding-bridge/live_loop/**` and `skills/argon-coding-bridge/scripts/browser_loop.py`
  (round 19).
- `skills/argon-coding-bridge/extension/content_script.js` and `service_worker.js` - the extension's
  own logic is fine; only its manifest may gain the `key`.
- `config/accounts_pool.json`, the account model, the profile layout, the migration code, the
  delivery bundle, and every existing CLI flag.

---

## SECTION 4 — THE REGRESSION SUITE

`skills/argon-coding-bridge/scripts/test_round22_extension_install.py` must run standalone with plain
`python` and must be hermetic: its own temp profile, its own free port, its own Chrome, and it
must NEVER touch `~/.gemini/argon_browser_profile` or any Google account. It must prove:

1. **`--load-extension` is dead and the bridge no longer relies on it.** Start a throwaway Chrome
   with the flag and assert `Extensions.getExtensions` is empty - the suite must DOCUMENT the dead
   flag rather than assert a browser-specific behaviour forever, so phrase it as "the bridge must
   not depend on the flag": assert the bridge's own launch args no longer contain
   `--disable-extensions-except`, and that the CDP install path works.
2. **The one-click install works**: `ensure_companion_extension_installed()` on a throwaway Chrome
   returns `installed: True` with a non-empty id.
3. **It is idempotent**: calling it twice does NOT install twice and does not change the id.
4. **It is VERIFIED, not assumed**: after the install, `Extensions.getExtensions` lists our path,
   the service-worker target exists, and a Gemini page shows the content-script marker.
5. **A failure is reported, not hidden**: point the function at a directory with no
   `manifest.json` and assert it returns `installed: False` with a reason - never `True`.
6. **The launcher flags are clean**: assert the launch args the bridge builds contain no
   `--disable-extensions-except`.

Every number the suite prints must be measured in that run.

---

## SECTION 5 — VERIFICATION (run these; report the REAL output)

```powershell
# 1. syntax
python -m py_compile skills\argon-coding-bridge\scripts\safe_browser.py `
                      skills\argon-coding-bridge\scripts\dispatch_argon.py `
                      skills\argon-coding-bridge\scripts\test_round22_extension_install.py

# 2. the new regression suite (must print PASS for every case and exit 0)
python skills\argon-coding-bridge\scripts\test_round22_extension_install.py

# 3. the bridge's own self-test must still pass (it runs the round-20/21 suites too)
python skills\argon-coding-bridge\scripts\dispatch_argon.py --self-test

# 4. round 19's live loop must still pass
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-loop-selftest

# 5. the one click, against the OWNER'S real Argon browser - this is the acceptance test
python skills\argon-coding-bridge\scripts\dispatch_argon.py --install-extension
```

Put these exact commands in the NOTES block as `VERIFICATION COMMAND`, and paste the REAL output of
steps 2 and 5 into the chat reply. Step 5 must show the extension installed with its id AND the
content script confirmed alive on a Gemini page. **Never report a measurement you did not take, and
never report "installed" without the verification behind it.**

---

## SECTION 6 — THE DELIVERY CONTRACT

Deliver **ONE** `apply_changes.ps1` containing every change, exactly as the task prompt template
describes.

1. **New files** are delivered as a file entry with a single `New` block and **no `Old` block at
   all** - that is how the bridge's pre-flight recognises "create this file". The apply script must
   create the parent directory, write UTF-8 without BOM, then re-read and verify.
2. **Existing files** use a `Blocks` entry with BOTH `Old` and `New`, where `Old` is the **exact
   current text** of that region (byte for byte, from the archive you received - the archive IS the
   current tree).
3. The two-phase rule still applies: **Phase 1 checks every `Old` block of every file before the
   first file is written**; a missing target throws while the project is untouched. Phase 2 backs
   up, writes, and re-reads to verify.
4. Use `[System.StringComparison]::Ordinal` + `[System.Text.StringBuilder]`, single-quoted
   here-strings (`@' ... '@`), and UTF-8 without BOM. Never `.Replace()` on a large needle.
5. The NOTES block must carry a `## ROOT CAUSE` section naming **this** root cause with its
   `file:line` and the measured evidence, plus a table of every change.
6. **Do not** delete anything, do not touch `config/accounts_pool.json`, the account model, the
   profile layout, the migration code, the delivery bundle, or any existing CLI flag.
7. The chat reply stays short: what changed, a table of the files, how to verify, and the pointer
   to the attached `apply_changes.ps1`.

---

## SECTION 7 — THE ACCEPTANCE CRITERIA

- [ ] `ensure_companion_extension_installed()` installs the extension into a RUNNING browser in one
      CDP call and verifies it (getExtensions + the service-worker target).
- [ ] Calling it twice is idempotent and keeps the SAME extension id.
- [ ] The bridge never launches Chrome with `--disable-extensions-except` any more.
- [ ] `--install-extension` exists, is idempotent, and exits 0 only after the content script is
      confirmed alive on a real Gemini page.
- [ ] `Open_Argon_Install_Extension.bat` is a working one-click launcher.
- [ ] The log never says "loaded" unless it was verified; it prints the id or says why it failed.
- [ ] The extension appears in `chrome://extensions` as "Argon Coding Bridge Companion".
- [ ] The send pipeline uses `path=extension` (not `python_fallback`) when the extension is present.
- [ ] The extension has a stable id (a `key` in its manifest), the same from any install path.
- [ ] `test_round22_extension_install.py` passes every case and exits 0.
- [ ] `--self-test` and `--browser-loop-selftest` still pass.
- [ ] No existing CLI flag changed behaviour; `config/accounts_pool.json` untouched.
