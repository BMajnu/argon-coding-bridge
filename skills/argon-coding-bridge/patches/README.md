# Argon Coding Bridge — Re-Appliable Bridge Fix Scripts (R3 / F3)

These four idempotent patch scripts live inside the repository under
`skills/argon-coding-bridge/scripts/patches/` so that running `Open_Argon_Update.bat`
(`sync_plugin.py`) preserves them across every plugin self-update and across every
workspace project:

| Patch Script | Bridge Fix | Target |
|---|---|---|
| `skills/argon-coding-bridge/scripts/patches/patch_bridge_task_toggle.py` | `_is_toggle_control()` guard so Chat/Task detection only inspects real segmented toggle controls and never matches prose in the chat transcript | `dispatch_argon.py` |
| `skills/argon-coding-bridge/scripts/patches/patch_bridge_large_archive.py` | `MAX_DATATRANSFER_BASE64_ARCHIVE_BYTES` (15 MB raw cap) checked before reading/base64-encoding archives so 47 MB+ archives never crash the renderer | `dispatch_argon.py` |
| `skills/argon-coding-bridge/scripts/patches/patch_bridge_cdp_setfiles.py` | `_attach_zip_via_cdp_set_files()` using CDP `DOM.setFileInputFiles` with the file input's `objectId` for zero-base64, zero-dialog large archive uploads | `dispatch_argon.py` |
| `skills/argon-coding-bridge/scripts/patches/patch_bridge_attach_images.py` | `attach_images()` + `--attach-images` CLI flag for injecting PNG/JPEG/WebP files into the composer's hidden file input without opening the OS file dialog | `dispatch_argon.py` |

**The toggle guard belongs in ONE place only:** `_task_toggle_present()`, the mode detection.
Applying it to the chat-title lookup would blank the conversation title, and applying it to the
guest-page detection would stop the bridge from recognising a guest page - Gemini would then
answer *"argon_context.zip did not come through"*. `patch_bridge_task_toggle.py` therefore patches
that one function and nothing else.

## How to Verify or Re-Apply After a Plugin Update

All four fixes are already landed directly in `skills/argon-coding-bridge/scripts/dispatch_argon.py`.
Each script is idempotent: running it checks whether its fix is already present in `dispatch_argon.py`
(returning `status="already_applied"` with 0 modifications) or applies it cleanly if missing:

```powershell
python skills\argon-coding-bridge\scripts\patches\patch_bridge_task_toggle.py
python skills\argon-coding-bridge\scripts\patches\patch_bridge_large_archive.py
python skills\argon-coding-bridge\scripts\patches\patch_bridge_cdp_setfiles.py
python skills\argon-coding-bridge\scripts\patches\patch_bridge_attach_images.py
```

After syncing the installed plugin copies (`Open_Argon_Update.bat` or `python skills\argon-coding-bridge\scripts\sync_plugin.py`),
verify the bridge end-to-end with:

```powershell
python skills\argon-coding-bridge\scripts\dispatch_argon.py --self-test
```