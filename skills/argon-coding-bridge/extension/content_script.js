// MV3 Companion Content Script for Gemini 4 Argon Coding Bridge (Round 16)
// Moves the four fragile operations out of brittle selectors/OS dialogs:
//   1. attach: DataTransfer File injection into input[type=file] (no OS file dialog)
//   2. paste: synthetic ClipboardEvent('paste') with text/plain in DataTransfer (no clipboard grant)
//   3. model + mode + send: MutationObserver + semantic matcher inside composer container
//   4. delivery capture: Drive viewer/text page walk + network fileId listener

(function () {
  "use strict";

  const EXTENSION_VERSION = "1.16.0";
  const RE_TASK = /^\s*(task|টাস্ক)\s*$/i;
  const RE_CHAT = /^\s*(chat|চ্যাট|চাট)\s*$/i;
  const RE_ARGON = /(code\s*&\s*analysis\s*4\s*argon|4\s*argon|gemini[\s\-_]*4[\s\-_]*argon|(?<![\w\-])argon(?![\w\-])|আর\u09cd?গন)/i;
  const RE_FLASH = /(?:^|\b)(?:3\.8\s*flash|2\.5\s*flash|2\.0\s*flash|gemini[\s\-_]*flash|flash|ফ্ল্যাশ|4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)/i;
  const RE_HYDROGEN = /(?:^|\b)(?:4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)/i;
  const MAX_BASE64_ATTACH_BYTES = 8 * 1024 * 1024;
  const RE_UPLOAD = /(upload|attach|file|আপলোড|ডিজাইসো|ফাইল|সংযুক্ত|টুল)/i;
  const RE_SEND = /(send|submit|পাঠান)/i;

  const companionRuntime = {
    version: EXTENSION_VERSION,
    cachedPromptText: "",
    keepPromptAlive: false,
    lastModelSeen: "",
    flashRevertDetected: false,
    composerClearedCount: 0,
    driveFileIds: [],
    observerInstalled: false
  };

  function markDocumentReady() {
    try {
      if (document && document.documentElement) {
        document.documentElement.setAttribute("data-argon-companion-extension", EXTENSION_VERSION);
      }
    } catch (_err) {}
  }

  markDocumentReady();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", markDocumentReady, { once: true });
  }

  function sendServiceWorkerMessage(action, payload) {
    return new Promise((resolve) => {
      try {
        if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
          chrome.runtime.sendMessage({ protocol: "argon.companion.v1", action: action, payload: payload || {} }, (resp) => {
            if (chrome.runtime.lastError) {
              resolve({ ok: false, reason: chrome.runtime.lastError.message || "service worker error" });
              return;
            }
            resolve(resp || { ok: false, reason: "empty service worker response" });
          });
          return;
        }
      } catch (err) {
        resolve({ ok: false, reason: String(err) });
        return;
      }
      resolve({ ok: false, reason: "chrome.runtime.sendMessage unavailable" });
    });
  }

  function isVisible(el) {
    if (!el || !(el instanceof Element)) {
      return false;
    }
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function isInsideNavigation(el) {
    let cur = el;
    for (let i = 0; i < 12 && cur; i++, cur = cur.parentElement) {
      const role = (cur.getAttribute && (cur.getAttribute("role") || "")) || "";
      const tag = (cur.tagName || "").toLowerCase();
      const cls = (cur.getAttribute && (cur.getAttribute("class") || "")) || "";
      const id = cur.id || "";
      if (
        role === "navigation" ||
        tag === "nav" ||
        tag === "aside" ||
        /history|conversation|chat[-_ ]?list|sidebar/i.test(cls + " " + role + " " + id)
      ) {
        return true;
      }
    }
    return false;
  }

  function findLowestComposer() {
    const selectors = [
      "rich-textarea div[contenteditable='true']",
      "div.input-area-container div[contenteditable='true']",
      "div[contenteditable='true']",
      "textarea[aria-label*='prompt' i]",
      "textarea"
    ];
    let best = null;
    let bestY = -1;
    for (const sel of selectors) {
      const nodes = Array.from(document.querySelectorAll(sel));
      for (const el of nodes) {
        if (!isVisible(el) || isInsideNavigation(el)) {
          continue;
        }
        const rect = el.getBoundingClientRect();
        if (rect.top >= bestY) {
          bestY = rect.top;
          best = el;
        }
      }
    }
    return best;
  }

  function findComposerContainer(composer) {
    const comp = composer || findLowestComposer();
    if (!comp) {
      return null;
    }
    let cur = comp.parentElement;
    let fallback = comp.parentElement;
    for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
      const cls = (cur.getAttribute("class") || "").toLowerCase();
      const hasSwitch = Boolean(cur.querySelector("button.input-area-switch, button[aria-haspopup]"));
      const hasButtons = cur.querySelectorAll("button, [role='button']").length >= 1;
      if (cls.includes("input-area") || hasSwitch) {
        return cur;
      }
      if (hasButtons) {
        fallback = cur;
      }
    }
    return fallback || document.body;
  }

  function getComposerText(composer) {
    const comp = composer || findLowestComposer();
    if (!comp) {
      return "";
    }
    const val = (typeof comp.value === "string" && comp.value) ? comp.value : (comp.innerText || comp.textContent || "");
    return String(val || "").trim();
  }

  function focusComposerDom(composer) {
    const comp = composer || findLowestComposer();
    if (!comp) {
      return false;
    }
    try {
      comp.focus();
      if (comp.isContentEditable && window.getSelection && document.createRange) {
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(comp);
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
      }
      return true;
    } catch (_err) {
      return false;
    }
  }

  function pasteViaDataTransfer(promptText, appendMode) {
    const composer = findLowestComposer();
    if (!composer) {
      return {
        ok: false,
        operation: "paste",
        path: "extension",
        reason: "composer editable was not found on the page"
      };
    }

    const text = String(promptText || "");
    if (!text.trim()) {
      return {
        ok: false,
        operation: "paste",
        path: "extension",
        reason: "refusing to paste an empty prompt"
      };
    }

    focusComposerDom(composer);
    const beforeText = getComposerText(composer);
    if (!appendMode && beforeText) {
      try {
        if (typeof composer.value === "string" && composer.tagName.toLowerCase() === "textarea") {
          composer.value = "";
        } else {
          composer.textContent = "";
        }
      } catch (_err) {}
    }

    let syntheticHandled = false;
    try {
      const dt = new DataTransfer();
      dt.setData("text/plain", text);
      const pasteEvent = new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: dt
      });
      const notCancelled = composer.dispatchEvent(pasteEvent);
      const afterSynthetic = getComposerText(composer);
      if (!notCancelled || afterSynthetic.length >= Math.min(text.trim().length, 8)) {
        syntheticHandled = afterSynthetic.length > 0;
      }
    } catch (_err) {
      syntheticHandled = false;
    }

    if (!syntheticHandled || getComposerText(composer).length < Math.min(text.trim().length, 8)) {
      try {
        focusComposerDom(composer);
        if (typeof composer.value === "string" && composer.tagName.toLowerCase() === "textarea") {
          composer.value = appendMode ? (composer.value + text) : text;
        } else {
          let inserted = false;
          try {
            if (document.queryCommandSupported && document.queryCommandSupported("insertText")) {
              inserted = document.execCommand("insertText", false, text);
            }
          } catch (_e) {
            inserted = false;
          }
          if (!inserted || getComposerText(composer).length < Math.min(text.trim().length, 8)) {
            composer.textContent = appendMode ? ((composer.textContent || "") + text) : text;
          }
        }
        composer.dispatchEvent(new InputEvent("input", {
          bubbles: true,
          cancelable: false,
          inputType: "insertFromPaste",
          data: text
        }));
        composer.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (err) {
        return {
          ok: false,
          operation: "paste",
          path: "extension",
          reason: `synthetic DataTransfer paste failed (${String(err)})`
        };
      }
    }

    const landed = getComposerText(composer);
    const expectedMin = Math.max(1, Math.min(Math.floor(text.trim().length / 2), 40));
    if (landed.length < expectedMin) {
      return {
        ok: false,
        operation: "paste",
        path: "extension",
        length: landed.length,
        reason: `synthetic DataTransfer paste landed only ${landed.length} chars (expected >= ${expectedMin})`
      };
    }

    companionRuntime.cachedPromptText = text;
    companionRuntime.keepPromptAlive = true;
    installComposerMutationGuard();
    sendServiceWorkerMessage("record_operation", {
      operation: "paste",
      ok: true,
      length: landed.length,
      targetId: composer.id || composer.tagName.toLowerCase(),
      at: new Date().toISOString()
    });

    return {
      ok: true,
      operation: "paste",
      path: "extension",
      length: landed.length,
      landedText: landed,
      targetId: composer.id || ""
    };
  }

  function findModelChipSemantic() {
    const composer = findLowestComposer();
    const container = findComposerContainer(composer);
    const roots = container ? [container, document] : [document];
    const selectors = [
      "button.input-area-switch",
      "button[aria-haspopup='true'].input-area-switch",
      "button.input-area-switch[aria-haspopup]",
      "button[aria-label*='mode picker' i]",
      "button[aria-label*='model' i][aria-haspopup]"
    ];
    for (const root of roots) {
      for (const sel of selectors) {
        const list = Array.from(root.querySelectorAll(sel));
        for (const el of list) {
          if (isVisible(el) && !isInsideNavigation(el)) {
            return el;
          }
        }
      }
    }
    return null;
  }

  function readModelChipName(chipEl) {
    const el = chipEl || findModelChipSemantic();
    if (!el) {
      return "";
    }
    const txt = String(el.innerText || el.textContent || "").trim();
    if (txt) {
      return txt;
    }
    const label = String(el.getAttribute("aria-label") || "").trim();
    if (!label) {
      return "";
    }
    const m = label.match(/currently\s+(.+)$/i);
    return (m ? m[1] : label).trim();
  }

  function isElementSelectedSemantic(el) {
    let cur = el;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const cls = (cur.getAttribute && (cur.getAttribute("class") || "")) || "";
      const ap = (cur.getAttribute && (cur.getAttribute("aria-pressed") || "")) || "";
      const asel = (cur.getAttribute && (cur.getAttribute("aria-selected") || "")) || "";
      const achk = (cur.getAttribute && (cur.getAttribute("aria-checked") || "")) || "";
      if (/gem-segmented-button-selected/i.test(cls) || ap === "true" || asel === "true" || achk === "true") {
        return true;
      }
    }
    return false;
  }

  function findTaskModeStateSemantic() {
    const chip = findModelChipSemantic();
    const chipName = readModelChipName(chip);
    if (chipName && /high/i.test(chipName)) {
      return { mode: "chat", chip: chipName, reason: `chip '${chipName}' indicates Chat mode thinking level` };
    }
    const allNodes = Array.from(document.querySelectorAll("button, [role='button'], [role='tab'], [role='radio'], span, div"));
    let taskCandidate = null;
    let chatSelected = false;
    for (const node of allNodes) {
      if (!isVisible(node) || isInsideNavigation(node)) {
        continue;
      }
      const txt = String(node.innerText || node.textContent || "").trim();
      if (RE_TASK.test(txt)) {
        taskCandidate = node;
        if (isElementSelectedSemantic(node)) {
          return { mode: "task", chip: chipName, element: node };
        }
      } else if (RE_CHAT.test(txt) && isElementSelectedSemantic(node)) {
        chatSelected = true;
      }
    }
    if (taskCandidate) {
      return { mode: chatSelected ? "chat" : "unknown", chip: chipName, element: taskCandidate };
    }
    return { mode: "unknown", chip: chipName, element: null };
  }

  async function ensureTaskModeAndArgonSemantic(options) {
    const opts = options || {};
    const state0 = findTaskModeStateSemantic();
    if (state0.mode === "chat" && state0.reason) {
      return {
        ok: false,
        operation: "mode",
        path: "extension",
        mode: "chat",
        chip: state0.chip,
        reason: state0.reason
      };
    }

    if (state0.mode !== "task" && state0.element) {
      try {
        state0.element.click();
        await new Promise((r) => setTimeout(r, 250));
      } catch (_err) {}
    }

    const stateAfterMode = findTaskModeStateSemantic();
    const modeOk = (
      stateAfterMode.mode === "task" ||
      Boolean(opts.manualSetup) ||
      (Boolean(opts.isContinuation) && String(opts.recordedWorkspaceMode || "").toLowerCase() === "task" && !stateAfterMode.element)
    );
    if (!modeOk) {
      return {
        ok: false,
        operation: "mode",
        path: "extension",
        mode: stateAfterMode.mode,
        chip: stateAfterMode.chip,
        reason: `Chat/Task toggle could not be verified as Task (observed=${stateAfterMode.mode})`
      };
    }

    let chip = findModelChipSemantic();
    if (!chip) {
      return {
        ok: false,
        operation: "model",
        path: "extension",
        model: "(missing)",
        reason: "model picker chip is missing from the page DOM (Rule R1: a missing model picker is a failure, not a pass)"
      };
    }
    let chipName = readModelChipName(chip);
    if (chip && RE_ARGON.test(chipName) && !RE_FLASH.test(chipName) && !RE_HYDROGEN.test(chipName)) {
      companionRuntime.lastModelSeen = chipName;
      installComposerMutationGuard();
      return {
        ok: true,
        operation: "model",
        path: "extension",
        mode: "task",
        model: chipName
      };
    }

    if (chip) {
      try {
        chip.click();
        await new Promise((r) => setTimeout(r, 250));
      } catch (_err) {}
    }

    const optionSelectors = [
      "[role='listbox'] [role='option']",
      "[role='menu'] [role='menuitem']",
      "[role='menu'] [role='menuitemradio']",
      "mat-option",
      "li[role='option']"
    ];
    let clickedArgon = false;
    let sawDisabledArgon = false;
    for (const sel of optionSelectors) {
      const items = Array.from(document.querySelectorAll(sel));
      for (const item of items) {
        if (!isVisible(item)) {
          continue;
        }
        const txt = String(item.innerText || item.textContent || "").trim();
        const aria = String(item.getAttribute("aria-label") || "").trim();
        if (!RE_ARGON.test(txt + " " + aria) || RE_FLASH.test(txt + " " + aria)) {
          continue;
        }
        const disabled = (
          String(item.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
          item.hasAttribute("disabled") ||
          item.disabled === true
        );
        if (disabled) {
          sawDisabledArgon = true;
          continue;
        }
        try {
          item.click();
          clickedArgon = true;
          await new Promise((r) => setTimeout(r, 250));
          break;
        } catch (_err) {}
      }
      if (clickedArgon) {
        break;
      }
    }

    if (sawDisabledArgon && !clickedArgon) {
      return {
        ok: false,
        operation: "model",
        path: "extension",
        model: chipName,
        reason: "4 Argon option is present in the picker but disabled (aria-disabled=true)"
      };
    }

    chip = findModelChipSemantic();
    chipName = readModelChipName(chip);
    const checkedOption = document.querySelector(
      "[role='option'][aria-selected='true'], [role='menuitem'][aria-checked='true'], [role='menuitemradio'][aria-checked='true']"
    );
    const checkedText = checkedOption ? String(checkedOption.innerText || checkedOption.textContent || "").trim() : "";
    const effectiveModel = (RE_ARGON.test(chipName) && !RE_FLASH.test(chipName))
      ? chipName
      : ((RE_ARGON.test(checkedText) && !RE_FLASH.test(checkedText)) ? checkedText : chipName);

    if (!effectiveModel || RE_FLASH.test(effectiveModel) || !RE_ARGON.test(effectiveModel)) {
      return {
        ok: false,
        operation: "model",
        path: "extension",
        model: effectiveModel || "(not visible)",
        reason: RE_FLASH.test(effectiveModel || "")
          ? `Hard Flash guard aborted: model chip still reads '${effectiveModel}'`
          : `model chip reads '${effectiveModel || "(not visible)"}' instead of 4 Argon`
      };
    }

    companionRuntime.lastModelSeen = effectiveModel;
    companionRuntime.flashRevertDetected = false;
    installComposerMutationGuard();
    return {
      ok: true,
      operation: "model",
      path: "extension",
      mode: "task",
      model: effectiveModel
    };
  }

  function findUploadButtonSemantic(composer) {
    const container = findComposerContainer(composer);
    const roots = container ? [container, document] : [document];
    let best = null;
    let bestY = -1;
    for (const root of roots) {
      const buttons = Array.from(root.querySelectorAll("button, [role='button']"));
      for (const btn of buttons) {
        if (!isVisible(btn) || isInsideNavigation(btn)) {
          continue;
        }
        const aria = String(btn.getAttribute("aria-label") || "");
        const tip = String(btn.getAttribute("data-tooltip") || "");
        const txt = String(btn.innerText || btn.textContent || "").trim();
        const hasPopup = String(btn.getAttribute("aria-haspopup") || "").toLowerCase();
        if (RE_UPLOAD.test(aria + " " + tip + " " + txt) || (txt === "+" && hasPopup)) {
          const rect = btn.getBoundingClientRect();
          if (rect.top >= bestY) {
            bestY = rect.top;
            best = btn;
          }
        }
      }
      if (best) {
        return best;
      }
    }
    return null;
  }

  function findFileInputInDom() {
    const inputs = Array.from(document.querySelectorAll("input[type='file']"));
    if (inputs.length === 0) {
      return null;
    }
    for (const inp of inputs) {
      const accept = String(inp.getAttribute("accept") || "").toLowerCase();
      if (accept.includes(".zip")) {
        return inp;
      }
    }
    return inputs[0];
  }

  function base64ToUint8Array(b64) {
    const binary = atob(b64);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  async function attachArchiveViaDataTransfer(payload) {
    const opts = payload || {};
    const fileName = String(opts.fileName || "argon_context.zip").trim();
    const expectedSize = Number(opts.expectedSize || 0);
    let base64Data = String(opts.base64Data || "");

    if (!base64Data && opts.archiveUrl) {
      const swFetch = await sendServiceWorkerMessage("fetch_archive_bytes", {
        archiveUrl: opts.archiveUrl
      });
      if (!swFetch || !swFetch.ok || !swFetch.base64Data) {
        return {
          ok: false,
          operation: "attach",
          path: "extension",
          reason: (swFetch && swFetch.reason) ? swFetch.reason : "could not fetch archive bytes for DataTransfer attach"
        };
      }
      base64Data = swFetch.base64Data;
    }

    if (!base64Data) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: "no archive payload (base64Data or archiveUrl) provided to attachArchiveViaDataTransfer"
      };
    }

    let fileInput = findFileInputInDom();
    if (!fileInput) {
      const uploadBtn = findUploadButtonSemantic();
      if (uploadBtn) {
        try {
          uploadBtn.click();
          await new Promise((r) => setTimeout(r, 250));
        } catch (_err) {}
      }
      fileInput = findFileInputInDom();
    }

    if (!fileInput) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: "the upload menu rendered no file input"
      };
    }

    let bytes = null;
    try {
      bytes = base64ToUint8Array(base64Data);
    } catch (err) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: `invalid base64 archive payload (${String(err)})`
      };
    }

    if (expectedSize > 0 && bytes.byteLength !== expectedSize) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: `archive byte length mismatch (${bytes.byteLength} !== expected ${expectedSize})`
      };
    }

    try {
      const fileObj = new File([bytes], fileName, {
        type: "application/zip",
        lastModified: Date.now()
      });
      const dt = new DataTransfer();
      dt.items.add(fileObj);
      fileInput.files = dt.files;
      fileInput.dispatchEvent(new Event("input", { bubbles: true }));
      fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    } catch (err) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: `DataTransfer File injection into input[type=file] failed (${String(err)})`
      };
    }

    await new Promise((r) => setTimeout(r, 120));

    // If Gemini cleared the composer when the attachment completed, restore the prompt immediately.
    if (companionRuntime.keepPromptAlive && companionRuntime.cachedPromptText) {
      const curText = getComposerText();
      if (curText.length < Math.min(companionRuntime.cachedPromptText.trim().length, 8)) {
        companionRuntime.composerClearedCount += 1;
        pasteViaDataTransfer(companionRuntime.cachedPromptText, false);
      }
    }

    const stem = fileName.replace(/\.[^.]+$/, "");
    const bodyText = String((document.body && document.body.innerText) || "");
    const inputHasFile = Boolean(fileInput.files && fileInput.files.length > 0 && fileInput.files[0].size === bytes.byteLength);
    const chipVisible = Boolean(
      (fileName && bodyText.toLowerCase().includes(fileName.toLowerCase())) ||
      (stem && bodyText.toLowerCase().includes(stem.toLowerCase()))
    );

    sendServiceWorkerMessage("record_operation", {
      operation: "attach",
      ok: Boolean(inputHasFile || chipVisible),
      fileName: fileName,
      size: bytes.byteLength,
      chipVisible: chipVisible,
      inputHasFile: inputHasFile,
      at: new Date().toISOString()
    });

    return {
      ok: Boolean(inputHasFile || chipVisible),
      operation: "attach",
      path: "extension",
      fileName: fileName,
      stem: stem,
      size: bytes.byteLength,
      chipVisible: chipVisible,
      inputHasFile: inputHasFile,
      composerRestored: companionRuntime.composerClearedCount > 0,
      reason: (inputHasFile || chipVisible) ? "" : "DataTransfer injected file but neither input.files nor attachment chip matched"
    };
  }

  function findSemanticSendButton(composer) {
    const comp = composer || findLowestComposer();
    const container = findComposerContainer(comp);
    const roots = container ? [container, document] : [document];

    for (const root of roots) {
      const buttons = Array.from(root.querySelectorAll("button, [role='button']"));
      let rightmostCandidate = null;
      let rightmostX = -1;

      for (const btn of buttons) {
        if (!isVisible(btn) || isInsideNavigation(btn)) {
          continue;
        }
        const disabled = (
          String(btn.getAttribute("aria-disabled") || "").toLowerCase() === "true" ||
          btn.hasAttribute("disabled") ||
          btn.disabled === true
        );
        if (disabled) {
          continue;
        }
        const cls = String(btn.getAttribute("class") || "");
        const aria = String(btn.getAttribute("aria-label") || "");
        const tip = String(btn.getAttribute("data-tooltip") || "");
        const testId = String(btn.getAttribute("data-testid") || "");
        const txt = String(btn.innerText || btn.textContent || "").trim();
        const combined = `${cls} ${aria} ${tip} ${testId} ${txt}`;

        if (/input-area-switch|mode\s*picker|mic|voice|audio|stop/i.test(combined)) {
          continue;
        }
        if (RE_UPLOAD.test(combined) || txt === "+") {
          continue;
        }
        if (RE_TASK.test(txt) || RE_CHAT.test(txt)) {
          continue;
        }
        if (
          RE_SEND.test(combined) ||
          /send-button|submit/i.test(combined) ||
          String(btn.getAttribute("type") || "").toLowerCase() === "submit"
        ) {
          return btn;
        }
        if (root === container) {
          const rect = btn.getBoundingClientRect();
          if (rect.left >= rightmostX) {
            rightmostX = rect.left;
            rightmostCandidate = btn;
          }
        }
      }
      if (rightmostCandidate) {
        return rightmostCandidate;
      }
    }
    return null;
  }

  async function semanticClickSend(payload) {
    const opts = payload || {};
    const composer = findLowestComposer();
    if (!composer) {
      return {
        ok: false,
        operation: "send",
        path: "extension",
        reason: "composer editable was not found before send"
      };
    }

    // 1. Re-verify prompt text in composer (re-paste if cleared by upload completion).
    const expectedPrompt = String(opts.promptText || companionRuntime.cachedPromptText || "");
    const expectedMin = Math.max(1, Number(opts.expectedMinLen || Math.min(Math.floor(expectedPrompt.trim().length / 2), 40) || 1));
    let currentText = getComposerText(composer);
    if (currentText.length < expectedMin && expectedPrompt.trim()) {
      companionRuntime.composerClearedCount += 1;
      const repasted = pasteViaDataTransfer(expectedPrompt, false);
      if (!repasted.ok) {
        return {
          ok: false,
          operation: "send",
          path: "extension",
          reason: `composer was cleared after upload and re-paste failed (${repasted.reason})`
        };
      }
      currentText = getComposerText(composer);
    }

    if (currentText.length < expectedMin) {
      return {
        ok: false,
        operation: "send",
        path: "extension",
        reason: `composer is empty before send (len=${currentText.length}, expected>=${expectedMin})`
      };
    }

    // 2. Hard Flash Guard right before send.
    const modelCheck = await ensureTaskModeAndArgonSemantic({
      manualSetup: Boolean(opts.manualSetup),
      isContinuation: Boolean(opts.isContinuation),
      recordedWorkspaceMode: opts.recordedWorkspaceMode || "task"
    });
    if (!modelCheck.ok) {
      return {
        ok: false,
        operation: modelCheck.operation || "model",
        path: "extension",
        reason: modelCheck.reason || "Hard Flash guard aborted send: model chip is not 4 Argon"
      };
    }

    // Stop auto-repopulating once we intentionally click Send.
    companionRuntime.keepPromptAlive = false;

    const sendBtn = findSemanticSendButton(composer);
    if (!sendBtn) {
      return {
        ok: false,
        operation: "send",
        path: "extension",
        reason: "semantic send button was not found inside the composer container"
      };
    }

    try {
      sendBtn.click();
    } catch (err) {
      return {
        ok: false,
        operation: "send",
        path: "extension",
        reason: `clicking semantic send button failed (${String(err)})`
      };
    }

    await new Promise((r) => setTimeout(r, 150));
    const afterClickText = getComposerText(composer);
    if (afterClickText.length >= expectedMin) {
      try {
        focusComposerDom(composer);
        composer.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter",
          code: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true
        }));
      } catch (_err) {}
      await new Promise((r) => setTimeout(r, 100));
    }

    const finalComposerText = getComposerText(composer);
    const emptied = finalComposerText.length < expectedMin;
    sendServiceWorkerMessage("record_operation", {
      operation: "send",
      ok: emptied,
      composerClearedCount: companionRuntime.composerClearedCount,
      at: new Date().toISOString()
    });

    return {
      ok: emptied,
      operation: "send",
      path: "extension",
      composerEmptied: emptied,
      composerClearedCount: companionRuntime.composerClearedCount,
      reason: emptied ? "" : "send button was clicked but the composer did not empty"
    };
  }

  function installComposerMutationGuard() {
    if (companionRuntime.observerInstalled || !document.body) {
      return;
    }
    companionRuntime.observerInstalled = true;
    const observer = new MutationObserver(() => {
      try {
        const chip = findModelChipSemantic();
        const chipName = readModelChipName(chip);
        if (chipName) {
          companionRuntime.lastModelSeen = chipName;
          if (RE_FLASH.test(chipName)) {
            companionRuntime.flashRevertDetected = true;
            if (document.documentElement) {
              document.documentElement.setAttribute("data-argon-flash-revert", chipName);
            }
          }
        }
        if (companionRuntime.keepPromptAlive && companionRuntime.cachedPromptText) {
          const comp = findLowestComposer();
          if (comp && getComposerText(comp).length === 0) {
            companionRuntime.composerClearedCount += 1;
            pasteViaDataTransfer(companionRuntime.cachedPromptText, false);
          }
        }
      } catch (_err) {}
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["aria-label", "aria-selected", "aria-checked", "class"]
    });
  }

  function recordDriveUrlIfPresent(rawUrl) {
    const url = String(rawUrl || "");
    if (!url.includes("drive.google.com") && !url.includes("/viewer/")) {
      return;
    }
    const m = url.match(/[?&]id=([\w-]{10,})/) || url.match(/\/file\/d\/([\w-]{10,})/);
    if (m && m[1]) {
      const fid = m[1];
      if (!companionRuntime.driveFileIds.includes(fid)) {
        companionRuntime.driveFileIds.push(fid);
      }
      if (document.documentElement) {
        document.documentElement.setAttribute("data-argon-last-drive-file-id", fid);
      }
      sendServiceWorkerMessage("record_drive_file_id", { fileId: fid, url: url });
    }
  }

  try {
    if (typeof PerformanceObserver !== "undefined") {
      const perfObs = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry && entry.name) {
            recordDriveUrlIfPresent(entry.name);
          }
        }
      });
      perfObs.observe({ entryTypes: ["resource"] });
    }
  } catch (_err) {}

  async function dispatchCompanionCommand(action, payload) {
    const act = String(action || "").trim();
    const pl = payload || {};
    if (act === "ping") {
      return {
        ok: true,
        operation: "ping",
        path: "extension",
        version: EXTENSION_VERSION,
        lastModelSeen: companionRuntime.lastModelSeen,
        driveFileIds: companionRuntime.driveFileIds.slice()
      };
    }
    if (act === "paste_prompt") {
      return pasteViaDataTransfer(pl.promptText || "", Boolean(pl.appendMode));
    }
    if (act === "ensure_mode_and_model") {
      return await ensureTaskModeAndArgonSemantic(pl);
    }
    if (act === "attach_archive") {
      return await attachArchiveViaDataTransfer(pl);
    }
    if (act === "click_send") {
      return await semanticClickSend(pl);
    }
    if (act === "capture_delivery") {
      const fid = pl.fileId || (companionRuntime.driveFileIds.length ? companionRuntime.driveFileIds[companionRuntime.driveFileIds.length - 1] : "");
      return await sendServiceWorkerMessage("capture_delivery", Object.assign({}, pl, { fileId: fid }));
    }
    if (act === "get_state" || act === "set_state") {
      return await sendServiceWorkerMessage(act, pl);
    }
    return {
      ok: false,
      operation: act || "unknown",
      path: "extension",
      reason: `unknown companion command: ${act}`
    };
  }

  // Expose custom event bridge so Playwright/CDP can invoke the companion extension from isolated or main world.
  window.addEventListener("argon:companion:request", (ev) => {
    const detail = (ev && ev.detail) ? ev.detail : {};
    const reqId = String(detail.requestId || "");
    const action = String(detail.action || "");
    const payload = detail.payload || {};
    dispatchCompanionCommand(action, payload)
      .then((result) => {
        const out = Object.assign({ protocol: "argon.companion.v1", requestId: reqId }, result || {});
        if (document.documentElement) {
          document.documentElement.setAttribute("data-argon-companion-response-" + reqId, JSON.stringify(out));
        }
        window.dispatchEvent(new CustomEvent("argon:companion:response", { detail: out }));
      })
      .catch((err) => {
        const out = {
          protocol: "argon.companion.v1",
          requestId: reqId,
          ok: false,
          operation: action,
          path: "extension",
          reason: String(err)
        };
        if (document.documentElement) {
          document.documentElement.setAttribute("data-argon-companion-response-" + reqId, JSON.stringify(out));
        }
        window.dispatchEvent(new CustomEvent("argon:companion:response", { detail: out }));
      });
  });

  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (!message || message.protocol !== "argon.companion.v1") {
        return false;
      }
      dispatchCompanionCommand(message.action, message.payload)
        .then((res) => sendResponse(res))
        .catch((err) => sendResponse({
          ok: false,
          operation: String(message.action || "error"),
          path: "extension",
          reason: String(err)
        }));
      return true;
    });
  }
})();
