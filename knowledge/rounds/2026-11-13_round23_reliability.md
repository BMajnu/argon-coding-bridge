# ROUND 23 — Make the Argon bridge reliable: ONE session, ONE model, ONE browser, VERIFIED

You are the coding model for the **Argon Coding Bridge**. This is round 23.

**This round is not a feature round. It is a reliability round.** The owner says the bridge has
become riddled with bugs and that *not even a single session sends properly any more*. Your job is
to make one session send, reliably, on the right model, with every step proved - and to make the
whole thing SIMPLE.

## WHAT IS ALREADY IN THE TREE (and what is NOT)

- **Rounds 19, 20 and 21 ARE applied.** The live browser control loop (`live_loop/`,
  `scripts/browser_loop.py`), the browser that never closes by itself, the always-in-sync account
  pool, the fatal busy-slot rule and the 90-second host wait are all in the tree. Do not regress
  them.
- **Round 22's patch was captured but NEVER APPLIED** (two of its OLD blocks did not match the
  tree). So the extension install it contained is **NOT** in the tree, and **you must implement it
  yourself** - see R2 and Section 5. Do not assume any round-22 code exists.
- `knowledge/rounds/` holds the brief of every round. Read the ones you need; do not re-investigate
  what they already measured.

---

## SECTION 0 — THE OWNER'S ORIGINAL PROMPT (verbatim; this is the task that started the round)

> "আমাদের এখানে যে ব্রাউজার এক্সটেনশন টি রয়েছে এটি জাস্ট ফার্স্ট টাইম 1 ক্লিকে এই যে আর্গন browser টি রয়েছে সেটির মধ্যে
> ইন্সটল হয়ে যাবে এবং পরবর্তীতে আর ইন্সটল করা করার প্রয়োজন পড়বে না কেননা এটি অলরেডি এখানে থাকবে কিন্তু বর্তমানে যে এটি হচ্ছে যে প্রতিবার
> হয়তো ইন্সটল করার প্রয়োজন পড়তেছে এবং আছে কিনা অথবা এটি ব্রাউজার মত কখনো ইন্সটলই হচ্ছে না যে কারণে এখনো যে কোন এজেন্ট কোন ধরনের একটু
> অর্থাৎ ইমেজ আপলোড অথবা ডিসপ্যাচ ফাইলটি আপলোড করতে অনেক সময় নিচ্ছে অনেক সময় পুরোপুরি ব্যর্থ হছে পাঠানোর জন্য অচ্ছে এই সমস্যাগুলো সলভ করার
> জন্যই এক্সটেনশন থেকে বের করা হয়েছিল জেটির মাধ্যমে এই টোটাল প্রসেসটি খুব ইজি ওয়েতে খুব দ্রুতভাবে করা সম্ভব হবে কিন্তু বর্তমানে এটি হচ্ছে না ... ব্রাউজারে
> থ্রি ডট মেনু থেকে যখন এক্সটেনশন মেটে যাচ্ছি তখন কোন ধরনের এক্সটেনশন সেখানে একজাস্ট করছে না বা দেখাচ্ছেনা যেখানে কোন এক্সটেনশন রয়েছে তো এটিও ফিক্স করতে হবে
> এবং এটি যেহেতু ফ্রি ইন্সটল থাকবে এখানে তো অলরেডি সেখানে এবং এক্সটেনশন থাকে এক্সটেনশন হিসেবে করবে এবং এই সমস্যাগুলো যে হ্যান্ডেলিং বাঁকগুলো যেন ফিক্স
> হয়ে যায় ১০০% ভাবে এটা নিশ্চিত করতে হবে।"

= *The browser extension we have should install into the Argon browser with a single click on the
first time, and afterwards there should be no need to install it again, because it will already be
there. Right now it seems an install is needed every time, and it may not even be installed - which
is why uploading an image or the dispatch file often takes forever or fails outright. The extension
was built to solve exactly those problems and to make the whole process easy and fast, but right now
it is not happening. When I look in the browser's three-dot menu, no extension shows up - that has
to be fixed too. It should stay installed there for free, and the handling of these problems has to
be fixed 100%.*

---

## SECTION 1 — WHAT THE OWNER ACTUALLY NEEDS

