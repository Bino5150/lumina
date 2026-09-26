// BROWSER-COMPANION-01A -- extension service worker behaviour tests.
// Runs the REAL chrome_companion/extension/worker.js (+ policy.js via its own
// importScripts) in a Node VM against a mocked chrome.* surface. The page-side
// extraction functions the worker injects are executed for real against a
// fake DOM, exactly as Chrome serialises and runs them.
// Run: node tests/chrome_companion_js/worker_test.js   (exit 0 = all passed)
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const EXT_DIR = path.resolve(__dirname, "../../chrome_companion/extension");
const WORKER_SRC = fs.readFileSync(path.join(EXT_DIR, "worker.js"), "utf8");
const POLICY_SRC = fs.readFileSync(path.join(EXT_DIR, "policy.js"), "utf8");
const EXT_ID = "abcdefghijklmnopabcdefghijklmnop";
const CID = "c".repeat(32);
const CID2 = "d".repeat(32);
const tick = () => new Promise((resolve) => setImmediate(resolve));
// Objects built inside the VM realm have that realm's prototypes; compare data only.
const plain = (value) => JSON.parse(JSON.stringify(value));
async function settle(n = 8) {
  for (let i = 0; i < n; i++) await tick();
}
let seq = 0;
// Request ids carry the hub's per-connection sequence number in their first
// 12 hex digits (R2 / P1); the harness numbers requests the same way.
const ridFor = (n) => n.toString(16).padStart(12, "0") + "a".repeat(20);
const rid = () => ridFor(++seq);
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const TABS = () => [
  { id: 7, windowId: 1, active: true, incognito: false, status: "complete",
    url: "https://www.reddit.com/r/AgentsInteractive/", title: "AgentsInteractive" },
  { id: 8, windowId: 1, active: false, incognito: false, status: "complete",
    url: "chrome://settings/passwords", title: "Settings - Passwords" },
  { id: 9, windowId: 2, active: false, incognito: true, status: "complete",
    url: "https://private.test/", title: "Private" },
  { id: 10, windowId: 1, active: false, incognito: false, status: "complete",
    url: "https://github.com/", title: "GitHub" },
  { id: 11, windowId: 1, active: false, incognito: false, status: "complete",
    url: "https://passwords.google.com/", title: "Google Password Manager" },
];
const PAGES = () => ({
  7: { href: "https://www.reddit.com/r/AgentsInteractive/", title: "AgentsInteractive",
    text: "Hello   AgentsInteractive \n\n\n\n second line\t\ttabs",
    anchors: [
      { href: "/r/AgentsInteractive/comments/1", text: "  First   post " },
      { href: "https://www.reddit.com/r/AgentsInteractive/comments/1", text: "dupe" },
      { href: "javascript:alert(1)", text: "js" },
      { href: "mailto:someone@example.test", text: "mail" },
      { href: "https://user:secret@example.test/a", text: "creds" },
      { href: "https://other.test/x", text: "", ariaLabel: "labelled" },
      { href: "data:text/html,hi", text: "data" },
    ] },
  10: { href: "https://github.com/", title: "GitHub", text: "GitHub home", anchors: [] },
});

function runInPage(func, args, page) {
  const anchors = (page.anchors || []).map((a) => ({
    getAttribute: (name) => (name === "href" ? a.href : name === "aria-label" ? a.ariaLabel ?? null
      : name === "title" ? a.title ?? null : null),
    innerText: a.text ?? "",
  }));
  const document = {
    body: { innerText: page.text ?? "" },
    documentElement: { textContent: page.text ?? "" },
    title: page.title ?? "",
    baseURI: page.href,
    querySelectorAll: () => anchors,
  };
  const location = { href: page.locationHref || page.href, origin: new URL(page.href).origin };
  return vm.runInNewContext(`(${func.toString()}).apply(null, args)`, { document, location, URL, args });
}

function makeEnv(opts = {}) {
  const env = {
    storage: opts.storage || {},
    ports: [],
    alarms: [],
    timers: [],
    listeners: { message: [], alarm: [], startup: [], installed: [], permissionRemoved: [] },
    tabs: opts.tabs || TABS(),
    granted: new Set(opts.granted || ["https://www.reddit.com/*"]),
    pages: opts.pages || PAGES(),
    injections: [],
    queries: 0,
    storageGetGate: opts.storageGetGate || null,
    storageSetHook: null,
    connectThrows: false,
    tabsGetHook: null,
    createHook: null,
    updateHook: null,
    createCalls: [],
    updateCalls: [],
    injectionHook: null,
    // Chrome's per-document identity (webNavigation documentId): a tab keeps
    // its id across navigations, a document does not. Replace an entry to
    // model a new document in that tab (e.g. a same-URL reload).
    documents: {},
    frameHook: null,
    frameCalls: [],
    containsHook: null,
    // R3: chrome.permissions.remove. Chrome applies the removal at once (what
    // contains() answers changes synchronously) and dispatches onRemoved only
    // later, after an asynchronous network-service update; remove() resolves
    // after that. removeHook can hold or fail the call. With holdEvents set,
    // Chrome's permission events are queued until releaseEvents() -- the
    // delayed-onRemoved schedule of Goblin's AR3.
    removeHook: null,
    removeOverride: null,
    removeCalls: [],
    holdEvents: false,
    heldEvents: [],
  };
  const docOf = (tabId) => env.documents[tabId] ?? `doc-${tabId}`;
  const chrome = {
    runtime: {
      id: EXT_ID,
      lastError: undefined,
      getURL: (p) => `chrome-extension://${EXT_ID}/${p}`,
      getManifest: () => ({ version: "0.1.0" }),
      connectNative(name) {
        if (env.connectThrows) throw new Error("Specified native messaging host not found.");
        assert.equal(name, "org.lumina.chrome_companion");
        const port = {
          sent: [],
          disconnected: false,
          onMessage: { addListener(fn) { port.onMessageFn = fn; } },
          onDisconnect: { addListener(fn) { port.onDisconnectFn = fn; } },
          postMessage(message) {
            if (port.disconnected) throw new Error("Attempting to use a disconnected port object");
            port.sent.push(JSON.parse(JSON.stringify(message)));
          },
          // Like Chrome: disconnect() does NOT fire this side's own onDisconnect.
          disconnect() { port.disconnected = true; },
          deliver(message) { port.onMessageFn(message); },
          hostGone(errorMessage) {
            port.disconnected = true;
            chrome.runtime.lastError = errorMessage ? { message: errorMessage } : undefined;
            port.onDisconnectFn(port);
            chrome.runtime.lastError = undefined;
          },
        };
        env.ports.push(port);
        return port;
      },
      onStartup: { addListener: (fn) => env.listeners.startup.push(fn) },
      onInstalled: { addListener: (fn) => env.listeners.installed.push(fn) },
      onMessage: { addListener: (fn) => env.listeners.message.push(fn) },
    },
    storage: {
      local: {
        async get(keys) {
          if (env.storageGetGate) await env.storageGetGate;
          const out = {};
          for (const key of [].concat(keys)) if (key in env.storage) out[key] = env.storage[key];
          return JSON.parse(JSON.stringify(out));
        },
        async set(values) {
          if (env.storageSetHook) await env.storageSetHook(values);
          Object.assign(env.storage, JSON.parse(JSON.stringify(values)));
        },
      },
    },
    alarms: {
      async create(name, info) { env.alarms.push({ name, info }); },
      async clear(name) { env.alarms.push({ name, cleared: true }); return true; },
      onAlarm: { addListener: (fn) => env.listeners.alarm.push(fn) },
    },
    tabs: {
      async query(query) {
        env.queries += 1;
        const tabs = query.active ? env.tabs.filter((t) => t.active) : env.tabs;
        return tabs.map((t) => ({ ...t }));
      },
      async get(id) {
        if (env.tabsGetHook) {
          const hooked = await env.tabsGetHook(id);
          if (hooked !== undefined) return hooked;
        }
        const tab = env.tabs.find((t) => t.id === id);
        if (!tab) throw new Error(`No tab with id: ${id}.`);
        return { ...tab };
      },
      async create(details) {
        env.createCalls.push(details);
        if (env.createHook) {
          const hooked = await env.createHook(details);
          if (hooked !== undefined) return hooked;
        }
        const id = Math.max(...env.tabs.map((t) => t.id)) + 1;
        env.tabs.forEach((t) => { if (t.windowId === 1) t.active = false; });
        const tab = { id, windowId: 1, active: true, incognito: false,
          status: "complete", url: details.url, title: "" };
        env.tabs.push(tab);
        return { ...tab };
      },
      async update(id, details) {
        env.updateCalls.push({ id, details });
        if (env.updateHook) {
          const hooked = await env.updateHook(id, details);
          if (hooked !== undefined) return hooked;
        }
        const tab = env.tabs.find((t) => t.id === id);
        if (!tab) throw new Error("tab gone");
        if (details.active) {
          env.tabs.forEach((t) => { if (t.windowId === tab.windowId) t.active = false; });
          tab.active = true;
        }
        return { ...tab };
      },
    },
    permissions: {
      async contains({ origins }) {
        const answer = origins.every((o) => env.granted.has(o));
        if (env.containsHook) {
          const hooked = await env.containsHook(origins, answer);
          if (hooked !== undefined) return hooked;
        }
        return answer;
      },
      async remove({ origins }) {
        env.removeCalls.push(origins);
        if (env.removeHook) await env.removeHook(origins);
        // R5: replaces Chrome's removal outright -- e.g. resolve false and
        // leave the grant in place (Goblin's AR5), or throw.
        if (env.removeOverride) return env.removeOverride(origins);
        origins.forEach((o) => env.granted.delete(o));
        await tick();
        env.announceRemoved(origins);
        return true;
      },
      onRemoved: { addListener: (fn) => env.listeners.permissionRemoved.push(fn) },
    },
    webNavigation: {
      async getFrame(details) {
        env.frameCalls.push(details);
        if (env.frameHook) {
          const hooked = await env.frameHook(details, env.frameCalls.length);
          if (hooked !== undefined) return hooked;
        }
        const tab = env.tabs.find((t) => t.id === details.tabId);
        if (!tab || details.frameId !== 0) return null;
        return { documentId: docOf(tab.id), documentLifecycle: "active", frameType: "outermost_frame",
          parentFrameId: -1, errorOccurred: false, url: tab.url };
      },
    },
    scripting: {
      async executeScript(details) {
        env.injections.push(details);
        if (env.injectionHook) {
          const hooked = await env.injectionHook(details);
          if (hooked !== undefined) return hooked;
        }
        const { tabId, documentIds } = details.target;
        if (documentIds) {
          // Chromium scripting_utils.cc CollectFramesForInjection: an unknown
          // document, or one not in this tab, is refused outright.
          const tab = env.tabs.find((t) => t.id === tabId);
          if (!tab || !documentIds.includes(docOf(tabId))) {
            throw new Error(`No document with id ${documentIds[0]} in tab with id ${tabId}`);
          }
        }
        const page = env.pages[tabId];
        if (!page) {
          throw new Error('Cannot access contents of url "https://mail.google.com/mail/u/0/#secret". '
            + "Extension manifest must request permission to access this host.");
        }
        return [{ documentId: docOf(tabId), frameId: 0, result: runInPage(details.func, details.args, page) }];
      },
    },
  };
  env.chrome = chrome;
  const context = vm.createContext({
    chrome, console, URL, crypto: webcrypto,
    setTimeout: (fn, ms) => { env.timers.push({ fn, ms, done: false }); return env.timers.length; },
    clearTimeout: (id) => { if (env.timers[id - 1]) env.timers[id - 1].done = true; },
    importScripts: (name) => {
      assert.equal(name, "policy.js");
      vm.runInContext(POLICY_SRC, context);
    },
  });
  vm.runInContext(WORKER_SRC, context);
  env.popup = (message, sender = { id: EXT_ID, url: chrome.runtime.getURL("popup.html") }) =>
    new Promise((resolve) => {
      const keepOpen = env.listeners.message[0](message, sender, resolve);
      if (keepOpen !== true) resolve("NO_RESPONSE");
    });
  env.port = () => env.ports[env.ports.length - 1];
  env.runTimers = () => {
    for (const timer of env.timers.filter((t) => !t.done)) {
      timer.done = true;
      timer.fn();
    }
  };
  env.fireAlarm = () => env.listeners.alarm.forEach((fn) => fn({ name: "lumina-companion-reconnect" }));
  env.announceRemoved = (origins) => {
    const fire = () => env.listeners.permissionRemoved.forEach((fn) => fn({ origins, permissions: [] }));
    if (env.holdEvents) env.heldEvents.push(fire);
    else fire();
  };
  env.releaseEvents = () => {
    const held = env.heldEvents.splice(0);
    held.forEach((fire) => fire());
    return held.length;
  };
  // The owner withdraws a site grant OUTSIDE the extension (chrome://extensions,
  // the toolbar menu): Chrome drops it, then (maybe later) tells the worker.
  env.revoke = (pattern, { event = true } = {}) => {
    env.granted.delete(pattern);
    if (event) env.announceRemoved([pattern]);
  };
  // The owner's popup revoke button: a message to the worker (R3).
  env.popupRevoke = (pattern) => env.popup({ kind: "revoke_site", pattern });
  // Evaluate an expression against the worker's own top-level state, in the
  // same synchronous run as the caller (R4: what holds AT receipt).
  env.peek = (expression) => vm.runInContext(expression, context);
  return env;
}

async function toReady(env, cid = CID) {
  await settle();
  const port = env.port();
  assert.ok(port, "worker connected");
  port.deliver({ v: 1, type: "host_status", state: "hub_connected" });
  port.deliver({ v: 1, type: "welcome", connection_id: cid, limits: {} });
  await settle();
  assert.equal(env.storage.companionStatus.state, "READY");
  return port;
}

function request(op, extra = {}) {
  return { v: 1, type: "request", connection_id: CID, request_id: rid(), op, tab_id: null,
    deadline_ms: Date.now() + 10000, args: {}, ...extra };
}

async function call(port, req) {
  port.deliver(req);
  await settle(12);
  return port.sent.filter((m) => m.type === "response" && m.request_id === req.request_id);
}

async function single(port, req) {
  const responses = await call(port, req);
  assert.equal(responses.length, 1, `exactly one response for ${req.op}`);
  return responses[0];
}

const TESTS = [];
const test = (name, fn) => TESTS.push({ name, fn });

// ── Startup / identity ─────────────────────────────────────────────────

test("persisted PAUSE is read before any connection on every wake path", async () => {
  const gate = deferred();
  const env = makeEnv({ storage: { companionPaused: true }, storageGetGate: gate.promise });
  env.listeners.startup.forEach((fn) => fn());
  env.listeners.installed.forEach((fn) => fn());
  env.fireAlarm();
  await settle();
  assert.equal(env.ports.length, 0, "no connection before the pause switch is read");
  gate.resolve();
  await settle();
  env.fireAlarm();
  env.listeners.startup.forEach((fn) => fn());
  await settle();
  assert.equal(env.ports.length, 0, "persisted pause blocks every connection path");
  assert.equal(env.storage.companionStatus.state, "PAUSED");
});

