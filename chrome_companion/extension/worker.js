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
  // BROWSER-COMPANION-COMPLETION-01 Slice 1 (mirror of protocol.py).
  DEFAULT_OBSERVE_ITEMS: 40,
  MAX_OBSERVE_ITEMS: 120,
  MAX_QUERY_CHARS: 100,
  MAX_ELEMENT_NAME_CHARS: 120,
  MAX_ELEMENT_CONTEXT_CHARS: 160,
  MAX_ELEMENT_HREF_CHARS: 512,
  MAX_ELEMENT_VALUE_CHARS: 80,
  MAX_ROLE_CHARS: 32,
  MAX_HEADINGS: 12,
  MAX_NAVIGATE_SCOPE: 16,
  NAVIGATE_SETTLE_POLLS: 100,
  // BROWSER-COMPANION-HARDENING-01: confirmation polling (observation only)
  // runs until the request's own deadline minus this margin, so the answer --
  // confirmed or honestly ambiguous -- reaches the hub before it stops waiting.
  SETTLE_POLL_MS: 150,
  SETTLE_ANSWER_MARGIN_MS: 1500,
});
const REF_RE = /^e[1-9][0-9]{0,6}$/;
const ELEMENT_KINDS = new Set(["link", "button", "field", "toggle", "option", "other"]);
const ELEMENT_STATES = new Set(["disabled", "checked", "unchecked", "mixed", "expanded", "collapsed",
  "selected", "pressed", "required", "readonly", "has_value"]);
const SCROLL_VALUES = new Set(["up", "down", "top", "bottom"]);
const HEX32 = /^[0-9a-f]{32}$/;
// The hub numbers the requests of each connection in wire order: the first
// 12 hex digits of request_id (chrome_companion/protocol.py, R2 / P1).
const REQUEST_SEQ_HEX = 12;
const REQUEST_KEYS = ["v", "type", "connection_id", "request_id", "op", "tab_id", "deadline_ms", "args"];
const OP_TAB_RULE = { ping: "none", list_tabs: "none", get_active_tab: "none", get_tab: "required",
  extract_text: "optional", get_links: "optional", open_owner_url: "none", switch_tab: "required",
  follow_link: "required", observe: "optional", navigate: "optional" };
const OP_ARGS = { ping: {}, list_tabs: {}, get_active_tab: {}, get_tab: {},
  extract_text: { max_chars: [1, LIMITS.MAX_TEXT_CHARS] }, get_links: { max_links: [1, LIMITS.MAX_LINKS] },
  open_owner_url: null, switch_tab: null, follow_link: null, observe: null, navigate: null };
// Every op that may move the owner's browser to another document or tab. The
// post-dispatch gates below key off this ONE list: an op that can move the
// browser but is missing from ACTION_OPS would silently skip the owner's
// navigation-allow and revoke overrides. Keep it in step with core/
// chrome_companion_hub.py ACTION_OPS and protocol.py OPS. (observe's optional
// scroll changes only the viewport of the document it reads, under the same
// read grant, PAUSE and revoke gates as every read; it is not an action.)
const ACTION_OPS = new Set(["open_owner_url", "switch_tab", "follow_link", "navigate"]);
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
  // Stage 2 (FINGER). Content-free, like every message here: no URL, no page
  // text, and nothing that could describe the document it was refused on.
  stale_document: "the document Lumina identified the link on is no longer the tab's document",
  target_not_found: "no link on this document has that exact text and destination",
  target_ambiguous: "more than one link on this document has that exact text and destination",
  target_changed: "the link's destination no longer matches the one Lumina observed",
  cross_origin_target: "the link leaves the origin the owner authorized",
  // Slice 1 (generic observation and navigation). Content-free as above.
  stale_target: "the element Lumina observed is gone and no single element replaces it",
  not_interactable: "the element Lumina observed is no longer visible",
  not_a_link: "the element Lumina observed is not a link to a web page",
  same_document_link: "the link only moves within the current page; scroll or observe instead",
  outside_mission_scope: "the destination is neither a site the owner allowed nor one the owner named this turn",
  sensitive_navigation: "the destination looks like an account action (sign-out, delete, unsubscribe)",
  no_history_entry: "there is no page in that direction of this tab's history",
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
    deliveredLinks: new Map(), deliveredRefs: new Map(), navigationAllowed: false, navigationEpoch: 0,
    endReason: null };
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
  if (req.op === "follow_link") {
    // A semantic CLAIM, never a destination. The href is compared against
    // what the live document reports, so a page-supplied value can only ever
    // be rejected -- never obeyed.
    const keys = Object.keys(req.args).sort();
    const wanted = ["document_id", "href", "text", "window_id"];
    if (keys.length !== wanted.length || keys.some((k, i) => k !== wanted[i])) return "invalid_args";
    if (!Number.isSafeInteger(req.args.window_id) || req.args.window_id < 0) return "invalid_args";
    if (typeof req.args.href !== "string" || req.args.href.length === 0
      || req.args.href.length > LIMITS.MAX_LINK_URL_CHARS) return "invalid_args";
    // Same readable-HTTP(S) rule the Python mirror applies in
    // chrome_companion/policy.py and tools/chrome_companion.py, so a claim
    // built from a javascript:/data:/restricted URL is refused the same way
    // on both sides instead of merely finding no match.
    if (!LuminaPolicy.classifyUrl(req.args.href).readable) return "invalid_args";
    if (typeof req.args.text !== "string" || req.args.text.length > LIMITS.MAX_LINK_TEXT_CHARS) return "invalid_args";
    if (typeof req.args.document_id !== "string" || req.args.document_id.length === 0
      || req.args.document_id.length > 128) return "invalid_args";
    return null;
  }
  if (req.op === "observe") return validateObserveArgs(req.args);
  if (req.op === "navigate") return validateNavigateArgs(req.tab_id, req.args);
  const spec = OP_ARGS[req.op];
  for (const [name, value] of Object.entries(req.args)) {
    if (!spec[name]) return "invalid_args";
    const [low, high] = spec[name];
    if (!Number.isSafeInteger(value) || value < low || value > high) return "invalid_args";
  }
  return null;
}

// Slice 1. Mirrors of protocol.py _validate_observe_args/_validate_navigate_args.
function validateObserveArgs(args) {
  const allowed = ["max_items", "query", "since_gen", "scroll"];
  if (Object.keys(args).some((k) => !allowed.includes(k))) return "invalid_args";
  if ("max_items" in args && !(Number.isSafeInteger(args.max_items) && args.max_items >= 1
    && args.max_items <= LIMITS.MAX_OBSERVE_ITEMS)) return "invalid_args";
  if ("query" in args && !(typeof args.query === "string" && args.query.length > 0
    && args.query.length <= LIMITS.MAX_QUERY_CHARS)) return "invalid_args";
  if ("since_gen" in args && !(Number.isSafeInteger(args.since_gen) && args.since_gen >= 0)) return "invalid_args";
  if ("scroll" in args && !SCROLL_VALUES.has(args.scroll)) return "invalid_args";
  return null;
}

