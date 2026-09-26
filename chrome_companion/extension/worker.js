// Lumina Chrome Companion service worker (BROWSER-COMPANION-01A).
//
// A SENSOR, NOT AN AUTHORITY SOURCE. This worker answers bounded reads and
// two owner-granted navigation requests from Lumina's hub (via the native
// host) in this Chrome profile. It has no content scripts, no page message
// bridge (window.postMessage), or externally_connectable surface: the page
// can never manufacture a command. Page text is read
// by chrome.scripting.executeScript in the ISOLATED world, only on sites the
// owner granted through this extension's popup, never on restricted surfaces
// (policy.js) or incognito tabs.
//
// Lifecycle: DISCONNECTED -> CONNECTING -> IDENTIFIED -> READY, PAUSED blocks
// everything. Every connection is a fresh session object (new port, new hub
// connection_id, fresh request sequence); a session that ended is dead and
// anything it was still awaiting is dropped, never answered on a successor.
// PAUSE is persisted before it is acknowledged, and the worker stays paused
// (fail closed) until the persisted switch has been read on every startup.
//
// Owner control (BROWSER-COMPANION-01A-R2 / B1): every PAUSE/RESUME command
// takes the next controlEpoch the instant it arrives, and the newest command
// is the only one that can change what runs. PAUSE takes effect in that same
// instant (no await between the decision and the teardown); its durable
// write, like every owner write, is applied strictly in arrival order. A
// RESUME unpauses only once its own write has landed AND no newer command has
// arrived -- so a persisted PAUSE always means no session, and an older
// RESUME, connect, or startup read can never undo a newer PAUSE.
//
// Site-grant withdrawal (BROWSER-COMPANION-01A-R3 / B2-R3). What Chrome lets a
// worker know, from Chromium's own source (extensions/browser/permissions/
// permissions_updater.cc): a revocation changes what permissions.contains()
// answers at once, but permissions.onRemoved is dispatched only after an
// asynchronous network-service update -- nothing orders it before a LATER
// contains() that sees a re-grant. So the worker cannot rely on the event to
// learn of a revocation that was already undone. Therefore:
//  * A revoke from THIS extension's popup goes through the worker
//    (revokeSite): it is marked pending BEFORE Chrome is asked to remove
//    anything, no read commits or even injects while it is pending, and
//    every read in progress when Chrome confirms it is void. A read that
//    overlaps a popup revoke in any way never publishes -- even if the owner
//    re-grants before it ends, and whenever Chrome's event arrives.
//    "Overlaps" is measured from the moment the WORKER RECEIVES the popup's
//    message, not from the owner's click (BROWSER-COMPANION-01A-R4 / AR4):
//    the popup is a separate context, and no message crosses to the worker
//    instantly, so a read that commits between the click and that receipt
//    was still authorized as far as anything in the extension can know. The
//    worker's answer is the acknowledgement ({invalidated: true}); the popup
//    shows "Revoking…" until it arrives, claims the revoke only then, and
//    says plainly when it had to remove the grant itself without one.
//    The revoked site is also BLOCKED from that receipt (R5 / AR5), durably,
//    and stays blocked until the owner allows it again in the popup
//    (R5.1) -- whatever Chrome's grant says, then or later: Chrome's
//    settings can take access away, but never give the companion back an
//    authorization the owner revoked. The popup says "Revoked." only if
//    Chrome was also seen to no longer grant the site (revokeSite).
//  * A revoke made OUTSIDE the extension (chrome://extensions site access,
//    the toolbar's extensions menu) voids the read if it is still in effect
//    at the post-read check, or if Chrome has delivered onRemoved by the send.
//    One revoked AND re-granted before the post-read check, whose onRemoved
//    Chrome has not delivered yet, is not observable through any extension
//    API, and is NOT detected (documented contract boundary).
"use strict";

importScripts("policy.js");

const HOST_NAME = "org.lumina.chrome_companion";
const PROTOCOL_VERSION = 1;
const PAUSE_KEY = "companionPaused";
const INSTANCE_KEY = "companionInstanceId";
const BLOCK_KEY = "companionBlockedSites";
const STATUS_KEY = "companionStatus";
const RECONNECT_ALARM = "lumina-companion-reconnect";
const RETRY_DELAYS_MS = [1000, 2000, 5000, 10000, 20000];
const LIMITS = Object.freeze({
  DEFAULT_TEXT_CHARS: 12000,
  MAX_TEXT_CHARS: 30000,
  DEFAULT_LINKS: 60,
  MAX_LINKS: 150,
  MAX_TABS: 100,
  MAX_TAB_URL_CHARS: 4096,
  MAX_LINK_URL_CHARS: 2048,
  MAX_TITLE_CHARS: 300,
  MAX_LINK_TEXT_CHARS: 200,
});
const HEX32 = /^[0-9a-f]{32}$/;
// The hub numbers the requests of each connection in wire order: the first
// 12 hex digits of request_id (chrome_companion/protocol.py, R2 / P1).
const REQUEST_SEQ_HEX = 12;
const REQUEST_KEYS = ["v", "type", "connection_id", "request_id", "op", "tab_id", "deadline_ms", "args"];
const OP_TAB_RULE = { ping: "none", list_tabs: "none", get_active_tab: "none", get_tab: "required",
  extract_text: "optional", get_links: "optional", open_owner_url: "none", switch_tab: "required" };
const OP_ARGS = { ping: {}, list_tabs: {}, get_active_tab: {}, get_tab: {},
  extract_text: { max_chars: [1, LIMITS.MAX_TEXT_CHARS] }, get_links: { max_links: [1, LIMITS.MAX_LINKS] },
  open_owner_url: null, switch_tab: null };