> "আমাদের পুরো আর্গন ব্রিজ কোডিং এজেন্টটিতে বাগের ছড়াছড়ি... কোনো একটি সিঙ্গেল কাজও ঠিকমতো হচ্ছে না... একটি সিঙ্গেল সেশনও ঠিকমতো পাঠাতে
> পারছে না... আমি এমন একটি ইজি, স্মার্ট ও সহজ ওয়ে চাই, যেখানে কোনো ধরনের সিঙ্গেল বাগ থাকবে না এবং এটি ১০০% বাগপ্রুফ হবে।"

= *The whole Argon bridge coding agent is full of bugs; not a single job works properly. Not even a
single session can be sent properly. I want an easy, smart and simple way where there is no single
bug and it is 100% bug-proof.*

**The deliverable is therefore a BRIDGE THAT PROVES ITSELF, not a new feature.** But the owner's
ORIGINAL prompt (Section 0) is also a deliverable: the one-click extension install must exist and
must work.

---

## SECTION 2 — THE EIGHT MEASURED FACTS THAT EXPLAIN EVERY SYMPTOM

Every one of these was measured on the owner's machine. Do not re-investigate them; FIX them.

### F1 — The tasks have been running on "Hydrogen", not on "Code & analysis 4 Argon"

The Gemini model picker holds TWO models:

```
[CHECKED]  Everyday help    4 Hydrogen    New   <- what was selected
[       ]  Code & analysis  4 Argon              <- what the bridge is FOR
```

In the Phase 6 conversation, **all 12 completed responses carried the model-name element
"Hydrogen"**. The bridge's own guard (`_verify_model_chip_is_argon`) only rejects **Flash**, so
Hydrogen passed it. And the composer's chip after the send reads the ACCOUNT DEFAULT, not the model
that answered - so the guard looked at the wrong signal at the wrong moment.

### F2 — The companion extension was never installed

Chrome 137+ removed `--load-extension` from the branded build. Measured on Chrome 157: four launch
configurations, every one of them `Extensions.getExtensions: []`. The live Argon browser carries the
flag on its command line and still has **0 extensions**, `chrome://extensions` lists nothing, and the
content-script marker is null on every page. The bridge nevertheless printed
`[+] Companion extension loaded from: ...` - it had only proved the DIRECTORY existed. Because
`_extension_is_available()` was therefore always false, **every** attach, paste, send and delivery
capture silently degraded to the pure-Python path - which is where the uploads hang and fail.

The fix is measured and works: the DevTools Protocol's `Extensions.loadUnpacked` installs the
unpacked extension into the RUNNING browser in **0.03 s**, it then appears in `chrome://extensions`
as "Argon Coding Bridge Companion", and its content script really runs (marker `1.16.0`).

**And this is the step that breaks every dispatch.** Measured in round 23, twice, on the owner's
machine, with the extension missing:

```
prompt paste   (python_datatransfer) : OK (48469 chars in composer)
attachment argon_context.zip (2031048 bytes, 59 entries) (python_fallback) : FAILED
                                            (the upload menu rendered no file input)
```

The paste works. **The ATTACH is what fails** - and the attach is the one operation the companion
extension exists for: the extension injects the file into the composer's hidden `input[type=file]`
without ever opening the OS file dialog. Without it, the pure-Python path has to open Gemini's
upload menu and find a file input, and the current Gemini UI renders none. So the missing
extension is not a cosmetic problem - **it is why not a single session gets through.**

### F3 — The bridge's own fixes live in a WORKSPACE BACKUP, not in the repo

This is why the same bugs keep coming back. Four bridge fixes exist ONLY as scripts under

```
G:\Developing\Penflow\Penflow-WordCraft-Rust-Agent-Studio\.argon_backup\briefs\
    patch_bridge_task_toggle.py
    patch_bridge_large_archive.py
    patch_bridge_cdp_setfiles.py
    patch_bridge_attach_images.py
```

and **none of them is in this repository**:

| the fix | in the repo? |
|---|---|
| `--attach-images` (PNG injection into the composer's hidden image input, no OS file dialog) | **NO** (0 matches) |
| `DOM.setFileInputFiles` with the input's `objectId` (the 47 MB archive path) | **NO** (0 matches) |
| the base64 size cap (the ~63 MB base64 crash) | **NO** (0 matches) |
| `_is_toggle_control()` (the Task/Chat toggle detection) | **NO** (0 matches) |

The plugin's self-update (`Open_Argon_Update.bat` + `sync_plugin.py`) overwrites the installed
`dispatch_argon.py` with the REPO's copy, so a fix that only ever lived in a workspace backup is
**silently deleted on the next plugin update** - and the bug returns in every project.

### F4 — Two lanes fight over the ONE browser

Round 22, measured: both lanes started at the same instant, neither found a running Chrome, so both
tried to LAUNCH one on the same profile. One won the profile lock; the other died with
`Profile ... is busy. Refusing to hijack the running browser.` and exit code 7 - the exact log line
is "profile is busy". The other lane's tab was then navigated away by a PARALLEL session using the
same browser, and the lane read the WRONG chat, concluded that "a newer prompt superseded" it, and
gave up while Argon was still writing the answer.

### F5 — The lane's deadline is 30 minutes

Round 22: the prompt was sent at 15:11:39; the lane gave up at 15:38; Argon was STILL writing
("Created Round 22 hermetic regression suite..."). The delivery was therefore never captured.

### F6 — The delivery viewer OVERFLOWS the screen, the composer lands on top of it, and then the WHOLE TAB HANGS

### F6.1 - THE EXACT SYMPTOM (the owner's own words and screenshots)

> "Always it keeping hanged and not taking click or response on it and you are trying with creating new tab and
> trying again and again but same. But after few try now it release but you are not able to send it again."

What happens, step by step:

1. The Gemini conversation is open. The **generated-file viewer** (the `apply_changes.ps1` panel) or the
   **"Keep the chat reply brief..." tooltip** is drawn over the page. The composer's
   "Ask a follow-up" box is drawn ON TOP of that panel, and the whole page grows past the viewport.
2. **The tab stops taking clicks ANYWHERE.** Not the Download button - the sidebar, the model chip,
   the composer, the tabs. Nothing responds. The page is not "the button is in the wrong place"; the
   **renderer is blocked**.
3. **Creating a new tab does NOT help** - the new tab loads the same heavy Gemini UI in the same
   wedged browser and is unresponsive too. Retrying over and over makes it worse: each attempt
   leaves another half-loaded document and another overlay.
4. After a few attempts it "releases" - the page answers again - **but the send does not work any
   more.** The composer is in a state where the prompt does not go out (the paste lands, the send
   does not fire, or the attachment does not attach).

### F6.2 - WHAT THE BRIDGE MEASURED

```
the tab stops responding entirely: it takes NO click anywhere at all
the CDP endpoint stops answering: connect_over_cdp reaches "ws connected" and then TIMES OUT
the DevTools HTTP endpoint stops responding; the browser process is alive but its renderer is blocked
the browser stayed wedged for about an hour (15:38 -> 16:41)
every dispatch in that window made ZERO progress (the round-23 lane started at 16:36 and could do nothing)
the owner's own work was blocked too - a parallel session's tabs sit in the same browser
recovery that works: a GRACEFUL browser close (cookie snapshot taken, session intact) + relaunch
                -> the CDP endpoint answered again in 34 ms
recovery that does NOT work: opening new tabs, clicking around, retrying the send
```

### F6.3 - WHY IT HAPPENS

Three things combine:

- The bridge **clicks into a panel whose geometry it does not control** (the generated-file viewer),
  in a page that can reflow to any height, and then **leaves the panel open**.
- The bridge and **the owner drive the SAME page at the same time**. A page cannot have two drivers.
- The bridge's **retry ladder keeps retrying into a dead page**, and every retry opens another
  document in a renderer that is already blocked.

### F6.4 - THE FIX (this is the heart of the round)

- **One driver at a time.** The bridge must never open a tab and drive it while the owner is using
  the same browser. If the owner has a tab open in the Argon browser, the bridge attaches to THAT
  browser but must leave the owner's tab alone.
- **Probe before and after every page interaction.** A cheap round-trip into the page before
  acting and another one after. A page that does not answer is "wedged" - never "try again".
- **On a wedge: STOP. Do not retry in a loop and do not open new tabs to try again.** Close the
  wedged tab; if the CDP endpoint does not answer, close the browser GRACEFULLY (the cookie
  snapshot keeps the session intact) and relaunch it. Then continue.
- **Never leave an overlay on the page.** The capture must close the viewer and restore the
  composer before it reports anything.
- **The lane must say "the page is wedged", never "a newer prompt superseded me"** (that was a
  wrong-chat read, see R16).


The owner reports (and the screenshot shows) that when the **Download** button is clicked - by the
owner or by the agent - the download section sometimes **hangs** and the whole screen **overflows**:
the generated-file viewer expands into a full-height code panel, the floating `Download / Drive`
menu is left hanging over it, the composer ("Ask a follow-up") is drawn ON TOP of the code, and the
Download button can no longer be clicked (or the click lands on nothing).

**And then it gets worse - the whole browser wedges.** Measured while this round was being
dispatched:

- The tab **stops responding entirely**: it takes **no click anywhere at all**, not even on the
  sidebar, not on the model chip, not on the composer. It is not "the button is in the wrong
  place" - the page is dead.
- The CDP endpoint stops answering too: `connect_over_cdp` reaches "ws connected" and then
  **times out**, and the DevTools HTTP endpoint stops responding. The browser process is alive but
  its renderer/main thread is blocked.
- The browser was wedged for **about an hour** (from ~15:38, when a capture left the viewer open,
  until ~16:41 when the owner noticed). Every dispatch in that window was affected - the round-23
  lane started at 16:36 and made **zero progress** because the page it needed to drive was dead.
- The owner's own work was blocked too: a parallel session's tabs sit in the same browser.
- Recovery: a graceful browser close + relaunch brought it back (the CDP endpoint answered in
  **34 ms** afterwards).

That is exactly the operation the bridge performs to collect `apply_changes.ps1`. The bridge clicks
into a panel whose geometry it does not control, in a page that can reflow to any height, and then
leaves the panel open.

### F7 — Chrome warns about a launch flag that does absolutely nothing

Every page in the Argon browser carries this banner:

> "You are using an unsupported command-line flag: `--disable-blink-features=AutomationControlled`.
> Stability and security will suffer."

The flag is in `DEFAULT_HOST_ARGS` (`safe_browser.py`). Its only job is to hide
`navigator.webdriver`. **Measured on the owner's machine, with and without it:**

```
WITH the flag    : navigator.webdriver = false, languages 2, plugins 5, chrome = false
WITHOUT the flag : navigator.webdriver = false, languages 2, plugins 5, chrome = false
the flag changes anything? NO - it is pointless here
```

It is pointless **because** the bridge launches Chrome with an EXPLICIT
`--remote-debugging-port=<n>` (not `--remote-debugging-port=0`) and without
`--enable-automation`, so Chrome never sets the automation flag in the first place. The flag
therefore buys the owner nothing and costs a warning banner on every single page.

### F8 — A slot number is NOT an account index, and the bridge used it as one

`_url_slot_for_account()` swallowed every error and fell back to the pool **slot number**:

```python
    try:
        acct_idx = PoolConfig().get_account_index(slot)
        return f"/u/{acct_idx}/"
    except Exception:
        return normalize_slot(slot) if slot else "/u/0/"     # <- the pool slot number!
```

So a slot whose account is not signed in opened `/u/<slot number>/` - a **different Google
account**. That is the mechanism behind "the task ran under the wrong account". There is no correct
fallback in shared mode: the fallback must be an error, never a guess.

---

## SECTION 3 — THE DESIGN PRINCIPLE (this is what "easy, smart, simple" means here)

**ONE mechanism per job. Every mechanism proves itself. Nothing degrades silently.**

1. **One path, not two.** The extension path and the pure-Python fallback are two paths, and the
   fallback always wins because the fast path was never verified. Pick the ONE path that works and
   make the other one an explicit, loud error - not a silent downgrade.
2. **A claim in the log is a measurement.** "loaded", "installed", "on Argon", "sent" may only be
   printed when the run actually measured it in that same run.
3. **When in doubt, fail loudly.** A dispatch that cannot prove the model, the extension or the
   archive must abort with a clear reason, not continue in a degraded mode.
4. **A fix lives in the repo.** Otherwise the plugin's self-update deletes it and the bug returns.
5. **Keep it small.** Every mechanism added here must REMOVE a branch, not add one.

---

## SECTION 4 — THE HARD RULES (non-negotiable; all of them are required)

- **R1** The only sendable model is `Code & analysis 4 Argon`. Hydrogen and Flash are both
  rejected. The model picker must be read AFTER the selection and BEFORE the send, and a missing
  picker is a failure, not a pass. The pre-flight log must name the model it actually found, and
  the reply's model name must be checked too.
- **R2** The companion extension must be INSTALLED and VERIFIED through the CDP `Extensions` domain
  (`Extensions.getExtensions` / `Extensions.loadUnpacked`), never inferred from a directory. It must
  be re-installed automatically whenever the browser comes up (Chrome discards an unpacked
  extension on every restart - measured three ways: hard kill, graceful close,
  developer_mode=on - all empty). `--disable-extensions-except` must NEVER be passed to Chrome: it
  BLOCKS `loadUnpacked` (measured). The log must print the extension id, and the word "loaded"
  may only be used when it was verified.
- **R3** The four bridge fixes listed in F3 must land in THIS repository, and the repo must carry
  the re-appliable patch scripts (under `skills/argon-coding-bridge/scripts/patches/`) so a plugin
  self-update can never silently regress the bridge. After the plugin's self-update runs, the
  bridge must still pass Section 6.
- **R4** The follow-up lane and the new lane must not run concurrently against the same Argon
  browser. When the second one finds the profile busy it must WAIT for the first one's browser
  and ATTACH to it - never die, and never rotate onto another account.
- **R5** The wait deadline must default to at least 90 minutes, and the delivery capture must
  survive a lane giving up: the patch can still be captured from the conversation afterwards.
- **R6** The patch's `Old` blocks must be copied **VERBATIM** from the archive. Measured in round
  22: two of Argon's nine `Old` blocks did not match the tree because they were reconstructed from
  memory, and its `New` blocks were written against a pre-round-21 shape, so applying them verbatim
  would have DELETED the round-21 fix. If a block does not match the archive, the patch is wrong.
- **R7** Nothing from rounds 19-21 may regress: the live browser control loop, the browser that
  never closes by itself, the always-in-sync account pool, the fatal busy-slot rule and the
  90-second host wait.
- **R8** No silent degradation anywhere: no `except Exception: pass` around a step that decides
  whether the fast path is usable.
- **R9** Nothing may touch `config/accounts_pool.json`, the account model, the profile layout, the
  migration code or the delivery bundle.
- **R10** The patch must be verifiable by ONE command that proves a single session sends end to
  end, with the model, the extension and the delivery all reported from real measurements.
- **R11** The delivery capture must never depend on clicking into an uncontrolled, overflowing
  panel. It must (a) put the viewer into a known geometry first (collapse it, or scroll the real
  button into a bounded viewport) before clicking, (b) verify that the click actually produced a
  download within a bounded time, and (c) on any doubt fall back to the path that is already
  proven - the Drive text endpoint - instead of hanging. The capture must also leave the viewer
  closed, so the composer is not left overlapped afterwards.
- **R12** The delivery capture must be exercised in the regression suite **with the viewer open and
  overflowing**, not only in the happy path - that is the case the owner actually hit.
- **R13** A wedged page must be DETECTED and RECOVERED, never waited on. Every step that touches
  the page must be bounded by a timeout, and a timeout on the page means "the page is wedged", not
  "try the next step". The tab must be recovered by reloading it (or navigating it back to the
  conversation) through a channel that does not need the wedged renderer, and the lane must then
  continue. After any capture the bridge must PROVE the page is still responsive (a cheap
  round-trip into the page) and say so.
- **R13a** On a wedge the bridge must **STOP, not loop**: it must NOT retry the same operation in a
  loop and it must NOT open new tabs to "try again" - a new tab loads the same heavy UI in the same
  blocked renderer and makes it worse. The recovery is: close the wedged tab; if the CDP endpoint
  does not answer, close the browser GRACEFULLY (the cookie snapshot keeps the session intact) and
  relaunch it (measured: the endpoint answered again in **34 ms** afterwards); then continue.
- **R13b** **One driver at a time.** The bridge must never drive a page while the owner is driving
  the same browser. If the owner has a tab open in the Argon browser, the bridge may attach to that
  browser but must leave the owner's tab alone, and must not leave an overlay on it.
- **R13c** After a wedge the page may answer again but the **send may still not work** (measured:
  "after few try now it release but you are not able to send it again"). So a wedge must end with a
  **fresh page** - reload the conversation, re-set the mode and the model, re-attach, and only then
  paste and send.
- **R14** Every launch flag must be justified by a measurement, and an unsupported flag that
  changes nothing must be removed. `--disable-blink-features=AutomationControlled` must GO
  (measured: `navigator.webdriver` is false with and without it) - it only produces the
  "unsupported command-line flag" banner on every page.
- **R15** A slot number is NOT an account index. `_url_slot_for_account()` must NEVER fall back to
  the slot number; when the account is not signed in it must raise and the lane must stop with a
  clear message (F8).
- **R16** A lane must confirm that its tab is still on ITS OWN conversation before it reads
  anything from it. Measured in round 22: a parallel session navigated the lane's tab to another
  chat, the lane read the other chat's last prompt, concluded "a newer prompt superseded me", and
  gave up while Argon was still working. A wrong-chat read must be detected, not believed.

---

## SECTION 5 — THE FILES

### MODIFY

```
skills/argon-coding-bridge/scripts/dispatch_argon.py     R1, R2, R4, R5, R8, R11, R13, R14, R15, R16
skills/argon-coding-bridge/scripts/safe_browser.py       R2, R4, R14
skills/argon-coding-bridge/scripts/manual_handoff.py     R3 (the self-update must not regress it)
skills/argon-coding-bridge/extension/manifest.json      R2 (the stable "key" - a stable extension id)
```

### CREATE

```
skills/argon-coding-bridge/scripts/test_round23_reliability.py      the regression suite (Section 6)
skills/argon-coding-bridge/scripts/patches/patch_bridge_task_toggle.py
skills/argon-coding-bridge/scripts/patches/patch_bridge_large_archive.py
skills/argon-coding-bridge/scripts/patches/patch_bridge_cdp_setfiles.py
skills/argon-coding-bridge/scripts/patches/patch_bridge_attach_images.py
skills/argon-coding-bridge/patches/README.md                      how to re-apply them after a plugin update
```

The four `patches/*.py` files are the SAME fixes the other session already wrote (they are in
`G:\Developing\Penflow\Penflow-WordCraft-Rust-Agent-Studio\.argon_backup\briefs\`), **ported into
this repository** so they survive the plugin's self-update. Port them, do not rewrite them from
memory - read the workspace copies, and make each one idempotent and re-appliable.

### THE OWNER'S ORIGINAL ASK, DELIVERED AS A FEATURE

The original prompt (Section 0) is about the ONE-CLICK install. It must exist and work:

```
skills/argon-coding-bridge/scripts/dispatch_argon.py   a --install-extension flag: it attaches to
                                                        the running Argon browser, installs the
                                                        extension through the CDP Extensions
                                                        domain, VERIFIES it, opens a Gemini tab,
                                                        reloads it, proves the content script is
                                                        alive (data-argon-companion-extension), and
                                                        exits 0 only then. It must be idempotent.
Open_Argon_Install_Extension.bat                       a one-click launcher for it, in the repo root
skills/argon-coding-bridge/extension/manifest.json     a "key" so the extension id is the same from
                                                        any install path
```

### DO NOT TOUCH

- `skills/argon-coding-bridge/live_loop/**` and `scripts/browser_loop.py` (round 19).
- `skills/argon-coding-bridge/extension/content_script.js` and `service_worker.js`.
- `config/accounts_pool.json`, the account model, the profile layout, the migration code, the
  delivery bundle, and every existing CLI flag's behaviour.

---

## SECTION 6 — THE ACCEPTANCE CRITERIA (this is what "100% bug-proof" means here)

- [ ] `--self-test` runs a **real single-session send** on a throwaway profile and proves:
      the model picker read `Code & analysis 4 Argon` (and a Hydrogen picker is REFUSED), the
      companion extension was installed and VERIFIED (id printed), the archive was attached, the
      prompt was pasted and sent, and the delivery chip was captured.
- [ ] A Hydrogen model picker is refused with a loud message, exactly like Flash.
- [ ] No log line ever claims "loaded", "installed", "on Argon" or "sent" without a measurement
      behind it in that same run.
- [ ] `--install-extension` exists, is idempotent, and exits 0 only after the content script is
      confirmed alive on a real Gemini page.
- [ ] The extension shows up in `chrome://extensions` as "Argon Coding Bridge Companion".
- [ ] The repository contains all four bridge fixes, and `skills/argon-coding-bridge/scripts/patches/`
      contains the re-appliable scripts.
- [ ] Running the plugin's self-update (`Open_Argon_Update.bat`) and then `--self-test` still
      passes - the update can no longer silently regress the bridge.
- [ ] The two lanes cannot fight: a second lane that finds the profile busy WAITS for the first
      lane's browser and attaches to it, and the operator's launcher runs them SEQUENTIALLY.
- [ ] A lane that finds its tab on a DIFFERENT conversation detects it and reports it instead of
      declaring itself superseded (R16).
- [ ] The wait deadline defaults to at least 90 minutes.
- [ ] The delivery capture survives the **overflowing viewer**: the regression suite opens the
      generated-file viewer, lets it overflow, and still captures the patch - and the viewer is
      closed afterwards, with the composer no longer overlapped.
- [ ] **A wedged page is detected and recovered**: the regression suite wedges a page on purpose
      (a blocked renderer), and the bridge must notice, reload the tab through a path that does
      not need the wedged renderer, continue, and report the recovery - instead of hanging until
      its deadline. It must NOT open new tabs to try again, and after the recovery it must use a
      FRESH page (reload, re-set the mode and the model, re-attach) before pasting and sending,
      because a released page may still refuse to send.
- [ ] The browser no longer shows the "unsupported command-line flag" banner, and
      `navigator.webdriver` is still `false` (measured after the change).
- [ ] A slot whose account is not signed in is REFUSED with a clear message; no code path can
      turn a pool slot number into an account index.
- [ ] `test_round23_reliability.py` passes every case and exits 0.
- [ ] `--browser-loop-selftest` (round 19) and both round-20/21 suites still pass.
- [ ] `config/accounts_pool.json` is untouched; no credential appears anywhere in the patch.

---

## SECTION 7 — THE DELIVERY CONTRACT

Deliver **ONE** `apply_changes.ps1` containing every change.

1. **New files** are delivered as a file entry with a single `New` block and **no `Old` block at
   all** - that is how the bridge's pre-flight recognises "create this file". The apply script must
   create the parent directory, write UTF-8 without BOM, then re-read and verify.
2. **Existing files** use a `Blocks` entry with BOTH `Old` and `New`, where `Old` is the **EXACT
   current text, copied verbatim from the archive you received** (R6).
3. The two-phase rule still applies: **Phase 1 checks every `Old` block of every file before the
   first file is written**; a missing target throws while the project is untouched. Phase 2 backs
   up, writes, and re-reads to verify.
4. Use `[System.StringComparison]::Ordinal` + `[System.Text.StringBuilder]`, single-quoted
   here-strings (`@' ... '@`), and UTF-8 without BOM. Never `.Replace()` on a large needle.
5. The NOTES block must carry a `## ROOT CAUSE` section naming **all eight** facts from Section 2
   with their `file:line`, and a table of every change.
6. The chat reply stays short: what changed, a table of the files, how to verify, and the pointer
   to the attached `apply_changes.ps1`.

---

## SECTION 8 — HOW TO VERIFY (run these; report the REAL output)

```powershell
# 1. syntax
python -m py_compile skills\argon-coding-bridge\scripts\dispatch_argon.py `
                      skills\argon-coding-bridge\scripts\safe_browser.py `
                      skills\argon-coding-bridge\scripts\manual_handoff.py `
                      skills\argon-coding-bridge\scripts\test_round23_reliability.py

# 2. the new reliability suite
python skills\argon-coding-bridge\scripts\test_round23_reliability.py

# 3. the bridge's own self-test - it now proves a REAL single-session send
python skills\argon-coding-bridge\scripts\dispatch_argon.py --self-test

# 4. the one click the owner asked for
python skills\argon-coding-bridge\scripts\dispatch_argon.py --install-extension

# 5. the previous rounds must not regress
python skills\argon-coding-bridge\scripts\test_round20_lifecycle_and_sync.py
python skills\argon-coding-bridge\scripts\test_round21_dispatch_lane.py
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-loop-selftest
```

Put these exact commands in the NOTES block as `VERIFICATION COMMAND` and paste the REAL output of
steps 2, 3 and 4 into the chat reply. **Never report a measurement you did not take.**
