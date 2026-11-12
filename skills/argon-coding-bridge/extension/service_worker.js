// MV3 Companion Service Worker for Gemini 4 Argon Coding Bridge (Round 16)
// Owns the four fragile operations (attach, paste, model/mode/send, delivery capture)
// and persists run state in chrome.storage.local so Gemini DOM changes and OS file
// dialogs cannot break the send or capture pipeline.

const EXTENSION_VERSION = "1.16.0";
const STORAGE_STATE_KEY = "argon_companion_state_v1";

async function readCompanionState() {
  try {
    const res = await chrome.storage.local.get([STORAGE_STATE_KEY]);
    return (res && res[STORAGE_STATE_KEY]) ? res[STORAGE_STATE_KEY] : {
      version: EXTENSION_VERSION,
      runs: {},
      driveFileIds: [],
      lastOperation: null,
      updatedAt: new Date().toISOString()
    };
  } catch (err) {
    return {
      version: EXTENSION_VERSION,
      runs: {},
      driveFileIds: [],
      lastOperation: null,
      error: String(err)
    };
  }
}

async function writeCompanionState(patch) {
  try {
    const cur = await readCompanionState();
    const next = Object.assign({}, cur, patch || {}, {
      version: EXTENSION_VERSION,
      updatedAt: new Date().toISOString()
    });
    await chrome.storage.local.set({ [STORAGE_STATE_KEY]: next });
    return next;
  } catch (err) {
    return { version: EXTENSION_VERSION, error: String(err) };
  }
}

async function recordDriveFileId(fileId, sourceUrl) {
  if (!fileId || typeof fileId !== "string") {
    return;
  }
  const cur = await readCompanionState();
  const ids = Array.isArray(cur.driveFileIds) ? cur.driveFileIds.slice(-30) : [];
  if (!ids.includes(fileId)) {
    ids.push(fileId);
  }
  await writeCompanionState({
    driveFileIds: ids,
    lastDriveFileId: fileId,
    lastDriveUrl: sourceUrl || ""
  });
}

function decodeDriveTextEnvelope(raw) {
  if (typeof raw !== "string" || !raw) {
    return null;
  }
  let body = raw;
  if (body.startsWith(")]}'")) {
    body = body.slice(4);
  }
  body = body.replace(/^[\r\n]+/, "");
  try {
    const parsed = JSON.parse(body);
    if (!parsed || typeof parsed !== "object") {
      return null;
    }
    const data = parsed.data;
    if (data === undefined || data === null) {
      return null;
    }
    if (Array.isArray(data)) {
      return data.map((item) => String(item)).join("");
    }
    return String(data);
  } catch (_err) {
    return null;
  }
}

async function captureDriveDeliveryWalk(payload) {
  const opts = payload || {};
  const curState = await readCompanionState();
  const fileId = String(opts.fileId || curState.lastDriveFileId || "").trim();
  const maxPages = Math.max(1, Number(opts.maxPages || 12));
  const authUser = String(opts.authUser !== undefined ? opts.authUser : "0").trim();
  const baseOrigin = String(opts.baseOrigin || "https://drive.google.com").replace(/\/+$/, "");

  if (!fileId) {
    return {
      ok: false,
      operation: "delivery",
      path: "extension",
      reason: "the Drive preview did not expose a file id"
    };
  }

  const chunks = [];
  for (let pageNo = 0; pageNo < maxPages; pageNo++) {
    const url = `${baseOrigin}/viewer/text?id=${encodeURIComponent(fileId)}&authuser=${encodeURIComponent(authUser)}&page=${pageNo}`;
    let raw = "";
    try {
      const resp = await fetch(url, {
        method: "GET",
        credentials: "include",
        cache: "no-store"
      });
      if (!resp.ok && pageNo === 0) {
        return {
          ok: false,
          operation: "delivery",
          path: "extension",
          reason: `the Drive text endpoint returned HTTP ${resp.status} on page 0`
        };
      }
      raw = await resp.text();
    } catch (err) {
      return {
        ok: false,
        operation: "delivery",
        path: "extension",
        reason: `the Drive text endpoint failed on page ${pageNo} (${String(err)})`
      };
    }

    const decoded = decodeDriveTextEnvelope(raw);
    if (decoded === null) {
      if (pageNo === 0) {
        return {
          ok: false,
          operation: "delivery",
          path: "extension",
          reason: "the Drive text endpoint did not return a file body"
        };
      }
      break;
    }
    chunks.push(decoded);
    if (decoded.length < 200) {
      break;
    }
    if (chunks.length > 1 && chunks[0] === chunks[1]) {
      chunks.length = 1;
      break;
    }
  }

  if (chunks.length === 0) {
    return {
      ok: false,
      operation: "delivery",
      path: "extension",
      reason: "the Drive text endpoint returned no content"
    };
  }

  const fullText = chunks.length === 1 ? chunks[0] : chunks.join("");
  await writeCompanionState({
    lastOperation: {
      operation: "delivery",
      ok: true,
      fileId: fileId,
      pages: chunks.length,
      length: fullText.length,
      at: new Date().toISOString()
    }
  });
  return {
    ok: true,
    operation: "delivery",
    path: "extension",
    fileId: fileId,
    pages: chunks.length,
    text: fullText
  };
}