test("instance id is random, persisted, and stable across a worker restart", async () => {
  const env = makeEnv();
  await settle();
  const id = env.storage.companionInstanceId;
  assert.match(id, /^[0-9a-f]{32}$/);
  assert.deepEqual(env.port().sent[0], { v: 1, type: "hello", extension_id: EXT_ID, instance_id: id,
    extension_version: "0.1.0" });
  const restarted = makeEnv({ storage: env.storage });
  await settle();
  assert.equal(restarted.port().sent[0].instance_id, id);
  const other = makeEnv();
  await settle();
  assert.notEqual(other.storage.companionInstanceId, id, "another profile gets another identity");
});

test("storage failure keeps the worker paused (fail closed)", async () => {
  const env = makeEnv({ storageGetGate: Promise.reject(new Error("storage down")) });
  env.listeners.startup.forEach((fn) => fn());
  env.fireAlarm();
  await settle();
  assert.equal(env.ports.length, 0, "never connects when the pause switch cannot be read");
  assert.equal(env.storage.companionStatus.state, "PAUSED");
  assert.equal(env.storage.companionStatus.lastError, "storage_unavailable");
});

test("handshake: requests before READY tear the session down unexecuted", async () => {
  const env = makeEnv();
  await settle();
  const port = env.port();
  port.deliver(request("list_tabs"));
  await settle();
  assert.equal(port.disconnected, true);
  assert.equal(env.queries, 0);
  const env2 = makeEnv();
  await settle();
  env2.port().deliver({ v: 1, type: "welcome", connection_id: CID, limits: {} });
  await settle();
  assert.equal(env2.port().disconnected, true, "welcome before host identification is a violation");
});

// ── Tabs ───────────────────────────────────────────────────────────────

test("list_tabs omits incognito, withholds restricted surfaces, reports site access", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const res = await single(port, request("list_tabs"));
  assert.equal(res.ok, true);
  const byId = Object.fromEntries(res.result.tabs.map((t) => [t.tab_id, t]));
  assert.deepEqual(Object.keys(byId).map(Number).sort((a, b) => a - b), [7, 8, 10, 11]);
  assert.equal(res.result.total, 4);
  for (const id of [8, 11]) {
    assert.equal(byId[id].restricted, true);
    assert.equal(byId[id].url, null);
    assert.equal(byId[id].title, null);
    assert.equal(byId[id].site_access, "restricted");
  }
  assert.equal(byId[8].restriction, "restricted_scheme");
  assert.equal(byId[11].restriction, "restricted_host");
  assert.equal(byId[7].site_access, "granted");
  assert.equal(byId[10].site_access, "not_granted");
  assert.equal(byId[7].incognito, false);
  assert.equal(env.injections.length, 0, "listing tabs never touches page content");
});

test("tab list is bounded", async () => {
  const tabs = Array.from({ length: 150 }, (_, i) => ({ id: i, windowId: 1, active: i === 0, incognito: false,
    status: "complete", url: `https://site${i}.test/`, title: `t${i}` }));
  const env = makeEnv({ tabs });
  const port = await toReady(env);
  const res = await single(port, request("list_tabs"));
  assert.equal(res.result.tabs.length, 100);
  assert.equal(res.result.total, 150);
  assert.equal(res.truncated, true);
});

test("get_tab: incognito is not found; restricted is described without URL/title", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const incognito = await single(port, request("get_tab", { tab_id: 9 }));
  assert.equal(incognito.ok, false);
  assert.equal(incognito.error.code, "tab_not_found");
  const restricted = await single(port, request("get_tab", { tab_id: 8 }));
  assert.equal(restricted.result.url, null);
  assert.equal(restricted.result.restricted, true);
});

// ── Page reads ─────────────────────────────────────────────────────────

test("extract_text never injects without owner-granted site access", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const res = await single(port, request("extract_text", { tab_id: 10 }));
  assert.equal(res.ok, false);
  assert.equal(res.error.code, "site_access_required");
  assert.equal(env.injections.length, 0);
  env.granted.add("https://github.com/*");
  const granted = await single(port, request("extract_text", { tab_id: 10 }));
  assert.equal(granted.ok, true);
  env.granted.delete("https://github.com/*");
  const revoked = await single(port, request("get_links", { tab_id: 10 }));
  assert.equal(revoked.error.code, "site_access_required");
});

test("restricted and incognito tabs are never read, even with every permission granted", async () => {
  const env = makeEnv({ granted: ["https://www.reddit.com/*", "https://passwords.google.com/*",
    "https://private.test/*", "https://github.com/*"] });
  const port = await toReady(env);
  for (const [tabId, code] of [[8, "restricted_surface"], [11, "restricted_surface"], [9, "tab_not_found"]]) {
    for (const op of ["extract_text", "get_links"]) {
      const res = await single(port, request(op, { tab_id: tabId }));
      assert.equal(res.ok, false);
      assert.equal(res.error.code, code, `${op} on tab ${tabId}`);
    }
  }
  assert.equal(env.injections.length, 0);
});

test("R1/F2: equivalent spellings of restricted hosts stay restricted, whatever Chrome would grant", async () => {
  const spellings = ["https://accounts.google.com/", "https://accounts.google.com./", "https://ACCOUNTS.GOOGLE.COM./",
    "https://passwords.google.com/", "https://passwords.google.com./", "https://PASSWORDS.GOOGLE.COM./",
    "https://accounts.google.com../signin"];
  for (const url of spellings) {
    const tabs = TABS();
    tabs[0].url = url;
    const pages = PAGES();
    pages[7] = { href: url, title: "Sign in", text: "CANARY_RESTRICTED_ALIAS", anchors: [] };
    // Grant every pattern a naive classifier could ask for: this test never
    // relies on Chrome refusing the permission for us.
    const raw = new URL(url).hostname;
    const env = makeEnv({ tabs, pages, granted: [`https://${raw}/*`, "https://accounts.google.com./*",
      "https://passwords.google.com./*", "https://accounts.google.com/*", "https://passwords.google.com/*"] });
    const port = await toReady(env);
    const listed = (await single(port, request("list_tabs"))).result.tabs.find((t) => t.tab_id === 7);
    assert.equal(listed.restricted, true, url);
    assert.equal(listed.restriction, "restricted_host", url);
    assert.equal(listed.url, null);
    assert.equal(listed.site_access, "restricted");
    for (const req of [request("extract_text", { tab_id: 7 }), request("get_links", { tab_id: 7 }),
      request("extract_text")]) {
      const res = await single(port, req);
      assert.equal(res.ok, false, url);
      assert.equal(res.error.code, "restricted_surface", `${req.op} ${url}`);
    }
    assert.equal(env.injections.length, 0, `never injected into ${url}`);
    assert.doesNotMatch(JSON.stringify(port.sent), /CANARY_RESTRICTED_ALIAS/);
  }
});

test("extract_text reads bounded visible text in the isolated world", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const res = await single(port, request("extract_text", { tab_id: 7, args: { max_chars: 500 } }));
  assert.equal(res.ok, true);
  assert.equal(res.result.text, "Hello AgentsInteractive\n\nsecond line tabs");
  assert.equal(res.tab_id, 7);
  assert.deepEqual(res.observed, { url: "https://www.reddit.com/r/AgentsInteractive/",
    origin: "https://www.reddit.com", document_id: "doc-7" });
  const inj = env.injections[0];
  assert.equal(inj.world, "ISOLATED");
  assert.deepEqual(plain(inj.target), { tabId: 7, documentIds: ["doc-7"] });
  const small = await single(port, request("extract_text", { tab_id: 7, args: { max_chars: 5 } }));
  assert.equal(small.result.text, "Hello");
  assert.equal(small.truncated, true);
  const active = await single(port, request("extract_text"));
  assert.equal(active.tab_id, 7, "defaults to the active tab");
  assert.equal(env.storage.companionStatus.lastAction.host, "www.reddit.com");
});

test("text bounds never split a surrogate pair", async () => {
  const pages = PAGES();
  pages[7].text = "abcd\u{1F600}efgh";
  const env = makeEnv({ pages });
  const port = await toReady(env);
  const res = await single(port, request("extract_text", { tab_id: 7, args: { max_chars: 5 } }));
  assert.equal(res.result.text, "abcd");
});

test("navigation during a read is stale and discarded", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  let calls = 0;
  env.tabsGetHook = (id) => {
    calls += 1;
    return calls === 2 ? { ...env.tabs[0], url: "https://www.reddit.com/r/elsewhere/" } : undefined;
  };
  const moved = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(moved.error.code, "navigated_during_request");
  env.tabsGetHook = null;
  const pages = PAGES();
  pages[7].locationHref = "https://evil.test/";
  env.pages = pages;
  const swapped = await single(port, request("get_links", { tab_id: 7 }));
  assert.equal(swapped.error.code, "navigated_during_request");
});

// ── R1/F3: document identity, not URL equality ─────────────────────────

const READ_OPS = [["extract_text", "CANARY"], ["get_links", "CANARY"]];

function canaryPage(text) {
  return { href: "https://www.reddit.com/r/AgentsInteractive/", title: "Synthetic", text,
    anchors: [{ href: `https://canary.test/${text}`, text }] };
}

test("R1/F3: the read is bound to Chrome's document identity for the tab's main frame", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const res = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(res.ok, true);
  assert.equal(res.observed.document_id, "doc-7");
  assert.deepEqual(plain(env.injections[0].target), { tabId: 7, documentIds: ["doc-7"] });
  assert.deepEqual(plain(env.frameCalls), [{ tabId: 7, frameId: 0 }, { tabId: 7, frameId: 0 }],
    "identity is taken before the read and re-checked after it");
});

test("R1/F3 (Goblin's synthetic): same tab, same URL, injection answered by a different document", async () => {
  for (const [op] of READ_OPS) {
    const env = makeEnv();
    const port = await toReady(env);
    env.injectionHook = async (details) => [{ documentId: "new-document-after-reload", frameId: 0,
      result: runInPage(details.func, details.args, canaryPage("CANARY_NEW_DOCUMENT")) }];
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.ok, false, op);
    assert.equal(res.error.code, "navigated_during_request", op);
    assert.doesNotMatch(JSON.stringify(port.sent), /CANARY_NEW_DOCUMENT/);
  }
});

test("R1/F3: a same-URL reload that lands before the read completes is stale", async () => {
  for (const [op] of READ_OPS) {
    const env = makeEnv();
    const port = await toReady(env);
    env.injectionHook = async (details) => {
      // The script ran in document A... then the tab reloaded: same tab id,
      // same URL, new document -- before the read completed.
      const result = [{ documentId: "doc-7", frameId: 0, result: runInPage(details.func, details.args, env.pages[7]) }];
      env.documents[7] = "doc-7-after-reload";
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "navigated_during_request", op);
  }
});

test("R1/F3: a reload between validation and injection -- Chrome refuses the stale target", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  env.injectionHook = async () => {
    env.documents[7] = "doc-7-after-reload"; // new document, same URL, before the script is dispatched
    return undefined; // Chrome then refuses: "No document with id doc-7 in tab with id 7"
  };
  const res = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(res.error.code, "navigated_during_request");
  assert.doesNotMatch(JSON.stringify(res), /No document with id/, "raw Chrome errors never cross");
});

test("R1/F3: A -> B -> A navigation (a new document at the same URL) is stale", async () => {
  for (const [op] of READ_OPS) {
    const env = makeEnv();
    const port = await toReady(env);
    const urlA = env.tabs[0].url;
    env.injectionHook = async (details) => {
      const result = [{ documentId: "doc-7", frameId: 0, result: runInPage(details.func, details.args, env.pages[7]) }];
      env.tabs[0].url = "https://www.reddit.com/r/elsewhere/";   // -> B
      env.documents[7] = "doc-7-B";
      env.tabs[0].url = urlA;                                       // -> A again, as a NEW document
      env.documents[7] = "doc-7-A-again";
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "navigated_during_request", op);
  }
});

test("R1/F3: a main document that is not active is never read", async () => {
  for (const lifecycle of ["prerender", "cached", "pending_deletion", undefined]) {
    const env = makeEnv();
    const port = await toReady(env);
    env.frameHook = (details) => ({ documentId: "doc-7", documentLifecycle: lifecycle, frameType: "outermost_frame",
      url: env.tabs[0].url });
    const res = await single(port, request("extract_text", { tab_id: 7 }));
    assert.equal(res.error.code, "navigated_during_request", String(lifecycle));
    assert.equal(env.injections.length, 0);
  }
});

test("R1/F3: without Chrome's document identity the read fails closed", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  delete env.chrome.webNavigation;
  const res = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(res.error.code, "document_identity_unavailable");
  assert.equal(env.injections.length, 0);
  const env2 = makeEnv();
  const port2 = await toReady(env2);
  env2.frameHook = () => ({ documentLifecycle: "active", url: env2.tabs[0].url }); // no documentId
  assert.equal((await single(port2, request("get_links", { tab_id: 7 }))).error.code, "navigated_during_request");
  assert.equal(env2.injections.length, 0);
});

test("R1/F3: the bound document must be the one whose URL passed policy", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  // A navigation committed between the tab lookup and the identity lookup.
  env.frameHook = () => ({ documentId: "doc-7", documentLifecycle: "active", url: "https://evil.test/" });
  const res = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(res.error.code, "navigated_during_request");
  assert.equal(env.injections.length, 0);
});

test("R1/F3: a same-document navigation (same identity, new URL) during the read is stale", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  env.frameHook = (details, n) => (n === 2
    ? { documentId: "doc-7", documentLifecycle: "active", url: "https://www.reddit.com/r/AgentsInteractive/#pushed" }
    : undefined);
  const res = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(res.error.code, "navigated_during_request");
});

test("R1/F3: the tab closing before its identity is read is tab_closed", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  env.frameHook = () => {
    env.tabs = env.tabs.filter((t) => t.id !== 7);
    return null;
  };
  const res = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(res.error.code, "tab_closed");
  assert.equal(env.injections.length, 0);
});

test("tab closing or permission revoked mid-read are explicit failures", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  env.injectionHook = async () => {
    env.tabs = env.tabs.filter((t) => t.id !== 7);
    throw new Error("No tab with id: 7");
  };
  const closed = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(closed.error.code, "tab_closed");
  const env2 = makeEnv();
  const port2 = await toReady(env2);
  env2.injectionHook = async () => {
    env2.granted.clear();
    throw new Error("Cannot access contents of the page");
  };
  const revoked = await single(port2, request("extract_text", { tab_id: 7 }));
  assert.equal(revoked.error.code, "site_access_required");
});

