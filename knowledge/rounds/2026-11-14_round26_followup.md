ADDENDUM TO THE SAME ROUND - same bug, but I just measured the ROOT CAUSE. Please fold this into the delivery you are already building. Everything else in the round stays as it is.

## The duplicate send is NOT only the python-side composer clear

The root cause is in the COMPANION EXTENSION:
`skills/argon-coding-bridge/extension/content_script.js`

- `paste_prompt` stores `companionRuntime.cachedPromptText = text`, sets
  `companionRuntime.keepPromptAlive = true`, and calls `installComposerMutationGuard()`.
- The MutationObserver re-pastes the cached prompt whenever the composer is cleared.
  It CANNOT tell "the archive upload cleared the composer" (which is what it is FOR -
  re-paste before the send) from "the SEND cleared the composer" (which happens AFTER).
- So the sequence is:
  1. paste -> observer armed
  2. archive upload clears the composer -> observer re-pastes (correct, intended)
  3. the send happens -> the composer is empty -> **the observer re-pastes the prompt again**
  4. the extension's own `click_send` verification then sees the prompt still in the
     composer -> it reports FAILURE (`ok: false`)
  5. the pure-Python fallback runs, finds the prompt in the composer, and **clicks send
     AGAIN** -> a SECOND TURN in the same conversation.

## MEASURED (2026-11-14, two lanes)

Round 25 lane, chat `https://gemini.google.com/u/6/app/33c76e3f17784385`:
```
[+] PROMPT SENT at 12:39:04          <- ONE send in the bridge's own log
```
but the live DOM holds TWO `user-query` elements, both with the full 30,848-character prompt
(the second one inside a parent with the class `queued-steering-turn`), and TWO
`model-response` elements.

Round 26 lane (the one you are working on), chat
`https://gemini.google.com/u/4/app/d0815dd9c06ea36d`: exactly the same - two `user-query`
elements (27,646 / 27,633 chars) and two `model-response` elements.

The bridge's own log shows ONE send. So the second turn is produced by the leftover re-paste
described above. **Every dispatch currently costs TWO Gemini turns.**

## REQUIRED (this OVERRIDES the round-26 rule "do not touch the extension" - the extension IS
the root cause and is now in scope)

1. `keepPromptAlive` must be turned OFF as soon as a send has been performed, and
   `cachedPromptText` must be cleared with it. The observer must never re-paste the prompt
   after the conversation has gained a turn.
2. The observer may only re-paste BEFORE the send - that is the only reason it exists (the
   archive upload clears the composer).
3. The extension's `click_send` must NOT report failure because the prompt is still in the
   composer when the observer re-pasted it. It must report SUCCESS once it has performed the
   send, so the pure-Python fallback does not send a second time.
4. The python side of the round (clear the composer after a verified send, always log the
   outcome) stays exactly as briefed - it is the belt to this pair of braces.

## Also

- The extension is loaded via CDP `Extensions.loadUnpacked` and the bridge re-installs it on
  every browser start, so the fix is live on the next browser start. If the self-test can
  reload the extension itself, do that; otherwise say so in the delivery notes.
- Keep the round's other work unchanged: the python-side composer clear, the
  `queued-steering-turn` = "already sent" guard, and `test_round26_composer_clear.py`.

Deliver ONE `apply_changes.ps1` for the whole round, including this extension fix.
