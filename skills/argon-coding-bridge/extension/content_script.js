// MV3 Companion Content Script for Gemini 4 Argon Coding Bridge (Round 16)
// Moves the four fragile operations out of brittle selectors/OS dialogs:
//   1. attach: DataTransfer File injection into input[type=file] (no OS file dialog)
//   2. paste: synthetic ClipboardEvent('paste') with text/plain in DataTransfer (no clipboard grant)
//   3. model + mode + send: MutationObserver + semantic matcher inside composer container
//   4. delivery capture: Drive viewer/text page walk + network fileId listener

(function () {
  "use strict";

  const EXTENSION_VERSION = "1.17.0";
  const SHARED_ARGON_MODEL_LABELS = [
    "Code & analysis 4 Argon",
    "কোড ও বিশ্লেষণ 4 Argon",
    "4 Argon",
    "Gemini 4 Argon",
    "Argon",
    "আরগন",
    "আর্গন"
  ];
  const RE_TASK = /^\s*(task|টাস্ক)\s*$/i;
  const RE_CHAT = /^\s*(chat|চ্যাট|চাট)\s*$/i;
  const RE_ARGON = /(code\s*&\s*analysis\s*4\s*argon|কোড\s*ও\s*বিশ্লেষণ\s*4\s*argon|4\s*argon|gemini[\s\-_]*4[\s\-_]*argon|(?<![\w\-])argon(?![\w\-])|আর\u09cd?গন|আর্গন)/i;
  const RE_FLASH = /(?:^|\b)(?:3\.8\s*flash|2\.5\s*flash|2\.0\s*flash|gemini[\s\-_]*flash|flash|ফ্ল্যাশ|4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)/i;
  const RE_HYDROGEN = /(?:^|\b)(?:4\s*hydrogen|gemini[\s\-_]*4[\s\-_]*hydrogen|hydrogen|হাইড্রোজেন|everyday\s*help)(?:\b|$)/i;
  const MAX_BASE64_ATTACH_BYTES = 8 * 1024 * 1024;
  const RE_UPLOAD = /(upload|attach|file|আপলোড|ডিজাইসো|ফাইল|সংযুক্ত|টুল)/i;
  const RE_SEND = /(send|submit|পাঠান)/i;

  // Round 27 Contract 3 R3.1: the model chip is LOCALISED, so the accepted Argon labels
  // must be the same list the Core uses (`SHARED_ARGON_MODEL_LABELS` in
  // round23_reliability.py). Alias to `SHARED_ARGON_MODEL_LABELS` as single source of truth.
  const ARGON_MODEL_LABELS = SHARED_ARGON_MODEL_LABELS;

  const companionRuntime = {
    version: EXTENSION_VERSION,
    cachedPromptText: "",
    keepPromptAlive: false,
    sendPerformed: false,
    turnsAtPaste: 0,
    disarmReason: "",
    lastModelSeen: "",
    flashRevertDetected: false,
    composerClearedCount: 0,
    driveFileIds: [],
    observerInstalled: false
  };

  function getEffectiveBase64AttachCap(opts) {
    const o = opts || {};
    if (o.maxBase64Bytes !== undefined && o.maxBase64Bytes !== null && !Number.isNaN(Number(o.maxBase64Bytes))) {
      return Math.max(0, Math.floor(Number(o.maxBase64Bytes)));
    }
    if (o.effectiveBase64CapBytes !== undefined && o.effectiveBase64CapBytes !== null && !Number.isNaN(Number(o.effectiveBase64CapBytes))) {
      return Math.max(0, Math.floor(Number(o.effectiveBase64CapBytes)));
    }
    try {
      if (document && document.documentElement) {
        const attrVal =
          document.documentElement.getAttribute("data-argon-max-base64-attach-bytes") ||
          document.documentElement.getAttribute("data-argon-max-base64-bytes");
        if (attrVal !== null && attrVal !== "" && !Number.isNaN(Number(attrVal)) && Number(attrVal) > 0) {
          return Math.max(0, Math.floor(Number(attrVal)));
        }
      }
      if (typeof window !== "undefined" && window.ARGON_MAX_BASE64_ATTACH_MB !== undefined && window.ARGON_MAX_BASE64_ATTACH_MB !== "") {
        const mb = Number(window.ARGON_MAX_BASE64_ATTACH_MB);
        if (!Number.isNaN(mb) && mb >= 0) {
          return Math.floor(mb * 1024 * 1024);
        }
      }
    } catch (_err) {}
    return MAX_BASE64_ATTACH_BYTES;
  }

  function countConversationTurns() {
    const selectors = [
      "user-query",
      "[data-test-id='user-query']",
      "message-turn.user-query",
      "message-turn:has(user-query)",
      ".user-query-container",
      ".user-turn",
      "model-response",
      "[data-test-id='model-response']"
    ];
    try {
      return document.querySelectorAll(selectors.join(",")).length;
    } catch (_err) {
      return document.querySelectorAll("user-query, .user-turn, model-response").length;
    }
  }

  function hasQueuedSteeringTurn() {
    try {
      return Boolean(document.querySelector(".queued-steering-turn, [class*='queued-steering-turn']"));
    } catch (_err) {
      return false;
    }
  }

  function conversationAlreadyContainsPrompt(promptText) {
    if (hasQueuedSteeringTurn()) {
      return true;
    }
    const raw = String(promptText || companionRuntime.cachedPromptText || "")
      .replace(/`/g, "")
      .replace(/\s+/g, " ")
      .trim();
    if (!raw || raw.length < 16) {
      return false;
    }
    const probe = raw.slice(0, Math.min(raw.length, 120));
    try {
      const turns = Array.from(
        document.querySelectorAll(
          "user-query, [data-test-id='user-query'], message-turn.user-query, .user-query-container, .user-turn"
        )
      );
      for (const t of turns) {
        if (t.closest && t.closest("rich-textarea, .input-area, .chat-input, footer, form, [data-test-id='chat-input']")) {
          continue;
        }
        const txt = String(t.innerText || t.textContent || "")
          .replace(/`/g, "")
          .replace(/\s+/g, " ")
          .trim();
        if (txt && txt.includes(probe)) {
          return true;
        }
      }
    } catch (_err) {}
    return false;
  }

  function shouldDisarmObserverForDomState(promptText) {
    if (companionRuntime.sendPerformed) {
      return "send_performed";
    }
    if (hasQueuedSteeringTurn()) {
      return "queued_steering_turn_present";
    }
    if (countConversationTurns() > companionRuntime.turnsAtPaste) {
      return "conversation_gained_turn";
    }
    if (conversationAlreadyContainsPrompt(promptText)) {
      return "prompt_already_in_conversation";
    }
    return "";
  }

  function disarmPromptAliveGuard(reason) {
    companionRuntime.keepPromptAlive = false;
    companionRuntime.cachedPromptText = "";
    companionRuntime.sendPerformed = true;
    companionRuntime.disarmReason = String(reason || "send_performed");
  }

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

  function countConversationDomTurns() {
    try {
      return document.querySelectorAll(
        "user-query, [data-test-id='user-query'], message-turn.user-query, .user-query-container, .user-turn, model-response, [data-test-id='model-response']"
      ).length;
    } catch (_err) {
      return 0;
    }
  }

  function hasQueuedSteeringTurnInDom() {
    try {
      return Boolean(document.querySelector(".queued-steering-turn, [class*='queued-steering-turn']"));
    } catch (_err) {
      return false;
    }
  }

  function disarmCompanionPromptGuard(reason) {
    companionRuntime.keepPromptAlive = false;
    companionRuntime.cachedPromptText = "";
    companionRuntime.sendPerformed = true;
    companionRuntime.disarmReason = String(reason || "disarmed");
    try {
      if (document && document.documentElement) {
        document.documentElement.setAttribute("data-argon-prompt-guard-disarmed", companionRuntime.disarmReason);
      }
    } catch (_err) {}
  }

  function getComposerText(composer) {
    if (companionRuntime.keepPromptAlive || companionRuntime.cachedPromptText) {
      const curTurns = countConversationDomTurns();
      if (
        companionRuntime.sendPerformed ||
        hasQueuedSteeringTurnInDom() ||
        (typeof companionRuntime.turnsAtPaste === "number" && curTurns > companionRuntime.turnsAtPaste)
      ) {
        disarmCompanionPromptGuard(
          hasQueuedSteeringTurnInDom() ? "queued_steering_turn_detected" : "conversation_gained_turn"
        );
        return "";
      }
    }
    const comp = composer || findLowestComposer();
    if (!comp) {
      return "";
    }
    const val = (typeof comp.value === "string" && comp.value) ? comp.value : (comp.innerText || comp.textContent || "");
    return String(val || "").trim();
  }

  function focusComposerDom(composer) {
    if (!companionRuntime.keepPromptAlive) {
      companionRuntime.sendPerformed = false;
      companionRuntime.turnsAtPaste = countConversationDomTurns();
      companionRuntime.disarmReason = "";
    }
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

    const domDisarm = shouldDisarmObserverForDomState(text);
    if (domDisarm) {
      disarmPromptAliveGuard(domDisarm);
    } else if (!companionRuntime.sendPerformed) {
      companionRuntime.cachedPromptText = text;
      companionRuntime.keepPromptAlive = true;
      installComposerMutationGuard();
    }
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

  async function waitForModelChipSemantic(waitMs, pollMs) {
    const totalMs = Math.max(0, Number(waitMs || 0));
    const stepMs = Math.max(25, Math.min(500, Number(pollMs || 200)));
    const deadline = Date.now() + totalMs;
    while (true) {
      const chip = findModelChipSemantic();
      if (chip) {
        return chip;
      }
      if (Date.now() >= deadline) {
        return null;
      }
      await new Promise((r) => setTimeout(r, stepMs));
    }
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
    const isCont = Boolean(opts.isContinuation);
    const waitMs = (opts.waitForChipMs !== undefined)
      ? Math.max(0, Number(opts.waitForChipMs || 0))
      : ((opts.waitForChipSeconds !== undefined)
        ? Math.max(0, Number(opts.waitForChipSeconds || 0) * 1000)
        : (isCont ? 45000 : 0));
    let waitedOnContinuation = false;
    if (!chip && waitMs > 0) {
      // Round 27 W7: on a CONTINUATION page the chip can take seconds to hydrate, so
      // WAIT for it instead of taking a single snapshot. `waitedOnContinuation` says we
      // did wait (it used to be referenced without ever being defined, which made every
      // model check throw a ReferenceError and broke the whole send path).
      waitedOnContinuation = isCont;
      const deadlineMs = Date.now() + waitMs;
      while (!chip && Date.now() < deadlineMs) {
        await new Promise((r) => setTimeout(r, Math.min(150, Math.max(25, deadlineMs - Date.now()))));
        chip = findModelChipSemantic();
      }
    }
    if (!chip) {
      const waitSec = Math.max(1, Math.round((waitMs || 45000) / 1000));
      return {
        ok: false,
        operation: "model",
        path: "extension",
        model: "(not visible)",
        reason: isCont
          ? `model chip (not visible) after ${waitSec} s on a continuation page - the conversation is still hydrating`
          : "model picker chip is missing from the page DOM (Rule R1: a missing model picker is a failure, not a pass)"
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
        model: chipName,
        waitedOnContinuation: waitedOnContinuation,
        supportedLabels: ARGON_MODEL_LABELS.slice()
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
      model: effectiveModel,
      waitedOnContinuation: waitedOnContinuation,
      supportedLabels: ARGON_MODEL_LABELS.slice()
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

  function getEffectiveBase64CapBytes(opts) {
    return getEffectiveBase64AttachCap(opts);
  }

  async function attachArchiveViaDataTransfer(payload) {
    const opts = payload || {};
    const fileName = String(opts.fileName || "argon_context.zip").trim();
    const expectedSize = Number(opts.expectedSize || 0);
    const effectiveCap = getEffectiveBase64CapBytes(opts);
    const allowOverCap = Boolean(opts.allowOverCap);
    let base64Data = String(opts.base64Data || "");
    const estimatedBytes = expectedSize > 0
      ? expectedSize
      : (base64Data ? Math.floor((base64Data.length * 3) / 4) : 0);
    if (!allowOverCap && estimatedBytes > effectiveCap) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: "size_cap_exceeded",
        maxBase64Bytes: effectiveCap,
        size: estimatedBytes,
        detail: `the base64 route can carry at most ${(effectiveCap / (1024 * 1024)).toFixed(2)} MB; above that the zero-copy route is used`
      };
    }

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

    if (!allowOverCap && bytes.byteLength > effectiveCap) {
      const capMb = Math.round((effectiveCap / (1024 * 1024)) * 100) / 100;
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        fileName: fileName,
        size: bytes.byteLength,
        capBytes: effectiveCap,
        reason: "size_cap_exceeded",
        detail: `the base64 route can carry at most ${capMb} MB; above that the zero-copy route is used`
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

    // If Gemini cleared the composer when the attachment completed, restore the prompt immediately (ONLY before send).
    if (companionRuntime.keepPromptAlive && companionRuntime.cachedPromptText) {
      const disarmCheck = shouldDisarmObserverForDomState(companionRuntime.cachedPromptText);
      if (disarmCheck) {
        disarmPromptAliveGuard(disarmCheck);
      } else {
        const curText = getComposerText();
        if (curText.length < Math.min(companionRuntime.cachedPromptText.trim().length, 8)) {
          companionRuntime.composerClearedCount += 1;
          pasteViaDataTransfer(companionRuntime.cachedPromptText, false);
        }
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
      capBytes: effectiveCap,
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

    // 1. Re-verify prompt text in composer (re-paste ONLY before send if cleared by upload completion).
    const expectedPrompt = String(opts.promptText || companionRuntime.cachedPromptText || "");
    const expectedMin = Math.max(1, Number(opts.expectedMinLen || Math.min(Math.floor(expectedPrompt.trim().length / 2), 40) || 1));
    let currentText = getComposerText(composer);
    const domDisarm = shouldDisarmObserverForDomState(expectedPrompt);
    if (domDisarm) {
      disarmPromptAliveGuard(domDisarm);
    } else if (
      currentText.length < expectedMin &&
      expectedPrompt.trim() &&
      !companionRuntime.sendPerformed &&
      countConversationTurns() <= companionRuntime.turnsAtPaste
    ) {
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
      recordedWorkspaceMode: opts.recordedWorkspaceMode || "task",
      waitForChipMs: opts.waitForChipMs !== undefined ? opts.waitForChipMs : 0
    });
    if (!modelCheck.ok) {
      return {
        ok: false,
        operation: modelCheck.operation || "model",
        path: "extension",
        reason: modelCheck.reason || "Hard Flash guard aborted send: model chip is not 4 Argon"
      };
    }

    const sendBtn = findSemanticSendButton(composer);
    if (!sendBtn) {
      return {
        ok: false,
        operation: "send",
        path: "extension",
        reason: "semantic send button was not found inside the composer container"
      };
    }

    // Turn keepPromptAlive OFF and clear cachedPromptText immediately as send is performed.
    disarmPromptAliveGuard("click_send");

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
    const finalComposerText = getComposerText(composer);
    const emptied = finalComposerText.length < expectedMin;
    sendServiceWorkerMessage("record_operation", {
      operation: "send",
      ok: true,
      composerEmptied: emptied,
      composerClearedCount: companionRuntime.composerClearedCount,
      at: new Date().toISOString()
    });

    return {
      ok: true,
      operation: "send",
      path: "extension",
      sendPerformed: true,
      keepPromptAlive: companionRuntime.keepPromptAlive,
      cachedPromptText: companionRuntime.cachedPromptText,
      composerEmptied: emptied,
      composerClearedCount: companionRuntime.composerClearedCount,
      reason: ""
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
        if (companionRuntime.keepPromptAlive || companionRuntime.cachedPromptText) {
          const disarmCheck = shouldDisarmObserverForDomState(companionRuntime.cachedPromptText);
          if (disarmCheck) {
            disarmPromptAliveGuard(disarmCheck);
          } else if (companionRuntime.keepPromptAlive && companionRuntime.cachedPromptText) {
            const comp = findLowestComposer();
            if (comp && getComposerText(comp).length === 0) {
              companionRuntime.composerClearedCount += 1;
              pasteViaDataTransfer(companionRuntime.cachedPromptText, false);
            }
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

    document.addEventListener("click", (ev) => {
      try {
        if (!companionRuntime.keepPromptAlive && !companionRuntime.cachedPromptText) {
          return;
        }
        const target = ev && ev.target instanceof Element ? ev.target.closest("button, [role='button']") : null;
        if (!target) {
          return;
        }
        const sendBtn = findSemanticSendButton();
        if (sendBtn && (target === sendBtn || sendBtn.contains(target))) {
          disarmPromptAliveGuard("send_button_click");
        }
      } catch (_err) {}
    }, true);

    document.addEventListener("keydown", (ev) => {
      try {
        if (!companionRuntime.keepPromptAlive && !companionRuntime.cachedPromptText) {
          return;
        }
        if (ev && ev.key === "Enter" && !ev.shiftKey) {
          const comp = findLowestComposer();
          if (comp && (ev.target === comp || comp.contains(ev.target))) {
            disarmPromptAliveGuard("composer_enter_keydown");
          }
        }
      } catch (_err) {}
    }, true);
  }

  function extractChipFileNameSemantic(chip) {
    if (!chip || !(chip instanceof Element)) {
      return "";
    }
    const selectors = [
      "generated-file-senna-preview .title-container [title]",
      ".title-container [title]",
      "[data-file-name]",
      ".file-name-lr",
      ".title-container"
    ];
    for (const sel of selectors) {
      const node = chip.querySelector(sel);
      if (node) {
        const attr = String(node.getAttribute("title") || node.getAttribute("data-file-name") || "").trim();
        if (attr && !/^(export|fullscreen|close|download|drive)$/i.test(attr)) {
          return attr;
        }
        const txt = String(node.innerText || node.textContent || "").trim();
        if (txt) {
          const m = txt.match(/([A-Za-z0-9_.\- ()]+\.[A-Za-z0-9]{1,8})/);
          if (m && m[1]) {
            return m[1].trim();
          }
        }
      }
    }
    const raw = String(chip.getAttribute("data-file-name") || chip.getAttribute("title") || chip.innerText || chip.textContent || "").trim();
    const m = raw.match(/([A-Za-z0-9_.\- ()]+\.[A-Za-z0-9]{1,8})/);
    if (m && m[1]) {
      return m[1].trim();
    }
    if (/apply_changes/i.test(raw)) {
      return "apply_changes.ps1";
    }
    return "";
  }

  function findLastTurnDeliveryChipSemantic() {
    const turnSelectors = [
      "model-response",
      "[data-test-id='model-response']",
      ".model-response-text",
      "message-turn.model-response",
      "div.model-response"
    ];
    let turns = [];
    for (const sel of turnSelectors) {
      const found = Array.from(document.querySelectorAll(sel));
      if (found.length > 0) {
        turns = found;
        break;
      }
    }
    const userNodes = Array.from(
      document.querySelectorAll("user-query, [data-test-id='user-query'], message-turn.user-query, .user-query-container, .user-turn")
    );
    const lastUser = userNodes.length > 0 ? userNodes[userNodes.length - 1] : null;
    const isAfterLastUser = (node) => {
      if (!lastUser || !node) {
        return true;
      }
      if (lastUser === node || (lastUser.contains && lastUser.contains(node))) {
        return false;
      }
      const pos = node.compareDocumentPosition(lastUser);
      return !(pos & Node.DOCUMENT_POSITION_FOLLOWING);
    };

    if (turns.length > 0) {
      const newestTurn = turns[turns.length - 1];
      if (!isAfterLastUser(newestTurn)) {
        return { found: false, turnIndex: turns.length, fileName: "", fileId: "" };
      }
      const chips = Array.from(newestTurn.querySelectorAll("generated-file")).filter(isVisible);
      const patchChips = chips.filter((c) => {
        const fn = extractChipFileNameSemantic(c).toLowerCase();
        return fn.includes("apply_changes") || fn.endsWith(".ps1");
      });
      if (patchChips.length > 0) {
        const chosen = patchChips[patchChips.length - 1];
        const fname = extractChipFileNameSemantic(chosen) || "apply_changes.ps1";
        const fid = String(chosen.getAttribute("data-file-id") || chosen.getAttribute("data-drive-file-id") || "").trim();
        return {
          found: true,
          turnIndex: turns.length,
          fileName: fname,
          fileId: fid
        };
      }
      return { found: false, turnIndex: turns.length, fileName: "", fileId: "" };
    }

    const allChips = Array.from(document.querySelectorAll("generated-file")).filter((c) => isVisible(c) && isAfterLastUser(c));
    const matching = allChips.filter((c) => {
      const fn = extractChipFileNameSemantic(c).toLowerCase();
      return fn.includes("apply_changes") || fn.endsWith(".ps1");
    });
    if (matching.length > 0) {
      const chosen = matching[matching.length - 1];
      return {
        found: true,
        turnIndex: 1,
        fileName: extractChipFileNameSemantic(chosen) || "apply_changes.ps1",
        fileId: String(chosen.getAttribute("data-file-id") || chosen.getAttribute("data-drive-file-id") || "").trim()
      };
    }
    return { found: false, turnIndex: 0, fileName: "", fileId: "" };
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
        supportedModelLabels: ARGON_MODEL_LABELS.slice(),
        driveFileIds: companionRuntime.driveFileIds.slice()
      };
    }
    if (act === "paste_prompt") {
      companionRuntime.sendPerformed = false;
      companionRuntime.turnsAtPaste = countConversationTurns();
      companionRuntime.disarmReason = "";
      return pasteViaDataTransfer(pl.promptText || "", Boolean(pl.appendMode));
    }
    if (act === "disarm_prompt_guard") {
      const reasonStr = pl.reason || "disarm_prompt_guard";
      disarmPromptAliveGuard(reasonStr);
      disarmCompanionPromptGuard(reasonStr);
      return {
        ok: true,
        operation: "disarm_prompt_guard",
        path: "extension",
        keepPromptAlive: false,
        cachedPromptText: "",
        sendPerformed: Boolean(companionRuntime.sendPerformed),
        disarmReason: String(companionRuntime.disarmReason || ""),
        reason: String(companionRuntime.disarmReason || "")
      };
    }
    if (act === "get_runtime_state") {
      const cachedStr = String(companionRuntime.cachedPromptText || "");
      return {
        ok: true,
        operation: "get_runtime_state",
        path: "extension",
        version: EXTENSION_VERSION,
        keepPromptAlive: Boolean(companionRuntime.keepPromptAlive),
        cachedPromptText: cachedStr,
        cachedPromptTextLength: cachedStr.length,
        sendPerformed: Boolean(companionRuntime.sendPerformed),
        turnsAtPaste: Number(companionRuntime.turnsAtPaste || 0),
        currentTurns: countConversationDomTurns(),
        queuedSteeringPresent: hasQueuedSteeringTurn(),
        composerClearedCount: Number(companionRuntime.composerClearedCount || 0),
        disarmReason: String(companionRuntime.disarmReason || ""),
        sharedModelLabels: SHARED_ARGON_MODEL_LABELS.slice()
      };
    }
    if (act === "ensure_mode_and_model") {
      return await ensureTaskModeAndArgonSemantic(pl);
    }
    if (act === "attach_archive") {
      return await attachArchiveViaDataTransfer(pl);
    }
    if (act === "click_send") {
      const opts = pl || {};
      const composer = findLowestComposer();
      if (!composer) {
        return {
          ok: false,
          operation: "send",
          path: "extension",
          reason: "composer editable was not found before send"
        };
      }
      const expectedPrompt = String(opts.promptText || companionRuntime.cachedPromptText || "");
      const expectedMin = Math.max(1, Number(opts.expectedMinLen || Math.min(Math.floor(expectedPrompt.trim().length / 2), 40) || 1));
      let currentText = getComposerText(composer);
      if (
        currentText.length < expectedMin &&
        expectedPrompt.trim() &&
        !companionRuntime.sendPerformed &&
        countConversationDomTurns() <= Number(companionRuntime.turnsAtPaste || 0)
      ) {
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
      const sendBtn = findSemanticSendButton(composer);
      if (!sendBtn) {
        return {
          ok: false,
          operation: "send",
          path: "extension",
          reason: "semantic send button was not found inside the composer container"
        };
      }
      disarmCompanionPromptGuard("click_send");
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
      const finalComposerText = getComposerText(composer);
      const emptied = finalComposerText.length < expectedMin;
      sendServiceWorkerMessage("record_operation", {
        operation: "send",
        ok: true,
        composerEmptied: emptied,
        composerClearedCount: companionRuntime.composerClearedCount,
        at: new Date().toISOString()
      });
      return {
        ok: true,
        operation: "send",
        path: "extension",
        sendPerformed: true,
        keepPromptAlive: false,
        cachedPromptText: "",
        cachedPromptTextLength: 0,
        composerEmptied: emptied,
        composerClearedCount: companionRuntime.composerClearedCount,
        reason: ""
      };
    }
    if (act === "capture_delivery") {
      const fid = pl.fileId || (companionRuntime.driveFileIds.length ? companionRuntime.driveFileIds[companionRuntime.driveFileIds.length - 1] : "");
      const capRes = await sendServiceWorkerMessage("capture_delivery", Object.assign({}, pl, { fileId: fid }));
      if (capRes && typeof capRes === "object") {
        capRes.fileName = String(pl.fileName || capRes.fileName || "apply_changes.ps1");
      }
      return capRes;
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