test("raw Chrome error text (which can embed URLs) never crosses the bridge", async () => {
  const env = makeEnv({ granted: ["https://github.com/*"], pages: {} });
  const port = await toReady(env);
  const res = await single(port, request("extract_text", { tab_id: 10 }));
  assert.equal(res.error.code, "injection_failed");
  assert.doesNotMatch(JSON.stringify(res), /mail\.google\.com|secret/);
});

test("get_links filters schemes, strips credentials, dedupes, and bounds", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const res = await single(port, request("get_links", { tab_id: 7 }));
  const hrefs = res.result.links.map((l) => l.href);
  assert.deepEqual(hrefs, ["https://www.reddit.com/r/AgentsInteractive/comments/1",
    "https://example.test/a", "https://other.test/x"]);
  assert.equal(res.result.links[0].text, "First post");
  assert.equal(res.result.links[0].same_origin, true);
  assert.equal(res.result.links[2].text, "labelled");
  assert.equal(res.result.links[2].same_origin, false);
  assert.doesNotMatch(JSON.stringify(res), /secret@|javascript:|mailto:|data:text/);
  const pages = PAGES();
  pages[7].anchors = Array.from({ length: 400 }, (_, i) => ({ href: `https://x.test/${i}`, text: `l${i}` }));
  env.pages = pages;
  const bounded = await single(port, request("get_links", { tab_id: 7, args: { max_links: 150 } }));
  assert.equal(bounded.result.links.length, 150);
  assert.equal(bounded.result.total_links, 400);
  assert.equal(bounded.truncated, true);
});

// ── Request validation ─────────────────────────────────────────────────

test("duplicate request ids are executed once and answered once", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const req = request("list_tabs");
  port.deliver(req);
  port.deliver({ ...req });
  await settle(12);
  assert.equal(port.sent.filter((m) => m.request_id === req.request_id).length, 1);
  assert.equal(env.queries, 1);
});

test("wrong connection, unknown op, malformed args and stale deadlines are refused unexecuted", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const cases = [
    [request("list_tabs", { connection_id: CID2 }), "wrong_connection"],
    [request("click", { tab_id: 7 }), "unknown_op"],
    [request("type", { tab_id: 7 }), "unknown_op"],
    [request("list_tabs", { tab_id: 7 }), "invalid_args"],
    [request("get_tab", { tab_id: true }), "invalid_args"],
    [request("extract_text", { args: { max_chars: 30001 } }), "invalid_args"],
    [request("extract_text", { args: { selector: "#x" } }), "invalid_args"],
    [{ ...request("list_tabs"), extra: 1 }, "invalid_args"],
    [request("list_tabs", { deadline_ms: Date.now() - 1 }), "deadline_expired"],
  ];
  for (const [req, code] of cases) {
    const res = await single(port, req);
    assert.equal(res.ok, false);
    assert.equal(res.error.code, code, JSON.stringify(req));
  }
  assert.equal(env.queries, 0);
  assert.equal(env.injections.length, 0);
});

test("an unknown protocol version tears the session down", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  port.deliver({ ...request("list_tabs"), v: 2 });
  await settle();
  assert.equal(port.disconnected, true);
  assert.equal(env.queries, 0);
  assert.equal(env.storage.companionStatus.state, "DISCONNECTED");
});

// ── PAUSE ──────────────────────────────────────────────────────────────

test("only the extension's own popup can operate PAUSE", async () => {
  const env = makeEnv();
  await toReady(env);
  const hostile = [
    { id: EXT_ID, url: "https://evil.test/", tab: { id: 7 } },
    { id: "ponmlkjihgfedcbaponmlkjihgfedcba", url: `chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba/popup.html` },
    { id: EXT_ID, url: `chrome-extension://${EXT_ID}/popup.html`, tab: { id: 7 } },
    { id: EXT_ID, url: `chrome-extension://${EXT_ID}/other.html` },
  ];
  for (const sender of hostile) {
    assert.equal(await env.popup({ kind: "set_paused", paused: true }, sender), "NO_RESPONSE");
    assert.equal(await env.popup({ kind: "get_status" }, sender), "NO_RESPONSE");
  }
  assert.notEqual(env.storage.companionPaused, true);
  assert.equal(env.port().disconnected, false);
});

test("PAUSE is persisted before ack, says bye, disconnects, and blocks every path", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const pauseWrite = deferred();
  env.storageSetHook = (values) => ("companionPaused" in values ? pauseWrite.promise : undefined);
  let ack = null;
  const acked = env.popup({ kind: "set_paused", paused: true }).then((r) => { ack = r; });
  await settle();
  assert.equal(ack, null, "not acknowledged before the PAUSE write lands");
  assert.equal(port.disconnected, true, "disconnects immediately");
  assert.deepEqual(port.sent.at(-1), { v: 1, type: "bye", connection_id: CID, reason: "paused" });
  pauseWrite.resolve();
  env.storageSetHook = null;
  await acked;
  assert.deepEqual(plain(ack), { ok: true, paused: true });
  assert.equal(env.storage.companionPaused, true);
  // Late status writes must never overwrite the separate PAUSE key.
  await settle();
  assert.equal(env.storage.companionPaused, true);
  port.deliver(request("list_tabs"));
  env.fireAlarm();
  env.runTimers();
  env.listeners.startup.forEach((fn) => fn());
  await settle();
  assert.equal(env.ports.length, 1, "no reconnection while paused");
  assert.equal(env.queries, 0, "nothing executes while paused");
  assert.equal((await env.popup({ kind: "get_status" })).state, "PAUSED");
});

test("PAUSE during an in-flight read drops its result", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const injection = deferred();
  env.injectionHook = () => injection.promise;
  const req = request("extract_text", { tab_id: 7 });
  port.deliver(req);
  await settle();
  assert.equal(env.injections.length, 1);
  await env.popup({ kind: "set_paused", paused: true });
  injection.resolve([{ documentId: "doc-7", frameId: 0, result: { href: "https://www.reddit.com/r/AgentsInteractive/",
    title: "t", text: "LATE", total_chars: 4, truncated: false } }]);
  await settle(12);
  assert.equal(port.sent.filter((m) => m.type === "response").length, 0);
});

test("RESUME builds a fresh session; the old session's identity is dead", async () => {
  const env = makeEnv();
  const port1 = await toReady(env);
  const used = request("list_tabs");
  await single(port1, used);
  await env.popup({ kind: "set_paused", paused: true });
  const ack = await env.popup({ kind: "set_paused", paused: false });
  assert.deepEqual(plain(ack), { ok: true, paused: false });
  assert.equal(env.ports.length, 2);
  const port2 = await toReady(env, CID2);
  assert.notEqual(port2, port1);
  const stale = await single(port2, request("list_tabs"));
  assert.equal(stale.error.code, "wrong_connection", "requests addressed to the old connection are refused");
  const fresh = await single(port2, { ...used, connection_id: CID2 });
  assert.equal(fresh.ok, true, "the new session has an empty request-id memory");
});

// ── Reconnect / lifecycle ──────────────────────────────────────────────

test("a read awaited across disconnect + reconnect never answers on the new session", async () => {
  const env = makeEnv();
  const port1 = await toReady(env);
  const injection = deferred();
  env.injectionHook = () => injection.promise;
  const req = request("extract_text", { tab_id: 7 });
  port1.deliver(req);
  await settle();
  port1.hostGone();
  await settle();
  assert.equal(env.storage.companionStatus.state, "DISCONNECTED");
  env.runTimers();
  await settle();
  assert.equal(env.ports.length, 2);
  const port2 = await toReady(env, CID2);
  injection.resolve([{ documentId: "doc-7", frameId: 0, result: { href: "https://www.reddit.com/r/AgentsInteractive/",
    title: "t", text: "STALE", total_chars: 5, truncated: false } }]);
  await settle(12);
  assert.equal(port1.sent.filter((m) => m.type === "response").length, 0);
  assert.equal(port2.sent.filter((m) => m.type === "response").length, 0);
});

test("reconnect schedule: quick timers, durable alarm, cleared once READY", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  port.hostGone();
  await settle();
  assert.equal(env.timers.at(-1).ms, 1000);
  assert.deepEqual(plain(env.alarms.at(-1)), { name: "lumina-companion-reconnect", info: { periodInMinutes: 0.5 } });
  env.fireAlarm();
  await settle();
  await toReady(env, CID2);
  assert.deepEqual(plain(env.alarms.at(-1)), { name: "lumina-companion-reconnect", cleared: true });
});

test("rejections and host failures are surfaced; rejection backs off to alarm cadence", async () => {
  const env = makeEnv();
  await settle();
  const timersBefore = env.timers.length;
  env.port().deliver({ v: 1, type: "host_status", state: "hub_connected" });
  env.port().deliver({ v: 1, type: "reject", reason: "unpaired" });
  await settle();
  assert.equal(env.storage.companionStatus.lastError, "unpaired");
  assert.equal(env.timers.length, timersBefore, "no fast retry loop while unpaired");
  assert.equal(env.alarms.at(-1).info.periodInMinutes, 0.5);

  const missing = makeEnv();
  missing.connectThrows = true;
  await settle();
  assert.equal(missing.storage.companionStatus.lastError, "host_not_installed");

  const gone = makeEnv();
  await settle();
  gone.port().hostGone("Specified native messaging host not found.");
  await settle();
  assert.equal(gone.storage.companionStatus.lastError, "host_not_installed");

  const offline = makeEnv();
  await settle();
  offline.port().deliver({ v: 1, type: "host_status", state: "hub_unavailable" });
  offline.port().hostGone();
  await settle();
  assert.equal(offline.storage.companionStatus.lastError, "lumina_unreachable");
});

// ═══ BROWSER-COMPANION-01A-R2 ═══════════════════════════════════════════

// Deterministic PRNG for the stress tests (mulberry32).
function prng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Parks every owner write to companionPaused until the test releases it.
// Writes are issued one at a time (the worker serializes them), in order.
function holdPauseWrites(env) {
  const held = [];
  env.storageSetHook = (values) => {
    if (!("companionPaused" in values)) return undefined;
    const d = deferred();
    held.push({ value: values.companionPaused, ...d });
    return d.promise;
  };
  return held;
}

// The B1 law, observed from outside: a persisted PAUSE means no live port --
// nothing that could carry a request -- and the worker says PAUSED.
async function assertPausedMeansDead(env, label) {
  if (env.storage.companionPaused !== true) return;
  assert.equal(env.ports.filter((p) => !p.disconnected).length, 0, `${label}: persisted PAUSE with a live port`);
  assert.equal((await env.popup({ kind: "get_status" })).state, "PAUSED", `${label}: persisted PAUSE, not PAUSED`);
}

const LATE_PAGE = (text) => [{ documentId: "doc-7", frameId: 0, result: { href: "https://www.reddit.com/r/AgentsInteractive/",
  title: text, text, total_chars: text.length, truncated: false } }];

// ── R2 / B1: PAUSE / RESUME overlap ────────────────────────────────────

test("R2/B1 (Goblin's R2-2): a held PAUSE write and an overlapping RESUME never leave PAUSE persisted with a session", async () => {
  const env = makeEnv();
  const port1 = await toReady(env);
  const held = holdPauseWrites(env);
  const pauseAck = env.popup({ kind: "set_paused", paused: true });
  await settle();
  assert.equal(port1.disconnected, true, "PAUSE tore the session down before its write landed");
  const resumeAck = env.popup({ kind: "set_paused", paused: false });
  await settle();
  assert.equal(held.length, 1, "RESUME's write waits behind PAUSE's");
  assert.equal(env.ports.length, 1, "RESUME cannot connect before it is persisted");
  port1.deliver(request("extract_text", { tab_id: 7 }));
  await settle();
  assert.equal(env.injections.length, 0, "nothing reads in the window");
  held[0].resolve(); // PAUSE lands first ...
  await settle();
  assert.equal(env.storage.companionPaused, true);
  await assertPausedMeansDead(env, "PAUSE landed");
  assert.equal(held.length, 2, "... then RESUME's write is issued");
  assert.equal(held[1].value, false);
  held[1].resolve(); // ... and RESUME, the newest command, lands last
  assert.deepEqual(plain(await pauseAck), { ok: true, paused: true, superseded: true });
  assert.deepEqual(plain(await resumeAck), { ok: true, paused: false });
  assert.equal(env.storage.companionPaused, false);
  const port2 = await toReady(env, CID2);
  assert.notEqual(port2, port1, "a valid RESUME builds a FRESH session");
  assert.equal((await single(port2, request("list_tabs", { connection_id: CID2 }))).ok, true);
  assert.equal((await single(port2, request("list_tabs"))).error.code, "wrong_connection",
    "the pre-pause session's identity is dead");
});

test("R2/B1: a RESUME overtaken by a newer PAUSE never unpauses, connects, or reads", async () => {
  for (const startPaused of [true, false]) {
    const env = makeEnv({ storage: startPaused ? { companionPaused: true } : {} });
    const port1 = startPaused ? null : await toReady(env);
    await settle();
    const portsBefore = env.ports.length;
    const held = holdPauseWrites(env);
    const resumeAck = env.popup({ kind: "set_paused", paused: false });
    await settle();
    const pauseAck = env.popup({ kind: "set_paused", paused: true });
    await settle();
    if (port1) assert.equal(port1.disconnected, true, "the newer PAUSE acted at once");
    held[0].resolve(); // the overtaken RESUME's write lands late
    await settle();
    assert.equal(env.ports.length, portsBefore, "the overtaken RESUME did not connect");
    assert.equal((await env.popup({ kind: "get_status" })).state, "PAUSED");
    held[1].resolve();
    assert.deepEqual(plain(await resumeAck), { ok: true, paused: false, superseded: true });
    assert.deepEqual(plain(await pauseAck), { ok: true, paused: true });
    assert.equal(env.storage.companionPaused, true);
    env.fireAlarm();
    env.runTimers();
    env.listeners.startup.forEach((fn) => fn());
    await settle();
    assert.equal(env.ports.length, portsBefore, "nothing reconnects afterwards");
    await assertPausedMeansDead(env, `startPaused=${startPaused}`);
    assert.equal(env.queries + env.injections.length, 0);
  }
});

test("R2/B1: two popup commands in the same tick -- the later one wins, durably and in memory", async () => {
  for (const [first, second] of [[true, false], [false, true], [true, true], [false, false]]) {
    const env = makeEnv();
    await toReady(env);
    const a = env.popup({ kind: "set_paused", paused: first });
    const b = env.popup({ kind: "set_paused", paused: second });
    await Promise.all([a, b]);
    await settle();
    assert.equal(env.storage.companionPaused, second, `${first}->${second}`);
    const alive = env.ports.filter((p) => !p.disconnected).length;
    assert.equal(alive, second ? 0 : 1, `${first}->${second}: live ports`);
    await assertPausedMeansDead(env, `${first}->${second}`);
  }
});

