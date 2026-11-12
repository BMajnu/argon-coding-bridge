// Argon Live Browser Control Loop - MV3 Content Script (Round 19, Lane B)
// Protocol: argon.live.v1
// Responsibilities:
//   1. Mark document ready with data-argon-live-extension="1.19.0".
//   2. Keep a persistent chrome.runtime.connect() port open to reset the MV3 service worker idle timer.
//   3. Build the interactive element map with the same live_map.js algorithm and forward maps
//      to the service worker via chrome.runtime.sendMessage (and the open port).
//   4. Execute incoming actions from the service worker against the live DOM with stale-element checks.

(function () {
  "use strict";

  var EXTENSION_VERSION = "1.19.0";
  var PROTOCOL = "argon.live.v1";
  var keepAlivePort = null;
  var heartbeatTimer = null;

  function markExtensionAttribute() {
    try {
      if (document && document.documentElement) {
        document.documentElement.setAttribute("data-argon-live-extension", EXTENSION_VERSION);
      }
    } catch (_err) {}
  }

  markExtensionAttribute();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", markExtensionAttribute, { once: true });
  }

  // Ensure window.__argonLive is available in the content-script isolated world
  // as well as injected into the main world when needed.
  function ensureLiveMapRuntime() {
    if (window.__argonLive && typeof window.__argonLive.buildMap === "function") {
      return window.__argonLive;
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
    var state = {
      protocol: PROTOCOL,
      gen: 0,
      lastMap: null,
      lastDomElements: [],
      debounceTimer: null,
      observer: null
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
      var inner = cleanText(el.innerText || el.textContent || "", MAX_NAME_LEN);
      if (inner) {
        return inner;
      }
      var attrs = ["alt", "placeholder", "title", "name"];
      for (var i = 0; i < attrs.length; i++) {
        var v = cleanText(el.getAttribute(attrs[i]), MAX_NAME_LEN);
        if (v) {
          return v;
        }
      }
      return "";
    }

    function isInteractive(el) {
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
        return String(el.getAttribute("type") || el.type || "").toLowerCase() !== "hidden";
      }
      var role = String(el.getAttribute("role") || "").trim().toLowerCase();
      if (role && ALLOWED_ROLES[role]) {
        return true;
      }
      if (el.hasAttribute("contenteditable") && String(el.getAttribute("contenteditable") || "").toLowerCase() !== "false") {
        return true;
      }
      if (el.hasAttribute("onclick") || el.hasAttribute("tabindex")) {
        return true;
      }
      return false;
    }

    function buildMap(reason) {
      state.gen += 1;
      var sel = "a[href],button,input,select,textarea,[role],[contenteditable],[onclick],[tabindex]";
      var nodes = Array.prototype.slice.call(document.querySelectorAll(sel));
      var rawItems = [];
      var vw = window.innerWidth || 1280;
      var vh = window.innerHeight || 800;

      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        if (!isInteractive(el)) {
          continue;
        }
        var tag = String(el.tagName || "").toLowerCase();
        var role = String(el.getAttribute("role") || "").trim().toLowerCase();
        var name = computeAccessibleName(el);
        var type = String(el.getAttribute("type") || el.type || "").trim().toLowerCase();
        var val = (tag === "input" && type === "password") ? "" : String(el.value || "").slice(0, 200);
        var href = tag === "a" ? String(el.href || el.getAttribute("href") || "") : "";
        var box = el.getBoundingClientRect();
        var rect = [Math.round(box.left), Math.round(box.top), Math.round(box.width), Math.round(box.height)];
        var style = window.getComputedStyle ? window.getComputedStyle(el) : null;
        var vis = rect[2] > 0 && rect[3] > 0 && (!style || (style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0));
        var inVp = vis && (rect[0] + rect[2] >= 0) && (rect[0] <= vw) && (rect[1] + rect[3] >= 0) && (rect[1] <= vh);
        rawItems.push({
          domEl: el,
          inViewport: inVp,
          entry: { id: 0, tag: tag, role: role, name: name, type: type, value: val, href: href, rect: rect, visible: vis }
        });
      }

      var truncated = false;
      var dropped = 0;
      var chosen = rawItems;
      if (rawItems.length > MAX_ELEMENTS) {
        truncated = true;
        dropped = rawItems.length - MAX_ELEMENTS;
        var inV = [];
        var outV = [];
        for (var j = 0; j < rawItems.length; j++) {
          if (rawItems[j].inViewport) {
            inV.push(rawItems[j]);
          } else {
            outV.push(rawItems[j]);
          }
        }
        chosen = inV.concat(outV).slice(0, MAX_ELEMENTS);
      }

      var elements = [];
      var domRefs = [];
      for (var k = 0; k < chosen.length; k++) {
        chosen[k].entry.id = k;
        elements.push(chosen[k].entry);
        domRefs.push(chosen[k].domEl);
      }
      state.lastDomElements = domRefs;

      var txt = document.body ? String(document.body.innerText || document.body.textContent || "").trim().slice(0, MAX_TEXT_LEN) : "";
      var map = {
        gen: state.gen,
        url: String(location.href || ""),
        title: String(document.title || ""),
        readyState: String(document.readyState || ""),
        count: elements.length,
        domNodes: document.getElementsByTagName("*").length,
        truncated: truncated,
        reason: String(reason || "mutation"),
        at: Date.now(),
        elements: elements,
        text: txt
      };
      if (truncated) {
        map.note = "dropped " + dropped + " interactive elements beyond " + MAX_ELEMENTS + " cap (viewport elements kept first)";
      }
      state.lastMap = map;
      return map;
    }

    function executeDomAction(action, allowEval) {
      var t0 = Date.now();
      var act = action || {};
      var op = String(act.op || "map");
      if (op === "map") {
        var m = buildMap("map");
        return { ok: true, op: "map", gen: m.gen, elapsed_ms: Date.now() - t0, note: "built live map", map: m };
      }
      if (op === "text") {
        var tm = buildMap("text");
        return { ok: true, op: "text", gen: tm.gen, elapsed_ms: Date.now() - t0, text: tm.text, map: tm };
      }
      if (op === "scroll") {
        var dx = typeof act.dx === "number" ? act.dx : 0;
        var dy = typeof act.dy === "number" ? act.dy : 600;
        window.scrollBy(dx, dy);
        var sm = buildMap("action");
        return { ok: true, op: "scroll", gen: sm.gen, elapsed_ms: Date.now() - t0, note: "scrolled", map: sm };
      }
      if (op === "click" || op === "type" || op === "press" || op === "select") {
        var pre = buildMap("pre_action");
        var id = Number(act.id);
        var actual = (id >= 0 && id < pre.elements.length) ? pre.elements[id] : null;
        if (act.expect && typeof act.expect === "object") {
          var expTag = act.expect.tag ? String(act.expect.tag).toLowerCase() : null;
          var expName = act.expect.name !== undefined ? String(act.expect.name) : null;
          if (!actual || (expTag && actual.tag !== expTag) || (expName !== null && actual.name !== expName)) {
            var aTag = actual ? actual.tag : "none";
            var aName = actual ? actual.name : "";
            return {
              ok: false,
              op: op,
              error: "stale_element",
              message: "element " + id + " is now <" + aTag + "> '" + aName + "', expected <" + (expTag || "") + "> '" + (expName || "") + "'",
              gen: pre.gen,
              elapsed_ms: Date.now() - t0,
              map: pre
            };
          }
        } else if (!actual) {
          return { ok: false, op: op, error: "element_not_found", message: "element " + id + " not found", gen: pre.gen, elapsed_ms: Date.now() - t0, map: pre };
        }
        var domEl = state.lastDomElements[id];
        if (op === "click") {
          domEl.click();
        } else if (op === "type") {
          domEl.focus();
          domEl.value = (act.clear === false ? String(domEl.value || "") : "") + String(act.text || "");
          domEl.dispatchEvent(new Event("input", { bubbles: true }));
          domEl.dispatchEvent(new Event("change", { bubbles: true }));
        } else if (op === "press") {
          var k = String(act.key || "Enter");
          domEl.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true }));
          domEl.dispatchEvent(new KeyboardEvent("keyup", { key: k, bubbles: true }));
        } else if (op === "select") {
          domEl.value = String(act.value || "");
          domEl.dispatchEvent(new Event("change", { bubbles: true }));
        }
        var post = buildMap("action");
        return { ok: true, op: op, gen: post.gen, elapsed_ms: Date.now() - t0, note: op + " " + actual.tag + " '" + actual.name + "'", map: post };
      }
      if (op === "eval") {
        var em = buildMap("pre_action");
        if (!allowEval) {
          return { ok: false, op: "eval", error: "eval_disabled", message: "eval is disabled unless --browser-allow-eval is set", gen: em.gen, elapsed_ms: Date.now() - t0, map: em };
        }
      }
      var fm = buildMap("demand");
      return { ok: false, op: op, error: "unsupported_op", message: "unsupported op: " + op, gen: fm.gen, elapsed_ms: Date.now() - t0, map: fm };
    }

    function pushMap(reason) {
      var m = buildMap(reason || "mutation");
      forwardMapToServiceWorker(m);
      return m;
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
        if (state.debounceTimer !== null) {
          clearTimeout(state.debounceTimer);
        }
        state.debounceTimer = setTimeout(function () {
          state.debounceTimer = null;
          pushMap("mutation");
        }, DEBOUNCE_MS);
      });
      try {
        state.observer.observe(root, { childList: true, subtree: true, attributes: true });
      } catch (_e) {}
    }

    window.__argonLive = {
      protocol: PROTOCOL,
      buildMap: buildMap,
      pushMap: pushMap,
      executeDomAction: executeDomAction,
      installObserver: installObserver
    };
    return window.__argonLive;
  }

  function forwardMapToServiceWorker(mapObj) {
    if (!mapObj) {
      return;
    }
    var envelope = {
      protocol: PROTOCOL,
      type: "map",
      map: mapObj
    };
    try {
      if (keepAlivePort) {
        keepAlivePort.postMessage(envelope);
      }
    } catch (_ePort) {}
    try {
      if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        chrome.runtime.sendMessage(envelope, function () {
          if (chrome.runtime && chrome.runtime.lastError) {
            // Ignore transient worker wake errors; port reconnects automatically.
          }
        });
      }
    } catch (_eMsg) {}
  }

  // Keep a persistent chrome.runtime.connect() port open to reset the MV3 service worker's idle timer.
  function openKeepAlivePort() {
    if (typeof chrome === "undefined" || !chrome.runtime || typeof chrome.runtime.connect !== "function") {
      return;
    }
    try {
      keepAlivePort = chrome.runtime.connect({ name: PROTOCOL });
      keepAlivePort.onMessage.addListener(function (msg) {
        if (!msg || msg.protocol !== PROTOCOL) {
          return;
        }
        if (msg.type === "action") {
          var live = ensureLiveMapRuntime();
          var res = live.executeDomAction(msg.action || {}, Boolean(msg.allowEval));
          try {
            keepAlivePort.postMessage({
              protocol: PROTOCOL,
              type: "result",
              requestId: msg.requestId || "",
              result: res
            });
          } catch (_eSend) {}
        }
      });
      keepAlivePort.onDisconnect.addListener(function () {
        keepAlivePort = null;
        setTimeout(openKeepAlivePort, 1000);
      });
    } catch (_err) {
      keepAlivePort = null;
      setTimeout(openKeepAlivePort, 2000);
    }
  }

  openKeepAlivePort();
  if (!heartbeatTimer) {
    heartbeatTimer = setInterval(function () {
      if (!keepAlivePort) {
        openKeepAlivePort();
        return;
      }
      try {
        keepAlivePort.postMessage({ protocol: PROTOCOL, type: "keepalive", at: Date.now() });
      } catch (_e) {
        keepAlivePort = null;
        openKeepAlivePort();
      }
    }, 20000);
  }

  window.addEventListener("argon:live:map", function (ev) {
    if (ev && ev.detail) {
      forwardMapToServiceWorker(ev.detail);
    }
  });

  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
      if (!message || message.protocol !== PROTOCOL) {
        return false;
      }
      var live = ensureLiveMapRuntime();
      if (message.type === "action") {
        var res = live.executeDomAction(message.action || {}, Boolean(message.allowEval));
        sendResponse(res);
        return true;
      }
      if (message.type === "map") {
        var m = live.buildMap("demand");
        sendResponse({ ok: true, op: "map", gen: m.gen, map: m });
        return true;
      }
      return false;
    });
  }

  var runtime = ensureLiveMapRuntime();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      runtime.installObserver();
      runtime.pushMap("init");
    }, { once: true });
  } else {
    runtime.installObserver();
    runtime.pushMap("init");
  }
})();