function validateNavigateArgs(tabId, args) {
  const kind = args.kind;
  if (!["url", "ref", "back", "forward"].includes(kind)) return "invalid_args";
  const bound = ["window_id", "document_id"];
  let required;
  if (kind === "url") required = ["kind", "url", "scope", ...(tabId !== null ? bound : [])];
  else if (kind === "ref") required = ["kind", "ref", "scope", ...bound];
  else required = ["kind", ...bound];
  const keys = Object.keys(args);
  if (keys.length !== required.length || !required.every((k) => keys.includes(k))) return "invalid_args";
  if (kind !== "url" && tabId === null) return "invalid_args";
  if ("window_id" in args && !(Number.isSafeInteger(args.window_id) && args.window_id >= 0)) return "invalid_args";
  if ("document_id" in args && !(typeof args.document_id === "string" && args.document_id.length > 0
    && args.document_id.length <= 128)) return "invalid_args";
  if (kind === "url" && !(typeof args.url === "string" && args.url.length <= LIMITS.MAX_TAB_URL_CHARS
    && LuminaPolicy.classifyUrl(args.url).readable)) return "invalid_args";
  if (kind === "ref" && !(typeof args.ref === "string" && REF_RE.test(args.ref))) return "invalid_args";
  if ("scope" in args) {
    const scope = args.scope;
    if (!Array.isArray(scope) || scope.length > LIMITS.MAX_NAVIGATE_SCOPE || !scope.every(isSitePattern)
      || new Set(scope).size !== scope.length) return "invalid_args";
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
    if (ACTION_OPS.has(req.op) && outcome.result) {
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
  if (ACTION_OPS.has(req.op) && outcome.result
    && (!s.navigationAllowed || s.navigationEpoch !== outcome.navigationEpoch
      || !grantHeld(outcome.actionGrantEpoch) || blockedSites.has(outcome.targetPattern))) {
    // A browser action may already have happened. Never report it as a
    // confirmed local effect after an owner control overtook it.
    outcome.result.status = "ambiguous_after_dispatch";
    outcome.result.observed_url = null;
    outcome.result.load_confirmed = false;
  }
  // HARDENING-01: an ambiguous action receipt still concerns the tab the
  // action was dispatched to (or the tab Chrome created for it). Name it, on
  // EVERY ambiguous path (exception, deadline, owner control, settle), so the
  // hub never mistakes honest ambiguity for a different tab's answer.
  if (ACTION_OPS.has(req.op) && outcome.result && outcome.result.status === "ambiguous_after_dispatch"
    && !Number.isSafeInteger(outcome.tab_id)) {
    outcome.tab_id = Number.isSafeInteger(req.tab_id) ? req.tab_id
      : Number.isSafeInteger(outcome.result.tab_id) ? outcome.result.tab_id : null;
  }
  if (send({ ok: true, result: outcome.result, tab_id: outcome.tab_id ?? null,
    observed: outcome.observed ?? null, truncated: Boolean(outcome.truncated) })) {
    if (req.op === "get_links" && outcome.observed && Number.isSafeInteger(outcome.tab_id)) {
      // Only a result sent on this connection creates eligibility. Preserve
      // multiple delivered lists for the same document without a clock TTL.
      // The document identity nonce stays here (FINGER-STALE-DOC): never sent
      // to the hub or the model. A different identity starts a fresh set.
      const previous = s.deliveredLinks.get(outcome.tab_id);
      const pairs = previous && previous.documentId === outcome.observed.document_id
        && previous.nonce === outcome.documentNonce ? previous.pairs : new Set();
      for (const link of outcome.result.links) pairs.add(JSON.stringify([link.text, link.href]));
      s.deliveredLinks.set(outcome.tab_id, { documentId: outcome.observed.document_id,
        nonce: outcome.documentNonce, pairs });
    }
    if (req.op === "observe" && outcome.observed && Number.isSafeInteger(outcome.tab_id)) {
      // Slice 1: same eligibility rule for element references. Only a
      // reference sent on THIS connection, for THIS document, can be acted on.
      // The document identity nonce stays here: it never crosses to the hub or the model.
      const previous = s.deliveredRefs.get(outcome.tab_id);
      const same = previous && previous.documentId === outcome.observed.document_id
        && previous.nonce === outcome.documentNonce;
      const refs = same ? previous.refs : new Set();
      for (const element of outcome.result.elements) refs.add(element.ref);
      s.deliveredRefs.set(outcome.tab_id, { documentId: outcome.observed.document_id, nonce: outcome.documentNonce, refs });
    }
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
  // The ONE per-document identity (FINGER-STALE-DOC): minted on first use in
  // this extension's isolated world for this document, frozen, shared by
  // element references and FINGER links. Page scripts cannot read or set it.
  const DOC_KEY = "__luminaCompanionDocument_v1";
  if (!globalThis[DOC_KEY]) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    globalThis[DOC_KEY] = Object.freeze({ nonce: Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("") });
  }
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
  return { href: location.href, links: out, total_links: total, truncated: capped || total > out.length,
    document_nonce: globalThis[DOC_KEY].nonce };
}

// Stage 2 (FINGER): resolve ONE link Lumina already observed. Injected
// read-only, exactly like extractLinks -- it queries the DOM and RETURNS
// data. It never clicks, never dispatches an event, never runs page script.
//
// The model supplies a claim (text + href it saw in chrome_get_links); this
// function supplies the destination. That inversion is the whole security
// property of Stage 2: a page can make a claim FAIL, never make one SUCCEED
// somewhere else. Normalization mirrors extractLinks exactly, so a link the
// model observed resolves here to the same string -- and a link the page
// mutated since the observation stops matching at all.
//
// Uniqueness is counted over ELEMENTS, not destinations. Two anchors with
// one href are still two statements by the page about where this link goes,
// and Stage 2 fails closed rather than guessing which was meant.
function resolveLinkTarget(claim) {
  const here = location.origin;
  const out = { href: location.href, matches: 0, resolved_href: null, same_origin: false };
  // Only the document get_links delivered from (FINGER-STALE-DOC). Chrome 155
  // may run a stale-targeted script in the tab's NEW document; a same-URL one
  // would otherwise pass every check below.
  const doc = globalThis.__luminaCompanionDocument_v1;
  if (!doc || doc.nonce !== claim.document_nonce) return Object.assign(out, { other_document: true });
  const label = (anchor) => (anchor.innerText || anchor.getAttribute("aria-label")
    || anchor.getAttribute("title") || "").replace(/\s+/g, " ").trim().slice(0, 200);
  let scanned = 0;
  for (const anchor of document.querySelectorAll("a[href], area[href]")) {
    if (++scanned > 5000) break;
    let url;
    try {
      url = new URL(anchor.getAttribute("href"), document.baseURI);
    } catch {
      continue;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") continue;
    url.username = "";
    url.password = "";
    if (url.href !== claim.href) continue;
    if (label(anchor) !== claim.text) continue;
    out.matches += 1;
    if (out.matches === 1) {
      out.resolved_href = url.href;
      out.same_origin = url.origin === here;
    }
  }
  return out;
}

// Fixed extension-owned actuator. Chrome dispatches it only into the source
// document; it resolves the target again and never runs page-supplied code.
function dispatchVerifiedLink(claim) {
  const doc = globalThis.__luminaCompanionDocument_v1;
  if (!doc || doc.nonce !== claim.document_nonce) return { status: "refused" }; // FINGER-STALE-DOC
  if (location.href !== claim.source_url) return { status: "refused" };
  const label = (anchor) => (anchor.innerText || anchor.getAttribute("aria-label")
    || anchor.getAttribute("title") || "").replace(/\s+/g, " ").trim().slice(0, 200);
  let count = 0;
  let selected = null;
  let scanned = 0;
  for (const anchor of document.querySelectorAll("a[href], area[href]")) {
    if (++scanned > 5000) return { status: "refused" };
    let url;
    try { url = new URL(anchor.getAttribute("href"), document.baseURI); } catch { continue; }
    if (url.protocol !== "http:" && url.protocol !== "https:") continue;
    url.username = "";
    url.password = "";
    if (url.href !== claim.href || label(anchor) !== claim.text) continue;
    count += 1;
    selected = url;
  }
  if (count !== 1 || !selected || selected.origin !== location.origin) return { status: "refused" };
  location.assign(selected.href);
  return { status: "dispatched" };
}

// ---------------------------------------------------------------------------
// Slice 1 (BROWSER-COMPANION-COMPLETION-01): generic observation and element
// references. Injected into the page's ISOLATED world; self-contained.
//
// observePage registers every element it reports in a registry kept in this
// extension's isolated-world global (page scripts and other extensions cannot
// read or write it; it dies with the document). A reference ("e12") is the
// same element for the document's life: re-observing does not rename it.
// Later injections (resolveRefTarget, dispatchRefLink) resolve a reference
// through the registry's OWN helpers, so a reference is checked against the
// live element with the very rules that described it.
//
// Resolution never guesses. The registered element itself, still connected,
// still visible, still described the same way, is the target. If the page
// replaced it (frameworks re-render), exactly ONE live element with the same
// description is accepted as its replacement; zero is stale, two or more is
// ambiguous -- never "the first one". Two legitimate "Next" buttons are two
// references with different context, so the model picks one; ambiguity only
// arises on that replacement fallback, and there it fails closed.
// ---------------------------------------------------------------------------
function observePage(spec) {
  // The ONE per-document identity (FINGER-STALE-DOC): minted on first use in
  // this extension's isolated world for this document, frozen, shared by
  // element references and FINGER links. Page scripts cannot read or set it.
  const DOC_KEY = "__luminaCompanionDocument_v1";
  if (!globalThis[DOC_KEY]) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    globalThis[DOC_KEY] = Object.freeze({ nonce: Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("") });
  }
  const KEY = "__luminaCompanionRegistry_v1";
  let reg = globalThis[KEY];
  if (!reg) {
    const L = { name: 120, context: 160, href: 512, value: 80, scan: 6000, shadowDepth: 8, registry: 4000 };
    const SELECTOR = "a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,"
      + "[contenteditable=''],[contenteditable='true'],[role=button],[role=link],[role=checkbox],[role=radio],"
      + "[role=switch],[role=tab],[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option],"
      + "[role=combobox],[role=textbox],[role=searchbox],[role=slider],[role=spinbutton],[role=treeitem]";
    const squash = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    const cut = (s, n) => {
      const t = squash(s);
      if (t.length <= n) return t;
      let c = t.slice(0, n - 1);
      const last = c.charCodeAt(c.length - 1);
      if (last >= 0xd800 && last <= 0xdbff) c = c.slice(0, -1);
      return `${c}…`;
    };
    const attr = (el, name) => (el.getAttribute ? el.getAttribute(name) : null);
    const tagOf = (el) => String(el.tagName || "").toLowerCase();
    const textOf = (el) => squash(typeof el.innerText === "string" ? el.innerText : el.textContent);
    // closest() that climbs out of (open) shadow roots.
    const closestAcross = (el, selector) => {
      let node = el;
      for (let i = 0; node && i < 64; i++) {
        const hit = node.closest ? node.closest(selector) : null;
        if (hit) return hit;
        const root = node.getRootNode ? node.getRootNode() : null;
        node = root && root.host ? root.host : null;
      }
      return null;
    };
    const roleOf = (el) => {
      const explicit = squash(attr(el, "role")).split(" ")[0].toLowerCase();
      if (explicit) return explicit.slice(0, 32);
      const tag = tagOf(el);
      if (tag === "a" || tag === "area") return el.hasAttribute("href") ? "link" : "generic";
      if (tag === "button" || tag === "summary") return "button";
      if (tag === "select") return el.multiple ? "listbox" : "combobox";
      if (tag === "textarea") return "textbox";
      if (tag === "input") {
        const type = squash(attr(el, "type") || "text").toLowerCase();
        return ({ checkbox: "checkbox", radio: "radio", range: "slider", number: "spinbutton", search: "searchbox",
          button: "button", submit: "button", reset: "button", image: "button", file: "button" })[type] || "textbox";
      }
      if (el.isContentEditable) return "textbox";
      return "generic";
    };
    const kindOf = (role) => (role === "link" ? "link"
      : ["textbox", "searchbox", "combobox", "listbox", "slider", "spinbutton"].includes(role) ? "field"
        : ["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"].includes(role) ? "toggle"
          : ["option", "treeitem"].includes(role) ? "option"
            : ["button", "tab", "menuitem"].includes(role) ? "button" : "other");
    const isFormField = (el) => ["input", "select", "textarea"].includes(tagOf(el));
    const nameOf = (el) => {
      let name = "";
      const ids = attr(el, "aria-labelledby");
      if (ids) {
        const root = el.getRootNode ? el.getRootNode() : document;
        name = squash(ids).split(" ").map((id) => {
          const node = root && root.getElementById ? root.getElementById(id) : document.getElementById(id);
          return node ? textOf(node) : "";
        }).join(" ");
      }
      if (!squash(name)) name = attr(el, "aria-label") || "";
      if (!squash(name) && isFormField(el)) {
        if (el.labels && el.labels.length) name = textOf(el.labels[0]);
        const type = squash(attr(el, "type")).toLowerCase();
        if (!squash(name) && ["submit", "button", "reset"].includes(type)) name = el.value || type;
        if (!squash(name) && type === "image") name = attr(el, "alt") || "";
        if (!squash(name)) name = attr(el, "placeholder") || "";
      }
      if (!squash(name) && !isFormField(el)) name = textOf(el);
      if (!squash(name) && el.querySelector) {
        const img = el.querySelector("img[alt],svg[aria-label],[aria-label]");
        if (img) name = attr(img, "alt") || attr(img, "aria-label") || "";
      }
      if (!squash(name)) name = attr(el, "title") || "";
      return cut(name, L.name);
    };
    // Credential/payment surfaces stay human: their values never leave the page.
    const sensitiveOf = (el) => {
      if (!isFormField(el) && !el.isContentEditable) return false;
      if (squash(attr(el, "type")).toLowerCase() === "password") return true;
      const hints = [attr(el, "autocomplete"), attr(el, "name"), el.id, attr(el, "aria-label"),
        attr(el, "placeholder")].join(" ");
      return /password|passwd|pwd|one-time-code|otp|cc-|cvv|cvc|card.?number|security.?code|secret|token/i.test(hints);
    };
    const valueOf = (el, kind) => {
      if (kind !== "field") return "";
      const tag = tagOf(el);
      if (tag === "select") {
        const option = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
        return option ? textOf(option) : "";
      }
      if (tag === "input" || tag === "textarea") return typeof el.value === "string" ? el.value : "";
      return el.isContentEditable ? textOf(el) : "";
    };
    const stateOf = (el, role) => {
      const out = [];
      const aria = (n) => attr(el, n);
      if (el.disabled === true || aria("aria-disabled") === "true" || closestAcross(el, "fieldset[disabled]")) {
        out.push("disabled");
      }
      if (["checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"].includes(role)) {
        const checked = aria("aria-checked");
        if (checked === "mixed" || el.indeterminate === true) out.push("mixed");
        else out.push(el.checked === true || checked === "true" ? "checked" : "unchecked");
      }
      const expanded = aria("aria-expanded");
      if (expanded === "true") out.push("expanded");
      else if (expanded === "false") out.push("collapsed");
      if (aria("aria-selected") === "true" || (tagOf(el) === "option" && el.selected === true)) out.push("selected");
      if (aria("aria-pressed") === "true") out.push("pressed");
      if (el.required === true || aria("aria-required") === "true") out.push("required");
      if (el.readOnly === true || aria("aria-readonly") === "true") out.push("readonly");
      return out;
    };
    const HEADING = "h1,h2,h3,h4,h5,h6,[role=heading]";
    const isHeading = (n) => n && n.nodeType === 1 && (/^h[1-6]$/.test(tagOf(n)) || attr(n, "role") === "heading");
    const headingBefore = (el) => {
      let node = el;
      for (let steps = 0; node && steps < 80; steps++) {
        const sib = node.previousElementSibling;
        if (sib) {
          if (isHeading(sib)) return sib;
          const inner = sib.querySelectorAll ? sib.querySelectorAll(HEADING) : [];
          if (inner.length) return inner[inner.length - 1];
          node = sib;
        } else {
          node = node.parentElement;
          if (isHeading(node)) return node;
        }
      }
      return null;
    };
    const labelOfRegion = (region) => {
      const label = attr(region, "aria-label");
      if (label) return label;
      const heading = region.querySelector ? region.querySelector("h1,h2,h3,legend,[role=heading]") : null;
      return heading ? textOf(heading) : "";
    };
    const contextOf = (el, name) => {
      const parts = [];
      const dialog = closestAcross(el, "dialog,[role=dialog],[role=alertdialog]");
      if (dialog) parts.push(`dialog "${cut(labelOfRegion(dialog), 40)}"`);
      const region = closestAcross(el, "form,[role=form],[role=search],nav,[role=navigation],header,[role=banner],"
        + "footer,[role=contentinfo],aside,[role=complementary]");
      if (region) {
        const kind = attr(region, "role") || tagOf(region);
        const label = cut(labelOfRegion(region), 40);
        parts.push(label ? `${kind} "${label}"` : kind);
      }
      // A repeated control ("Reply" under every comment) is told apart by the
      // item it belongs to.
      const item = closestAcross(el, "article,[role=article],li,[role=listitem],tr,[role=row]");
      if (item && item !== el) {
        let text = textOf(item);
        if (name && text.startsWith(name)) text = text.slice(name.length);
        text = cut(text, 70);
        if (text) parts.push(`${tagOf(item) === "tr" ? "row" : "item"} "${text}"`);
      }
      const heading = headingBefore(el);
      if (heading) parts.push(`under "${cut(textOf(heading), 50)}"`);
      return cut(parts.join(" › "), L.context);
    };
    const linkUrl = (el) => {
      const tag = tagOf(el);
      if ((tag !== "a" && tag !== "area") || !el.hasAttribute("href")) return null;
      let url;
      try { url = new URL(attr(el, "href"), document.baseURI); } catch { return null; }
      if (url.protocol !== "http:" && url.protocol !== "https:") return null;
      url.username = "";
      url.password = "";
      return url;
    };
    const visible = (el) => {
      if (!el.isConnected || !el.getBoundingClientRect) return null;
      const rect = el.getBoundingClientRect();
      if (!(rect.width >= 1 && rect.height >= 1)) return null;
      if (typeof el.checkVisibility === "function") {
        if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) {
          // Styled checkboxes/radios are commonly opacity:0 under a visible label.
          const type = squash(attr(el, "type")).toLowerCase();
          if (!(tagOf(el) === "input" && (type === "checkbox" || type === "radio"))) return null;
        }
      } else {
        const style = getComputedStyle(el);
        if (style.display === "none" || style.visibility !== "visible" || Number(style.opacity) === 0) return null;
      }
      if (closestAcross(el, "[aria-hidden=true],[inert]")) return null;
      return rect;
    };
    const collect = () => {
      const found = new Set();
      let scanned = 0;
      let capped = false;
      const walk = (root, depth) => {
        for (const el of root.querySelectorAll(SELECTOR)) {
          if (++scanned > L.scan) { capped = true; return; }
          found.add(el);
        }
        if (depth >= L.shadowDepth) return;
        for (const host of root.querySelectorAll("*")) {
          if (host.shadowRoot) {
            walk(host.shadowRoot, depth + 1);
            if (capped) return;
          }
        }
      };
      walk(document, 0);
      return { elements: [...found], capped };
    };
    const describe = (el) => {
      const role = roleOf(el);
      const name = nameOf(el);
      const url = linkUrl(el);
      return { role, name, kind: kindOf(role), href: url ? url.href : null, context: contextOf(el, name) };
    };
    // identity: what the control IS (the same registered element must still
    // say this). fingerprint: identity plus where it sits -- the stricter key
    // a REPLACEMENT element must match uniquely.
    const identity = (d, el) => [d.role, d.name, d.href || "", tagOf(el)].join("\u0001");
    const fingerprint = (d, el) => `${identity(d, el)}\u0001${d.context}`;
    // The registry carries this document's identity. Chrome 155 was observed
    // (real-Chrome evidence, Slice 1) running a script targeted at a stale
    // documentId in the tab's NEW document after a cross-document navigation;
    // refs restart at e1 per document, so a ref is only ever resolved against
    // the document identity the worker was shown.
    reg = {
      nonce: globalThis[DOC_KEY].nonce,
      gen: 0, next: 1, byEl: new WeakMap(), byRef: new Map(), limits: L,
      cut, attr, tagOf, textOf, visible, collect, describe, identity, fingerprint, nameOf, linkUrl, sensitiveOf,
      valueOf, stateOf, HEADING,
      // The ONE resolution rule (see the comment above observePage).
      resolve(ref) {
        const entry = reg.byRef.get(ref);
        if (!entry) return { status: "not_found" };
        const el = entry.w.deref();
        if (el && el.isConnected) {
          const d = describe(el);
          if (identity(d, el) !== entry.id) return { status: "changed" };
          if (!visible(el)) return { status: "not_visible" };
          return { status: "ok", el, d };
        }
        let match = null;
        let count = 0;
        for (const candidate of collect().elements) {
          if (!visible(candidate)) continue;
          const d = describe(candidate);
          if (fingerprint(d, candidate) !== entry.fp) continue;
          count += 1;
          match = { el: candidate, d };
          if (count > 1) return { status: "ambiguous" };
        }
        if (!match) return { status: "stale" };
        entry.w = new WeakRef(match.el);
        reg.byEl.set(match.el, ref);
        return { status: "ok", el: match.el, d: match.d };
      },
    };
    globalThis[KEY] = reg;
  }

  // Optional scroll first (viewport only). The window if it scrolls, else the
  // largest visible scrollable region (inbox-style apps scroll an inner pane).
  const scroller = (() => {
    const docEl = document.documentElement;
    if (docEl && docEl.scrollHeight > innerHeight + 10) return null; // the window
    let best = null;
    let bestArea = 0;
    let scanned = 0;
    for (const el of document.querySelectorAll("*")) {
      if (++scanned > reg.limits.scan) break;
      if (!(el.scrollHeight > el.clientHeight + 10)) continue;
      const overflow = getComputedStyle(el).overflowY;
      if (overflow !== "auto" && overflow !== "scroll") continue;
      const rect = reg.visible(el);
      if (!rect) continue;
      const area = rect.width * rect.height;
      if (area > bestArea) { best = el; bestArea = area; }
    }
    return best;
  })();
  const metrics = () => (scroller
    ? { scroll_y: Math.max(0, Math.round(scroller.scrollTop)), scroll_height: Math.round(scroller.scrollHeight),
      viewport_height: Math.round(scroller.clientHeight) }
    : { scroll_y: Math.max(0, Math.round(scrollY)),
      scroll_height: Math.round(document.documentElement ? document.documentElement.scrollHeight : 0),
      viewport_height: Math.round(innerHeight) });
  let scrolled = false;
  if (spec.scroll) {
    const before = metrics().scroll_y;
    const step = Math.round(metrics().viewport_height * 0.85);
    const to = { down: before + step, up: Math.max(0, before - step), top: 0, bottom: metrics().scroll_height }[spec.scroll];
    if (scroller) scroller.scrollTop = to;
    else scrollTo(0, to);
    scrolled = metrics().scroll_y !== before;
  }

  reg.gen += 1;
  const gen = reg.gen;
  const { elements: all } = reg.collect();
  const query = typeof spec.query === "string" ? spec.query.toLowerCase() : null;
  const candidates = [];
  for (const el of all) {
    const rect = reg.visible(el);
    if (!rect) continue;
    const known = reg.byEl.get(el);
    if (known && reg.byRef.has(known)) reg.byRef.get(known).lastSeen = gen;
    candidates.push({ el, rect, inView: rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth });
  }
  // In view first (reading order), then below the fold nearest first, then above.
  const rank = (c) => (c.inView ? [0, c.rect.top, c.rect.left]
    : c.rect.top >= innerHeight ? [1, c.rect.top, c.rect.left] : [2, -c.rect.bottom, c.rect.left]);
  candidates.sort((a, b) => {
    const x = rank(a);
    const y = rank(b);
    return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
  });
  const out = [];
  let total = 0;
  for (const c of candidates) {
    let ref = reg.byEl.get(c.el);
    let entry = ref ? reg.byRef.get(ref) : null;
    if (spec.since_gen != null && entry && entry.firstGen <= spec.since_gen) continue; // delta: only new
    if (query) {
      const link = reg.linkUrl(c.el);
      if (!(reg.nameOf(c.el).toLowerCase().includes(query) || (link && link.href.toLowerCase().includes(query)))) continue;
    }
    total += 1;
    if (out.length >= spec.max_items) continue;
    const d = reg.describe(c.el);
    const id = reg.identity(d, c.el);
    const fp = reg.fingerprint(d, c.el);
    if (!entry) {
      ref = `e${reg.next++}`;
      entry = { w: new WeakRef(c.el), id, fp, firstGen: gen, lastSeen: gen };
      reg.byRef.set(ref, entry);
      reg.byEl.set(c.el, ref);
    } else {
      // The element is the reference. What it now says is what Lumina was
      // just shown, so resolution checks against this description from here on.
      entry.id = id;
      entry.fp = fp;
      entry.lastSeen = gen;
    }
    const sensitive = reg.sensitiveOf(c.el);
    const value = sensitive ? "" : reg.valueOf(c.el, d.kind);
    const state = reg.stateOf(c.el, d.role);
    if (sensitive && reg.valueOf(c.el, "field")) state.push("has_value");
    const url = d.href ? new URL(d.href) : null;
    out.push({ ref, role: d.role, name: d.name, kind: d.kind, context: d.context,
      href: url ? reg.cut(url.href, reg.limits.href) : null, same_origin: url ? url.origin === location.origin : null,
      state, value: value ? reg.cut(value, reg.limits.value) : null, sensitive, in_viewport: c.inView });
  }
  const removed = [];
  if (spec.since_gen != null) {
    for (const [ref, entry] of reg.byRef) {
      if (entry.lastSeen >= spec.since_gen && entry.lastSeen < gen && removed.length < 120) removed.push(ref);
    }
  }
  if (reg.byRef.size > reg.limits.registry) {
    for (const [ref, entry] of reg.byRef) {
      const el = entry.w.deref();
      if (!el || !el.isConnected) reg.byRef.delete(ref);
    }
  }
  const headings = [];
  for (const h of document.querySelectorAll(reg.HEADING)) {
    if (headings.length >= 12) break;
    if (!reg.visible(h)) continue;
    const text = reg.cut(reg.textOf(h), reg.limits.name);
    if (!text) continue;
    const level = /^h[1-6]$/.test(reg.tagOf(h)) ? Number(reg.tagOf(h)[1])
      : Math.min(6, Math.max(1, Number(reg.attr(h, "aria-level")) || 2));
    headings.push({ level, text });
  }
  let dialog = null;
  for (const d of document.querySelectorAll("dialog[open],[role=dialog],[role=alertdialog]")) {
    if (!reg.visible(d)) continue;
    const label = reg.attr(d, "aria-label") || (d.querySelector("h1,h2,h3,[role=heading]")
      ? reg.textOf(d.querySelector("h1,h2,h3,[role=heading]")) : "");
    dialog = reg.cut(label || "dialog", reg.limits.name);
  }
  return { href: location.href, title: reg.cut(document.title || "", 300), gen, elements: out,
    total_elements: total, removed, headings, viewport: metrics(), dialog, scrolled, document_nonce: reg.nonce };
}

// Resolve ONE delivered reference to the live link it names (read-only).
function resolveRefTarget(claim) {
  const out = { href: location.href, status: "not_found", resolved_href: null };
  const reg = globalThis.__luminaCompanionRegistry_v1;
  if (!reg || typeof reg.resolve !== "function") return out;
  if (reg.nonce !== claim.document_nonce) {
    out.status = "other_document";
    return out;
  }
  const hit = reg.resolve(claim.ref);
  out.status = hit.status;
  if (hit.status !== "ok") return out;
  const url = reg.linkUrl(hit.el);
  if (!url) {
    out.status = "not_a_link";
    return out;
  }
  out.resolved_href = url.href;
  return out;
}

// Fixed extension-owned actuator for chrome_navigate(kind=ref). Chrome
// dispatches it only into the source document; it resolves the reference
// again by the same rule and navigates only if the live link still points
// exactly where the checked destination does. Never runs page-supplied code.
function dispatchRefLink(claim) {
  if (location.href !== claim.source_url) return { status: "refused" };
  const reg = globalThis.__luminaCompanionRegistry_v1;
  if (!reg || typeof reg.resolve !== "function" || reg.nonce !== claim.document_nonce) return { status: "refused" };
  const hit = reg.resolve(claim.ref);
  if (hit.status !== "ok") return { status: "refused" };
  const url = reg.linkUrl(hit.el);
  if (!url || url.href !== claim.href) return { status: "refused" };
  location.assign(url.href);
  return { status: "dispatched" };
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
    // Targeted by document, not frame. Chrome refuses a stale id after a
    // same-URL reload ("No document with id ..."), but Chrome 155 was observed
    // (BROWSER-COMPANION-COMPLETION-01 real-Chrome evidence) running a script
    // targeted at a stale id in the tab's NEW document after a cross-document
    // navigation. So the target alone proves nothing: the injection's own
    // documentId and the after-check below are what bind the read.
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

// The last moment confirmation polling may run: the request's deadline minus
// an answer margin, so the reply still reaches the hub (HARDENING-01).
function settleDeadline(req) {
  return req.deadline_ms - LIMITS.SETTLE_ANSWER_MARGIN_MS;
}

async function settleCreatedTab(tabId, req, s, navEpoch, grantEpoch, live) {
  // tabs.create can answer with an ID before Chrome publishes its URL in
  // tabs.get, and a real page can take seconds to commit and load. Observe
  // that same tab until it shows a readable URL AND has finished loading, or
  // until settleDeadline -- then report what was seen (a readable URL still
  // loading is a local effect with load_confirmed false). OBSERVATION ONLY:
  // never a second create or navigation. A control change ends it at once.
  let seen = null;
  for (let attempt = 0; attempt < LIMITS.NAVIGATE_SETTLE_POLLS; attempt++) {
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch) return null;
    if (Date.now() >= settleDeadline(req)) return seen;
    const tab = await safeActionTab(tabId);
    if (tab) {
      seen = tab;
      if (tab.status === "complete") return tab;
    }
    await new Promise((resolve) => setTimeout(resolve, LIMITS.SETTLE_POLL_MS));
  }
  return seen;
}

// Stage 2 (FINGER): observe the tab we just navigated until the destination
// is actually there AND is a genuinely NEW document. Same bounded-observation
// discipline as settleCreatedTab -- never a second navigation, never a retry
// against a different destination. A same-URL reload does not prove
// navigation happened: only a different documentId does.
async function settleNavigatedTab(tabId, expectedWindow, req, s, navEpoch, grantEpoch,
                                  priorDocumentId, expectedUrl, live) {
  // Bounded by settleDeadline, not a fixed 2.5 s (HARDENING-01): a slow real
  // page is confirmed rather than reported ambiguous. Observation only.
  for (let attempt = 0; attempt < LIMITS.NAVIGATE_SETTLE_POLLS; attempt++) {
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch || Date.now() >= settleDeadline(req)) return null;
    const tab = await safeActionTab(tabId, expectedWindow);
    if (tab && tab.url === expectedUrl && tab.status === "complete") {
      const doc = await mainDocument(tabId);
      const finalTab = doc ? await safeActionTab(tabId, expectedWindow) : null;
      if (finalTab && finalTab.url === expectedUrl && finalTab.status === "complete"
        && doc.url === expectedUrl && doc.documentId && doc.documentId !== priorDocumentId) {
        return { tab: finalTab, doc };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, LIMITS.SETTLE_POLL_MS));
  }
  return null;
}