test("R2/B1: PAUSE during connect kills the handshake; the old port's late frames and disconnect are ignored", async () => {
  for (const stage of ["CONNECTING", "IDENTIFIED"]) {
    const env = makeEnv();
    await settle();
    const port = env.port();
    if (stage === "IDENTIFIED") port.deliver({ v: 1, type: "host_status", state: "hub_connected" });
    const held = holdPauseWrites(env);
    const ack = env.popup({ kind: "set_paused", paused: true });
    await settle();
    assert.equal(port.disconnected, true, `${stage}: torn down before the PAUSE write landed`);
    port.deliver({ v: 1, type: "host_status", state: "hub_connected" });
    port.deliver({ v: 1, type: "welcome", connection_id: CID, limits: {} });
    port.deliver(request("list_tabs"));
    port.hostGone();
    await settle();
    held[0].resolve();
    await ack;
    await settle();
    assert.equal(env.queries, 0, stage);
    assert.equal(env.ports.length, 1, stage);
    await assertPausedMeansDead(env, stage);
  }
});

test("R2/B1: PAUSE during a pending retry: no timer, alarm, startup or install event reconnects", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  port.hostGone();
  await settle();
  assert.ok(env.timers.some((t) => !t.done), "a retry is pending");
  const held = holdPauseWrites(env);
  const ack = env.popup({ kind: "set_paused", paused: true });
  const fireAll = () => {
    for (const timer of env.timers) if (!timer.done) timer.fn(); // even a timer PAUSE cleared
    env.fireAlarm();
    env.listeners.startup.forEach((fn) => fn());
    env.listeners.installed.forEach((fn) => fn());
  };
  fireAll(); // while the PAUSE write is still pending
  await settle();
  assert.equal(env.ports.length, 1);
  held[0].resolve();
  await ack;
  fireAll();
  await settle();
  assert.equal(env.ports.length, 1);
  assert.ok(env.alarms.some((a) => a.cleared), "the durable reconnect alarm is cleared");
  await assertPausedMeansDead(env, "after retry");
});

test("R2/B1 (Goblin's AR2 seam): no gap between PAUSE and teardown, even if Chrome's alarm API stalls", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const alarmClear = deferred();
  env.chrome.alarms.clear = () => alarmClear.promise;
  const injection = deferred();
  env.injectionHook = () => injection.promise;
  port.deliver(request("extract_text", { tab_id: 7 }));
  await settle();
  assert.equal(env.injections.length, 1);
  const ack = env.popup({ kind: "set_paused", paused: true });
  assert.equal(port.disconnected, true, "teardown in the same tick as the PAUSE command");
  injection.resolve(LATE_PAGE("CANARY_AFTER_PAUSE"));
  await settle(12);
  assert.equal(port.sent.filter((m) => m.type === "response").length, 0);
  assert.deepEqual(plain(await ack), { ok: true, paused: true }, "acknowledged without waiting on the alarm API");
  alarmClear.resolve(true);
  assert.doesNotMatch(JSON.stringify(port.sent), /CANARY_AFTER_PAUSE/);
});

test("R2/B1: PAUSE while a read is being set up means it is never dispatched into the page", async () => {
  for (const op of ["extract_text", "get_links"]) {
    for (const stall of ["tabs.get", "permissions", "frame"]) {
      const env = makeEnv();
      const port = await toReady(env);
      const gate = deferred();
      let armed = true;
      const hold = async () => {
        if (armed) {
          armed = false;
          await gate.promise;
        }
        return undefined;
      };
      if (stall === "tabs.get") env.tabsGetHook = hold;
      if (stall === "permissions") env.containsHook = hold;
      if (stall === "frame") env.frameHook = hold;
      port.deliver(request(op, { tab_id: 7 }));
      await settle();
      void env.popup({ kind: "set_paused", paused: true });
      gate.resolve();
      await settle(12);
      assert.equal(env.injections.length, 0, `${op}/${stall}: nothing was injected after PAUSE`);
      assert.equal(port.sent.filter((m) => m.type === "response").length, 0, `${op}/${stall}`);
    }
  }
});

test("R2/B1: PAUSE during service-worker wake -- the late startup read cannot unpause", async () => {
  const gate = deferred();
  const env = makeEnv({ storage: { companionPaused: false }, storageGetGate: gate.promise });
  const ack = env.popup({ kind: "set_paused", paused: true });
  await settle();
  gate.resolve(); // the startup read now returns the OLD value: not paused
  assert.deepEqual(plain(await ack), { ok: true, paused: true });
  await settle();
  assert.equal(env.ports.length, 0, "never connected");
  await assertPausedMeansDead(env, "wake");
  const restarted = makeEnv({ storage: env.storage }); // worker (or Chrome) restart
  await settle();
  assert.equal(restarted.ports.length, 0, "PAUSE survives the restart");

  const gate2 = deferred();
  const env2 = makeEnv({ storage: { companionPaused: true }, storageGetGate: gate2.promise });
  const ack2 = env2.popup({ kind: "set_paused", paused: false });
  await settle();
  assert.equal(env2.ports.length, 0, "no connection before the wake read and the RESUME write");
  gate2.resolve(); // returns the OLD value: paused
  assert.deepEqual(plain(await ack2), { ok: true, paused: false });
  await settle();
  assert.equal(env2.ports.length, 1, "the RESUME -- newer than the startup read -- connects once");
});

test("R2/B1: an owner write that fails never unpauses, and a failed PAUSE still stops everything now", async () => {
  const env = makeEnv({ storage: { companionPaused: true } });
  await settle();
  env.storageSetHook = (values) => {
    if ("companionPaused" in values) throw new Error("QUOTA_BYTES quota exceeded");
  };
  assert.deepEqual(plain(await env.popup({ kind: "set_paused", paused: false })),
    { ok: false, error: "resume_not_persisted" });
  await settle();
  assert.equal(env.ports.length, 0, "an unpersisted RESUME never connects");
  assert.equal((await env.popup({ kind: "get_status" })).state, "PAUSED");

  const env2 = makeEnv();
  const port = await toReady(env2);
  env2.storageSetHook = env.storageSetHook;
  assert.deepEqual(plain(await env2.popup({ kind: "set_paused", paused: true })), { ok: false, error: "pause_not_persisted" });
  assert.equal(port.disconnected, true, "in memory, a PAUSE is effective even when its write fails");
  env2.fireAlarm();
  env2.runTimers();
  await settle();
  assert.equal(env2.ports.length, 1);
});

test("R2/B1: 100 rapid overlapping PAUSE/RESUME commands with random write latency keep the law at every step", async () => {
  // Stress: R2_SEEDS="1-500" node worker_test.js sweeps many more schedules.
  const range = /^(\d+)-(\d+)$/.exec(process.env.R2_SEEDS || "");
  const seeds = range ? Array.from({ length: range[2] - range[1] + 1 }, (_, i) => Number(range[1]) + i) : [0xb1, 0xb2, 0xb3];
  for (const seed of seeds) {
    const rng = prng(seed);
    const env = makeEnv();
    await toReady(env);
    const held = holdPauseWrites(env);
    const acks = [];
    let lastCommand = false;
    let cid = 0;
    for (let step = 0; step < 100; step++) {
      const roll = rng();
      if (roll < 0.45) {
        lastCommand = rng() < 0.5;
        acks.push(env.popup({ kind: "set_paused", paused: lastCommand }));
      } else if (roll < 0.75) {
        const next = held.find((h) => !h.done);
        if (next) {
          next.done = true;
          next.resolve();
        }
      } else {
        // Drive the newest port's handshake, then try to read through it.
        const port = env.port();
        if (port && !port.disconnected) {
          port.deliver({ v: 1, type: "host_status", state: "hub_connected" });
          port.deliver({ v: 1, type: "welcome", connection_id: (++cid).toString(16).padStart(32, "0"), limits: {} });
          port.deliver(request("list_tabs", { connection_id: cid.toString(16).padStart(32, "0") }));
        }
      }
      await settle(rng() < 0.5 ? 1 : 4);
      await assertPausedMeansDead(env, `seed ${seed} step ${step}`);
    }
    for (let guard = 0; guard < 300 && held.some((h) => !h.done); guard++) {
      const next = held.find((h) => !h.done);
      next.done = true;
      next.resolve();
      await settle(4);
      await assertPausedMeansDead(env, `seed ${seed} drain`);
    }
    await Promise.all(acks);
    await settle();
    assert.equal(env.storage.companionPaused === true, lastCommand, `seed ${seed}: the last command is what persisted`);
    // The random handshakes above may re-send a welcome to a READY port -- a
    // protocol violation the worker answers with teardown + a retry timer --
    // so let every pending reconnect fire before counting (paused: none may).
    env.runTimers();
    env.fireAlarm();
    await settle();
    await assertPausedMeansDead(env, `seed ${seed} after timers`);
    const alive = env.ports.filter((p) => !p.disconnected).length;
    assert.equal(alive, lastCommand ? 0 : 1, `seed ${seed}: live ports`);
    const restarted = makeEnv({ storage: env.storage });
    await settle();
    assert.equal(restarted.ports.length, lastCommand ? 0 : 1, `seed ${seed}: a restart agrees with the persisted switch`);
  }
});

// ── R2 / B2: the site grant covers the read's whole interval ───────────

const GRANT = "https://www.reddit.com/*";
const READ_KINDS = ["extract_text", "get_links"];
// The worker's revoke answer, every field pinned (R5 / AR5): ok ONLY when
// reads were invalidated, the block is saved, AND Chrome's grant was seen
// gone afterwards. The site stays blocked until the owner allows it (R5.1).
const REVOKE_OK = { ok: true, invalidated: true, blocked: true, chrome_access: false, remove_result: true };
const ALLOW = { kind: "allow_site", pattern: "https://www.reddit.com/*" }; // the popup's Allow, after Chrome granted
const canaryPages = () => {
  const pages = PAGES();
  pages[7] = { href: "https://www.reddit.com/r/AgentsInteractive/", title: "CANARY_TITLE",
    text: "CANARY_REVOKED_TEXT", anchors: [{ href: "https://canary.test/CANARY_LINK", text: "CANARY_LINK_TEXT" }] };
  return pages;
};
const readNow = (env, details) =>
  [{ documentId: "doc-7", frameId: 0, result: runInPage(details.func, details.args, env.pages[7]) }];
const assertNoCanary = (port, label) => assert.doesNotMatch(JSON.stringify(port.sent), /CANARY/, label);

test("R2/B2 (Goblin's R2-1): a grant withdrawn after a SUCCESSFUL injection voids the read", async () => {
  for (const op of READ_KINDS) {
    for (const event of [false, true]) { // false: Chrome's onRemoved not delivered yet
      const env = makeEnv({ pages: canaryPages() });
      const port = await toReady(env);
      env.injectionHook = async (details) => {
        const result = readNow(env, details); // the script has run and read the page
        env.revoke(GRANT, { event });
        return result;
      };
      const res = await single(port, request(op, { tab_id: 7 }));
      assert.equal(res.ok, false, `${op} event=${event}`);
      assert.equal(res.error.code, "site_access_required", `${op} event=${event}`);
      assert.equal(res.error.message, "site access not granted: the owner must allow this site in the Lumina Companion popup");
      assertNoCanary(port, `${op} event=${event}`);
    }
  }
});

test("R2/B2: withdrawn and re-granted during the read is still void -- authorization is for the read's own interval", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      env.revoke(GRANT);
      env.granted.add(GRANT); // the owner grants it again before the read ends
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", op);
    assertNoCanary(port, op);
    env.injectionHook = null;
    const fresh = await single(port, request(op, { tab_id: 7 }));
    assert.equal(fresh.ok, true, `${op}: a NEW read under the re-granted access succeeds`);
  }
});

test("R2/B2: revoked after injection, before the document re-check", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    let calls = 0;
    env.tabsGetHook = () => {
      if (++calls === 2) env.revoke(GRANT, { event: false }); // the post-injection tab lookup
      return undefined;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", op);
    assertNoCanary(port, op);
  }
});

test("R2/B2: revoked after the final grant check, before the send -- caught in the send's own tick", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    let calls = 0;
    env.containsHook = () => {
      if (++calls === 2) queueMicrotask(() => env.revoke(GRANT)); // lands after the check has answered "granted"
      return undefined;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(calls, 2, "pre-read check and commit check");
    assert.equal(res.error.code, "site_access_required", op);
    assertNoCanary(port, op);
  }
});

test("R2/B2: the commit gate is bound to the OBSERVED document's origin, not another tab's", async () => {
  const pages = PAGES();
  pages[10] = { href: "https://github.com/", title: "CANARY_GH", text: "CANARY_GITHUB", anchors: [] };
  const env = makeEnv({ pages, granted: [GRANT, "https://github.com/*"] });
  const port = await toReady(env);
  env.injectionHook = async (details) => {
    const result = [{ documentId: "doc-10", frameId: 0, result: runInPage(details.func, details.args, env.pages[10]) }];
    env.revoke("https://github.com/*", { event: false }); // the ACTIVE tab (reddit) stays granted
    return result;
  };
  const res = await single(port, request("extract_text", { tab_id: 10 }));
  assert.equal(res.error.code, "site_access_required");
  assertNoCanary(port, "tab 10");
});

test("R2/B2: grant A then navigate to B, or lose A's grant and come back to A -- nothing crosses", async () => {
  for (const scenario of ["to_B", "revoke_then_back_to_A"]) {
    const env = makeEnv({ pages: canaryPages(), granted: [GRANT, "https://github.com/*"] });
    const port = await toReady(env);
    const urlA = env.tabs[0].url;
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      if (scenario === "revoke_then_back_to_A") env.revoke(GRANT);
      env.tabs[0].url = "https://github.com/"; // -> B, a new document
      env.documents[7] = "doc-7-B";
      if (scenario === "revoke_then_back_to_A") {
        env.tabs[0].url = urlA;                // -> A again, yet another document
        env.documents[7] = "doc-7-A2";
      }
      return result;
    };
    const res = await single(port, request("extract_text", { tab_id: 7 }));
    // Documented precedence: a replaced document is reported before a withdrawn grant.
    assert.equal(res.error.code, "navigated_during_request", scenario);
    assertNoCanary(port, scenario);
  }
});

test("R2/B2: PAUSE, revocation, navigation and host death together deliver nothing", async () => {
  for (const combo of [["pause", "revoke"], ["revoke", "host"], ["navigate", "revoke", "pause"], ["host", "pause"]]) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      for (const action of combo) {
        if (action === "pause") void env.popup({ kind: "set_paused", paused: true });
        if (action === "revoke") env.revoke(GRANT);
        if (action === "navigate") env.documents[7] = "doc-7-reloaded";
        if (action === "host") port.hostGone();
      }
      return result;
    };
    port.deliver(request("extract_text", { tab_id: 7 }));
    await settle(16);
    const responses = port.sent.filter((m) => m.type === "response");
    assert.ok(responses.every((r) => r.ok === false), combo.join("+"));
    if (combo.includes("pause") || combo.includes("host")) assert.equal(responses.length, 0, combo.join("+"));
    assertNoCanary(port, combo.join("+"));
  }
});

