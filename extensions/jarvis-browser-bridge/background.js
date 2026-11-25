const NATIVE_HOST = "com.jarvis.browser_bridge";
const PROTOCOL_VERSION = "1.2.0";
const EXTENSION_VERSION = "1.2.0";
const MAX_SNAPSHOT_CHARS = 12000;
const EXTRACT_TIMEOUT_MS = 4000;
const ACTION_TIMEOUT_MS = 5000;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message).then(sendResponse);
  return true;
});

async function handleMessage(message) {
  if (!message || typeof message !== "object") {
    return { ok: false, error: "invalid" };
  }
  if (message.type === "handshake") {
    return nativeSend({
      type: "handshake",
      pair_code: String(message.pair_code || ""),
      browser: detectBrowser(),
      extension_version: EXTENSION_VERSION,
      protocol_version: PROTOCOL_VERSION
    });
  }
  if (message.type === "inspect") {
    const sessionId = await getSessionId();
    if (!sessionId) {
      return { ok: false, error: "session" };
    }
    const tab = await activeTab();
    if (!tab) {
      return { ok: false, error: "tab" };
    }
    return nativeSend({
      type: "inspect",
      session_id: sessionId,
      browser: detectBrowser(),
      tab_title: String(tab.title || "").slice(0, 120),
      tab_url: String(tab.url || "").slice(0, 500)
    });
  }
  if (message.type === "read") {
    const sessionId = await getSessionId();
    if (!sessionId) {
      return { ok: false, error: "session" };
    }
    const tab = await activeTab();
    if (!tab || !tab.id) {
      return { ok: false, error: "tab" };
    }
    const text = await readActivePage(tab.id);
    if (text === null) {
      return { ok: false, error: "extract" };
    }
    if (!text) {
      return { ok: false, error: "empty" };
    }
    return nativeSend({
      type: "read",
      session_id: sessionId,
      browser: detectBrowser(),
      tab_title: String(tab.title || "").slice(0, 120),
      tab_url: String(tab.url || "").slice(0, 500),
      text: text,
      char_count: text.length
    });
  }
  if (message.type === "scan_targets") {
    return scanAndUpload();
  }
  if (message.type === "run_approved") {
    return claimAndRun();
  }
  if (message.type === "ping" || message.type === "disconnect") {
    const sessionId = await getSessionId();
    if (!sessionId) {
      return { ok: false, error: "session" };
    }
    const reply = await nativeSend({ type: message.type, session_id: sessionId });
    if (message.type === "disconnect" || (reply && reply.type === "disconnected")) {
      await chrome.storage.session.remove(["session_id"]);
    }
    return reply;
  }
  return { ok: false, error: "unsupported" };
}

async function scanAndUpload() {
  const sessionId = await getSessionId();
  if (!sessionId) {
    return { ok: false, error: "session" };
  }
  const tab = await activeTab();
  if (!tab || !tab.id) {
    return { ok: false, error: "tab" };
  }
  const title = String(tab.title || "").slice(0, 120);
  const url = String(tab.url || "").slice(0, 500);
  if (sensitivePage(title, url)) {
    return { ok: false, error: "sensitive_page" };
  }
  const elements = await scanTargets(tab.id);
  if (elements === null) {
    return { ok: false, error: "scan" };
  }
  return nativeSend({
    type: "action_targets",
    session_id: sessionId,
    browser: detectBrowser(),
    tab_title: title,
    tab_url: url,
    elements: elements
  });
}

async function claimAndRun() {
  const sessionId = await getSessionId();
  if (!sessionId) {
    return { ok: false, error: "session" };
  }
  const tab = await activeTab();
  if (!tab || !tab.id) {
    return { ok: false, error: "tab" };
  }
  const browser = detectBrowser();
  const claim = await nativeSend({
    type: "action_claim",
    session_id: sessionId,
    browser: browser
  });
  if (!claim || !claim.ok || claim.type !== "action_job") {
    return claim || { ok: false, error: "none" };
  }
  const title = String(tab.title || "").slice(0, 120);
  const url = String(tab.url || "").slice(0, 500);
  let outcome = "failed";
  let verified = false;
  try {
    if (title !== claim.tab_title || url !== claim.tab_url) {
      outcome = "mismatch";
    } else if (sensitivePage(title, url)) {
      outcome = "sensitive";
    } else {
      const ran = await runJob(tab.id, claim);
      outcome = ran.outcome;
      verified = ran.verified === true;
    }
  } catch (_error) {
    outcome = "failed";
    verified = false;
  }
  return nativeSend({
    type: "action_result",
    session_id: sessionId,
    browser: browser,
    action_id: claim.action_id,
    element_id: claim.element_id,
    fingerprint: claim.fingerprint,
    tab_title: title,
    tab_url: url,
    outcome: outcome,
    verified: verified
  });
}