async function fetchArchiveAsBase64(archiveUrl) {
  const resp = await fetch(archiveUrl, { method: "GET", cache: "no-store" });
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status} fetching archive from ${archiveUrl}`);
  }
  const buf = await resp.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
  }
  return {
    base64: btoa(binary),
    byteLength: bytes.length
  };
}

async function handleExtensionMessage(message, sender) {
  const msg = message || {};
  const action = String(msg.action || "").trim();
  const payload = msg.payload || {};

  if (action === "ping") {
    const state = await readCompanionState();
    return {
      ok: true,
      operation: "ping",
      path: "extension",
      version: EXTENSION_VERSION,
      state: state
    };
  }

  if (action === "get_state") {
    const state = await readCompanionState();
    return {
      ok: true,
      operation: "state",
      path: "extension",
      state: state
    };
  }

  if (action === "set_state") {
    const state = await writeCompanionState(payload);
    return {
      ok: true,
      operation: "state",
      path: "extension",
      state: state
    };
  }

  if (action === "record_drive_file_id") {
    await recordDriveFileId(payload.fileId, payload.url);
    return {
      ok: true,
      operation: "drive_id",
      path: "extension",
      fileId: payload.fileId || ""
    };
  }

  if (action === "fetch_archive_bytes") {
    try {
      const fetched = await fetchArchiveAsBase64(String(payload.archiveUrl || ""));
      return {
        ok: true,
        operation: "attach",
        path: "extension",
        base64Data: fetched.base64,
        byteLength: fetched.byteLength
      };
    } catch (err) {
      return {
        ok: false,
        operation: "attach",
        path: "extension",
        reason: `service worker could not fetch archive (${String(err)})`
      };
    }
  }

  if (action === "capture_delivery") {
    return await captureDriveDeliveryWalk(payload);
  }

  if (action === "record_operation") {
    await writeCompanionState({ lastOperation: payload });
    return {
      ok: true,
      operation: String(payload.operation || "record"),
      path: "extension"
    };
  }

  if (sender && sender.tab && sender.tab.id && payload.forwardToTab) {
    try {
      const tabResp = await chrome.tabs.sendMessage(sender.tab.id, {
        protocol: "argon.companion.v1",
        action: action,
        payload: payload
      });
      return tabResp;
    } catch (err) {
      return {
        ok: false,
        operation: action,
        path: "extension",
        reason: `tab message failed (${String(err)})`
      };
    }
  }

  return {
    ok: false,
    operation: action || "unknown",
    path: "extension",
    reason: `unsupported companion action: ${action}`
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleExtensionMessage(message, sender)
    .then((res) => sendResponse(res))
    .catch((err) => sendResponse({
      ok: false,
      operation: (message && message.action) ? String(message.action) : "error",
      path: "extension",
      reason: String(err)
    }));
  return true;
});