test("R2/B2: tab metadata reports the live grant state; it never carries page content", async () => {
  const env = makeEnv({ pages: canaryPages() });
  const port = await toReady(env);
  assert.equal((await single(port, request("get_tab", { tab_id: 7 }))).result.site_access, "granted");
  env.revoke(GRANT);
  const after = await single(port, request("get_tab", { tab_id: 7 }));
  assert.equal(after.result.site_access, "not_granted");
  assert.equal(env.injections.length, 0);
  assertNoCanary(port, "metadata");
});

test("R2 (Goblin's deadline survivor): a read that completes after its deadline is refused, not delivered", async () => {
  const env = makeEnv({ pages: canaryPages() });
  const port = await toReady(env);
  const injection = deferred();
  env.injectionHook = () => injection.promise;
  const req = request("extract_text", { tab_id: 7, deadline_ms: Date.now() + 30 });
  port.deliver(req);
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 60));
  injection.resolve(LATE_PAGE("CANARY_LATE"));
  await settle(12);
  const [res] = port.sent.filter((m) => m.type === "response");
  assert.equal(res.error.code, "deadline_expired");
  assertNoCanary(port, "deadline");
});

// ── R2 / P1: a request id gets one life ────────────────────────────────

test("R2/P1: a request id executes at most once for the connection's whole life -- far past the old 1024 cache", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const sent = [];
  for (let i = 0; i < 3000; i++) {
    const req = request("list_tabs");
    sent.push(req);
    port.deliver(req);
    if (i % 250 === 0) await settle();
  }
  await settle(12);
  assert.equal(env.queries, 3000);
  const answered = (req) => port.sent.filter((m) => m.type === "response" && m.request_id === req.request_id).length;
  for (const index of [0, 1, 1022, 1023, 1024, 1025, 2998, 2999]) {
    port.deliver({ ...sent[index] }); // replay: oldest ... just past the old cache boundary ... newest
  }
  await settle(12);
  assert.equal(env.queries, 3000, "no replay executed");
  for (const index of [0, 1, 1022, 1023, 1024, 1025, 2998, 2999]) assert.equal(answered(sent[index]), 1, `id #${index}`);
});

test("R2/P1: replays after a failure, a timeout, a stale answer, and an out-of-order id are never executed", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const failed = request("extract_text", { args: { max_chars: 0 } });
  const expired = request("list_tabs", { deadline_ms: Date.now() - 1 });
  await single(port, failed);
  await single(port, expired);
  const later = request("list_tabs");
  const earlier = { ...request("list_tabs"), request_id: ridFor(seq - 5) }; // lower than one already accepted
  await single(port, later);
  for (const replay of [failed, expired, later, earlier]) {
    port.deliver({ ...replay });
  }
  await settle(12);
  assert.equal(env.queries, 1, "only `later` ever executed");
  assert.equal(port.sent.filter((m) => m.request_id === earlier.request_id).length, 0,
    "an id lower than the connection's sequence is never executed or answered");
  for (const req of [failed, expired, later]) {
    assert.equal(port.sent.filter((m) => m.request_id === req.request_id).length, 1);
  }
});

test("R2/P1: after PAUSE/RESUME or a reconnect, an old id cannot run -- it names a dead connection", async () => {
  for (const how of ["pause", "reconnect"]) {
    const env = makeEnv();
    const port1 = await toReady(env);
    const old = request("list_tabs");
    await single(port1, old);
    if (how === "pause") {
      await env.popup({ kind: "set_paused", paused: true });
      port1.deliver({ ...old }); // on the dead port
      await settle();
      await env.popup({ kind: "set_paused", paused: false });
    } else {
      port1.hostGone();
      await settle();
      env.runTimers();
    }
    const port2 = await toReady(env, CID2);
    // Replayed with a sequence number higher than anything port2 has seen.
    const replay = { ...old, request_id: ridFor(9999) };
    const res = await single(port2, replay);
    assert.equal(res.error.code, "wrong_connection", how);
    assert.equal(env.queries, 1, `${how}: the old request never ran again`);
    assert.equal((await single(port2, { ...old, connection_id: CID2 })).ok, true,
      `${how}: the fresh connection starts its own sequence`);
  }
});

// ── R2 / P2: a noncanonical tab URL is not a readable surface ──────────

test("R2/P2: a backslash-spelled restricted host reported by the tab API is never read", async () => {
  // WHATWG (Chrome, policy.js) ends the authority at the backslash: every one
  // of these IS accounts.google.com to the browser, whatever urlsplit thinks.
  for (const url of ["https://accounts.google.com\\@evil.test/", "https:\\\\accounts.google.com\\x",
    "https://accounts.google.com:443\\@evil.test/", "https://u:p@accounts.google.com\\\\@evil.test/p"]) {
    assert.equal(new URL(url).hostname, "accounts.google.com", url);
    const tabs = TABS();
    tabs[0].url = url;
    const pages = PAGES();
    pages[7] = { href: url, title: "t", text: "CANARY_BACKSLASH", anchors: [] };
    const env = makeEnv({ tabs, pages, granted: [GRANT, "https://evil.test/*", "https://accounts.google.com/*"] });
    const port = await toReady(env);
    for (const op of READ_KINDS) {
      assert.equal((await single(port, request(op, { tab_id: 7 }))).error.code, "restricted_surface", `${op} ${url}`);
    }
    const listed = (await single(port, request("list_tabs"))).result.tabs.find((t) => t.tab_id === 7);
    assert.equal(listed.restricted, true, url);
    assert.equal(listed.url, null, url);
    assert.equal(env.injections.length, 0, url);
    assertNoCanary(port, url);
  }
});


// ── R3 / B2-R3: revoke ordering. Chrome gives no ordering between a
// revocation's onRemoved and a LATER contains() that sees a re-grant
// (permissions_updater.cc dispatches the event after an async network-service
// update). Popup revokes are ordered by the worker itself; outside revokes
// that are undone before Chrome announces them are a documented boundary. ──

test("R3 (Goblin's AR3 schedule) via the POPUP: revoke, re-grant, onRemoved held past the response -- nothing crosses", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    env.holdEvents = true;
    const port = await toReady(env);
    env.injectionHook = async (details) => {
      const result = readNow(env, details); // the page script has run and read the canary
      assert.deepEqual(plain(await env.popupRevoke(GRANT)), REVOKE_OK);
      env.granted.add(GRANT); // the owner re-grants before the read ends
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.ok, false, op);
    assert.equal(res.error.code, "site_access_required", op);
    assertNoCanary(port, op);
    assert.equal(env.releaseEvents(), 1, "Chrome's onRemoved was still held when the answer went out");
    assert.equal(env.removeCalls.length, 1, "the WORKER asked Chrome to remove the grant");
  }
});

test("R3 CONTRACT BOUNDARY (Goblin's AR3 schedule, OUTSIDE revoke): revoked and re-granted outside the extension, onRemoved not yet delivered -- not observable, not detected", async () => {
  // Documented, deliberate: when a grant is withdrawn in chrome://extensions or
  // the toolbar menu and restored BEFORE the post-read check, and Chrome has
  // not delivered onRemoved yet, no extension API reveals that it happened:
  // contains() already says granted, the event is still in Chrome. The worker
  // cannot fail closed on what it cannot see. What does hold: Chrome refuses
  // an injection while the grant is absent, and the post-read check sees the
  // grant in force. This test pins that boundary so it cannot silently widen
  // (or be claimed closed) -- see the top of worker.js and the R3 report.
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      env.revoke(GRANT, { event: false });
      env.granted.add(GRANT);
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.ok, true, `${op}: the boundary case publishes (documented)`);
    assert.match(JSON.stringify(res), /CANARY/, op);
    assert.equal(res.observed.document_id, "doc-7", "bound to the one document read");
    env.listeners.permissionRemoved.forEach((fn) => fn({ origins: [GRANT], permissions: [] }));
    const next = await single(port, request(op, { tab_id: 7 }));
    assert.equal(next.ok, true, `${op}: a late event voids reads in flight, not the re-granted site`);
  }
});

test("R3: the same outside revoke IS caught whenever Chrome has told the worker, or it is still in effect", async () => {
  for (const op of READ_KINDS) {
    for (const when of ["event_before_send", "still_revoked"]) {
      const env = makeEnv({ pages: canaryPages() });
      const port = await toReady(env);
      env.injectionHook = async (details) => {
        const result = readNow(env, details);
        env.revoke(GRANT, { event: when === "event_before_send" });
        if (when === "event_before_send") env.granted.add(GRANT);
        return result;
      };
      const res = await single(port, request(op, { tab_id: 7 }));
      assert.equal(res.error.code, "site_access_required", `${op} ${when}`);
      assertNoCanary(port, `${op} ${when}`);
    }
  }
});

test("R3: a popup revoke arriving at ANY point of the read voids it -- before injection it is never even dispatched", async () => {
  const points = ["pre_check", "document_lookup", "injection", "post_tab_lookup", "post_check"];
  for (const op of READ_KINDS) {
    for (const point of points) {
      const env = makeEnv({ pages: canaryPages() });
      env.holdEvents = true;
      const port = await toReady(env);
      let revoked = null;
      const revokeHere = () => {
        if (revoked) return;
        revoked = env.popupRevoke(GRANT).then(() => env.granted.add(GRANT)); // ...and a quick re-grant
      };
      let containsCalls = 0;
      env.containsHook = () => {
        containsCalls += 1;
        if ((point === "pre_check" && containsCalls === 1) || (point === "post_check" && containsCalls === 2)) revokeHere();
        return undefined; // answered from the state BEFORE this revoke
      };
      env.frameHook = () => { if (point === "document_lookup") revokeHere(); return undefined; };
      let tabGets = 0;
      env.tabsGetHook = () => { if (++tabGets === 2 && point === "post_tab_lookup") revokeHere(); return undefined; };
      env.injectionHook = async (details) => {
        const result = readNow(env, details);
        if (point === "injection") revokeHere();
        return result;
      };
      const res = await single(port, request(op, { tab_id: 7 }));
      await revoked;
      assert.equal(res.error.code, "site_access_required", `${op} @${point}`);
      assertNoCanary(port, `${op} @${point}`);
      if (point === "pre_check" || point === "document_lookup") {
        assert.equal(env.injections.length, 0, `${op} @${point}: revoke means no read -- nothing was injected`);
      }
    }
  }
});

test("R3: no read commits -- or injects -- while Chrome has not confirmed a popup revoke; once it has, the site is read afresh only after a re-grant", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    const chromeRemoval = deferred();
    env.removeHook = () => chromeRemoval.promise; // Chrome still busy removing
    const revoking = env.popupRevoke(GRANT);
    await settle();
    const during = await single(port, request(op, { tab_id: 7 }));
    assert.equal(during.error.code, "site_access_required", `${op}: removal pending`);
    assert.equal(env.injections.length, 0, `${op}: nothing injected while the removal is pending`);
    chromeRemoval.resolve();
    assert.deepEqual(plain(await revoking), REVOKE_OK);
    const after = await single(port, request(op, { tab_id: 7 }));
    assert.equal(after.error.code, "site_access_required", `${op}: removed`);
    env.granted.add(GRANT);
    assert.equal((await single(port, request(op, { tab_id: 7 }))).error.code, "site_access_required",
      `${op}: Chrome granting it again is not the owner allowing it (R5.1)`);
    assert.deepEqual(plain(await env.popup(ALLOW)), { ok: true });
    const regranted = await single(port, request(op, { tab_id: 7 }));
    assert.equal(regranted.ok, true, `${op}: a NEW read under a new grant works`);
  }
});

test("R3/R5: a read that begins while a popup revoke is pending is refused at its FIRST check -- the site is blocked from the worker's receipt -- and never injected; once Chrome completes and the owner allows it again, a NEW read works", async () => {
  // R3 caught this read at its injection gate (grantHeld). Since R5 the
  // block set at receipt refuses it before it even asks Chrome.
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    env.holdEvents = true; // so only the worker's own bookkeeping can catch it
    const port = await toReady(env);
    const chromeRemoval = deferred();
    env.removeHook = () => chromeRemoval.promise;
    let containsCalls = 0;
    env.containsHook = () => { containsCalls += 1; return undefined; };
    const revoking = env.popupRevoke(GRANT);
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", op);
    assert.equal(containsCalls, 0, `${op}: refused by the block, before asking Chrome`);
    assert.equal(env.injections.length, 0, `${op}: never dispatched into the page`);
    assertNoCanary(port, op);
    chromeRemoval.resolve();
    assert.deepEqual(plain(await revoking), REVOKE_OK);
    env.granted.add(GRANT); // Chrome grants the site again...
    assert.equal((await single(port, request(op, { tab_id: 7 }))).error.code, "site_access_required",
      `${op}: ...which is not the owner allowing it (R5.1)`);
    assert.deepEqual(plain(await env.popup(ALLOW)), { ok: true }); // the owner's Allow
    assert.equal((await single(port, request(op, { tab_id: 7 }))).ok, true, `${op}: a NEW read under the new grant`);
  }
});

test("R3: a popup revoke Chrome refuses still voids the reads it overlapped, and the popup is told", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    env.removeHook = () => Promise.reject(new Error("Chrome said no"));
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      assert.deepEqual(plain(await env.popupRevoke(GRANT)), { ok: false, invalidated: true, blocked: true,
        chrome_access: null, remove_result: null, error: "site_access_unconfirmed" });
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", op);
    assertNoCanary(port, op);
  }
});

test("R3: popup revoke combined with PAUSE, navigation, document replacement, host loss, reconnect, and re-grant + PAUSE -- no page content ever", async () => {
  const combos = [["revoke", "pause"], ["revoke", "navigate"], ["revoke", "reload"], ["revoke", "host"],
    ["revoke", "reconnect"], ["revoke", "regrant", "pause"], ["pause", "revoke", "regrant"],
    ["navigate", "revoke", "regrant"], ["host", "revoke", "regrant"]];
  for (const held of [true, false]) {
    for (const combo of combos) {
      for (const op of READ_KINDS) {
        const label = `${combo.join("+")} ${op} events ${held ? "held" : "prompt"}`;
        const env = makeEnv({ pages: canaryPages() });
        env.holdEvents = held;
        const port = await toReady(env);
        env.injectionHook = async (details) => {
          const result = readNow(env, details);
          for (const action of combo) {
            if (action === "revoke") await env.popupRevoke(GRANT);
            if (action === "regrant") env.granted.add(GRANT);
            if (action === "pause") void env.popup({ kind: "set_paused", paused: true });
            if (action === "navigate") { env.tabs[0].url = "https://github.com/"; env.documents[7] = "doc-7-B"; }
            if (action === "reload") env.documents[7] = "doc-7-reloaded";
            if (action === "host") port.hostGone();
            if (action === "reconnect") {
              port.hostGone();
              env.runTimers();
              await settle();
              env.port().deliver({ v: 1, type: "host_status", state: "hub_connected" });
              env.port().deliver({ v: 1, type: "welcome", connection_id: CID2, limits: {} });
            }
          }
          return result;
        };
        port.deliver(request(op, { tab_id: 7 }));
        await settle(24);
        for (const p of env.ports) {
          assert.ok(p.sent.filter((m) => m.type === "response").every((r) => r.ok === false), label);
          assertNoCanary(p, label);
        }
      }
    }
  }
});

