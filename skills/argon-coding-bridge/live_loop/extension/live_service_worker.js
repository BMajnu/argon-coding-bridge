// Argon Live Browser Control Loop - MV3 Service Worker (Round 19, Lane B)
// Protocol: argon.live.v1
// Responsibilities:
//   1. Store ONLY the daemon WebSocket URL and token in chrome.storage.local.
//   2. Hold ONE WebSocket to ws://127.0.0.1:<port>/live?token=<token> (strictly 127.0.0.1).
//   3. Accept persistent chrome.runtime.onConnect ports from content scripts to prevent MV3 idle termination.
//   4. Relay live maps from tabs to the daemon and relay actions from the daemon to tabs.

const EXTENSION_VERSION = "1.19.0";
const PROTOCOL = "argon.live.v1";
const STORAGE_CONFIG_KEY = "argon_live_loop_config_v1";

let activeSocket = null;
let socketConnecting = false;
let reconnectTimer = null;
const connectedPorts = new Map();
const lastMapByTabId = new Map();

function isStrictlyLocalhost127(urlStr) {
  if (typeof urlStr !== "string" || !urlStr) {
    return false;
  }
  try {
    const parsed = new URL(urlStr);
    const protoOk = parsed.protocol === "ws:" || parsed.protocol === "http:";
    return protoOk && parsed.hostname === "127.0.0.1";
  } catch (_e) {
    return false;
  }
}

async function readDaemonConfig() {
  try {
    const res = await chrome.storage.local.get([STORAGE_CONFIG_KEY]);
    const cfg = (res && res[STORAGE_CONFIG_KEY]) || {};
    return {
      wsUrl: typeof cfg.wsUrl === "string" ? cfg.wsUrl : "",
      token: typeof cfg.token === "string" ? cfg.token : ""
    };
  } catch (_e) {
    return { wsUrl: "", token: "" };
  }
}

async function writeDaemonConfig(wsUrl, token) {
  const cleanUrl = String(wsUrl || "").trim();
  const cleanToken = String(token || "").trim();
  if (cleanUrl && !isStrictlyLocalhost127(cleanUrl)) {
    throw new Error("Refusing non-127.0.0.1 daemon URL: " + cleanUrl);
  }
  const next = {
    wsUrl: cleanUrl,
    token: cleanToken
  };
  await chrome.storage.local.set({ [STORAGE_CONFIG_KEY]: next });
  return next;
}

function sendOverWebSocket(payload) {
  if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN) {
    return false;
  }
  try {
    activeSocket.send(JSON.stringify(payload));
    return true;
  } catch (_e) {
    return false;
  }
}

async function handleDaemonActionMessage(msg) {
  const reqId = String((msg && msg.requestId) || "");
  const action = (msg && msg.action) || {};
  const op = String(action.op || "map");

  try {
    const tabs = await chrome.tabs.query({});
    const targetTabIndex = typeof action.tab === "number" ? action.tab : 0;
    const targetTab = tabs[targetTabIndex] || tabs.find((t) => t.active) || tabs[0];

    if (op === "tabs") {
      const tabList = tabs.map((t, idx) => ({
        tab: idx,
        index: idx,
        id: t.id,
        url: t.url || "",
        title: t.title || "",
        active: Boolean(t.active)
      }));
      sendOverWebSocket({
        type: "result",
        requestId: reqId,
        ok: true,
        op: "tabs",
        tab: targetTabIndex,
        tabs: tabList,
        map: (targetTab && lastMapByTabId.get(targetTab.id)) || null
      });
      return;
    }

    if (op === "navigate") {
      const navUrl = String(action.url || "");
      if (!/^https?:\/\//i.test(navUrl)) {
        sendOverWebSocket({
          type: "result",
          requestId: reqId,
          ok: false,
          op: "navigate",
          error: "invalid_url_scheme",
          message: "navigate only accepts http:// and https:// URLs"
        });
        return;
      }
      if (targetTab && targetTab.id !== undefined) {
        await chrome.tabs.update(targetTab.id, { url: navUrl });
      }
      sendOverWebSocket({
        type: "result",
        requestId: reqId,
        ok: true,
        op: "navigate",
        tab: targetTabIndex,
        note: "navigated to " + navUrl,
        map: (targetTab && lastMapByTabId.get(targetTab.id)) || null
      });
      return;
    }

    if (!targetTab || targetTab.id === undefined) {
      sendOverWebSocket({
        type: "result",
        requestId: reqId,
        ok: false,
        op: op,
        error: "no_active_tab",
        message: "no browser tab available"
      });
      return;
    }

    chrome.tabs.sendMessage(
      targetTab.id,
      {
        protocol: PROTOCOL,
        type: "action",
        requestId: reqId,
        action: action,
        allowEval: Boolean(msg.allowEval)
      },
      (resp) => {
        if (chrome.runtime.lastError || !resp) {
          sendOverWebSocket({
            type: "result",
            requestId: reqId,
            ok: false,
            op: op,
            error: "content_script_unreachable",
            message: (chrome.runtime.lastError && chrome.runtime.lastError.message) || "no response from tab content script"
          });
          return;
        }
        if (resp.map) {
          lastMapByTabId.set(targetTab.id, resp.map);
        }
        sendOverWebSocket(Object.assign({ type: "result", requestId: reqId, tab: targetTabIndex }, resp));
      }
    );
  } catch (err) {
    sendOverWebSocket({
      type: "result",
      requestId: reqId,
      ok: false,
      op: op,
      error: "worker_exception",
      message: String(err && err.message ? err.message : err)
    });
  }
}