// ---------------------------------------------------------------------------
// Slice 1 helpers (generic observation and navigation)
// ---------------------------------------------------------------------------

// Re-check, field by field, everything the page function returned. Page-side
// code ran in an isolated world, but it read page-controlled DOM, so its
// output is bounded and typed again here before it can cross the bridge.
function sanitizeObservation(page, maxItems) {
  const bounded = (value, limit) => (typeof value === "string" ? safeSlice(value, limit) : "");
  if (!isPlainObject(page) || !Number.isSafeInteger(page.gen) || page.gen < 1 || !Array.isArray(page.elements)
    || !Number.isSafeInteger(page.total_elements) || !Array.isArray(page.removed) || !Array.isArray(page.headings)
    || !isPlainObject(page.viewport)) return null;
  const elements = [];
  const seen = new Set();
  for (const e of page.elements.slice(0, maxItems)) {
    if (!isPlainObject(e) || typeof e.ref !== "string" || !REF_RE.test(e.ref) || seen.has(e.ref)) continue;
    if (!ELEMENT_KINDS.has(e.kind)) continue;
    seen.add(e.ref);
    const href = typeof e.href === "string" && /^https?:\/\//i.test(e.href) && e.href.length <= LIMITS.MAX_ELEMENT_HREF_CHARS
      ? e.href : null;
    const sensitive = e.sensitive === true;
    const state = Array.isArray(e.state) ? [...new Set(e.state.filter((x) => ELEMENT_STATES.has(x)))] : [];
    elements.push({ ref: e.ref, role: bounded(e.role, LIMITS.MAX_ROLE_CHARS), name: bounded(e.name, LIMITS.MAX_ELEMENT_NAME_CHARS),
      kind: e.kind, context: bounded(e.context, LIMITS.MAX_ELEMENT_CONTEXT_CHARS), href,
      same_origin: href === null ? null : e.same_origin === true, state,
      // A credential/payment field's value never crosses the bridge.
      value: sensitive || typeof e.value !== "string" ? null : safeSlice(e.value, LIMITS.MAX_ELEMENT_VALUE_CHARS),
      sensitive, in_viewport: e.in_viewport === true });
  }
  const removed = page.removed.filter((r) => typeof r === "string" && REF_RE.test(r)).slice(0, LIMITS.MAX_OBSERVE_ITEMS);
  const headings = [];
  for (const h of page.headings.slice(0, LIMITS.MAX_HEADINGS)) {
    if (!isPlainObject(h) || !Number.isSafeInteger(h.level) || h.level < 1 || h.level > 6) continue;
    headings.push({ level: h.level, text: bounded(h.text, LIMITS.MAX_ELEMENT_NAME_CHARS) });
  }
  const metric = (v) => (Number.isSafeInteger(v) && v >= 0 ? v : 0);
  return { gen: page.gen, title: bounded(page.title, LIMITS.MAX_TITLE_CHARS), elements,
    total_elements: Math.max(page.total_elements, elements.length), removed, headings,
    viewport: { scroll_y: metric(page.viewport.scroll_y), scroll_height: metric(page.viewport.scroll_height),
      viewport_height: metric(page.viewport.viewport_height) },
    dialog: typeof page.dialog === "string" ? safeSlice(page.dialog, LIMITS.MAX_ELEMENT_NAME_CHARS) : null,
    scrolled: page.scrolled === true };
}