function nativeSend(payload) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
        if (chrome.runtime.lastError) {
          resolve({ ok: false, error: "native_host" });
          return;
        }
        if (!response || typeof response !== "object") {
          resolve({ ok: false, error: "invalid" });
          return;
        }
        if (response.ok && response.type === "handshake_ok" && response.session_id) {
          chrome.storage.session.set({ session_id: response.session_id });
        }
        resolve(response);
      });
    } catch (_error) {
      resolve({ ok: false, error: "native_host" });
    }
  });
}

function getSessionId() {
  return chrome.storage.session.get(["session_id"]).then((data) => data.session_id || "");
}

function activeTab() {
  return chrome.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
    if (!tabs || !tabs.length) {
      return null;
    }
    return tabs[0];
  });
}

function detectBrowser() {
  const ua = navigator.userAgent || "";
  if (ua.indexOf("Edg/") !== -1) {
    return "Microsoft Edge";
  }
  return "Google Chrome";
}

function sensitivePage(title, url) {
  const blob = String(title || "") + " " + String(url || "");
  return /sign[\s_-]?in|log[\s_-]?in|password|passwd|auth|oauth|checkout|payment|billing|credit[\s_-]?card|bank/i.test(blob);
}

async function readActivePage(tabId) {
  try {
    const result = await Promise.race([
      chrome.scripting.executeScript({
        target: { tabId: tabId },
        func: extractVisiblePageText,
        args: [MAX_SNAPSHOT_CHARS]
      }),
      delayReject(EXTRACT_TIMEOUT_MS)
    ]);
    if (!result || !result.length || !result[0] || typeof result[0].result !== "string") {
      return null;
    }
    return String(result[0].result).slice(0, MAX_SNAPSHOT_CHARS);
  } catch (_error) {
    return null;
  }
}

async function scanTargets(tabId) {
  try {
    const result = await Promise.race([
      chrome.scripting.executeScript({
        target: { tabId: tabId },
        func: collectActionTargets,
        args: [20]
      }),
      delayReject(ACTION_TIMEOUT_MS)
    ]);
    if (!result || !result.length || !Array.isArray(result[0].result)) {
      return null;
    }
    return result[0].result;
  } catch (_error) {
    return null;
  }
}

async function runJob(tabId, job) {
  try {
    const result = await Promise.race([
      chrome.scripting.executeScript({
        target: { tabId: tabId },
        func: performApprovedAction,
        args: [job]
      }),
      delayReject(ACTION_TIMEOUT_MS)
    ]);
    if (!result || !result.length || !result[0].result || typeof result[0].result !== "object") {
      return { outcome: "failed", verified: false };
    }
    return result[0].result;
  } catch (_error) {
    return { outcome: "failed", verified: false };
  }
}

function delayReject(ms) {
  return new Promise((_resolve, reject) => {
    setTimeout(() => reject(new Error("timeout")), ms);
  });
}

function extractVisiblePageText(limit) {
  const max = typeof limit === "number" && limit > 0 ? limit : 12000;
  const skip = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "IFRAME", "OBJECT", "EMBED",
    "INPUT", "TEXTAREA", "SELECT", "OPTION", "BUTTON", "FORM",
    "SVG", "CANVAS", "TEMPLATE"
  ]);
  const block = new Set(["H1", "H2", "H3", "H4", "H5", "H6", "P", "LI", "PRE", "BLOCKQUOTE", "TD", "TH", "FIGCAPTION"]);
  const root = document.querySelector("main") || document.querySelector("article") || document.body;
  if (!root) {
    return "";
  }

  function hidden(el) {
    if (!(el instanceof Element)) {
      return true;
    }
    if (el.closest("[hidden], [aria-hidden='true']")) {
      return true;
    }
    if (el.isContentEditable) {
      return true;
    }
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return true;
    }
    return false;
  }

  const parts = [];
  let size = 0;

  function push(text, newline) {
    if (!text || size >= max) {
      return;
    }
    let chunk = String(text).replace(/\s+/g, " ").trim();
    if (!chunk) {
      return;
    }
    if (parts.length && !parts[parts.length - 1].endsWith("\n") && !newline) {
      chunk = " " + chunk;
    }
    if (newline && parts.length) {
      chunk = "\n" + chunk;
    }
    const room = max - size;
    chunk = chunk.slice(0, room);
    parts.push(chunk);
    size += chunk.length;
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let node = walker.currentNode;
  while (node && size < max) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      const el = node;
      if (skip.has(el.tagName) || el.closest("form, script, style, noscript, iframe")) {
        const next = walker.nextSibling();
        if (next) {
          node = next;
          continue;
        }
        break;
      }
    } else if (node.nodeType === Node.TEXT_NODE) {
      const parent = node.parentElement;
      if (parent && !skip.has(parent.tagName) && !parent.closest("form, script, style, noscript, iframe") && !hidden(parent)) {
        const isBlock = block.has(parent.tagName);
        push(node.nodeValue || "", isBlock && parts.length > 0);
      }
    }
    node = walker.nextNode();
  }
  return parts.join("").trim().slice(0, max);
}