async function ensureWebSocketConnected() {
  if (activeSocket && (activeSocket.readyState === WebSocket.OPEN || activeSocket.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (socketConnecting) {
    return;
  }
  const cfg = await readDaemonConfig();
  if (!cfg.wsUrl || !cfg.token || !isStrictlyLocalhost127(cfg.wsUrl)) {
    return;
  }

  socketConnecting = true;
  try {
    const sep = cfg.wsUrl.includes("?") ? "&" : "?";
    const fullWsUrl = `${cfg.wsUrl}${sep}token=${encodeURIComponent(cfg.token)}`;
    const ws = new WebSocket(fullWsUrl);
    activeSocket = ws;

    ws.onopen = async () => {
      socketConnecting = false;
      const tabs = await chrome.tabs.query({});
      sendOverWebSocket({
        type: "hello",
        version: EXTENSION_VERSION,
        protocol: PROTOCOL,
        tabs: tabs.map((t, idx) => ({ tab: idx, url: t.url || "", title: t.title || "" }))
      });
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(String(event.data || "{}"));
        if (msg && msg.type === "action") {
          handleDaemonActionMessage(msg);
        }
      } catch (_e) {}
    };

    ws.onclose = () => {
      socketConnecting = false;
      activeSocket = null;
      if (reconnectTimer === null) {
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          ensureWebSocketConnected();
        }, 3000);
      }
    };

    ws.onerror = () => {
      socketConnecting = false;
    };
  } catch (_err) {
    socketConnecting = false;
    activeSocket = null;
  }
}

// Persistent port listener: keeps the MV3 service worker alive while tabs are open.
chrome.runtime.onConnect.addListener((port) => {
  if (!port || port.name !== PROTOCOL) {
    return;
  }
  const tabId = port.sender && port.sender.tab ? port.sender.tab.id : null;
  if (tabId !== null && tabId !== undefined) {
    connectedPorts.set(tabId, port);
  }
  ensureWebSocketConnected();

  port.onMessage.addListener((msg) => {
    if (!msg || msg.protocol !== PROTOCOL) {
      return;
    }
    if (msg.type === "map" && msg.map) {
      if (tabId !== null && tabId !== undefined) {
        lastMapByTabId.set(tabId, msg.map);
      }
      sendOverWebSocket({
        type: "map",
        tab: 0,
        map: msg.map
      });
    }
  });

  port.onDisconnect.addListener(() => {
    if (tabId !== null && tabId !== undefined) {
      connectedPorts.delete(tabId);
    }
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.protocol !== PROTOCOL) {
    return false;
  }
  if (message.type === "configure") {
    writeDaemonConfig(message.wsUrl, message.token)
      .then((cfg) => {
        ensureWebSocketConnected();
        sendResponse({ ok: true, config: { wsUrl: cfg.wsUrl } });
      })
      .catch((err) => {
        sendResponse({ ok: false, error: String(err) });
      });
    return true;
  }
  if (message.type === "map" && message.map) {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    if (tabId !== null && tabId !== undefined) {
      lastMapByTabId.set(tabId, message.map);
    }
    ensureWebSocketConnected();
    const sent = sendOverWebSocket({
      type: "map",
      tab: 0,
      map: message.map
    });
    sendResponse({ ok: true, forwarded: sent });
    return true;
  }
  return false;
});