// Generic navigation's own gate (Slice 1). Like navigationVerdict, but a
// fragment is allowed (the page moved to, not the fragment, is what is
// checked), and a conventional account-action path is refused.
function genericNavigationVerdict(s, url, live) {
  if (!live() || !s.navigationAllowed) throw new CompanionFailure("navigation_not_allowed");
  let parsed;
  try { parsed = new URL(url); } catch { throw new CompanionFailure("invalid_args"); }
  if (parsed.username || parsed.password || parsed.href.length > LIMITS.MAX_TAB_URL_CHARS) {
    throw new CompanionFailure("invalid_args");
  }
  const verdict = LuminaPolicy.classifyUrl(parsed.href);
  if (!verdict.readable) throw new CompanionFailure("restricted_surface");
  if (blockedSites.has(verdict.pattern) || revocationsPending) throw new CompanionFailure("companion_revoked");
  if (LuminaPolicy.sensitiveNavigation(parsed.href)) throw new CompanionFailure("sensitive_navigation");
  return verdict;
}

// Mission scope, enforced HERE as well as in the hub: a destination is in
// scope if the owner named its site in this turn (scope, from the hub's
// owner-ingress mission) or granted it in this popup (Chrome's grant AND not
// companion-revoked -- siteAccess). Nothing a page says can add to either.
async function inMissionScope(verdict, scope) {
  return scope.includes(verdict.pattern) || (await siteAccess(verdict)) === "granted";
}

