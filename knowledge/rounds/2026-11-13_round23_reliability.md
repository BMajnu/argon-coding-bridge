# ROUND 23 — Make the Argon bridge reliable: ONE session, ONE model, ONE browser, VERIFIED

You are the coding model for the **Argon Coding Bridge**. This is round 23.

Rounds 19-22 are in the tree and must not be regressed. Read `knowledge/rounds/` for their briefs.

**This round is not a feature round. It is a reliability round.** The owner says the bridge has
become riddled with bugs and that *not even a single session sends properly any more*. Your job is
to make one session send, reliably, on the right model, with every step proved - and to make the
whole thing SIMPLE.

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

The owner's words:

> "আমাদের পুরো আর্গন ব্রিজ কোডিং এজেন্টটিতে বাগের ছড়াছড়ি... কোনো একটি সিঙ্গেল কাজও ঠিকমতো হচ্ছে না... একটি সিঙ্গেল সেশনও ঠিকমতো পাঠাতে
> পারছে না... আমি এমন একটি ইজি, স্মার্ট ও সহজ ওয়ে চাই, যেখানে কোনো ধরনের সিঙ্গেল বাগ থাকবে না এবং এটি ১০০% বাগপ্রুফ হবে।"

= *The whole Argon bridge coding agent is full of bugs; not a single job works properly. Not even a
single session can be sent properly. I want an easy, smart and simple way where there is no single
bug and it is 100% bug-proof.*

**The deliverable of this round is therefore a BRIDGE THAT PROVES ITSELF, not a new feature.**

---

## SECTION 2 — THE FIVE MEASURED FACTS THAT EXPLAIN EVERY SYMPTOM

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

**R1 (hard rule): the only model the bridge may ever send on is `Code & analysis 4 Argon`.**
Hydrogen is rejected exactly like Flash. The guard must read the model picker AFTER the selection
and BEFORE the send, and it must treat "no model picker at all" as a failure, not as a pass.

### F2 — The companion extension was never installed

Chrome 137+ removed `--load-extension` from the branded build. Measured on Chrome 157: four
launch configurations, every one of them `Extensions.getExtensions: []`. The live Argon browser
carries the flag on its command line and still has **0 extensions**, `chrome://extensions` lists
nothing, and the content-script marker is null on every page. The bridge nevertheless printed
`[+] Companion extension loaded from: ...` - it had only proved the DIRECTORY existed. Because
`_extension_is_available()` was therefore always false, **every** attach, paste, send and delivery
capture silently degraded to the pure-Python path - which is where the uploads hang and fail.

The fix is measured and works: the DevTools Protocol's `Extensions.loadUnpacked` installs the
unpacked extension into the RUNNING browser in **0.03 s**, it then appears in `chrome://extensions`
as "Argon Coding Bridge Companion", and its content script really runs (marker `1.16.0`).

**R2 (hard rule): the bridge may only claim the extension is available when it has VERIFIED it**
through `Extensions.getExtensions` (and the service-worker target), and it must re-install it
automatically at every browser start, because Chrome discards an unpacked extension on every
restart (measured three ways: hard kill, graceful close, developer_mode=on - all empty).

### F3 — The bridge's own fixes live in a WORKSPACE BACKUP, not in the repo

This is why the same bugs keep coming back. The following four bridge fixes exist ONLY as scripts
under