test("R3: only the extension's own popup can revoke through the worker, and only a site pattern", async () => {
  const env = makeEnv({ pages: canaryPages() });
  await toReady(env);
  const popupUrl = env.chrome.runtime.getURL("popup.html");
  const senders = [{ id: EXT_ID, url: popupUrl, tab: { id: 7 } }, { id: "x".repeat(32), url: popupUrl },
    { id: EXT_ID, url: `chrome-extension://${EXT_ID}/other.html` }];
  for (const sender of senders) {
    assert.equal(await env.popup({ kind: "revoke_site", pattern: GRANT }, sender), "NO_RESPONSE");
  }
  for (const pattern of [null, 7, "", "<all_urls>", "https://*/*", "*://www.reddit.com/*", "https://www.reddit.com/x/*",
    "https://accounts.google.com/*", "file:///*", "https://www.reddit.com/*".repeat(20)]) {
    assert.equal(await env.popup({ kind: "revoke_site", pattern }), "NO_RESPONSE", String(pattern));
  }
  assert.equal(env.removeCalls.length, 0);
  assert.ok(env.granted.has(GRANT));
  assert.deepEqual(plain(await env.popupRevoke(GRANT)), REVOKE_OK);
  assert.deepEqual(plain(env.removeCalls), [[GRANT]]);
});

// ── The popup itself (popup.js, run for real against a fake DOM) ────────

const POPUP_SRC = fs.readFileSync(path.join(EXT_DIR, "popup.js"), "utf8");

// With `worker` (a makeEnv env), the popup talks to that REAL worker, and
// shares its Chrome permission state, over a bus whose revoke deliveries can
// be held in transit (R4 / AR4: the popup -> worker message is not instant).
// Without `worker`, the fake worker answers get_status / site_state /
// allow_site itself (site_state from chrome.permissions.blocked; allow_site
// from `allowAnswer`) and every other message with `workerAnswer`.
function makePopup({ workerAnswer, tabUrl = "https://www.reddit.com/r/AgentsInteractive/", worker = null,
  allowAnswer = async () => ({ ok: true }) } = {}) {
  const calls = { messages: [], removes: [], requests: [] };
  const elements = {};
  const element = (id) => {
    if (!elements[id]) {
      const listeners = {};
      elements[id] = { id, textContent: "", hidden: false, className: "", listeners,
        classList: { toggle() {} },
        addEventListener: (type, fn) => { listeners[type] = fn; } };
    }
    return elements[id];
  };
  const bus = { hold: false, held: [], drop: false };
  bus.release = () => bus.held.splice(0).forEach((deliver) => deliver());
  const toWorker = (message) => new Promise((resolve) => {
    const deliver = () => {
      if (bus.drop) return resolve(undefined); // no worker answered
      const sender = { id: EXT_ID, url: worker.chrome.runtime.getURL("popup.html") };
      if (worker.listeners.message[0](plain(message), sender, resolve) !== true) resolve(undefined);
    };
    if (bus.hold && message.kind === "revoke_site") bus.held.push(deliver);
    else deliver();
  });
  const chrome = {
    runtime: {
      async sendMessage(message) {
        calls.messages.push(message);
        if (worker) return toWorker(message);
        if (message.kind === "get_status") return { state: "READY", lastError: null, lastAction: null, instanceId: "a".repeat(32) };
        if (message.kind === "site_state") return { blocked: chrome.permissions.blocked === true };
        if (message.kind === "allow_site") {
          const answer = await allowAnswer(message);
          if (answer && answer.ok === true) chrome.permissions.blocked = false;
          return answer;
        }
        return workerAnswer(message);
      },
    },
    tabs: { async query() { return [{ id: 7, url: tabUrl }]; } },
    permissions: worker ? {
      contains: (details) => worker.chrome.permissions.contains(details),
      async remove(details) { calls.removes.push(details); return worker.chrome.permissions.remove(details); },
      async request(details) { calls.requests.push(details); details.origins.forEach((o) => worker.granted.add(o)); return true; },
    } : {
      granted: true,
      blocked: false, // the fake worker's block on this site (R5)
      removeThrows: false,
      async contains() { return chrome.permissions.granted; },
      async remove(details) {
        calls.removes.push(details);
        if (chrome.permissions.removeThrows) throw new Error("Chrome said no");
        chrome.permissions.granted = false;
        return true;
      },
      async request(details) { calls.requests.push(details); chrome.permissions.granted = true; return true; },
    },
    storage: { onChanged: { addListener() {} } },
  };
  const context = vm.createContext({ chrome, console, URL, navigator: {}, document: { getElementById: element } });
  vm.runInContext(POLICY_SRC, context);
  vm.runInContext(POPUP_SRC, context);
  const text = (id) => element(id).textContent;
  return { chrome, calls, elements, bus, text, run: (expression) => vm.runInContext(expression, context),
    click: async (id) => { await elements[id].listeners.click(); await settle(); } };
}

const REVOKED_NOTE = "Revoked. Lumina's companion confirmed it and discarded any read still in progress.";
const ACK = REVOKE_OK;

test("popup hides Navigation Allow when the connected worker is still the old version", async () => {
  const popup = makePopup({ workerAnswer: async () => undefined });
  await settle();
  assert.equal(popup.elements.navigation.hidden, true);
  assert.match(popup.text("navigation-state"), /Reload Lumina Chrome Companion at chrome:\/\/extensions/);
  assert.equal(popup.calls.messages.some((message) => message.kind === "set_navigation"), false);
});

test("R3 popup: Revoke asks the worker and never removes the grant behind its back", async () => {
  const popup = makePopup({ workerAnswer: async () => {
    popup.chrome.permissions.granted = false; // the worker had Chrome remove it
    return ACK;
  } });
  await settle();
  assert.equal(popup.elements.revoke.hidden, false, "granted site shows Revoke");
  await popup.click("revoke");
  assert.deepEqual(plain(popup.calls.messages.filter((m) => m.kind === "revoke_site")),
    [{ kind: "revoke_site", pattern: "https://www.reddit.com/*" }]);
  assert.equal(popup.calls.removes.length, 0, "the popup did not call permissions.remove itself");
  assert.equal(popup.text("tab-state"), "No site access");
  assert.equal(popup.text("revoke-note"), REVOKED_NOTE);
});

test("R3/R5 popup: a revoke Chrome did not complete is reported -- the site blocked by the companion, never 'Revoked.' -- and not retried behind the worker", async () => {
  const popup = makePopup({ workerAnswer: async () => {
    popup.chrome.permissions.blocked = true; // the worker blocked the site at receipt and kept it
    return { ok: false, invalidated: true, blocked: true, chrome_access: true, remove_result: false,
      error: "site_access_remove_failed" };
  } });
  await settle();
  await popup.click("revoke");
  assert.equal(popup.calls.removes.length, 0);
  assert.equal(popup.text("revoke-note"), "Companion access blocked. Chrome's site permission could not be removed — try again.");
  assert.equal(popup.elements["revoke-note"].className, "warn");
  assert.equal(popup.text("tab-state"), "Blocked by Lumina's companion — Chrome allows this site");
  assert.equal(popup.elements.revoke.hidden, false, "the owner can retry the removal");
  assert.equal(popup.elements.grant.hidden, false, "...or explicitly allow the site again");
});

test("R3 popup: with no worker to answer, the owner's revoke is never blocked", async () => {
  for (const workerAnswer of [async () => undefined, async () => { throw new Error("Could not establish connection"); }]) {
    const popup = makePopup({ workerAnswer });
    await settle();
    await popup.click("revoke");
    assert.deepEqual(plain(popup.calls.removes), [{ origins: ["https://www.reddit.com/*"] }]);
  }
});

// ── R4 / AR4: the click is not the revoke. Popup and worker are separate
// contexts; the strict boundary is the WORKER'S RECEIPT of the popup's
// message, and the popup claims the revoke only on the worker's
// acknowledgement. Without one it says so -- never "Revoked". ──

test("R4 (Goblin's AR4 schedule, retained): message held in transit -- a read that commits before the worker receives it is inside the contract, and the popup claims NOTHING until the acknowledgement", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    env.holdEvents = true;
    const port = await toReady(env);
    const popup = makePopup({ worker: env });
    await settle();
    assert.equal(popup.elements.revoke.hidden, false, `${op}: granted site shows Revoke`);
    const chromeRemoval = deferred();
    env.removeHook = () => chromeRemoval.promise; // Chrome will be slow to remove, too
    // 1. A read is in the page; the owner clicks Revoke; the popup's message
    //    is held in transit (Goblin's schedule).
    let clicking = null;
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      popup.bus.hold = true;
      clicking = popup.elements.revoke.listeners.click();
      return result;
    };
    const early = await single(port, request(op, { tab_id: 7 }));
    assert.ok(clicking, "the owner clicked while the read was in the page");
    assert.equal(early.ok, true, `${op}: committed before the worker received the revoke -- inside the stated contract`);
    assert.match(JSON.stringify(early), /CANARY/);
    assert.equal(popup.bus.held.length, 1, "the revoke is still in transit");
    assert.equal(env.removeCalls.length, 0, "and nobody has asked Chrome yet");
    const revokingShown = () => {
      assert.equal(popup.text("tab-state"), "Revoking…", `${op}: REVOKING…`);
      assert.equal(popup.text("revoke-note"), "Waiting for Lumina's companion to confirm…", op);
      assert.equal(popup.elements.revoke.hidden, true, `${op}: no second revoke`);
      assert.equal(popup.elements.grant.hidden, true, `${op}: no grant while revoking`);
    };
    revokingShown();
    // 2. Another read is in the page when the message finally arrives. From
    //    that receipt it is void -- though Chrome still holds the grant.
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      popup.bus.release();
      assert.equal(env.peek("grantHeld(grantRevocations)"), false, "void in the very dispatch that delivered it");
      return result;
    };
    const late = await single(port, request(op, { tab_id: 7 }));
    assert.equal(late.error.code, "site_access_required", `${op}: a read in progress at the receipt never publishes`);
    assert.ok(env.granted.has(GRANT), "Chrome had not removed the grant: the worker's receipt alone voided it");
    assert.equal(port.sent.filter((m) => /CANARY/.test(JSON.stringify(m))).length, 1, `${op}: only the pre-receipt read`);
    revokingShown(); // received, but Chrome has not finished: still no claim
    // 3. Chrome completes; the worker answers; only now does the popup say Revoked.
    chromeRemoval.resolve();
    await clicking;
    await settle();
    assert.equal(popup.text("tab-state"), "No site access", op);
    assert.equal(popup.text("revoke-note"), REVOKED_NOTE, op);
    assert.equal(popup.elements["revoke-note"].className, "muted");
    assert.equal(popup.calls.removes.length, 0, `${op}: the popup never removed the grant itself`);
    assert.deepEqual(plain(env.removeCalls), [[GRANT]], `${op}: the worker did`);
    // ...and a quick re-grant reads afresh; nothing from before it.
    env.injectionHook = null;
    await popup.click("grant");
    assert.equal(popup.text("revoke-note"), "", "a new grant clears the old revoke note");
    assert.equal((await single(port, request(op, { tab_id: 7 }))).ok, true, `${op}: a NEW read under the new grant`);
  }
});

test("R4: the acknowledgement point is synchronous -- the worker has voided every read before its dispatch of the revoke returns", async () => {
  const env = makeEnv({ pages: canaryPages() });
  await toReady(env);
  const chromeRemoval = deferred();
  env.removeHook = () => chromeRemoval.promise;
  assert.equal(env.peek("grantHeld(grantRevocations)"), true);
  const answer = env.popupRevoke(GRANT); // the listener runs to its return, nothing awaited
  assert.equal(env.peek("revocationsPending"), 1);
  assert.equal(env.peek("grantHeld(grantRevocations)"), false);
  chromeRemoval.resolve();
  assert.deepEqual(plain(await answer), ACK);
  assert.equal(env.peek("revocationsPending"), 0);
});

test("R4: while the worker has not answered the popup shows REVOKING…, never Revoked, and ignores further site clicks", async () => {
  const answer = deferred();
  const popup = makePopup({ workerAnswer: () => answer.promise });
  await settle();
  const clicking = popup.elements.revoke.listeners.click();
  await settle();
  assert.equal(popup.text("tab-state"), "Revoking…");
  assert.equal(popup.text("revoke-note"), "Waiting for Lumina's companion to confirm…");
  await popup.elements.revoke.listeners.click(); // a second click on the hidden button
  popup.elements.grant.listeners.click();
  await settle();
  assert.equal(popup.calls.messages.filter((m) => m.kind === "revoke_site").length, 1, "one revoke message");
  assert.equal(popup.calls.requests.length, 0, "no grant prompt while revoking");
  assert.equal(popup.calls.removes.length, 0);
  answer.resolve(ACK);
  await clicking;
  await settle();
  assert.equal(popup.text("revoke-note"), REVOKED_NOTE);
});

test("R4: a re-render while revoking (the tab re-checked) keeps REVOKING… and offers no site control", async () => {
  const answer = deferred();
  const popup = makePopup({ workerAnswer: () => answer.promise });
  await settle();
  const clicking = popup.elements.revoke.listeners.click();
  await settle();
  await popup.run("render()");
  assert.equal(popup.text("tab-state"), "Revoking…");
  assert.equal(popup.elements.revoke.hidden, true);
  assert.equal(popup.elements.grant.hidden, true);
  assert.equal(popup.text("revoke-note"), "Waiting for Lumina's companion to confirm…");
  answer.resolve(ACK);
  await clicking;
});