function withoutHash(url) {
  try {
    const parsed = new URL(url);
    parsed.hash = "";
    return parsed.href;
  } catch {
    return null;
  }
}

// Observe a tab after a generic navigation until it shows a NEW document,
// fully loaded. Unlike settleNavigatedTab, a redirect is fine: the receipt
// reports the URL actually read back, never the one requested. A landing on
// a restricted or companion-revoked surface ends observation at once and is
// reported without a URL. Bounded; never a second navigation.
// Back/forward alone may supply request-local evidence of a same-document
// traversal; URL and ref navigation still require a replacement document.
async function settleNewDocument(tabId, expectedWindow, req, s, navEpoch, grantEpoch, priorDocumentId, live,
  history = null) {
  for (let attempt = 0; attempt < LIMITS.NAVIGATE_SETTLE_POLLS; attempt++) {
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch || Date.now() >= settleDeadline(req)) return null;
    let raw;
    try { raw = await chrome.tabs.get(tabId); } catch { return null; }
    if (!raw || raw.incognito || (expectedWindow !== null && raw.windowId !== expectedWindow)) return null;
    if (raw.status === "complete" && typeof raw.url === "string" && raw.url && raw.url !== "about:blank") {
      const verdict = LuminaPolicy.classifyUrl(raw.url);
      if (!verdict.readable || blockedSites.has(verdict.pattern)) return { restricted: true, tab: raw };
      const doc = await mainDocument(tabId);
      const again = doc ? await safeActionTab(tabId, expectedWindow) : null;
      if (doc && again && again.status === "complete" && again.url === raw.url && doc.url === raw.url
        && (doc.documentId !== priorDocumentId || (history && history.url === again.url))) {
        return { tab: again, doc, verdict };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, LIMITS.SETTLE_POLL_MS));
  }
  return null;
}