```
G:\Developing\Penflow\Penflow-WordCraft-Rust-Agent-Studio\.argon_backup\briefs\
    patch_bridge_task_toggle.py        (5,106 B)
    patch_bridge_large_archive.py      (5,106 B -> patch_bridge_large_archive.py)
    patch_bridge_cdp_setfiles.py       (6,713 B)
    patch_bridge_attach_images.py      (9,463 B)
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

**R3 (hard rule): the REPOSITORY is the only source of truth for the bridge.** A fix that lives in
a workspace's `.argon_backup` is a fix that does not exist. Land these four fixes in the repo.

### F4 — Two lanes fight over the ONE browser

Round 22, measured: both lanes started at the same instant, neither found a running Chrome, so
both tried to LAUNCH one on the same profile. One won the profile lock; the other died with
`Profile ... is busy. Refusing to hijack the running browser.` and exit code 7 - the exact log line
is "profile is busy". The other lane's tab was then navigated away by a PARALLEL session using the
same browser, so the lane declared itself "superseded" and gave up while Argon was still writing
the answer.

**R4 (hard rule): the two lanes must never run concurrently against the same Argon browser.**
The dispatcher must run the follow-up lane to completion, and only then the new lane - or the two
lanes must use genuinely separate browser hosts.

### F5 — The lane's deadline is 30 minutes

Round 22: the prompt was sent at 15:11:39; the lane gave up at 15:38; Argon was STILL writing
("Created Round 22 hermetic regression suite..."). The delivery was therefore never captured.

**R5 (hard rule): the wait deadline must be long enough for a real patch to be written** (the
bridge already accepts `--deadline-seconds`; the DEFAULT must be raised, and the launcher used by
the operators must pass a generous value).

### F6 — The delivery viewer OVERFLOWS the screen, the composer lands on top of it, and then the WHOLE TAB HANGS

The owner reports (and the screenshot shows) that when the **Download** button is clicked - by the
owner or by the agent - the download section sometimes **hangs** and the whole screen **overflows**:
the generated-file viewer expands into a full-height code panel, the floating `Download / Drive`
menu is left hanging over it, the composer ("Ask a follow-up") is drawn ON TOP of the code, and the
Download button can no longer be clicked (or the click lands on nothing).

**And then it gets worse - the whole browser wedges.** Measured on the owner's machine while this
round was being dispatched:

- The tab **stops responding entirely**: it takes **no click anywhere at all**, not even on the
  sidebar, not on the model chip, not on the composer. It is not "the button is in the wrong
  place" - the page is dead.
- The CDP endpoint stops answering too: `connect_over_cdp` reaches "ws connected" and then
  **times out**, and the DevTools HTTP endpoint stops responding. The browser process is alive but
  its renderer/main thread is blocked.
- The browser had been wedged for **about an hour** (from ~15:38, when the round-22 capture left
  the viewer open, until ~16:41 when the owner noticed). Every dispatch in that window was
  affected - the round-23 lane started at 16:36 and made **zero progress** because the page it
  needed to drive was already dead.
- The owner's own work was blocked too: a parallel session's tabs sit in the same browser.

That is exactly the operation the bridge performs to collect `apply_changes.ps1`. The bridge clicks
into a panel whose geometry it does not control, in a page that can reflow to any height, and then
leaves the panel open.

**R11 (hard rule): the delivery capture must never depend on clicking into an uncontrolled,
overflowing panel.** It must (a) put the viewer into a known geometry first (collapse it, or
scroll the real button into a bounded viewport) before clicking, (b) verify that the click actually
produced a download within a bounded time, and (c) on any doubt fall back to the path that is
already proven - the Drive text endpoint - instead of hanging. The capture must also leave the
viewer closed, so the composer is not left overlapped afterwards.

**R13 (hard rule): a wedged page must be DETECTED and RECOVERED, never waited on.**
- Every step that touches the page must be bounded by a timeout, and a timeout on the page must be
  treated as "the page is wedged", not as "try the next step".
- A wedged tab must be recovered by reloading it (or navigating it back to the conversation)
  through a channel that does not need the wedged renderer, and the lane must then continue.
- The capture must never leave the viewer open: close it and put the composer back before the
  lane reports anything to the owner.
- After any capture attempt, the bridge must PROVE the page is still responsive (a cheap
  round-trip into the page) and say so. A page that does not answer is a failed run, and the
  failure must be reported with the recovery that was attempted.

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

## SECTION 4 — THE HARD RULES (non-negotiable)

- **R1** The only sendable model is `Code & analysis 4 Argon`. Hydrogen and Flash are both
  rejected. The model picker must be read after the selection and before the send, and a missing
  picker is a failure. The pre-flight log must name the model it actually found.
- **R2** The companion extension must be INSTALLED and VERIFIED through the CDP `Extensions`
  domain (`getExtensions` / `loadUnpacked`), never inferred from a directory. It must be
  re-installed automatically whenever the browser comes up, and the log must print the extension id.
  `--disable-extensions-except` must never be passed to Chrome (it BLOCKS `loadUnpacked`, measured).
- **R3** The four bridge fixes listed in F3 must land in THIS repository, and the repo must carry
  the re-appliable patch scripts so a plugin self-update can never silently regress the bridge. The
  self-update path must be made safe: after it runs, the bridge must still pass the checks in
  Section 6.
- **R4** The follow-up lane and the new lane must not run concurrently against the same Argon
  browser. When they must run together, the second one waits for the first one's browser to be
  free and attaches to it instead of dying.
- **R5** The wait deadline must default to at least 90 minutes, and the delivery capture must
  survive a lane giving up (the patch can still be captured from the conversation afterwards).
- **R6** The patch's `Old` blocks must be copied **VERBATIM** from the archive. Measured in round
  22: two of Argon's nine `Old` blocks did not match the tree because they were reconstructed from
  memory, and its `New` blocks were written against a pre-round-21 shape, so applying them verbatim
  would have DELETED the round-21 fix. If a block does not match the archive, the patch is wrong.
- **R7** Nothing from rounds 19-22 may regress: the live browser control loop, the browser that
  never closes by itself, the always-in-sync account pool, the fatal busy-slot rule, and the
  round-21/22 host wait.
- **R8** No silent degradation anywhere: no `except Exception: pass` around a step that decides
  whether the fast path is usable.
- **R9** Nothing may touch `config/accounts_pool.json`, the account model, the profile layout, the
  migration code or the delivery bundle.
- **R10** The patch must be verifiable by ONE command that proves a single session sends end to
  end, with the model, the extension and the delivery all reported from real measurements.
- **R11** The delivery capture must not click into an uncontrolled, overflowing viewer panel: put
  the viewer into a known geometry first, verify the click produced a download within a bounded
  time, and fall back to the proven Drive text endpoint instead of hanging. The viewer must be left
  closed so the composer is not overlapped afterwards.
- **R12** The delivery capture must be exercised in the regression suite **with the viewer open
  and overflowing**, not only in the happy path - that is the case the owner actually hit.

---

## SECTION 5 — THE FILES

### MODIFY

```
skills/argon-coding-bridge/scripts/dispatch_argon.py     R1, R2, R4, R5, R8
skills/argon-coding-bridge/scripts/safe_browser.py       R2, R4
skills/argon-coding-bridge/scripts/manual_handoff.py     R3 (the self-update must not regress it)
```

### CREATE

```
skills/argon-coding-bridge/scripts/test_round23_reliability.py   the regression suite
skills/argon-coding-bridge/scripts/patches/patch_bridge_task_toggle.py
skills/argon-coding-bridge/scripts/patches/patch_bridge_large_archive.py
skills/argon-coding-bridge/scripts/patches/patch_bridge_cdp_setfiles.py
skills/argon-coding-bridge/scripts/patches/patch_bridge_attach_images.py
skills/argon-coding-bridge/patches/README.md                     how to re-apply them after a plugin update
```

The four `patches/*.py` files are the SAME fixes the other session already wrote (they are in
`G:\Developing\Penflow\Penflow-WordCraft-Rust-Agent-Studio\.argon_backup\briefs\`), **ported into
this repository** so they survive the plugin's self-update. Port them, do not rewrite them from
memory - read the workspace copies, and make each one idempotent and re-appliable.

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
- [ ] The repository contains all four bridge fixes, and `skills/argon-coding-bridge/scripts/patches/`
      contains the re-appliable scripts.
- [ ] Running the plugin's self-update (`Open_Argon_Update.bat`) and then `--self-test` still
      passes - the update can no longer silently regress the bridge.
- [ ] The two lanes cannot fight: a second lane that finds the profile busy WAITS for the first
      lane's browser and attaches to it, and the operator's launcher runs them SEQUENTIALLY.
- [ ] The wait deadline defaults to at least 90 minutes.
- [ ] `test_round23_reliability.py` passes every case and exits 0.
- [ ] `--browser-loop-selftest` (round 19) and both round-20/21 suites still pass.
- [ ] `config/accounts_pool.json` is untouched; no credential appears anywhere in the patch.
- [ ] The delivery capture survives the **overflowing viewer**: the regression suite opens the
      generated-file viewer, lets it overflow, and still captures the patch - and the viewer is
      closed afterwards, with the composer no longer overlapped.
- [ ] **A wedged page is detected and recovered**: the regression suite wedges a page on purpose
      (a blocked renderer), and the bridge must notice, reload the tab through a path that does
      not need the wedged renderer, continue, and report the recovery - instead of hanging until
      its deadline.

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
5. The NOTES block must carry a `## ROOT CAUSE` section naming **all five** facts from Section 2
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

# 4. the previous rounds must not regress
python skills\argon-coding-bridge\scripts\test_round20_lifecycle_and_sync.py
python skills\argon-coding-bridge\scripts\test_round21_dispatch_lane.py
python skills\argon-coding-bridge\scripts\dispatch_argon.py --browser-loop-selftest
```

Put these exact commands in the NOTES block as `VERIFICATION COMMAND` and paste the REAL output of
steps 2 and 3 into the chat reply. **Never report a measurement you did not take.**
