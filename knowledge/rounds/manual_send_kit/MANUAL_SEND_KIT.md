# ROUND 23 - MANUAL SEND KIT
### (use this only if the automated dispatch fails)

Everything here is already prepared. Nothing needs research or analysis.

---

## 1. WHAT IS GOING TO ARGON

**The brief (read this first, it is the whole task):**
`knowledge/rounds/2026-11-13_round23_reliability.md` in the repository, and it is also
`TASK_BRIEF.md` inside `argon_context.zip`.

**The ZIP to attach:** `G:\Developing\argon-coding-bridge\argon_context.zip`
(about 2.0 MB, 58 entries, no credentials inside).

---

## 2. THE TWO CONVERSATIONS

| Lane | Account | Chat to use |
|---|---|---|
| **Follow-up** | `badiuzzaman.majnu@gmail.com` (browser `/u/0/`) | `https://gemini.google.com/u/0/app/90f2dd2cf2aae6e8` |
| **New** | `badiuzzaman.majnu1@gmail.com` (browser `/u/6/`) | `https://gemini.google.com/u/6/app` (a fresh chat) |

**Send the follow-up lane FIRST and let it finish. Only then the new lane.**
Two lanes at the same time on the same browser fight over it (measured in round 22: one
lane dies with "profile is busy" and the other loses its tab).

---

## 3. THE PROMPT TEXT (paste it as one message)

* Follow-up lane: `scratch\round23_manual\followup_prompt.md`
* New lane: `scratch\round23_manual\new_prompt.md`

Both files are plain text - open them, select all, copy.

---

## 4. WHY THE AUTOMATED DISPATCH CANNOT FINISH (measured twice today)

```
prompt paste   (python_datatransfer) : OK (48469 chars in composer)
attachment argon_context.zip (2031048 bytes) (python_fallback) : FAILED
                                            (the upload menu rendered no file input)
```

The paste works. **The ATTACH fails** - and the attach is exactly what the companion extension
exists for (it injects the file into the composer's hidden `input[type=file]`, no OS file dialog).
The extension is never installed (Chrome 137+ removed `--load-extension`), so the pure-Python
path has to open Gemini's upload menu, Gemini renders no file input, and the run dies at the
attach. That is why "not even a single session sends properly".

**So for THIS round, attach the ZIP by hand.** It is the one step the machine cannot do.

---

## 5. THE MODEL - THIS IS THE PART THAT WAS BROKEN

The Gemini model picker holds TWO models:

```
[ ]  Everyday help      4 Hydrogen     <- WRONG. Every task has been running on this.
[ ]  Code & analysis    4 Argon         <- CORRECT. Pick THIS one.
```

Before you send, open the model picker and **select "Code & analysis 4 Argon"**.
Then check the chip in the composer reads Argon. If it reads Hydrogen, do NOT send.

The reply must come from Argon. If the footer's model name says Hydrogen, the task was
answered by the wrong model - send it again after selecting Argon.

---

## 5. THE ATTACHMENT

Attach `argon_context.zip` to the message. Wait until the chip shows the upload finished
(it can take a minute for 2 MB). Only then send.

---

## 6. WHEN ARGON ANSWERS

- Argon writes `apply_changes.ps1` and attaches it as a **file chip**.
- Click the chip's **Download** button. If the viewer panel overflows the screen and the
  composer covers it, that is a known bug (round 23, rule R11) - collapse the panel, or use
  the file's **Drive** option, and tell me - it is being fixed in this very round.
- Save it as `apply_changes.ps1` somewhere obvious (for example
  `G:\Developing\argon-coding-bridge\downloads\dual\round23\manual\apply_changes.ps1`).

---

## 7. HOW TO APPLY IT

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

## 8. THE AUTOMATED DISPATCH (if you want the machine to do it)

```powershell
python G:\Developing\argon-coding-bridge\scratch\launch_round23.py both
```

`followup` runs the follow-up lane only, `new` runs the new lane only, `both` runs them one
after the other (never together).

The follow-up lane gets a private task-state file
(`%TEMP%\argon_state_round23_followup.json`), a 90-minute wait deadline
(`--deadline-seconds 5400`), and the pre-built archive.

**THE HALF-AUTOMATIC WAY (recommended):** do the model and the attachment by hand (Sections 5
and 6), then let the dispatcher paste and send:

```powershell
python G:\Developing\argon-coding-bridge\scratch\dispatch_argon.py --project-path G:\Developing\argon-coding-bridge `
  --slot /u/0/ --lane followup --manual-setup --deadline-seconds 5400 `
  --delivery-dir G:\Developing\argon-coding-bridge\downloads\dual\round23\followup
```

`--manual-setup` means: do NOT change the mode, the model or the attachment - they are already
done by hand - just attach nothing, paste the prompt and send it, then wait for the delivery.

---

## 9. IF THE DISPATCH GAVE UP BEFORE ARGON FINISHED

That happened in round 22: the lane's deadline expired while Argon was still writing. The
conversation is on the server, so the patch is still there. Open the chat, wait for the chip,
and download it by hand (Section 6). You do NOT need to send the prompt again.