const TAB_STATUS = new Set(["loading", "complete", "unloaded"]);
// Fixed, content-free error texts. Raw Chrome error strings can embed URLs,
// so they never cross the bridge.
const ERROR_MESSAGES = Object.freeze({
  wrong_connection: "request addressed to a different connection",
  unknown_op: "operation not supported by this companion",
  invalid_args: "malformed request arguments",
  deadline_expired: "request deadline passed before it could be answered",
  tab_not_found: "no readable tab with that id",
  no_active_tab: "no readable active tab",
  restricted_surface: "restricted surface: Lumina never reads this page",
  site_access_required: "site access not granted: the owner must allow this site in the Lumina Companion popup",
  tab_closed: "the tab closed during the request",
  navigated_during_request: "the tab navigated during the request; result discarded as stale",
  document_identity_unavailable: "Chrome did not provide a document identity for this tab; nothing was read",
  injection_failed: "Chrome could not read this page",
  chrome_api_error: "Chrome API call failed",
  navigation_not_allowed: "navigation is not allowed for this Companion session",
  companion_revoked: "the owner revoked this site in Lumina Companion",
});

let paused = true; // fail closed until the persisted switch is read
let controlEpoch = 0; // bumped by every owner PAUSE/RESUME, on arrival
let pauseWrites = Promise.resolve(); // owner writes, in arrival order
let grantRevocations = 0; // bumped by every site-grant withdrawal the worker makes or learns of
let revocationsPending = 0; // popup revokes begun here that Chrome has not confirmed yet
// Sites the owner revoked in the popup (R5 / AR5, R5.1). Never read while
// listed -- whatever Chrome's grant says -- until the owner allows the site
// again in the popup (allowSite). Durable.
const blockedSites = new Set();
let blockWrites = Promise.resolve(); // durable writes of blockedSites, in order
let instanceId = null;
let session = null; // the ONE live session, or null
let retryIndex = 0;
let retryTimer = null;
let lastError = null;
let lastAction = null;

class CompanionFailure extends Error {
  constructor(code, tabId = null) {
    super(code);
    this.code = code;
    this.tabId = tabId;
  }
}

function randomHex32() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

const ready = (async () => {
  try {
    const stored = await chrome.storage.local.get([PAUSE_KEY, INSTANCE_KEY, BLOCK_KEY]);
    // A block list that cannot be read as one fails closed, like the switch.
    const blocked = stored[BLOCK_KEY] === undefined ? [] : stored[BLOCK_KEY];
    if (!Array.isArray(blocked)) throw new Error("unreadable block list");
    // Merged, never replaced: a revoke that arrived first stays blocked.
    blocked.forEach((pattern) => { if (isSitePattern(pattern)) blockedSites.add(pattern); });
    let id = stored[INSTANCE_KEY];
    if (typeof id !== "string" || !HEX32.test(id)) {
      id = randomHex32();
      await chrome.storage.local.set({ [INSTANCE_KEY]: id });
    }
    instanceId = id;
    // An owner command that arrived while this read was pending is newer
    // than anything it returned: that command, not this read, decides.
    if (controlEpoch === 0) paused = stored[PAUSE_KEY] === true;
  } catch {
    paused = true;
    instanceId = null;
    lastError = "storage_unavailable";
  }
})();

// ---------------------------------------------------------------------------
// Status (informational only -- never writes the durable PAUSE key)
// ---------------------------------------------------------------------------

function publicState() {
  if (paused) return "PAUSED";
  if (!session) return "DISCONNECTED";
  return session.state;
}

function statusSnapshot() {
  return { state: publicState(), navigationAllowed: Boolean(session && session.state === "READY"
    && session.navigationAllowed && !paused), lastError, lastAction, updatedAt: Date.now() };
}

async function publishStatus() {
  try {
    await chrome.storage.local.set({ [STATUS_KEY]: statusSnapshot() });
  } catch {
    // informational only
  }
}

// ---------------------------------------------------------------------------
// Connection lifecycle
// ---------------------------------------------------------------------------

function clearRetryTimer() {
  if (retryTimer !== null) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }
}

function scheduleRetry() {
  if (paused) return;
  clearRetryTimer();
  if (retryIndex < RETRY_DELAYS_MS.length) {
    const delay = RETRY_DELAYS_MS[retryIndex++];
    retryTimer = setTimeout(() => {
      retryTimer = null;
      void ready.then(connect);
    }, delay);
  }
  // Durable fallback that survives a service-worker shutdown between retries.
  void Promise.resolve(chrome.alarms.create(RECONNECT_ALARM, { periodInMinutes: 0.5 })).catch(() => {});
}

function connect() {
  if (paused || session || !instanceId) return;
  clearRetryTimer();
  let port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch {
    lastError = "host_not_installed";
    scheduleRetry();
    void publishStatus();
    return;
  }
  const s = { port, state: "CONNECTING", connectionId: null, lastSeq: 0, closed: false,
    navigationAllowed: false, navigationEpoch: 0, endReason: null };
  session = s;
  port.onMessage.addListener((message) => onHostMessage(s, message));
  port.onDisconnect.addListener(() => onPortDisconnect(s));
  try {
    port.postMessage({
      v: PROTOCOL_VERSION,
      type: "hello",
      extension_id: chrome.runtime.id,
      instance_id: instanceId,
      extension_version: chrome.runtime.getManifest().version,
    });
  } catch {
    teardown(s, "host_closed");
    return;
  }
  void publishStatus();
}

// Our own disconnect (Chrome does not fire onDisconnect for the side that
// calls port.disconnect()).
function teardown(s, reason, { retry = true } = {}) {
  if (s.closed) return;
  s.closed = true;
  s.navigationAllowed = false;
  s.navigationEpoch += 1;
  if (session === s) session = null;
  lastError = reason;
  try {
    s.port.disconnect();
  } catch {
    // already gone
  }
  if (retry) scheduleRetry();
  void publishStatus();
}