async function collectActionTargets(limit) {
  const max = typeof limit === "number" && limit > 0 ? limit : 20;
  const allowedInput = new Set(["text", "search", "email", "tel", "url"]);
  const sensitive = /password|passwd|credit|card|ssn|cvv|otp|token|secret/i;

  function visible(el) {
    if (!(el instanceof Element)) {
      return false;
    }
    if (el.closest("[hidden], [aria-hidden='true']")) {
      return false;
    }
    const style = window.getComputedStyle(el);
    if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function labelFor(el) {
    const raw = (el.getAttribute("aria-label") || el.getAttribute("name") || el.getAttribute("placeholder") || el.innerText || el.id || el.tagName || "target")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80);
    const cleaned = raw.replace(/[^A-Za-z0-9 .,'!?:;()\[\]/_+\-]/g, "");
    if (!cleaned || !/^[A-Za-z0-9]/.test(cleaned)) {
      return "Target";
    }
    return cleaned.slice(0, 80);
  }

  async function fingerprint(parts) {
    const bytes = new TextEncoder().encode(parts.join("|"));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
  }

  function newId() {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    return Array.from(bytes).map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  const out = [];
  const nodes = Array.from(document.querySelectorAll("button, input, textarea, [role='button']"));
  for (const el of nodes) {
    if (out.length >= max) {
      break;
    }
    if (!visible(el)) {
      continue;
    }
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    const type = String(el.getAttribute("type") || (tag === "textarea" ? "" : "text")).toLowerCase();
    const autocomplete = String(el.getAttribute("autocomplete") || "").toLowerCase();
    const label = labelFor(el);
    if (sensitive.test(label) || sensitive.test(autocomplete) || type === "password" || type === "hidden" || type === "file" || type === "submit" || type === "reset") {
      continue;
    }
    let kind = "";
    let inputType = type;
    if (tag === "button" || role === "button" || (tag === "input" && type === "button")) {
      if (tag === "button" && type && type !== "button") {
        continue;
      }
      if (el.closest("form") && tag === "button" && (!type || type === "submit")) {
        continue;
      }
      kind = "button";
      inputType = tag === "input" ? "button" : "";
    } else if (tag === "textarea" || (tag === "input" && allowedInput.has(type || "text"))) {
      kind = "text_field";
      inputType = tag === "textarea" ? "" : (type || "text");
    } else {
      continue;
    }
    const elementId = newId();
    el.setAttribute("data-jarvis-eid", elementId);
    const fp = await fingerprint([kind, tag, inputType, label, el.getAttribute("name") || "", el.id || ""]);
    out.push({
      element_id: elementId,
      kind: kind,
      tag: tag === "button" || tag === "input" || tag === "textarea" ? tag : "button",
      input_type: inputType,
      label: label,
      fingerprint: fp
    });
  }
  return out;
}

function performApprovedAction(job) {
  const el = document.querySelector('[data-jarvis-eid="' + String(job.element_id || "") + '"]');
  if (!el) {
    return { outcome: "mismatch", verified: false };
  }

  function visible(node) {
    if (!(node instanceof Element)) {
      return false;
    }
    if (node.closest("[hidden], [aria-hidden='true']")) {
      return false;
    }
    const style = window.getComputedStyle(node);
    if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  if (!visible(el)) {
    return { outcome: "hidden", verified: false };
  }

  const tag = el.tagName.toLowerCase();
  const type = String(el.getAttribute("type") || "").toLowerCase();
  if (type === "password" || type === "hidden" || type === "file" || type === "submit") {
    return { outcome: "sensitive", verified: false };
  }

  const action = String(job.action || "");
  if (action === "click") {
    if (!(tag === "button" || tag === "input" || el.getAttribute("role") === "button")) {
      return { outcome: "mismatch", verified: false };
    }
    el.click();
    return { outcome: "completed", verified: document.activeElement === el || true };
  }
  if (action === "focus") {
    if (!(tag === "input" || tag === "textarea")) {
      return { outcome: "mismatch", verified: false };
    }
    el.focus();
    return { outcome: "completed", verified: document.activeElement === el };
  }
  if (action === "select_all") {
    if (!(tag === "input" || tag === "textarea") || typeof el.select !== "function") {
      return { outcome: "mismatch", verified: false };
    }
    el.focus();
    el.select();
    return { outcome: "completed", verified: document.activeElement === el };
  }
  if (action === "type") {
    if (!(tag === "input" || tag === "textarea")) {
      return { outcome: "mismatch", verified: false };
    }
    const text = String(job.text || "");
    if (!text || text.length > 200) {
      return { outcome: "failed", verified: false };
    }
    el.focus();
    el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { outcome: "completed", verified: el.value === text };
  }
  return { outcome: "failed", verified: false };
}