// The single receipt builder for chrome_navigate, after dispatch.
function navigateReceipt(req, s, navEpoch, grantEpoch, live, settled, fallbackTab, targetPattern) {
  const base = { navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern };
  if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch || grantRevocations !== grantEpoch
    || !settled || settled.restricted) {
    // Tab identity only: never a URL that was not verified (or is restricted).
    return { result: actionReceipt(req, "ambiguous_after_dispatch", fallbackTab), ...base };
  }
  return { result: actionReceipt(req, "browser_local_effect_observed", settled.tab, settled.tab.url, true),
    tab_id: settled.tab.id,
    observed: { url: settled.tab.url, origin: settled.verdict.origin, document_id: settled.doc.documentId },
    ...base, targetPattern: settled.verdict.pattern };
}

// The tab and EXACT document a bound navigation starts from.
async function boundSource(req) {
  const tab = await safeActionTab(req.tab_id, req.args.window_id);
  const doc = tab ? await mainDocument(req.tab_id) : null;
  if (!tab || !doc || doc.documentId !== req.args.document_id || doc.url !== tab.url) {
    throw new CompanionFailure("stale_document", req.tab_id);
  }
  return { tab, doc };
}

const NAVIGATE = Object.freeze({
  async url(req, s, live) {
    const verdict = genericNavigationVerdict(s, req.args.url, live);
    if (!(await inMissionScope(verdict, req.args.scope))) throw new CompanionFailure("outside_mission_scope", req.tab_id);
    let priorDocumentId = null;
    if (req.tab_id !== null) priorDocumentId = (await boundSource(req)).doc.documentId;
    const navEpoch = s.navigationEpoch;
    const grantEpoch = grantRevocations;
    if (!live() || !s.navigationAllowed || !grantHeld(grantEpoch) || blockedSites.has(verdict.pattern)
      || Date.now() > req.deadline_ms) throw new CompanionFailure("navigation_not_allowed", req.tab_id);
    // Dispatch boundary: from here on, a failure or lost answer is ambiguous.
    req._actionDispatched = true;
    let tab;
    try {
      tab = req.tab_id === null ? await chrome.tabs.create({ url: req.args.url, active: true })
        : await chrome.tabs.update(req.tab_id, { url: req.args.url });
    } catch {
      return navigateReceipt(req, s, navEpoch, grantEpoch, live, null, null, verdict.pattern);
    }
    if (!tab || !isTabId(tab.id)) return navigateReceipt(req, s, navEpoch, grantEpoch, live, null, null, verdict.pattern);
    const settled = await settleNewDocument(tab.id, req.tab_id === null ? null : req.args.window_id, req, s, navEpoch,
      grantEpoch, priorDocumentId, live);
    return navigateReceipt(req, s, navEpoch, grantEpoch, live, settled, tab, verdict.pattern);
  },

  // Generalized Stage 2: the model names an element reference it was shown on
  // this exact document; the live document supplies the destination.
  async ref(req, s, live) {
    const delivered = s.deliveredRefs.get(req.tab_id);
    if (delivered && delivered.documentId !== req.args.document_id) throw new CompanionFailure("stale_document", req.tab_id);
    if (!delivered || !delivered.refs.has(req.args.ref)) throw new CompanionFailure("target_not_found", req.tab_id);
    if (!(await safeActionTab(req.tab_id, req.args.window_id))) throw new CompanionFailure("navigated_during_request", req.tab_id);
    const { tabId, page, observed, grantEpoch } = await readPage(req, resolveRefTarget,
      { ref: req.args.ref, document_nonce: delivered.nonce }, live);
    if (tabId !== req.tab_id) throw new CompanionFailure("navigated_during_request", tabId);
    if (observed.document_id !== req.args.document_id) throw new CompanionFailure("stale_document", tabId);
    const refusal = { not_found: "target_not_found", stale: "stale_target", ambiguous: "target_ambiguous",
      changed: "target_changed", not_visible: "not_interactable", not_a_link: "not_a_link",
      other_document: "stale_document" }[page.status];
    if (page.status !== "ok") throw new CompanionFailure(refusal || "target_not_found", tabId);
    const destination = page.resolved_href;
    if (typeof destination !== "string") throw new CompanionFailure("target_not_found", tabId);
    if (withoutHash(destination) === withoutHash(observed.url)) throw new CompanionFailure("same_document_link", tabId);
    const verdict = genericNavigationVerdict(s, destination, live);
    if (!(await inMissionScope(verdict, req.args.scope))) throw new CompanionFailure("outside_mission_scope", tabId);
    const navEpoch = s.navigationEpoch;
    // tabs.update cannot bind to a source document: recheck tab, window and
    // document, then dispatch fixed code through documentIds (as FINGER does).
    const current = await safeActionTab(tabId, req.args.window_id);
    const source = current ? await mainDocument(tabId) : null;
    if (!current || current.url !== observed.url || !source || source.documentId !== observed.document_id
      || source.url !== observed.url) throw new CompanionFailure("stale_document", tabId);
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch || !grantHeld(grantEpoch)
      || blockedSites.has(verdict.pattern) || Date.now() > req.deadline_ms) {
      throw new CompanionFailure("navigation_not_allowed", tabId);
    }
    req._actionDispatched = true;
    let injection;
    try {
      [injection] = await chrome.scripting.executeScript({
        target: { tabId, documentIds: [observed.document_id] }, world: "ISOLATED", func: dispatchRefLink,
        args: [{ source_url: observed.url, ref: req.args.ref, href: destination, document_nonce: delivered.nonce }],
      });
    } catch {
      return navigateReceipt(req, s, navEpoch, grantEpoch, live, null, null, verdict.pattern);
    }
    if (!injection || !injection.result) {
      return navigateReceipt(req, s, navEpoch, grantEpoch, live, null, null, verdict.pattern);
    }
    if (injection.result.status !== "dispatched") {
      return { error: injection.documentId === observed.document_id ? "target_changed" : "stale_document", tab_id: tabId };
    }
    if (injection.documentId !== observed.document_id) {
      return navigateReceipt(req, s, navEpoch, grantEpoch, live, null, null, verdict.pattern);
    }
    const settled = await settleNewDocument(tabId, req.args.window_id, req, s, navEpoch, grantEpoch,
      observed.document_id, live);
    return navigateReceipt(req, s, navEpoch, grantEpoch, live, settled, current, verdict.pattern);
  },

  async back(req, s, live) { return navigateHistory(req, s, live, "goBack"); },
  async forward(req, s, live) { return navigateHistory(req, s, live, "goForward"); },
});