test("R4 fallback: no acknowledgement -- the popup removes the grant itself and says the companion did NOT confirm; never 'Revoked'", async () => {
  const unacknowledged = [
    async () => undefined,
    async () => null,
    async () => { throw new Error("Could not establish connection. Receiving end does not exist."); },
    async () => ({ ok: true }), // an answer that does not acknowledge invalidation is not one
    async () => ({ ok: true, invalidated: "yes" }),
  ];
  for (const [i, workerAnswer] of unacknowledged.entries()) {
    const popup = makePopup({ workerAnswer });
    await settle();
    await popup.click("revoke");
    assert.deepEqual(plain(popup.calls.removes), [{ origins: ["https://www.reddit.com/*"] }], `case ${i}`);
    assert.equal(popup.text("tab-state"), "No site access", `case ${i}`);
    assert.match(popup.text("revoke-note"), /^Site access removed in Chrome, but Lumina's companion did not confirm the revoke/, `case ${i}`);
    assert.match(popup.text("revoke-note"), /may still have reached Lumina/, `case ${i}`);
    assert.doesNotMatch(popup.text("revoke-note"), /^Revoked/, `case ${i}`);
    assert.equal(popup.elements["revoke-note"].className, "warn", `case ${i}: shown as the weaker outcome`);
  }
});

test("R4 fallback: when the popup's own removal fails too, it says so", async () => {
  const popup = makePopup({ workerAnswer: async () => undefined });
  popup.chrome.permissions.removeThrows = true;
  await settle();
  await popup.click("revoke");
  assert.equal(popup.calls.removes.length, 1);
  assert.equal(popup.text("tab-state"), "Readable by Lumina (read-only)");
  assert.equal(popup.text("revoke-note"), "Site access could not be removed — try again.");
  assert.equal(popup.elements["revoke-note"].className, "warn");
});

test("R4 fallback against the REAL worker: a revoke that never reaches it has only outside-revoke strength, and the popup says exactly that", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    env.holdEvents = true; // so only the post-read grant check can catch it
    const port = await toReady(env);
    const popup = makePopup({ worker: env });
    await settle();
    popup.bus.drop = true; // the message is lost: no worker acknowledgement
    let clicking = null;
    env.injectionHook = async (details) => {
      const result = readNow(env, details);
      clicking = popup.elements.revoke.listeners.click();
      await clicking; // the popup removed the grant itself before this read's post-check
      return result;
    };
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", `${op}: still in effect at the post-read check -- caught`);
    assertNoCanary(port, op);
    assert.equal(popup.calls.removes.length, 1, `${op}: the popup removed it`);
    assert.match(popup.text("revoke-note"), /did not confirm/, op);
    assert.equal(env.peek("revocationsPending"), 0, `${op}: the worker never heard of it`);
  }
});

test("R3 popup: Grant is unchanged -- Chrome's own prompt, from the click itself", async () => {
  const popup = makePopup({ workerAnswer: async () => ({ ok: true }) });
  popup.chrome.permissions.granted = false;
  await settle();
  await popup.click("grant");
  assert.deepEqual(plain(popup.calls.requests), [{ origins: ["https://www.reddit.com/*"] }]);
});

// ── R5 / AR5: Chrome's answer is CHECKED. remove()'s boolean is reported as
// given but decides nothing; the postcondition -- does Chrome still grant the
// site? (contains) -- decides. "Revoked." / ok:true only when reads were
// invalidated AND the grant is seen gone. Otherwise the worker keeps the site
// BLOCKED (from its receipt, durably) until the owner allows it in the popup. ──

const R5_CASES = {
  // label: [remove behaviour, contains behaviour for the postcondition, pinned answer]
  "remove true / access absent": ["remove", "real",
    { ok: true, invalidated: true, blocked: true, chrome_access: false, remove_result: true }],
  "remove false / access PRESENT (AR5)": ["false_keep", "real",
    { ok: false, invalidated: true, blocked: true, chrome_access: true, remove_result: false,
      error: "site_access_remove_failed" }],
  "remove false / access absent": ["false_gone", "real",
    { ok: true, invalidated: true, blocked: true, chrome_access: false, remove_result: false }],
  "remove throws": ["throw", "real",
    { ok: false, invalidated: true, blocked: true, chrome_access: null, remove_result: null,
      error: "site_access_unconfirmed" }],
  "remove true / contains throws": ["true_keep", "throw",
    { ok: false, invalidated: true, blocked: true, chrome_access: null, remove_result: true,
      error: "site_access_unconfirmed" }],
  "remove true / access PRESENT": ["true_keep", "real",
    { ok: false, invalidated: true, blocked: true, chrome_access: true, remove_result: true,
      error: "site_access_remove_failed" }],
  "remove non-boolean / access absent": ["undefined_gone", "real",
    { ok: true, invalidated: true, blocked: true, chrome_access: false, remove_result: null }],
};

function r5Chrome(env, removal, postcondition) {
  env.removeOverride = {
    remove: null,
    false_keep: async () => false,
    false_gone: async (origins) => { origins.forEach((o) => env.granted.delete(o)); return false; },
    true_keep: async () => true,
    undefined_gone: async (origins) => { origins.forEach((o) => env.granted.delete(o)); return undefined; },
    throw: async () => { throw new Error("Chrome said no"); },
  }[removal];
  if (postcondition === "throw") {
    // Only the revoke's own postcondition read throws; the popup's and the
    // reads' contains() keep answering.
    env.containsHook = () => {
      if (env.removeCalls.length > 0 && !env.postconditionThrown) {
        env.postconditionThrown = true;
        throw new Error("Chrome could not say");
      }
      return undefined;
    };
  }
}

const R5_NOTES = {
  site_access_remove_failed: "Companion access blocked. Chrome's site permission could not be removed — try again.",
  site_access_unconfirmed: "Companion access blocked. Chrome did not confirm removing its site permission — try again.",
};

test("R5 (Goblin's AR5, exact): remove() resolves false and Chrome keeps the grant -- no success answer, no 'Revoked.', the site stays blocked, and the canary never crosses on a later read -- not after the revoke ends, not after a worker restart", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    assert.equal((await single(port, request(op, { tab_id: 7 }))).ok, true, `${op}: readable before`);
    const mark = port.sent.length; // everything sent from here on must be canary-free
    const popup = makePopup({ worker: env });
    await settle();
    env.removeOverride = async () => false; // Chrome: "not removed" -- and the grant stays
    await popup.click("revoke");
    assert.deepEqual(plain(env.removeCalls), [[GRANT]], "the worker asked Chrome");
    assert.ok(env.granted.has(GRANT), "Chrome still grants the site");
    assert.equal(popup.text("revoke-note"), R5_NOTES.site_access_remove_failed, op);
    assert.equal(popup.elements["revoke-note"].className, "warn");
    assert.doesNotMatch(popup.text("revoke-note") + popup.text("tab-state"), /Revoked/, `${op}: no UI lie`);
    assert.equal(popup.text("tab-state"), "Blocked by Lumina's companion — Chrome allows this site");
    assert.equal(env.peek("revocationsPending"), 0, "the revoke has ended");
    for (let i = 0; i < 3; i++) { // later reads, pending long over
      await settle();
      const res = await single(port, request(op, { tab_id: 7 }));
      assert.equal(res.error.code, "site_access_required", `${op}: blocked on read ${i}`);
    }
    const listed = (await single(port, request("list_tabs"))).result.tabs.find((t) => t.tab_id === 7);
    assert.equal(listed.site_access, "not_granted", "listed as not granted, whatever Chrome holds");
    assert.doesNotMatch(JSON.stringify(port.sent.slice(mark)), /CANARY/, `${op}: nothing crossed after the revoke`);
    assert.deepEqual(plain(env.storage.companionBlockedSites), [GRANT], "the block is on disk");
    // The service worker is restarted (Chrome stops idle workers): a fresh
    // worker on the same storage, Chrome still granting.
    const restarted = makeEnv({ pages: canaryPages(), storage: env.storage, granted: [GRANT] });
    const port2 = await toReady(restarted);
    const after = await single(port2, request(op, { tab_id: 7 }));
    assert.equal(after.error.code, "site_access_required", `${op}: still blocked after a restart`);
    assert.equal(restarted.injections.length, 0);
    assertNoCanary(port2, op);
  }
});

test("R5/R5.1: the revoke answer, every field pinned, for every Chrome outcome -- ok only when Chrome's grant is SEEN gone -- and in EVERY outcome the site stays blocked until the owner's Allow", async () => {
  for (const [label, [removal, postcondition, expected]] of Object.entries(R5_CASES)) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    r5Chrome(env, removal, postcondition);
    const answer = plain(await env.popupRevoke(GRANT));
    assert.deepEqual(answer, expected, label);
    assert.equal(answer.ok === true, answer.chrome_access === false, `${label}: ok iff access seen absent`);
    assert.deepEqual(plain(await env.popup({ kind: "site_state", pattern: GRANT })), { blocked: true }, label);
    assert.deepEqual(plain(env.storage.companionBlockedSites), [GRANT], `${label}: on disk`);
    env.containsHook = null;
    env.granted.add(GRANT); // whatever Chrome holds now...
    const res = await single(port, request("extract_text", { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", `${label}: ...a revoked site is never read`);
    assertNoCanary(port, label);
    assert.deepEqual(plain(await env.popup(ALLOW)), { ok: true }, label);
    assert.equal((await single(port, request("extract_text", { tab_id: 7 }))).ok, true, `${label}: the owner's Allow`);
  }
});

test("R5 popup, against the REAL worker: 'Revoked.' only for the outcomes where Chrome's grant was seen gone; every other outcome says what Chrome did not do, as a warning", async () => {
  for (const [label, [removal, postcondition, expected]] of Object.entries(R5_CASES)) {
    const env = makeEnv({ pages: canaryPages() });
    await toReady(env);
    const popup = makePopup({ worker: env });
    await settle();
    r5Chrome(env, removal, postcondition);
    await popup.click("revoke");
    if (expected.ok) {
      assert.equal(popup.text("revoke-note"), REVOKED_NOTE, label);
      assert.equal(popup.text("tab-state"), "No site access", label);
    } else {
      assert.equal(popup.text("revoke-note"), R5_NOTES[expected.error], label);
      assert.equal(popup.elements["revoke-note"].className, "warn", label);
      assert.doesNotMatch(popup.text("revoke-note"), /Revoked/, label);
      assert.equal(popup.text("tab-state"), env.granted.has(GRANT)
        ? "Blocked by Lumina's companion — Chrome allows this site" : "No site access", label);
    }
    assert.equal(popup.calls.removes.length, 0, `${label}: an acknowledged revoke is never retried behind the worker`);
  }
});

test("R5 popup: an acknowledged answer is 'Revoked.' only with ok AND chrome_access false -- any other acknowledged shape is a warning", async () => {
  const shapes = [
    { ok: true, invalidated: true },
    { ok: true, invalidated: true, chrome_access: true },
    { ok: true, invalidated: true, chrome_access: null },
    { ok: false, invalidated: true, chrome_access: false },
  ];
  for (const answer of shapes) {
    const popup = makePopup({ workerAnswer: async () => answer });
    await settle();
    await popup.click("revoke");
    assert.doesNotMatch(popup.text("revoke-note"), /Revoked/, JSON.stringify(answer));
    assert.equal(popup.elements["revoke-note"].className, "warn", JSON.stringify(answer));
    assert.equal(popup.calls.removes.length, 0, "acknowledged: never retried behind the worker");
  }
});

test("R5.1 (Sol's pin): a SUCCESSFUL revoke stays blocked through a Chrome-settings re-grant and a worker restart; only the popup's Allow makes the site readable again", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    const popup = makePopup({ worker: env });
    await settle();
    await popup.click("revoke");
    assert.equal(popup.text("revoke-note"), REVOKED_NOTE, op);
    assert.ok(!env.granted.has(GRANT), "Chrome removed it");
    const mark = port.sent.length;
    // Chrome's settings grant the site again (chrome://extensions, the toolbar menu).
    env.granted.add(GRANT);
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.error.code, "site_access_required", `${op}: a Chrome re-grant does not undo the owner's revoke`);
    const listed = (await single(port, request("list_tabs"))).result.tabs.find((t) => t.tab_id === 7);
    assert.equal(listed.site_access, "not_granted");
    await popup.run("render()");
    assert.equal(popup.text("tab-state"), "Blocked by Lumina's companion — Chrome allows this site", op);
    assert.equal(popup.elements.grant.hidden, false, `${op}: Allow offered`);
    // The worker restarts, Chrome still granting the site.
    const restarted = makeEnv({ pages: canaryPages(), storage: env.storage, granted: [GRANT] });
    const port2 = await toReady(restarted);
    assert.equal((await single(port2, request(op, { tab_id: 7 }))).error.code, "site_access_required",
      `${op}: still blocked after a restart`);
    assert.doesNotMatch(JSON.stringify(port.sent.slice(mark)) + JSON.stringify(port2.sent), /CANARY/, op);
    // The owner's explicit Allow, from the popup (Chrome's prompt, then the worker is told).
    const popup2 = makePopup({ worker: restarted });
    await settle();
    await popup2.click("grant");
    assert.deepEqual(plain(popup2.calls.requests), [{ origins: [GRANT] }]);
    assert.equal(popup2.text("tab-state"), "Readable by Lumina (read-only)", op);
    assert.deepEqual(plain(restarted.storage.companionBlockedSites), []);
    const allowed = await single(port2, request(op, { tab_id: 7 }));
    assert.equal(allowed.ok, true, `${op}: readable again, by the owner's Allow`);
    assert.match(JSON.stringify(allowed), /CANARY/);
  }
});

test("R5: failed removal -> the owner revokes again and Chrome completes it -> 'Revoked.'", async () => {
  const env = makeEnv({ pages: canaryPages() });
  const port = await toReady(env);
  const popup = makePopup({ worker: env });
  await settle();
  env.removeOverride = async () => false;
  await popup.click("revoke");
  assert.equal(popup.text("revoke-note"), R5_NOTES.site_access_remove_failed);
  assert.equal(popup.elements.revoke.hidden, false, "Revoke offered again");
  env.removeOverride = null; // Chrome removes it this time
  await popup.click("revoke");
  assert.equal(popup.text("revoke-note"), REVOKED_NOTE);
  assert.equal(popup.text("tab-state"), "No site access");
  assert.ok(!env.granted.has(GRANT));
  assert.equal((await single(port, request("extract_text", { tab_id: 7 }))).error.code, "site_access_required");
  assertNoCanary(port, "retry");
});

test("R5: failed removal -> only the owner's explicit Allow in the popup lifts the block; Chrome still granting, or re-granting in its settings, does not", async () => {
  for (const op of READ_KINDS) {
    const env = makeEnv({ pages: canaryPages() });
    const port = await toReady(env);
    const popup = makePopup({ worker: env });
    await settle();
    env.removeOverride = async () => false;
    await popup.click("revoke");
    env.removeOverride = null;
    // Chrome's settings: removed and granted again. A grant Chrome holds is
    // not the owner allowing the site after a revoke Chrome did not complete.
    env.revoke(GRANT);
    env.granted.add(GRANT);
    assert.equal((await single(port, request(op, { tab_id: 7 }))).error.code, "site_access_required",
      `${op}: contains() true is not authorization`);
    assertNoCanary(port, op);
    await popup.click("grant"); // the owner's explicit Allow: Chrome's prompt, then the worker is told
    assert.deepEqual(plain(popup.calls.requests), [{ origins: [GRANT] }]);
    assert.equal(popup.text("tab-state"), "Readable by Lumina (read-only)", op);
    assert.equal(popup.text("revoke-note"), "", op);
    assert.deepEqual(plain(env.storage.companionBlockedSites), []);
    const res = await single(port, request(op, { tab_id: 7 }));
    assert.equal(res.ok, true, `${op}: a new authorization, a new read`);
    assert.match(JSON.stringify(res), /CANARY/);
  }
});

