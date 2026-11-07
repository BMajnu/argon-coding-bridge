# The rewritten _is_argon_working dropped the role progressbar check

- **ID:** `20261107-193414-the-rewritten-is-argon-working-dropped-t`
- **Created At:** 2026-11-07 19:34:14
- **Project:** `G:\Developing\argon-coding-bridge`
- **Branch:** `fixes/dispatch-argon`
- **Commit:** `(uncommitted)`
- **Tags:** python, guard, round13

## Scope
- `skills/argon-coding-bridge/scripts/dispatch_argon.py`

## Symptom
test_carry_over failed: a spinner counts as working (the chat is still loading)

## Root Cause
The round 13 rewrite of _is_argon_working moved loader detection strictly inside the newest assistant turn and dropped the page level role progressbar check that the baseline had, so a chat that is still loading but has no assistant turn yet was no longer seen as working

## Fix
Restored the role progressbar check in the page level fallback that runs only after the newest assistant turn has been ruled out, so a finished chat still counts as not working and a loading chat still counts as working

## Verified By
test_carry_over is back to 21 of 21 and the measured false positive on a finished chat is still False
