# ROUND 23 - THE MANUAL SEND KIT (both lanes)

Everything here is ready. Nothing needs research or analysis.

---

## 0. FIRST: IF THE BROWSER HANGS

The owner reported this exact symptom, so read it before you start:

> "Always it keeping hanged and not taking click or response on it and you are trying with creating new tab and
> trying again and again but same. But after few try now it release but you are not able to send it again."

**What it is:** the page stops taking clicks ANYWHERE (not just the Download button - the sidebar, the
model chip, the composer). The renderer is blocked. The CDP endpoint stops answering too
(`connect_over_cdp` reaches "ws connected" and then times out).

**What does NOT work:** opening new tabs and trying again. A new tab loads the same heavy UI in the
same blocked renderer and makes it worse.

**What DOES work, in this order:**
1. Close the wedged tab.
2. If the browser still does not answer, close the WHOLE browser gracefully - it flushes the cookies
   and the session survives:
   ```powershell
   python G:\Developing\argon-coding-bridge\skills\argon-coding-bridge\scripts\dispatch_argon.py --close-browser
   ```
3. Open it again:
   ```powershell
   python G:\Developing\argon-coding-bridge\skills\argon-coding-bridge\scripts\dispatch_argon.py --open-browser --slot /u/0/
   ```
   (measured: the CDP endpoint answered again in **34 ms** afterwards)
4. **Use a FRESH page.** A released page may still refuse to send - so reload the conversation,
   re-set the model, re-attach the ZIP, and only then paste and send.

---

## 1. WHAT IS GOING TO ARGON

**The brief (the whole task):** `knowledge/rounds/2026-11-13_round23_reliability.md` in the
repository. It is also `TASK_BRIEF.md` inside the archive.

**The ZIP to attach:** `G:\Developing\argon-coding-bridge\argon_context.zip`
(about 2.0 MB, 59 entries, no credentials inside).

---

## 2. THE TWO LANES

| Lane | Account | Conversation |
|---|---|---|
| **Follow-up** | `badiuzzaman.majnu@gmail.com` (browser `/u/0/`) | `https://gemini.google.com/u/0/app/90f2dd2cf2aae6e8` |
| **New** | `badiuzzaman.majnu1@gmail.com` (browser `/u/6/`) | `https://gemini.google.com/u/6/app` (a fresh conversation) |

**Send the follow-up lane FIRST and let it finish. Only then the new lane.**
Two lanes at the same time on the same browser fight over it (measured in round 22: one lane dies
with "profile is busy" and the other loses its tab).

---

## 3. THE PROMPT TEXT

* Follow-up lane: `followup_prompt.md` (in this folder)
* New lane: `new_prompt.md` (in this folder)

Both are plain text - open them, select all, copy. **They are also pasted automatically by the
`.cmd` files below**, so you do not have to copy them by hand if you use those.

---

## 4. THE MODEL - THIS IS THE PART THAT WAS BROKEN

The Gemini model picker holds TWO models:

```
[ ]  Everyday help      4 Hydrogen     <- WRONG. Every task has been running on this.
[ ]  Code & analysis    4 Argon         <- CORRECT. Pick THIS one.
```

Before you send, open the model picker and **select "Code & analysis 4 Argon"**.
Then check the chip in the composer reads Argon. If it reads Hydrogen, do NOT send.

The reply must come from Argon. If the footer's model name says Hydrogen, the task was answered by
the wrong model - send it again after selecting Argon.

---

## 5. THE ATTACHMENT - BY HAND, THIS TIME

The machine cannot do this one right now: without the companion extension the pure-Python path has
to open Gemini's upload menu, and the current Gemini UI renders no file input. Measured:

```
prompt paste   (python_datatransfer) : OK (48469 chars in composer)
attachment argon_context.zip (python_fallback) : FAILED (the upload menu rendered no file input)
```

So: press **+** -> upload the file -> pick
`G:\Developing\argon-coding-bridge\argon_context.zip` -> wait until the chip shows the upload
finished. **Do not paste until the chip is there.**

---

## 6. SEND

**Either fully by hand:** paste the prompt from step 3 into the composer and press send.

**Or half-automatically:** after you have set the model and attached the ZIP, double-click

* `SEND_followup.cmd`   (the follow-up lane)
* `SEND_new.cmd`        (the new lane)

They open the conversation, paste the right prompt, send it, and wait up to 90 minutes for the
delivery into `downloads\dual\round23\<lane>\`.

---

## 7. WHEN ARGON ANSWERS

- Argon writes `apply_changes.ps1` and attaches it as a **file chip**.
- Click the chip's **Download** button. If the viewer panel overflows the screen and the composer
  covers it, that is the known bug (round 23, rules R11/R13) - close the panel, or use the file's
  **Drive** option, and say so in the reply - it is being fixed in this very round.
- Save it as `apply_changes.ps1` somewhere obvious, e.g.
  `G:\Developing\argon-coding-bridge\downloads\dual\round23\manual\apply_changes.ps1`.

---

## 8. HOW TO APPLY IT

```powershell
# 1. back up the tree first
Copy-Item -Recurse -Force G:\Developing\argon-coding-bridge\skills `
                        G:\Developing\argon-coding-bridge\.argon_backup\r23_manual_backup\skills

# 2. apply the patch
pwsh -NoProfile -ExecutionPolicy Bypass -File <the folder>\apply_changes.ps1

# 3. verify
python G:\Developing\argon-coding-bridge\skills\argon-coding-bridge\scripts\dispatch_argon.py --self-test
python G:\Developing\argon-coding-bridge\skills\argon-coding-bridge\scripts\test_round23_reliability.py
```

---

## 9. THE FULLY AUTOMATIC DISPATCH (for reference)

```powershell
python G:\Developing\argon-coding-bridge\scratch\launch_round23.py both
```

`followup` runs the follow-up lane only, `new` runs the new lane only, `both` runs them one after
the other (never together). The follow-up lane gets a private task-state file
(`%TEMP%\argon_state_round23_followup.json`), a 90-minute wait deadline, and the pre-built
archive.

**Known to fail today** at the attach step, for the reason in Section 5 - that is exactly what
this round fixes.

---

## 10. WHAT THE FILES ARE

| File | What it is |
|---|---|
| `followup_prompt.md` | the exact prompt for the follow-up lane |
| `new_prompt.md` | the exact prompt for the new lane |
| `SEND_followup.cmd` | double-click: opens the chat, pastes, sends, waits for the delivery |
| `SEND_new.cmd` | the same for the new lane |
| `MANUAL_SEND_KIT.md` | this document |

---

## 11. IF THE DISPATCH GAVE UP BEFORE ARGON FINISHED

That happened in round 22: the lane's deadline expired while Argon was still writing. The
conversation is on the server, so the patch is still there. Open the chat, wait for the chip, and
download it by hand (Section 7). You do NOT need to send the prompt again.