test("R5 popup: an Allow the worker does not confirm is said plainly -- the companion may keep the site blocked", async () => {
  for (const allowAnswer of [async () => undefined, async () => ({ ok: false, error: "site_allow_not_saved" }),
    async () => { throw new Error("Could not establish connection"); }]) {
    const popup = makePopup({ workerAnswer: async () => ACK, allowAnswer });
    popup.chrome.permissions.granted = false;
    popup.chrome.permissions.blocked = true;
    await settle();
    assert.equal(popup.elements.grant.hidden, false);
    await popup.click("grant");
    assert.equal(popup.text("revoke-note"), "Chrome allows this site, but Lumina's companion did not confirm — it may keep the site blocked.");
    assert.equal(popup.elements["revoke-note"].className, "warn");
  }
});

test("R5: the owner declining Chrome's prompt leaves the block in place", async () => {
  const env = makeEnv({ pages: canaryPages() });
  const port = await toReady(env);
  const popup = makePopup({ worker: env });
  await settle();
  env.removeOverride = async () => false;
  await popup.click("revoke");
  popup.chrome.permissions.request = async (details) => { popup.calls.requests.push(details); return false; };
  await popup.click("grant");
  assert.deepEqual(plain(await env.popup({ kind: "site_state", pattern: GRANT })), { blocked: true });
  assert.equal((await single(port, request("extract_text", { tab_id: 7 }))).error.code, "site_access_required");
});

test("R5/R5.1: a block that cannot be saved is never a success -- not even when Chrome confirms the removal -- and the popup says so", async () => {
  const failBlockWrites = (env) => {
    env.storageSetHook = (values) => { if ("companionBlockedSites" in values) throw new Error("QUOTA_BYTES"); };
  };
  const env = makeEnv({ pages: canaryPages() });
  await toReady(env);
  failBlockWrites(env);
  env.removeOverride = async () => false;
  assert.deepEqual(plain(await env.popupRevoke(GRANT)), { ok: false, invalidated: true, blocked: true,
    chrome_access: true, remove_result: false, error: "site_block_not_saved" });
  const env2 = makeEnv({ pages: canaryPages() });
  await toReady(env2);
  const popup = makePopup({ worker: env2 });
  await settle();
  failBlockWrites(env2);
  env2.removeOverride = async () => false;
  await popup.click("revoke");
  assert.equal(popup.text("revoke-note"), "Companion access blocked for now, but the block could not be saved — try again.");
  assert.equal(popup.elements["revoke-note"].className, "warn");
  const env3 = makeEnv({ pages: canaryPages() });
  await toReady(env3);
  failBlockWrites(env3);
  assert.deepEqual(plain(await env3.popupRevoke(GRANT)), { ok: false, invalidated: true, blocked: true,
    chrome_access: false, remove_result: true, error: "site_block_not_saved" },
    "the owner's revoke must survive a restart; unsaved, it is not complete");
});

test("R5: an unreadable saved block list fails closed -- paused, no connection", async () => {
  for (const bad of [{}, "https://www.reddit.com/*", 7]) {
    const env = makeEnv({ storage: { companionBlockedSites: bad } });
    await settle();
    assert.equal(env.ports.length, 0, JSON.stringify(bad));
    assert.equal((await env.popup({ kind: "get_status" })).state, "PAUSED");
    assert.equal((await env.popup({ kind: "get_status" })).lastError, "storage_unavailable");
  }
  const env = makeEnv({ storage: { companionBlockedSites: [GRANT, "<all_urls>", 7, "https://*/*"] } });
  const port = await toReady(env);
  assert.deepEqual(plain(await env.popup({ kind: "site_state", pattern: GRANT })), { blocked: true },
    "valid entries load; malformed ones are dropped");
  assert.equal((await single(port, request("extract_text", { tab_id: 7 }))).error.code, "site_access_required");
});

test("R5: allow_site and site_state answer only this extension's popup, and only for a site pattern", async () => {
  const env = makeEnv({ pages: canaryPages() });
  const port = await toReady(env);
  env.removeOverride = async () => false;
  await env.popupRevoke(GRANT);
  const popupUrl = env.chrome.runtime.getURL("popup.html");
  for (const sender of [{ id: EXT_ID, url: popupUrl, tab: { id: 7 } }, { id: "x".repeat(32), url: popupUrl },
    { id: EXT_ID, url: `chrome-extension://${EXT_ID}/other.html` }]) {
    for (const kind of ["allow_site", "site_state"]) {
      assert.equal(await env.popup({ kind, pattern: GRANT }, sender), "NO_RESPONSE", `${kind} ${JSON.stringify(sender)}`);
    }
  }
  for (const pattern of [null, "<all_urls>", "https://*/*", "*://www.reddit.com/*", "https://accounts.google.com/*"]) {
    assert.equal(await env.popup({ kind: "allow_site", pattern }), "NO_RESPONSE", String(pattern));
  }
  assert.equal((await single(port, request("extract_text", { tab_id: 7 }))).error.code, "site_access_required",
    "still blocked after every refused allow");
});

test("R5 + R4 held-message contract: message in transit -> a pre-receipt read may commit, popup REVOKING…; at receipt the site is blocked; Chrome refuses -> no 'Revoked.', and it stays blocked", async () => {
  const env = makeEnv({ pages: canaryPages() });
  const port = await toReady(env);
  const popup = makePopup({ worker: env });
  await settle();
  env.removeOverride = async () => false;
  let clicking = null;
  env.injectionHook = async (details) => {
    const result = readNow(env, details);
    popup.bus.hold = true;
    clicking = popup.elements.revoke.listeners.click();
    return result;
  };
  const early = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(early.ok, true, "before the worker received it: inside the stated contract");
  assert.equal(popup.text("tab-state"), "Revoking…");
  env.injectionHook = null;
  popup.bus.release();
  assert.equal(env.peek("blockedSites.has('https://www.reddit.com/*')"), true, "blocked in the dispatch that delivered it");
  await clicking;
  await settle();
  assert.equal(popup.text("revoke-note"), R5_NOTES.site_access_remove_failed);
  const late = await single(port, request("extract_text", { tab_id: 7 }));
  assert.equal(late.error.code, "site_access_required");
  assert.equal(port.sent.filter((m) => /CANARY/.test(JSON.stringify(m))).length, 1, "only the pre-receipt read");
});

// ── BC-01B-A navigation: a separate owner session grant, never a read grant ──

test("navigation is off until the popup allows this exact connection", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const url = "https://github.com/Bino5150/lumina";
  const denied = await single(port, request("open_owner_url", { args: { url } }));
  assert.equal(denied.error.code, "navigation_not_allowed");
  assert.equal(env.createCalls.length, 0);
  const forged = await env.popup({ kind: "set_navigation", allowed: true },
    { id: EXT_ID, url: "https://github.com/" });
  assert.equal(forged, "NO_RESPONSE");
  assert.equal((await env.popup({ kind: "get_status" })).navigationAllowed, false);
  assert.deepEqual(plain(await env.popup({ kind: "set_navigation", allowed: true })),
    { ok: true, navigation_allowed: true });
  const req = request("open_owner_url", { args: { url } });
  const result = await single(port, req);
  assert.equal(result.result.status, "browser_local_effect_observed");
  assert.equal(result.result.operation_id, `${CID}:${req.request_id}`);
  assert.equal(result.result.observed_url, url);
  assert.equal(result.result.load_confirmed, true);
  assert.equal(env.createCalls.length, 1);
  assert.equal(env.injections.length, 0);
  port.deliver(req); // same connection/request id: never a second dispatch
  await settle();
  assert.equal(env.createCalls.length, 1);
});

test("new worker keeps the old ping shape unless the new hub opts in", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  const oldHubPing = await single(port, request("ping"));
  assert.deepEqual(plain(oldHubPing.result), { extension_version: "0.1.0" });
  const newHubPing = await single(port, request("ping", { args: { include_navigation: true } }));
  assert.deepEqual(plain(newHubPing.result), { extension_version: "0.1.0", navigation_allowed: true });
  const malformed = await single(port, request("ping", { args: { include_navigation: 1 } }));
  assert.equal(malformed.error.code, "invalid_args");
});

test("navigation grant is lost on PAUSE, reconnect, and worker restart", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  await env.popup({ kind: "set_paused", paused: true });
  assert.equal(port.disconnected, true);
  assert.equal((await env.popup({ kind: "get_status" })).navigationAllowed, false);
  await env.popup({ kind: "set_paused", paused: false });
  const next = env.port();
  next.deliver({ v: 1, type: "host_status", state: "hub_connected" });
  next.deliver({ v: 1, type: "welcome", connection_id: CID2, limits: {} });
  await settle();
  assert.equal((await env.popup({ kind: "get_status" })).navigationAllowed, false);
  const restarted = makeEnv({ storage: env.storage });
  await toReady(restarted);
  assert.equal((await restarted.popup({ kind: "get_status" })).navigationAllowed, false);
});

test("a Companion Revoke hides tab metadata and blocks both navigation operations", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  await env.popupRevoke("https://www.reddit.com/*");
  const listed = await single(port, request("list_tabs"));
  const reddit = listed.result.tabs.find((t) => t.tab_id === 7);
  assert.equal(reddit.restriction, "companion_revoked");
  assert.equal(reddit.url, null);
  assert.equal(reddit.title, null);
  const opened = await single(port, request("open_owner_url", {
    args: { url: "https://www.reddit.com/r/AgentsInteractive/" } }));
  assert.equal(opened.error.code, "companion_revoked");
  const switched = await single(port, request("switch_tab", { tab_id: 7,
    args: { window_id: 1, expected_url: "https://www.reddit.com/r/AgentsInteractive/" } }));
  assert.equal(switched.error.code, "navigated_during_request");
  assert.equal(env.createCalls.length, 0);
  assert.equal(env.updateCalls.length, 0);
});

test("Revoke during tab description hides metadata before the answer is sent", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  const held = deferred();
  let first = true;
  env.containsHook = async () => {
    if (!first) return undefined;
    first = false;
    await held.promise;
    return true; // stale Chrome answer from before Revoke
  };
  const req = request("list_tabs");
  port.deliver(req);
  await settle();
  await env.popupRevoke("https://www.reddit.com/*");
  held.resolve();
  await settle(16);
  const answer = port.sent.find((m) => m.request_id === req.request_id);
  assert.equal(answer.ok, false);
  assert.equal(answer.error.code, "site_access_required");
  assert.doesNotMatch(JSON.stringify(answer), /AgentsInteractive/);
});

test("switch_tab rejects a changed identity before dispatch and confirms an exact selection", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  const stale = await single(port, request("switch_tab", { tab_id: 10,
    args: { window_id: 1, expected_url: "https://github.com/old" } }));
  assert.equal(stale.error.code, "navigated_during_request");
  assert.equal(env.updateCalls.length, 0);
  const req = request("switch_tab", { tab_id: 10,
    args: { window_id: 1, expected_url: "https://github.com/" } });
  const selected = await single(port, req);
  assert.equal(selected.result.status, "browser_local_effect_observed");
  assert.equal(selected.result.tab_id, 10);
  assert.equal(selected.result.window_id, 1);
  assert.equal(env.updateCalls.length, 1);
});

test("a lost Chrome create answer is ambiguous and is never retried", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  env.createHook = async () => { throw new Error("Chrome response lost"); };
  const answer = await single(port, request("open_owner_url", {
    args: { url: "https://github.com/" } }));
  assert.equal(answer.ok, true);
  assert.equal(answer.result.status, "ambiguous_after_dispatch");
  assert.equal(env.createCalls.length, 1);
});

test("open observes a pending URL on the created tab without dispatching twice", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  const url = "https://www.reddit.com/";
  const tab = { id: 101, windowId: 1, active: true, incognito: false,
    status: "complete", url, title: "" };
  env.createHook = async () => { env.tabs.push(tab); return { ...tab, url: "" }; };
  let reads = 0;
  env.tabsGetHook = async (id) => {
    if (id === 101 && ++reads === 1) return { ...tab, url: "" };
    return undefined;
  };
  const req = request("open_owner_url", { args: { url } });
  port.deliver(req);
  await settle();
  env.runTimers(); // the one pending-URL observation delay
  await settle(16);
  const answer = port.sent.find((m) => m.request_id === req.request_id);
  assert.equal(answer.result.status, "browser_local_effect_observed");
  assert.equal(answer.result.observed_url, url);
  assert.equal(answer.result.load_confirmed, true);
  assert.equal(env.createCalls.length, 1);
  assert.equal(reads, 2);
});

test("Revoke overtaking a dispatched navigation yields only an ambiguous receipt", async () => {
  const env = makeEnv();
  const port = await toReady(env);
  await env.popup({ kind: "set_navigation", allowed: true });
  const held = deferred();
  env.createHook = async () => { await held.promise; return undefined; };
  const req = request("open_owner_url", { args: { url: "https://github.com/private" } });
  port.deliver(req);
  await settle();
  assert.equal(env.createCalls.length, 1);
  await env.popupRevoke("https://github.com/*");
  held.resolve();
  await settle(16);
  const answers = port.sent.filter((m) => m.request_id === req.request_id);
  assert.equal(answers.length, 1);
  assert.equal(answers[0].result.status, "ambiguous_after_dispatch");
  assert.equal(answers[0].result.observed_url, null);
  assert.equal(env.createCalls.length, 1);
});

// A test that awaits something that never settles would let Node drain its
// event loop and exit 0 without a summary. Fail closed: the exit code is 1
// until every test has run, and a test left hanging is named.
process.exitCode = 1;
let running = null;
process.on("exit", () => {
  if (running !== null) process.stdout.write(`FAIL - ${running}\nthe test never settled (awaited a promise that never resolves)\n`);
});
(async () => {
  let failed = 0;
  for (const { name, fn } of TESTS) {
    running = name;
    try {
      await fn();
      process.stdout.write(`ok - ${name}\n`);
    } catch (error) {
      failed += 1;
      process.stdout.write(`FAIL - ${name}\n${error && error.stack}\n`);
    }
  }
  running = null;
  process.stdout.write(`${TESTS.length - failed}/${TESTS.length} worker tests passed\n`);
  process.exitCode = failed ? 1 : 0;
})();
