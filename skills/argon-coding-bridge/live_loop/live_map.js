// Argon Live Browser Control Loop - Shared Interactive Element Map Builder (Round 19)
// Protocol: argon.live.v1
// Pure browser IIFE (zero Chrome APIs, zero Node APIs).
// Shared by Lane A (CDP page.add_init_script + page.expose_binding("__argonPush"))
// and Lane B (MV3 extension live_content_script.js via "argon:live:map" CustomEvent).

(function () {
  "use strict";

  if (typeof window === "undefined") {
    return;
  }

  var MAX_ELEMENTS = 800;
  var MAX_NAME_LEN = 120;
  var MAX_TEXT_LEN = 4000;
  var DEBOUNCE_MS = 18;
  var ALLOWED_ROLES = {
    button: true,
    link: true,
    menuitem: true,
    tab: true,
    option: true,
    checkbox: true,
    radio: true,
    switch: true
  };

  var existing = window.__argonLive;
  var state = {
    protocol: "argon.live.v1",
    gen: existing && typeof existing.gen === "number" ? existing.gen : 0,
    lastMap: existing && existing.lastMap ? existing.lastMap : null,
    lastDomElements: [],
    debounceTimer: null,
    observer: null,
    suppressObserver: false
  };

  function cleanText(raw, maxLen) {
    var s = String(raw === undefined || raw === null ? "" : raw).replace(/\s+/g, " ").trim();
    if (typeof maxLen === "number" && maxLen > 0 && s.length > maxLen) {
      return s.slice(0, maxLen);
    }
    return s;
  }

  function computeAccessibleName(el) {
    if (!el || typeof el.getAttribute !== "function") {
      return "";
    }
    var aria = cleanText(el.getAttribute("aria-label"), MAX_NAME_LEN);
    if (aria) {
      return aria;
    }
    var inner = "";
    try {
      inner = cleanText(el.innerText || el.textContent || "", MAX_NAME_LEN);
    } catch (_e) {
      inner = cleanText(el.textContent || "", MAX_NAME_LEN);
    }
    if (inner) {
      return inner;
    }
    var attrOrder = ["alt", "placeholder", "title", "name"];
    for (var i = 0; i < attrOrder.length; i++) {
      var val = cleanText(el.getAttribute(attrOrder[i]), MAX_NAME_LEN);
      if (val) {
        return val;
      }
    }
    var tag = String(el.tagName || "").toLowerCase();
    if (tag === "input" && typeof el.value === "string") {
      var t = String(el.getAttribute("type") || el.type || "").toLowerCase();
      if (t === "submit" || t === "button" || t === "reset") {
        var v = cleanText(el.value, MAX_NAME_LEN);
        if (v) {
          return v;
        }
      }
    }
    return "";
  }

  function isElementInteractive(el) {
    if (!el || el.nodeType !== 1) {
      return false;
    }
    var tag = String(el.tagName || "").toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) {
      return true;
    }
    if (tag === "button" || tag === "select" || tag === "textarea") {
      return true;
    }
    if (tag === "input") {
      var inpType = String(el.getAttribute("type") || el.type || "").toLowerCase();
      return inpType !== "hidden";
    }
    var role = String(el.getAttribute("role") || "").trim().toLowerCase();
    if (role && ALLOWED_ROLES[role]) {
      return true;
    }
    if (el.hasAttribute("contenteditable")) {
      var ce = String(el.getAttribute("contenteditable") || "").trim().toLowerCase();
      if (ce !== "false") {
        return true;
      }
    }
    if (el.hasAttribute("onclick")) {
      return true;
    }
    if (el.hasAttribute("tabindex")) {
      var ti = parseInt(String(el.getAttribute("tabindex") || "0"), 10);
      if (!isNaN(ti) && ti >= 0) {
        return true;
      }
    }
    return false;
  }

  function computeRectAndVisibility(el) {
    var rect = [0, 0, 0, 0];
    var visible = false;
    try {
      var box = el.getBoundingClientRect();
      var x = Math.round(box.left || 0);
      var y = Math.round(box.top || 0);
      var w = Math.round(box.width || 0);
      var h = Math.round(box.height || 0);
      rect = [x, y, w, h];
      if (w > 0 && h > 0) {
        var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
        if (!style || (style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0)) {
          visible = true;
        }
      }
    } catch (_e) {
      visible = false;
    }
    return { rect: rect, visible: visible };
  }

  function isInViewport(rect, visible) {
    if (!visible) {
      return false;
    }
    var vw = window.innerWidth || (document.documentElement && document.documentElement.clientWidth) || 1280;
    var vh = window.innerHeight || (document.documentElement && document.documentElement.clientHeight) || 800;
    var x = rect[0];
    var y = rect[1];
    var w = rect[2];
    var h = rect[3];
    return (x + w >= 0) && (x <= vw) && (y + h >= 0) && (y <= vh);
  }

  function extractValue(el, tag, type) {
    try {
      if (tag === "input" && type === "password") {
        return "";
      }
      if (tag === "input" || tag === "textarea" || tag === "select") {
        return String(el.value || "").slice(0, 200);
      }
      if (el.isContentEditable) {
        return cleanText(el.innerText || el.textContent || "", 200);
      }
    } catch (_e) {}
    return "";
  }

  function buildMap(reason) {
    state.gen += 1;
    var selector = [
      "a[href]",
      "button",
      "input",
      "select",
      "textarea",
      "[role]",
      "[contenteditable]",
      "[onclick]",
      "[tabindex]"
    ].join(",");

    var nodeList = [];
    var totalDomNodes = 0;
    try {
      totalDomNodes = document.getElementsByTagName("*").length;
      nodeList = Array.prototype.slice.call(document.querySelectorAll(selector));
    } catch (_e) {
      nodeList = [];
    }

    var rawItems = [];
    for (var i = 0; i < nodeList.length; i++) {
      var el = nodeList[i];
      if (!isElementInteractive(el)) {
        continue;
      }
      var tag = String(el.tagName || "").toLowerCase();
      var role = String(el.getAttribute("role") || "").trim().toLowerCase();
      var name = computeAccessibleName(el);
      var type = String(el.getAttribute("type") || (tag === "input" ? el.type : "") || "").trim().toLowerCase();
      var value = extractValue(el, tag, type);
      var href = "";
      if (tag === "a" || el.hasAttribute("href")) {
        try {
          href = String(el.href || el.getAttribute("href") || "");
        } catch (_e2) {
          href = String(el.getAttribute("href") || "");
        }
      }
      var rv = computeRectAndVisibility(el);
      rawItems.push({
        domEl: el,
        inViewport: isInViewport(rv.rect, rv.visible),
        entry: {
          id: 0,
          tag: tag,
          role: role,
          name: name,
          type: type,
          value: value,
          href: href,
          rect: rv.rect,
          visible: rv.visible
        }
      });
    }

    var truncated = false;
    var droppedCount = 0;
    var selected = rawItems;
    if (rawItems.length > MAX_ELEMENTS) {
      truncated = true;
      droppedCount = rawItems.length - MAX_ELEMENTS;
      var inVp = [];
      var outVp = [];
      for (var j = 0; j < rawItems.length; j++) {
        if (rawItems[j].inViewport) {
          inVp.push(rawItems[j]);
        } else {
          outVp.push(rawItems[j]);
        }
      }
      selected = inVp.concat(outVp).slice(0, MAX_ELEMENTS);
    }

    var elements = [];
    var domRefs = [];
    for (var k = 0; k < selected.length; k++) {
      var item = selected[k];
      item.entry.id = k;
      elements.push(item.entry);
      domRefs.push(item.domEl);
    }
    state.lastDomElements = domRefs;

    var bodyText = "";
    try {
      if (document.body) {
        bodyText = String(document.body.innerText || document.body.textContent || "")
          .replace(/[ \t]+\n/g, "\n")
          .replace(/\n{3,}/g, "\n\n")
          .trim()
          .slice(0, MAX_TEXT_LEN);
      }
    } catch (_e3) {
      bodyText = "";
    }

    var map = {
      gen: state.gen,
      url: String(location.href || ""),
      title: String(document.title || ""),
      readyState: String(document.readyState || ""),
      count: elements.length,
      domNodes: totalDomNodes,
      truncated: truncated,
      reason: String(reason || "demand"),
      at: Date.now(),
      elements: elements,
      text: bodyText
    };
    if (truncated) {
      map.note = "dropped " + droppedCount + " interactive elements beyond " + MAX_ELEMENTS + " cap (viewport elements kept first)";
    }
    state.lastMap = map;
    return map;
  }

  function pushMap(reason) {
    var map = buildMap(reason || "mutation");
    try {
      // Lane A: the CDP binding exists -> call it directly (this is the measured 12 ms path)
      if (typeof window.__argonPush === "function") {
        window.__argonPush(JSON.stringify(map));
      } else {
        // Lane B: no CDP attached -> the content script picks it up from the DOM event
        window.dispatchEvent(new CustomEvent("argon:live:map", { detail: map }));
      }
    } catch (_e) {}
    return map;
  }

  function scheduleMutationPush(reason) {
    if (state.suppressObserver) {
      return;
    }
    if (state.debounceTimer !== null) {
      clearTimeout(state.debounceTimer);
    }
    state.debounceTimer = setTimeout(function () {
      state.debounceTimer = null;
      pushMap(reason || "mutation");
    }, DEBOUNCE_MS);
  }

  function checkStaleExpect(id, expect, preMap) {
    var actual = (typeof id === "number" && id >= 0 && id < preMap.elements.length) ? preMap.elements[id] : null;
    if (expect && typeof expect === "object") {
      var expTag = expect.tag !== undefined && expect.tag !== null ? String(expect.tag).toLowerCase() : null;
      var expName = expect.name !== undefined && expect.name !== null ? String(expect.name) : null;
      var tagMatch = actual !== null && (expTag === null || actual.tag.toLowerCase() === expTag);
      var nameMatch = actual !== null && (expName === null || actual.name === expName);
      if (!actual || !tagMatch || !nameMatch) {
        var actualTag = actual ? actual.tag : "none";
        var actualName = actual ? actual.name : "";
        var expectedTagStr = expTag !== null ? expTag : (actual ? actual.tag : "element");
        var expectedNameStr = expName !== null ? expName : "";
        return {
          ok: false,
          error: "stale_element",
          message: "element " + id + " is now <" + actualTag + "> '" + actualName + "', expected <" + expectedTagStr + "> '" + expectedNameStr + "'"
        };
      }
    } else if (!actual) {
      return {
        ok: false,
        error: "element_not_found",
        message: "element " + id + " does not exist in map (count=" + preMap.count + ")"
      };
    }
    return { ok: true, element: actual, domEl: state.lastDomElements[id] };
  }

  function setNativeInputValue(domEl, text) {
    var tag = String(domEl.tagName || "").toLowerCase();
    try {
      var proto = tag === "textarea" ? window.HTMLTextAreaElement && window.HTMLTextAreaElement.prototype : window.HTMLInputElement && window.HTMLInputElement.prototype;
      var desc = proto ? Object.getOwnPropertyDescriptor(proto, "value") : null;
      if (desc && typeof desc.set === "function") {
        desc.set.call(domEl, text);
        return;
      }
    } catch (_e) {}
    domEl.value = text;
  }

  function executeDomAction(action, allowEval) {
    var t0 = Date.now();
    var act = action && typeof action === "object" ? action : {};
    var op = String(act.op || "map").trim();

    if (op === "map") {
      var curMap = buildMap("map");
      return {
        ok: true,
        op: "map",
        gen: curMap.gen,
        elapsed_ms: Math.max(0, Date.now() - t0),
        note: "built live element map",
        map: curMap
      };
    }

    if (op === "text") {
      var txtMap = buildMap("text");
      return {
        ok: true,
        op: "text",
        gen: txtMap.gen,
        elapsed_ms: Math.max(0, Date.now() - t0),
        text: txtMap.text,
        note: "extracted readable page text (" + txtMap.text.length + " chars)",
        map: txtMap
      };
    }

    if (op === "scroll") {
      var dx = typeof act.dx === "number" ? act.dx : 0;
      var dy = typeof act.dy === "number" ? act.dy : 600;
      window.scrollBy(dx, dy);
      var scrMap = buildMap("action");
      return {
        ok: true,
        op: "scroll",
        gen: scrMap.gen,
        elapsed_ms: Math.max(0, Date.now() - t0),
        note: "scrolled page by (" + dx + ", " + dy + ")",
        map: scrMap
      };
    }

    if (op === "back" || op === "forward" || op === "reload") {
      if (op === "back") {
        history.back();
      } else if (op === "forward") {
        history.forward();
      } else {
        location.reload();
      }
      var navMap = buildMap("action");
      return {
        ok: true,
        op: op,
        gen: navMap.gen,
        elapsed_ms: Math.max(0, Date.now() - t0),
        note: "executed history " + op,
        map: navMap
      };
    }

    if (op === "eval") {
      var evalPreMap = buildMap("pre_action");
      if (!allowEval) {
        return {
          ok: false,
          op: "eval",
          error: "eval_disabled",
          message: "eval is disabled by default; pass --browser-allow-eval to enable it",
          gen: evalPreMap.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          map: evalPreMap
        };
      }
      try {
        var evalFn = new Function("return (" + String(act.js || "null") + ");");
        var evalVal = evalFn();
        var evalPostMap = buildMap("action");
        return {
          ok: true,
          op: "eval",
          value: evalVal,
          gen: evalPostMap.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          note: "evaluated js expression",
          map: evalPostMap
        };
      } catch (err) {
        var errMap = buildMap("action");
        return {
          ok: false,
          op: "eval",
          error: "eval_error",
          message: String(err && err.message ? err.message : err),
          gen: errMap.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          map: errMap
        };
      }
    }

    if (op === "click" || op === "type" || op === "select" || op === "press") {
      var preMap = buildMap("pre_action");
      var targetElMeta = null;
      var domEl = null;

      if (op !== "press" || (act.id !== undefined && act.id !== null)) {
        var elId = Number(act.id);
        var check = checkStaleExpect(elId, act.expect, preMap);
        if (!check.ok) {
          return {
            ok: false,
            op: op,
            error: check.error,
            message: check.message,
            gen: preMap.gen,
            elapsed_ms: Math.max(0, Date.now() - t0),
            map: preMap
          };
        }
        targetElMeta = check.element;
        domEl = check.domEl;
      } else {
        domEl = document.activeElement || document.body;
        targetElMeta = {
          tag: String((domEl && domEl.tagName) || "body").toLowerCase(),
          name: computeAccessibleName(domEl)
        };
      }

      if (!domEl) {
        return {
          ok: false,
          op: op,
          error: "element_not_found",
          message: "DOM element reference is no longer attached",
          gen: preMap.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          map: preMap
        };
      }

      try {
        if (typeof domEl.scrollIntoView === "function") {
          domEl.scrollIntoView({ block: "nearest", inline: "nearest" });
        }
      } catch (_eScroll) {}

      if (op === "click") {
        try {
          if (typeof domEl.focus === "function") {
            domEl.focus({ preventScroll: true });
          }
        } catch (_eFocus) {}
        domEl.click();
        var clickPost = buildMap("action");
        return {
          ok: true,
          op: "click",
          gen: clickPost.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          note: "clicked " + targetElMeta.tag + " '" + targetElMeta.name + "'",
          map: clickPost
        };
      }

      if (op === "type") {
        var textToType = String(act.text !== undefined && act.text !== null ? act.text : "");
        var shouldClear = act.clear === undefined ? true : Boolean(act.clear);
        var shouldSubmit = Boolean(act.submit);
        try {
          if (typeof domEl.focus === "function") {
            domEl.focus({ preventScroll: true });
          }
        } catch (_eFocus2) {}
        var domTag = String(domEl.tagName || "").toLowerCase();
        if (domTag === "input" || domTag === "textarea") {
          var nextVal = shouldClear ? textToType : String(domEl.value || "") + textToType;
          setNativeInputValue(domEl, nextVal);
        } else if (domEl.isContentEditable) {
          domEl.textContent = shouldClear ? textToType : String(domEl.textContent || "") + textToType;
        } else {
          domEl.value = shouldClear ? textToType : String(domEl.value || "") + textToType;
        }
        try {
          domEl.dispatchEvent(new InputEvent("input", {
            bubbles: true,
            cancelable: false,
            data: textToType,
            inputType: "insertText"
          }));
        } catch (_eInp) {
          domEl.dispatchEvent(new Event("input", { bubbles: true }));
        }
        domEl.dispatchEvent(new Event("change", { bubbles: true }));
        if (shouldSubmit) {
          var enterOpts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true };
          domEl.dispatchEvent(new KeyboardEvent("keydown", enterOpts));
          domEl.dispatchEvent(new KeyboardEvent("keypress", enterOpts));
          domEl.dispatchEvent(new KeyboardEvent("keyup", enterOpts));
          if (domEl.form && typeof domEl.form.requestSubmit === "function") {
            try {
              domEl.form.requestSubmit();
            } catch (_eSub) {}
          }
        }
        var typePost = buildMap("action");
        return {
          ok: true,
          op: "type",
          gen: typePost.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          note: "typed into " + targetElMeta.tag + " '" + targetElMeta.name + "'",
          map: typePost
        };
      }

      if (op === "press") {
        var keyName = String(act.key || "Enter");
        try {
          if (typeof domEl.focus === "function") {
            domEl.focus({ preventScroll: true });
          }
        } catch (_eFocus3) {}
        var keyOpts = { key: keyName, code: keyName, bubbles: true, cancelable: true };
        domEl.dispatchEvent(new KeyboardEvent("keydown", keyOpts));
        domEl.dispatchEvent(new KeyboardEvent("keypress", keyOpts));
        domEl.dispatchEvent(new KeyboardEvent("keyup", keyOpts));
        var pressPost = buildMap("action");
        return {
          ok: true,
          op: "press",
          gen: pressPost.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          note: "pressed '" + keyName + "' on " + targetElMeta.tag + " '" + targetElMeta.name + "'",
          map: pressPost
        };
      }

      if (op === "select") {
        var selVal = String(act.value !== undefined && act.value !== null ? act.value : "");
        var matchedOpt = false;
        if (domEl.options && domEl.options.length) {
          for (var oi = 0; oi < domEl.options.length; oi++) {
            var opt = domEl.options[oi];
            if (opt.value === selVal || cleanText(opt.text, 120) === selVal) {
              opt.selected = true;
              domEl.selectedIndex = oi;
              matchedOpt = true;
              break;
            }
          }
        }
        if (!matchedOpt) {
          domEl.value = selVal;
        }
        domEl.dispatchEvent(new Event("input", { bubbles: true }));
        domEl.dispatchEvent(new Event("change", { bubbles: true }));
        var selPost = buildMap("action");
        return {
          ok: true,
          op: "select",
          gen: selPost.gen,
          elapsed_ms: Math.max(0, Date.now() - t0),
          note: "selected '" + selVal + "' on " + targetElMeta.tag + " '" + targetElMeta.name + "'",
          map: selPost
        };
      }
    }

    var fallbackMap = buildMap("demand");
    return {
      ok: false,
      op: op,
      error: "unsupported_dom_op",
      message: "unsupported DOM operation: " + op,
      gen: fallbackMap.gen,
      elapsed_ms: Math.max(0, Date.now() - t0),
      map: fallbackMap
    };
  }

  function installObserver() {
    if (state.observer || typeof MutationObserver === "undefined") {
      return;
    }
    var root = document.documentElement || document;
    if (!root) {
      return;
    }
    state.observer = new MutationObserver(function () {
      scheduleMutationPush("mutation");
    });
    try {
      state.observer.observe(root, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["href", "disabled", "hidden", "style", "class", "value", "aria-label", "role", "title"]
      });
    } catch (_e) {}
  }

  window.__argonLive = {
    protocol: "argon.live.v1",
    buildMap: buildMap,
    pushMap: pushMap,
    executeDomAction: executeDomAction,
    installObserver: installObserver,
    getLastMap: function () {
      return state.lastMap || buildMap("demand");
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      installObserver();
      pushMap("init");
    }, { once: true });
  } else {
    installObserver();
    pushMap("init");
  }

  window.addEventListener("load", function () {
    installObserver();
    pushMap("load");
  }, { once: true });
  window.addEventListener("popstate", function () {
    scheduleMutationPush("navigation");
  });
  window.addEventListener("hashchange", function () {
    scheduleMutationPush("navigation");
  });
})();