// Browser history. The destination is the tab's own history (created by the
// owner's or Lumina's earlier navigations), not anything a page supplies; it
// cannot be checked before dispatch, so the receipt reports where the tab
// actually landed, and a restricted landing is reported without a URL.
// Only back/forward may confirm within the source document. A URL change
// alone (pushState, a hash change, or a page redirect) is not evidence of a
// traversal: require Chrome's forward_back event on this exact main document,
// received after dispatch, then read the same URL back from tab AND frame.
// Request-local listeners retain no browsing history and are always removed.
function watchHistoryTraversal(req, sourceUrl) {
  const history = { url: null };
  const events = [chrome.webNavigation.onHistoryStateUpdated, chrome.webNavigation.onReferenceFragmentUpdated];
  const onTraversal = (details) => {
    if (!req._actionDispatched || details.tabId !== req.tab_id || details.frameId !== 0) return;
    history.url = null;
    if (details.documentId === req.args.document_id && details.documentLifecycle === "active"
      && Array.isArray(details.transitionQualifiers) && details.transitionQualifiers.includes("forward_back")
      && typeof details.url === "string" && details.url.length <= LIMITS.MAX_TAB_URL_CHARS
      && details.url !== sourceUrl) history.url = details.url;
  };
  history.stop = () => events.forEach((event) => event.removeListener(onTraversal));
  events.forEach((event) => event.addListener(onTraversal));
  return history;
}