function onPortDisconnect(s) {
  const detail = (chrome.runtime.lastError && chrome.runtime.lastError.message) || "";
  if (s.closed || s !== session) return;
  s.closed = true;
  s.navigationAllowed = false;
  s.navigationEpoch += 1;
  session = null;
  if (s.endReason) lastError = s.endReason;
  else if (/not found/i.test(detail)) lastError = "host_not_installed";
  else if (/forbidden/i.test(detail)) lastError = "host_forbidden";
  else lastError = s.state === "CONNECTING" ? "host_exited" : "disconnected";
  scheduleRetry();
  void publishStatus();
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function onHostMessage(s, message) {
  if (s.closed || s !== session) return;
  if (!isPlainObject(message) || message.v !== PROTOCOL_VERSION) {
    teardown(s, "protocol_violation");
    return;
  }
  switch (message.type) {
    case "host_status":
      if (message.state === "hub_connected" && s.state === "CONNECTING") {
        s.state = "IDENTIFIED";
        void publishStatus();
      } else if (message.state === "hub_unavailable" && s.state === "CONNECTING") {
        s.endReason = "lumina_unreachable"; // the host exits right after this
      } else {
        teardown(s, "protocol_violation");
      }
      return;
    case "welcome":
      if (s.state !== "IDENTIFIED" || typeof message.connection_id !== "string" || !HEX32.test(message.connection_id)) {
        teardown(s, "protocol_violation");
        return;
      }
      s.connectionId = message.connection_id;
      s.state = "READY";
      retryIndex = 0;
      lastError = null;
      clearRetryTimer();
      void Promise.resolve(chrome.alarms.clear(RECONNECT_ALARM)).catch(() => {});
      void publishStatus();
      return;
    case "reject": {
      const reason = typeof message.reason === "string" && /^[a-z_]{1,32}$/.test(message.reason) ? message.reason : "rejected";
      retryIndex = RETRY_DELAYS_MS.length; // pairing/installation needs the owner: retry at alarm cadence only
      teardown(s, reason);
      return;
    }
    case "request":
      if (s.state !== "READY") {
        teardown(s, "protocol_violation");
        return;
      }
      void handleRequest(s, message);
      return;
    default:
      teardown(s, "protocol_violation");
  }
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

function validateRequest(req) {
  const keys = Object.keys(req);
  if (keys.length !== REQUEST_KEYS.length || !REQUEST_KEYS.every((k) => keys.includes(k))) return "invalid_args";
  if (!Object.prototype.hasOwnProperty.call(OP_TAB_RULE, req.op)) return "unknown_op";
  const rule = OP_TAB_RULE[req.op];
  const tabOk = Number.isSafeInteger(req.tab_id) && req.tab_id >= 0;
  if (rule === "required" && !tabOk) return "invalid_args";
  if (rule === "optional" && !(req.tab_id === null || tabOk)) return "invalid_args";
  if (rule === "none" && req.tab_id !== null) return "invalid_args";
  if (!Number.isSafeInteger(req.deadline_ms) || req.deadline_ms <= 0) return "invalid_args";
  if (!isPlainObject(req.args)) return "invalid_args";
  if (req.op === "ping") {
    // The daily 01A hub sends {} and requires the exact 01A result shape.
    // Only a 01B-A hub opts in to the additional navigation status field.
    return Object.keys(req.args).length === 0
      || (Object.keys(req.args).length === 1 && req.args.include_navigation === true)
      ? null : "invalid_args";
  }
  if (req.op === "open_owner_url") {
    return Object.keys(req.args).length === 1 && typeof req.args.url === "string"
      && req.args.url.length > 0 && req.args.url.length <= LIMITS.MAX_TAB_URL_CHARS ? null : "invalid_args";
  }
  if (req.op === "switch_tab") {
    return Object.keys(req.args).length === 2 && Number.isSafeInteger(req.args.window_id)
      && typeof req.args.expected_url === "string" && req.args.expected_url.length > 0
      && req.args.expected_url.length <= LIMITS.MAX_TAB_URL_CHARS ? null : "invalid_args";
  }
  const spec = OP_ARGS[req.op];
  for (const [name, value] of Object.entries(req.args)) {
    if (!spec[name]) return "invalid_args";
    const [low, high] = spec[name];
    if (!Number.isSafeInteger(value) || value < low || value > high) return "invalid_args";
  }
  return null;
}

async function handleRequest(s, req) {
  const rid = req.request_id;
  if (typeof rid !== "string" || !HEX32.test(rid)) return; // cannot address a reply
  if (paused) return;

  const live = () => s === session && !s.closed && !paused && s.state === "READY";
  const send = (fields) => {
    if (!live()) return false; // never answer on a dead, replaced, or paused session
    try {
      s.port.postMessage({ v: PROTOCOL_VERSION, type: "response", connection_id: s.connectionId,
        request_id: rid, tab_id: null, observed: null, truncated: false, ...fields });
      return true;
    } catch {
      return false;
    }
  };
  const fail = (code, tabId = null) =>
    send({ ok: false, tab_id: tabId, error: { code, message: ERROR_MESSAGES[code] || "request failed" } });

  if (req.connection_id !== s.connectionId) return fail("wrong_connection");
  // AT MOST ONCE PER CONNECTION (R2 / P1). A request must carry a higher
  // sequence number than every request this session has already accepted.
  // A duplicate, a replay, or a stale request is never executed and never
  // answered -- for the connection's whole life, with one integer of state
  // (nothing to evict). An id from an earlier connection cannot run here at
  // all: it names that connection, not this one (wrong_connection above).
  const seq = parseInt(rid.slice(0, REQUEST_SEQ_HEX), 16);
  if (!(seq > s.lastSeq)) return;
  s.lastSeq = seq;
  const problem = validateRequest(req);
  if (problem) return fail(problem);
  if (Date.now() > req.deadline_ms) return fail("deadline_expired");

  const metadataEpoch = grantRevocations;
  let outcome;
  try {
    outcome = await OPERATIONS[req.op](req, live);
  } catch (error) {
    outcome = req._actionDispatched
      ? { result: actionReceipt(req, "ambiguous_after_dispatch") }
      : error instanceof CompanionFailure
        ? { error: error.code, tab_id: error.tabId }
        : { error: "chrome_api_error", tab_id: null };
  }
  // The awaited Chrome call may have spanned PAUSE, a disconnect, or a
  // reconnect. A result from a dead session is dropped, never forwarded.
  if (!live()) return;
  if (Date.now() > req.deadline_ms) {
    if ((req.op === "open_owner_url" || req.op === "switch_tab") && outcome.result) {
      outcome.result.status = "ambiguous_after_dispatch";
    } else {
      return fail("deadline_expired", outcome.tab_id ?? null);
    }
  }
  if (outcome.error) return fail(outcome.error, outcome.tab_id ?? null);
  if ((req.op === "list_tabs" || req.op === "get_tab" || req.op === "get_active_tab")
    && !grantHeld(metadataEpoch)) return fail("site_access_required", outcome.tab_id ?? null);
  // A popup Revoke can arrive while describeTab awaits Chrome permission.
  // Hide metadata again in the send tick, including a site just revoked.
  if (req.op === "list_tabs" || req.op === "get_tab" || req.op === "get_active_tab") {
    const hide = (tab) => {
      if (!tab || tab.restricted || !tab.url) return tab;
      const verdict = LuminaPolicy.classifyUrl(tab.url);
      return blockedSites.has(verdict.pattern)
        ? { ...tab, restricted: true, restriction: "companion_revoked",
          site_access: "not_granted", url: null, title: null }
        : tab;
    };
    if (req.op === "list_tabs") outcome.result.tabs = outcome.result.tabs.map(hide);
    else outcome.result = hide(outcome.result);
  }
  // Commit gate, part 2 (R2 / B2, R3 / B2-R3), in the same tick as the send:
  // no withdrawal the worker knows of began at any point since this read
  // began -- not even one the owner restored before it ended -- and no popup
  // revoke is still waiting for Chrome.
  if (outcome.grantEpoch !== undefined && !grantHeld(outcome.grantEpoch)) {
    return fail("site_access_required", outcome.tab_id ?? null);
  }
  if ((req.op === "open_owner_url" || req.op === "switch_tab") && outcome.result
    && (!s.navigationAllowed || s.navigationEpoch !== outcome.navigationEpoch
      || !grantHeld(outcome.actionGrantEpoch) || blockedSites.has(outcome.targetPattern))) {
    // A browser action may already have happened. Never report it as a
    // confirmed local effect after an owner control overtook it.
    outcome.result.status = "ambiguous_after_dispatch";
    outcome.result.observed_url = null;
    outcome.result.load_confirmed = false;
  }
  if (send({ ok: true, result: outcome.result, tab_id: outcome.tab_id ?? null,
    observed: outcome.observed ?? null, truncated: Boolean(outcome.truncated) })) {
    lastAction = { op: req.op, tab_id: outcome.tab_id ?? null, host: outcome.host ?? null, at: Date.now() };
    void publishStatus();
  }
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------

function safeSlice(text, limit) {
  if (text.length <= limit) return text;
  let cut = text.slice(0, limit);
  const last = cut.charCodeAt(cut.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1);
  return cut;
}

function isTabId(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

async function siteAccess(verdict) {
  if (!verdict.readable) return "restricted";
  // Every read -- its pre-check, its post-read commit check, list_tabs --
  // asks here, so a blocked site is unreadable on every path (R5 / AR5).
  if (blockedSites.has(verdict.pattern)) return "not_granted";
  try {
    return (await chrome.permissions.contains({ origins: [verdict.pattern] })) ? "granted" : "not_granted";
  } catch {
    return "not_granted";
  }
}

async function describeTab(tab, accessCache) {
  const base = { tab_id: tab.id, window_id: Number.isSafeInteger(tab.windowId) ? tab.windowId : -1,
    active: Boolean(tab.active), incognito: false, status: TAB_STATUS.has(tab.status) ? tab.status : null };
  const verdict = LuminaPolicy.classifyUrl(tab.url);
  if (!verdict.readable || tab.url.length > LIMITS.MAX_TAB_URL_CHARS) {
    return { ...base, restricted: true, restriction: verdict.reason || "url_too_long",
      site_access: "restricted", url: null, title: null };
  }
  // A Companion Revoke now hides metadata too. Chrome's tabs API can still
  // supply it, but Lumina must receive only an opaque tab identity.
  if (blockedSites.has(verdict.pattern)) {
    return { ...base, restricted: true, restriction: "companion_revoked",
      site_access: "not_granted", url: null, title: null };
  }
  let access = accessCache.get(verdict.pattern);
  if (access === undefined) {
    access = await siteAccess(verdict);
    accessCache.set(verdict.pattern, access);
  }
  return { ...base, restricted: false, restriction: null, site_access: access, url: tab.url,
    title: safeSlice(typeof tab.title === "string" ? tab.title : "", LIMITS.MAX_TITLE_CHARS) };
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab || tab.incognito || !isTabId(tab.id)) return null;
  return tab;
}

async function readableTab(tabId) {
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    throw new CompanionFailure("tab_not_found", tabId);
  }
  if (!tab || tab.incognito) throw new CompanionFailure("tab_not_found", tabId);
  return tab;
}

// Injected into the page's ISOLATED world; must be self-contained.
function extractVisibleText(maxChars) {
  const root = document.body || document.documentElement;
  let raw = "";
  if (root) raw = typeof root.innerText === "string" ? root.innerText : root.textContent || "";
  const budget = maxChars * 4 + 4096;
  const preCut = raw.length > budget;
  if (preCut) raw = raw.slice(0, budget);
  let text = raw
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t\f\v\u00a0]+/g, " ")
    .replace(/ ?\n ?/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  const total = text.length;
  const truncated = preCut || total > maxChars;
  if (text.length > maxChars) {
    text = text.slice(0, maxChars);
    const last = text.charCodeAt(text.length - 1);
    if (last >= 0xd800 && last <= 0xdbff) text = text.slice(0, -1);
  }
  return { href: location.href, title: String(document.title || "").slice(0, 300), text,
    total_chars: total, truncated };
}

// Injected into the page's ISOLATED world; must be self-contained.
function extractLinks(maxLinks) {
  const out = [];
  const seen = new Set();
  let total = 0;
  let scanned = 0;
  let capped = false;
  const here = location.origin;
  for (const anchor of document.querySelectorAll("a[href], area[href]")) {
    if (++scanned > 5000) {
      capped = true;
      break;
    }
    let url;
    try {
      url = new URL(anchor.getAttribute("href"), document.baseURI);
    } catch {
      continue;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") continue;
    url.username = "";
    url.password = "";
    const href = url.href;
    if (href.length > 2048 || seen.has(href)) continue;
    seen.add(href);
    total += 1;
    if (out.length >= maxLinks) continue;
    const label = (anchor.innerText || anchor.getAttribute("aria-label") || anchor.getAttribute("title") || "")
      .replace(/\s+/g, " ").trim().slice(0, 200);
    out.push({ text: label, href, same_origin: url.origin === here });
  }
  return { href: location.href, links: out, total_links: total, truncated: capped || total > out.length };
}

// Chrome's identity for the document now in the tab's main frame
// (webNavigation.getFrame, Chrome 106+): a browser-assigned UUID that changes
// whenever the frame loads a new document -- a same-URL reload included --
// and that page script cannot influence. null unless there is an ACTIVE main
// document (not prerendering, back/forward-cached, or pending deletion).
async function mainDocument(tabId) {
  let frame;
  try {
    frame = await chrome.webNavigation.getFrame({ tabId, frameId: 0 });
  } catch {
    return null;
  }
  if (!isPlainObject(frame) || frame.documentLifecycle !== "active" || typeof frame.url !== "string"
    || typeof frame.documentId !== "string" || frame.documentId.length === 0 || frame.documentId.length > 128) {
    return null;
  }
  return { documentId: frame.documentId, url: frame.url };
}

// Why a read bound to `documentId` could not run: the tab is gone, the owner
// withdrew site access, or that document is no longer the tab's document.
async function readFailure(tabId, verdict, documentId) {
  try {
    await chrome.tabs.get(tabId);
  } catch {
    return new CompanionFailure("tab_closed", tabId);
  }
  if ((await siteAccess(verdict)) !== "granted") return new CompanionFailure("site_access_required", tabId);
  const now = await mainDocument(tabId);
  if (!now || now.documentId !== documentId) return new CompanionFailure("navigated_during_request", tabId);
  return new CompanionFailure("injection_failed", tabId);
}

// No grant withdrawal has begun since `epoch` was taken, and no popup revoke
// is still waiting for Chrome (R3 / B2-R3).
function grantHeld(epoch) {
  return epoch === grantRevocations && revocationsPending === 0;
}

// Reads ONE document: the one in the tab's main frame when the read starts
// (BROWSER-COMPANION-01A-R1 / F3). If that document is replaced before the
// read completes -- navigation, a same-URL reload, A -> B -> A -- the result
// is stale and discarded. URL equality alone cannot tell those apart.
//
// The owner's site grant must authorize the read's WHOLE interval
// (BROWSER-COMPANION-01A-R2 / B2): it is checked before the read starts, and
// again -- for the observed document's own origin -- after the read and its
// document checks, as the commit gate; handleRequest then confirms, in the
// same tick as the send, that no withdrawal the worker knows of began
// meanwhile (grantHeld). What that covers -- every popup revoke, and an
// outside revoke still in effect or already announced by Chrome -- and what
// it cannot (an outside revoke undone before Chrome announces it) is set out
// at the top of this file.
async function readPage(req, func, funcArg, live) {
  const grantEpoch = grantRevocations; // taken BEFORE the pre-read grant check
  const tab = req.tab_id === null ? await activeTab() : await readableTab(req.tab_id);
  if (!tab) throw new CompanionFailure("no_active_tab");
  const tabId = tab.id;
  const before = tab.url;
  const verdict = LuminaPolicy.classifyUrl(before);
  if (!verdict.readable) throw new CompanionFailure("restricted_surface", tabId);
  if ((await siteAccess(verdict)) !== "granted") throw new CompanionFailure("site_access_required", tabId);
  if (!chrome.webNavigation || typeof chrome.webNavigation.getFrame !== "function") {
    throw new CompanionFailure("document_identity_unavailable", tabId); // never read without it
  }
  const start = await mainDocument(tabId);
  if (!start) throw await readFailure(tabId, verdict, null);
  if (start.url !== before) throw new CompanionFailure("navigated_during_request", tabId);
  // PAUSE MEANS NO READ (R2 / B1): a read whose session was paused or ended
  // while it was being set up is never dispatched into the page at all.
  if (!live()) throw new CompanionFailure("session_ended", tabId);
  // ...and REVOKE MEANS NO READ (R3 / B2-R3): nor is one the owner's revoke
  // has already overtaken.
  if (!grantHeld(grantEpoch)) throw new CompanionFailure("site_access_required", tabId);
  let injection;
  try {
    // Targeted by document, not frame: Chrome refuses outright if this
    // document is no longer in the tab ("No document with id ...").
    [injection] = await chrome.scripting.executeScript({
      target: { tabId, documentIds: [start.documentId] },
      world: "ISOLATED",
      func,
      args: [funcArg],
    });
  } catch {
    throw await readFailure(tabId, verdict, start.documentId);
  }
  let after;
  try {
    after = await chrome.tabs.get(tabId);
  } catch {
    throw new CompanionFailure("tab_closed", tabId);
  }
  const end = await mainDocument(tabId);
  // Same document, start to finish, by Chrome's identity: the injection went
  // to the bound document, and it is STILL the tab's active main document.
  // (InjectionResult.documentId alone is captured when Chrome dispatches the
  // script, so only the after-check proves nothing replaced it meanwhile.)
  if (!injection || injection.documentId !== start.documentId || !end || end.documentId !== start.documentId) {
    throw new CompanionFailure("navigated_during_request", tabId);
  }
  const page = injection.result;
  if (!isPlainObject(page) || typeof page.href !== "string") throw new CompanionFailure("injection_failed", tabId);
  // ...and every URL agrees: the one checked, the one the script saw, the
  // tab's and the document's after it ran (catches same-document navigation).
  if (page.href !== before || !after || after.url !== before || end.url !== before) {
    throw new CompanionFailure("navigated_during_request", tabId);
  }
  // Commit gate, part 1: the grant still covers the origin of the document
  // that was actually observed (verdict is that document's, proven above).
  if ((await siteAccess(verdict)) !== "granted") throw new CompanionFailure("site_access_required", tabId);
  let host = null;
  try {
    host = new URL(before).host;
  } catch {
    host = null;
  }
  return { tabId, page, host, grantEpoch,
    observed: { url: before, origin: verdict.origin, document_id: start.documentId } };
}

// BC-01B-A actions never execute page code. The owner supplies a URL in a
// trusted ingress event; the hub checks that provenance before forwarding it.
// The worker independently enforces its own live session grant and site block.
function navigationVerdict(s, url, live) {
  if (!live() || !s.navigationAllowed) throw new CompanionFailure("navigation_not_allowed");
  let parsed;
  try { parsed = new URL(url); } catch { throw new CompanionFailure("invalid_args"); }
  if (parsed.username || parsed.password || parsed.hash || parsed.href.length > LIMITS.MAX_TAB_URL_CHARS) {
    throw new CompanionFailure("invalid_args");
  }
  const verdict = LuminaPolicy.classifyUrl(parsed.href);
  if (!verdict.readable) throw new CompanionFailure("restricted_surface");
  if (blockedSites.has(verdict.pattern) || revocationsPending) throw new CompanionFailure("companion_revoked");
  return verdict;
}

function actionReceipt(req, status, tab = null, observedUrl = null, loadConfirmed = false) {
  return { operation_id: `${req.connection_id}:${req.request_id}`, status,
    tab_id: tab && isTabId(tab.id) ? tab.id : null,
    window_id: tab && Number.isSafeInteger(tab.windowId) ? tab.windowId : null,
    observed_url: observedUrl, load_confirmed: Boolean(loadConfirmed) };
}

async function safeActionTab(tabId, expectedWindow = null) {
  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch { return null; }
  if (!tab || tab.incognito || !isTabId(tab.id)
    || (expectedWindow !== null && tab.windowId !== expectedWindow)) return null;
  const verdict = LuminaPolicy.classifyUrl(tab.url);
  if (!verdict.readable || blockedSites.has(verdict.pattern)) return null;
  return tab;
}

async function settleCreatedTab(tabId, req, s, navEpoch, grantEpoch, live) {
  // tabs.create can answer with an ID before Chrome publishes its URL in
  // tabs.get. Observe that same tab briefly; never issue a second create or
  // navigation. A control change or deadline ends observation immediately.
  for (let attempt = 0; attempt < 20; attempt++) {
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch || Date.now() >= req.deadline_ms) return null;
    const tab = await safeActionTab(tabId);
    if (tab) return tab;
    if (attempt < 19) await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return null;
}

const OPERATIONS = Object.freeze({
  async ping(req) {
    const result = { extension_version: chrome.runtime.getManifest().version };
    if (req.args.include_navigation === true) {
      result.navigation_allowed = Boolean(session && session.navigationAllowed);
    }
    return { result };
  },

  async list_tabs() {
    const tabs = (await chrome.tabs.query({})).filter((t) => !t.incognito && isTabId(t.id));
    const chosen = tabs.slice(0, LIMITS.MAX_TABS);
    const cache = new Map();
    const described = [];
    for (const tab of chosen) described.push(await describeTab(tab, cache));
    return { result: { tabs: described, total: tabs.length }, truncated: tabs.length > chosen.length };
  },

  async get_active_tab() {
    const tab = await activeTab();
    if (!tab) return { result: null };
    return { result: await describeTab(tab, new Map()), tab_id: tab.id };
  },

  async get_tab(req) {
    const tab = await readableTab(req.tab_id);
    return { result: await describeTab(tab, new Map()), tab_id: tab.id };
  },

  async extract_text(req, live) {
    const maxChars = Math.min(req.args.max_chars ?? LIMITS.DEFAULT_TEXT_CHARS, LIMITS.MAX_TEXT_CHARS);
    const { tabId, page, host, observed, grantEpoch } = await readPage(req, extractVisibleText, maxChars, live);
    if (typeof page.text !== "string" || !Number.isSafeInteger(page.total_chars)) {
      throw new CompanionFailure("injection_failed", tabId);
    }
    const text = safeSlice(page.text, maxChars);
    return {
      result: { text, total_chars: Math.max(page.total_chars, text.length),
        title: safeSlice(typeof page.title === "string" ? page.title : "", LIMITS.MAX_TITLE_CHARS) },
      tab_id: tabId, observed, host, grantEpoch,
      truncated: Boolean(page.truncated) || text.length < page.text.length,
    };
  },

  async get_links(req, live) {
    const maxLinks = Math.min(req.args.max_links ?? LIMITS.DEFAULT_LINKS, LIMITS.MAX_LINKS);
    const { tabId, page, host, observed, grantEpoch } = await readPage(req, extractLinks, maxLinks, live);
    if (!Array.isArray(page.links) || !Number.isSafeInteger(page.total_links)) {
      throw new CompanionFailure("injection_failed", tabId);
    }
    const links = [];
    for (const link of page.links.slice(0, maxLinks)) {
      if (!isPlainObject(link) || typeof link.href !== "string" || link.href.length > LIMITS.MAX_LINK_URL_CHARS) continue;
      if (!/^https?:\/\//i.test(link.href)) continue;
      links.push({ text: safeSlice(typeof link.text === "string" ? link.text : "", LIMITS.MAX_LINK_TEXT_CHARS),
        href: link.href, same_origin: link.same_origin === true });
    }
    return {
      result: { links, total_links: Math.max(page.total_links, links.length) },
      tab_id: tabId, observed, host, grantEpoch,
      truncated: Boolean(page.truncated) || links.length < page.links.length,
    };
  },

  async open_owner_url(req, live) {
    const s = session;
    const verdict = navigationVerdict(s, req.args.url, live);
    const navEpoch = s.navigationEpoch;
    const grantEpoch = grantRevocations;
    if (Date.now() > req.deadline_ms) throw new CompanionFailure("deadline_expired");
    // This is the dispatch boundary. A failure or disconnect from here on is
    // ambiguous; Chrome may have created the tab before an answer was lost.
    let created;
    req._actionDispatched = true;
    try {
      created = await chrome.tabs.create({ url: req.args.url, active: true });
    } catch {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    if (!created || !isTabId(created.id)) {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    const current = await settleCreatedTab(created.id, req, s, navEpoch, grantEpoch, live);
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch || blockedSites.has(verdict.pattern) || !current) {
      return { result: actionReceipt(req, "ambiguous_after_dispatch", created),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    const doc = current.status === "complete" ? await mainDocument(created.id) : null;
    const loaded = Boolean(doc && doc.url === current.url);
    return { result: actionReceipt(req, "browser_local_effect_observed", current,
      current.url, loaded), tab_id: current.id, navigationEpoch: navEpoch,
      actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
  },

  async switch_tab(req, live) {
    const s = session;
    const before = await safeActionTab(req.tab_id, req.args.window_id);
    if (!before || before.url !== req.args.expected_url) {
      throw new CompanionFailure("navigated_during_request", req.tab_id);
    }
    const verdict = navigationVerdict(s, before.url, live);
    const navEpoch = s.navigationEpoch;
    const grantEpoch = grantRevocations;
    if (Date.now() > req.deadline_ms) throw new CompanionFailure("deadline_expired", req.tab_id);
    let updated;
    req._actionDispatched = true;
    try {
      updated = await chrome.tabs.update(req.tab_id, { active: true });
    } catch {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    const current = await safeActionTab(req.tab_id, req.args.window_id);
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch || blockedSites.has(verdict.pattern)
      || !current || !current.active || current.url !== before.url || !updated) {
      return { result: actionReceipt(req, "ambiguous_after_dispatch", updated),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    return { result: actionReceipt(req, "browser_local_effect_observed", current,
      current.url, current.status === "complete"), tab_id: current.id,
      navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
  },
});

// ---------------------------------------------------------------------------
// Owner controls (popup only) and wake-up events
// ---------------------------------------------------------------------------

// One owner command (R2 / B1). Deliberately NOT async: everything before the
// returned promise -- the new epoch, and for PAUSE the teardown -- happens in
// the tick the command arrives, with no await in between.
function setPaused(value) {
  const epoch = ++controlEpoch; // supersedes every earlier command, now
  if (value) {
    paused = true; // no session, no read, no reconnect from this instant
    clearRetryTimer();
    void Promise.resolve(chrome.alarms.clear(RECONNECT_ALARM)).catch(() => {});
    const s = session;
    if (s) {
      if (s.state === "READY") {
        try {
          s.port.postMessage({ v: PROTOCOL_VERSION, type: "bye", connection_id: s.connectionId, reason: "paused" });
        } catch {
          // port already gone
        }
      }
      teardown(s, "paused", { retry: false });
    }
  }
  // Owner writes are applied strictly in arrival order, so the newest
  // command's value is also the last one persisted.
  const persisted = pauseWrites.then(async () => {
    await ready;
    await chrome.storage.local.set({ [PAUSE_KEY]: value });
  });
  pauseWrites = persisted.catch(() => {});
  return persisted.then(() => {
    // A newer command owns the switch now; this one changes nothing more.
    if (epoch !== controlEpoch) return { ok: true, paused: value, superseded: true };
    lastError = null;
    if (!value) {
      paused = false; // only now: persisted, and still the newest command
      retryIndex = 0;
      connect();
    }
    void publishStatus();
    return { ok: true, paused: value };
  }, () => ({ ok: false, error: value ? "pause_not_persisted" : "resume_not_persisted" }));
}

// The owner's popup revoke (R3 / B2-R3). The worker, not the popup, removes
// the grant, so the invalidation is ordered before Chrome's removal by
// construction: the removal is marked pending in this tick, before
// permissions.remove() is even called, and while it is pending no read
// injects or commits (grantHeld). remove() resolves only after Chrome has
// applied the removal (PermissionsRemoveFunction answers from the updater's
// completion callback); the epoch moves then, so every read in progress at
// that moment -- one that began before this revoke, or while it was
// pending -- is void. Any read overlapping [pending, confirmed] is caught by
// one or the other.
//
// THE ACKNOWLEDGEMENT (R4 / AR4). The pending mark is made synchronously, in
// the very onMessage dispatch that delivers the popup's message -- before
// this function's first await -- so the instant the worker receives a revoke
// is the instant every read in progress is void. Every answer therefore
// carries invalidated: true.
//
// THE REVOKE IS THE OWNER'S, NOT CHROME'S (R5 / AR5, R5.1). The site is
// also BLOCKED at that same instant, and the block is saved before Chrome is
// asked. It stays -- after this revoke ends, across worker restarts, and
// whatever Chrome's grant does later -- until the owner allows the site
// again in the popup (allowSite). Reading needs BOTH Chrome's grant AND the
// companion's own authorization; Chrome's settings can remove the first,
// never restore the second.
//
// What Chrome did is checked, not assumed. remove()'s own boolean is
// reported as Chrome gave it (remove_result) but decides nothing: current
// Chromium resolves true even when there was nothing to remove, and the API
// documents false for "not removed". What decides is the postcondition, read
// back with contains() (chrome_access: false, true, or null if remove() or
// contains() failed). ok: true means exactly: reads were invalidated, the
// block is saved, AND Chrome's grant is confirmed absent. The popup says
// "Revoked." on nothing else; otherwise the answer says why (error).
async function revokeSite(pattern) {
  revocationsPending += 1; // the acknowledgement point: no await before this
  blockedSites.add(pattern); // ...and from this same instant, blocked, until the owner allows it
  void saveBlockedSites(); // durable before Chrome is even asked
  let removeResult = null; // remove()'s boolean, as Chrome gave it
  let chromeAccess = null; // the postcondition: does Chrome still grant it?
  try {
    const removed = await chrome.permissions.remove({ origins: [pattern] });
    removeResult = typeof removed === "boolean" ? removed : null;
    const granted = await chrome.permissions.contains({ origins: [pattern] });
    chromeAccess = typeof granted === "boolean" ? granted : null;
  } catch {
    chromeAccess = null; // Chrome's state is unknown
  } finally {
    grantRevocations += 1;
    revocationsPending -= 1;
  }
  const saved = await saveBlockedSites();
  const answer = { ok: saved && chromeAccess === false, invalidated: true, blocked: blockedSites.has(pattern),
    chrome_access: chromeAccess, remove_result: removeResult };
  if (!answer.ok) {
    answer.error = !saved ? "site_block_not_saved"
      : chromeAccess === true ? "site_access_remove_failed" : "site_access_unconfirmed";
  }
  return answer;
}

// The owner allowed the site again in the popup, after Chrome granted it
// there (R5.1): the ONLY thing that lifts a revoke's block.
async function allowSite(pattern) {
  blockedSites.delete(pattern);
  return (await saveBlockedSites()) ? { ok: true } : { ok: false, error: "site_allow_not_saved" };
}

// Persists the CURRENT block list (writes in order, so the last one wins).
// Resolves true once it is on disk, false if storage refused it.
function saveBlockedSites() {
  const write = blockWrites.then(async () => {
    await ready;
    await chrome.storage.local.set({ [BLOCK_KEY]: [...blockedSites].sort() });
  });
  blockWrites = write.catch(() => {});
  return write.then(() => true, () => false);
}

// Only a pattern the popup itself derives from a readable tab URL.
function isSitePattern(pattern) {
  if (typeof pattern !== "string" || pattern.length > 300 || !pattern.endsWith("/*")) return false;
  return LuminaPolicy.classifyUrl(pattern.slice(0, -1)).pattern === pattern;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Only this extension's own popup may use these controls -- never a page,
  // never another extension.
  if (sender.id !== chrome.runtime.id || sender.tab || sender.url !== chrome.runtime.getURL("popup.html")) return false;
  if (!isPlainObject(message)) return false;
  if (message.kind === "set_paused" && typeof message.paused === "boolean") {
    void setPaused(message.paused).then(sendResponse);
    return true;
  }
  if (message.kind === "set_navigation" && typeof message.allowed === "boolean") {
    const s = session;
    if (!s || s.closed || s.state !== "READY" || paused) {
      sendResponse({ ok: false, error: "not_connected" });
      return false;
    }
    s.navigationAllowed = message.allowed;
    s.navigationEpoch += 1;
    void publishStatus();
    sendResponse({ ok: true, navigation_allowed: s.navigationAllowed });
    return false;
  }
  if (message.kind === "revoke_site") {
    if (!isSitePattern(message.pattern)) return false;
    void revokeSite(message.pattern).then(sendResponse);
    return true;
  }
  if (message.kind === "allow_site") {
    if (!isSitePattern(message.pattern)) return false;
    void allowSite(message.pattern).then(sendResponse);
    return true;
  }
  if (message.kind === "site_state") {
    if (!isSitePattern(message.pattern)) return false;
    void ready.then(() => sendResponse({ blocked: blockedSites.has(message.pattern) }));
    return true;
  }
  if (message.kind === "get_status") {
    void ready.then(() => sendResponse({ ...statusSnapshot(), instanceId }));
    return true;
  }
  return false;
});

// Any site-grant withdrawal Chrome announces voids every read in progress
// (R2 / B2): its commit gate sees the epoch move and discards the result.
// (Chrome announces it late -- see the top of this file.)
chrome.permissions.onRemoved.addListener(() => {
  grantRevocations += 1;
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === RECONNECT_ALARM) void ready.then(connect);
});
chrome.runtime.onStartup.addListener(() => {
  void ready.then(connect);
});
chrome.runtime.onInstalled.addListener(() => {
  void ready.then(connect);
});

void ready.then(() => {
  connect();
  void publishStatus();
});