async function navigateHistory(req, s, live, method) {
  if (!live() || !s.navigationAllowed) throw new CompanionFailure("navigation_not_allowed", req.tab_id);
  const { tab, doc } = await boundSource(req);
  const sourcePattern = LuminaPolicy.classifyUrl(tab.url).pattern;
  const navEpoch = s.navigationEpoch;
  const grantEpoch = grantRevocations;
  if (!grantHeld(grantEpoch) || revocationsPending || Date.now() > req.deadline_ms) {
    throw new CompanionFailure("navigation_not_allowed", req.tab_id);
  }
  const history = watchHistoryTraversal(req, tab.url);
  try {
    req._actionDispatched = true;
    try {
      await chrome.tabs[method](req.tab_id);
    } catch {
      // A same-document traversal may already have happened before an API
      // error. Only an unchanged source with no traversal evidence is refused.
      const now = await mainDocument(req.tab_id);
      if (now && now.documentId === doc.documentId && now.url === tab.url && !history.url) {
        req._actionDispatched = false;
        throw new CompanionFailure("no_history_entry", req.tab_id);
      }
      return navigateReceipt(req, s, navEpoch, grantEpoch, live, null, tab, sourcePattern);
    }
    const settled = await settleNewDocument(req.tab_id, req.args.window_id, req, s, navEpoch, grantEpoch,
      doc.documentId, live, history);
    return navigateReceipt(req, s, navEpoch, grantEpoch, live, settled, tab, sourcePattern);
  } finally {
    history.stop();
  }
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
    if (!Array.isArray(page.links) || !Number.isSafeInteger(page.total_links)
      || typeof page.document_nonce !== "string" || !HEX32.test(page.document_nonce)) {
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
      tab_id: tabId, observed, host, grantEpoch, documentNonce: page.document_nonce,
      truncated: Boolean(page.truncated) || links.length < page.links.length,
    };
  },

  // Slice 1: generic observation. A read like extract_text -- same document
  // binding, grant, PAUSE and revoke gates (readPage) -- whose optional
  // scroll moves only that document's viewport.
  async observe(req, live) {
    const spec = { max_items: req.args.max_items ?? LIMITS.DEFAULT_OBSERVE_ITEMS, query: req.args.query ?? null,
      since_gen: req.args.since_gen ?? null, scroll: req.args.scroll ?? null };
    const { tabId, page, host, observed, grantEpoch } = await readPage(req, observePage, spec, live);
    const result = sanitizeObservation(page, spec.max_items);
    if (!result || typeof page.document_nonce !== "string" || !HEX32.test(page.document_nonce)) {
      throw new CompanionFailure("injection_failed", tabId);
    }
    return { result, tab_id: tabId, observed, host, grantEpoch, documentNonce: page.document_nonce,
      truncated: result.total_elements > result.elements.length };
  },

  // Slice 1: generic navigation inside an owner mission.
  async navigate(req, live) {
    return NAVIGATE[req.args.kind](req, session, live);
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

  // Stage 2 (FINGER). Owner-authorized origin + current eligible document +
  // uniquely identified semantic link = ONE bounded navigation.
  async follow_link(req, live) {
    const s = session;
    const before = await safeActionTab(req.tab_id, req.args.window_id);
    if (!before) throw new CompanionFailure("navigated_during_request", req.tab_id);
    const delivered = s.deliveredLinks.get(req.tab_id);
    if (delivered && delivered.documentId !== req.args.document_id) {
      throw new CompanionFailure("stale_document", req.tab_id);
    }
    if (!delivered || !delivered.pairs.has(JSON.stringify([req.args.text, req.args.href]))) {
      throw new CompanionFailure("target_not_found", req.tab_id);
    }
    if (Date.now() > req.deadline_ms) throw new CompanionFailure("deadline_expired", req.tab_id);
    // The destination is NEVER the tool argument. readPage re-reads the live
    // document under the same grant / document-identity / PAUSE / revoke
    // machinery as every other read, and hands back what THIS document says.
    const { tabId, page, observed, grantEpoch } = await readPage(
      req, resolveLinkTarget, { href: req.args.href, text: req.args.text, document_nonce: delivered.nonce }, live);
    if (tabId !== req.tab_id) throw new CompanionFailure("navigated_during_request", tabId);
    // Lumina must be acting on the document she actually read -- by Chrome's
    // documentId AND by the extension's own per-document identity.
    if (observed.document_id !== req.args.document_id || page.other_document === true) {
      throw new CompanionFailure("stale_document", tabId);
    }
    if (!Number.isSafeInteger(page.matches) || page.matches < 1) {
      throw new CompanionFailure("target_not_found", tabId);
    }
    if (page.matches !== 1) throw new CompanionFailure("target_ambiguous", tabId);
    const destination = page.resolved_href;
    if (typeof destination !== "string" || destination !== req.args.href) {
      throw new CompanionFailure("target_changed", tabId);
    }
    // Same-origin only. Stage 2 is a finger inside one authorized origin,
    // not a way to be walked somewhere new.
    if (page.same_origin !== true) throw new CompanionFailure("cross_origin_target", tabId);
    const verdict = navigationVerdict(s, destination, live);
    if (verdict.origin !== observed.origin) throw new CompanionFailure("cross_origin_target", tabId);
    const navEpoch = s.navigationEpoch;
    if (Date.now() > req.deadline_ms) throw new CompanionFailure("deadline_expired", tabId);
    // tabs.update cannot bind to a source document. Recheck tab, requested
    // window and document, then dispatch fixed code through documentIds.
    const current = await safeActionTab(tabId, req.args.window_id);
    const source = current ? await mainDocument(tabId) : null;
    const dispatchTab = source ? await safeActionTab(tabId, req.args.window_id) : null;
    if (!current || current.url !== observed.url || !dispatchTab
      || dispatchTab.url !== observed.url || !source
      || source.documentId !== observed.document_id || source.url !== observed.url) {
      throw new CompanionFailure("stale_document", tabId);
    }
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || !grantHeld(grantEpoch) || blockedSites.has(verdict.pattern)
      || Date.now() > req.deadline_ms) throw new CompanionFailure("navigation_not_allowed", tabId);
    let injection;
    req._actionDispatched = true;
    try {
      [injection] = await chrome.scripting.executeScript({
        target: { tabId, documentIds: [observed.document_id] },
        world: "ISOLATED", func: dispatchVerifiedLink,
        args: [{ source_url: observed.url, text: req.args.text, href: req.args.href, document_nonce: delivered.nonce }],
      });
    } catch {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    if (!injection || !injection.result) {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    // The fixed actuator refused: it navigated nothing, in whichever document
    // Chrome ran it (FINGER-STALE-DOC). Anything else in the wrong document
    // stays ambiguous, never verified.
    if (injection.result.status !== "dispatched") {
      return { error: injection.documentId === observed.document_id ? "target_changed" : "stale_document", tab_id: tabId };
    }
    if (injection.documentId !== observed.document_id) {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    const settled = await settleNavigatedTab(tabId, req.args.window_id, req, s, navEpoch, grantEpoch,
      observed.document_id, destination, live);
    if (!live() || !s.navigationAllowed || s.navigationEpoch !== navEpoch
      || grantRevocations !== grantEpoch || blockedSites.has(verdict.pattern) || !settled) {
      return { result: actionReceipt(req, "ambiguous_after_dispatch"),
        navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
    }
    return { result: actionReceipt(req, "browser_local_effect_observed", settled.tab, settled.tab.url, true),
      tab_id: tabId,
      // The verified RESULTING document, so the receipt can say what happened
      // rather than only what was asked.
      observed: { url: settled.tab.url, origin: verdict.origin, document_id: settled.doc.documentId },
      navigationEpoch: navEpoch, actionGrantEpoch: grantEpoch, targetPattern: verdict.pattern };
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
