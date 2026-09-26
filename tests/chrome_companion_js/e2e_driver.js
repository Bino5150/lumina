// BROWSER-COMPANION-01A end-to-end driver.
// Runs the REAL extension worker (worker.js + policy.js) in a Node VM whose
// chrome.runtime.connectNative() spawns the REAL native host
// (chrome_companion/native_host.py) and speaks Chrome's native-messaging
// framing to it over stdio -- so tests/test_chrome_companion_e2e.py exercises
// worker <-> host <-> hub with no protocol mocks in between.
//
// argv[2]: JSON config {python, hostScript, hostConfig, extensionId,
//                       storageFile, tabs, pages, granted, hostLog?}
// stdin:   JSON-line commands {cmd: pause|resume|status|grant|revoke|popup_revoke|popup_allow|
//                               chrome_refuses_removal|
//           hold_events|release_events|hold|release|alarm|exit}
// stdout:  JSON-line events   {event: status|ack|error, ...}
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const vm = require("node:vm");
const { spawn } = require("node:child_process");
const { webcrypto } = require("node:crypto");

const cfg = JSON.parse(process.argv[2]);
const EXT_DIR = path.resolve(__dirname, "../../chrome_companion/extension");
const LE = os.endianness() === "LE";
const children = new Set();
const emit = (event) => process.stdout.write(`${JSON.stringify(event)}\n`);

let storage = {};
try {
  storage = JSON.parse(fs.readFileSync(cfg.storageFile, "utf8"));
} catch {
  storage = {};
}
// Atomic (temp file + rename), like Chrome's own crash-consistent storage: a
// SIGKILLed "Chrome" must never leave a torn file that the next launch would
// read as an empty profile (a fresh instance id -> wrong_instance).
const persist = () => {
  const tmp = `${cfg.storageFile}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(storage));
  fs.renameSync(tmp, cfg.storageFile);
};
const tabs = cfg.tabs;
const pages = cfg.pages;
const granted = new Set(cfg.granted);
const listeners = { message: [], alarm: [], startup: [], installed: [], permissionRemoved: [] };
// {cmd: hold, tab} parks the NEXT injection into that tab until {cmd: release}
// (a page busy on its main thread), so a test can act while a read is pending.
const holds = new Map();

function nativePort(name) {
  if (name !== "org.lumina.chrome_companion") throw new Error("Specified native messaging host not found.");
  const child = spawn(cfg.python, [cfg.hostScript, "--config", cfg.hostConfig, `chrome-extension://${cfg.extensionId}/`],
    { stdio: ["pipe", "pipe", "pipe"] });
  children.add(child);
  const handlers = { message: [], disconnect: [] };
  let closed = false;
  let buffer = Buffer.alloc(0);
  child.stdout.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    while (buffer.length >= 4) {
      const size = LE ? buffer.readUInt32LE(0) : buffer.readUInt32BE(0);
      if (buffer.length < 4 + size) break;
      const message = JSON.parse(buffer.subarray(4, 4 + size).toString("utf8"));
      buffer = buffer.subarray(4 + size);
      if (!closed) handlers.message.forEach((fn) => fn(message));
    }
  });
  // Host stderr is Chrome's log; with cfg.hostLog set, tests can audit it.
  child.stderr.on("data", (chunk) => {
    if (cfg.hostLog) fs.appendFileSync(cfg.hostLog, chunk);
  });
  child.on("exit", () => {
    children.delete(child);
    if (closed) return;
    closed = true;
    handlers.disconnect.forEach((fn) => fn());
  });
  return {
    onMessage: { addListener: (fn) => handlers.message.push(fn) },
    onDisconnect: { addListener: (fn) => handlers.disconnect.push(fn) },
    postMessage(message) {
      if (closed) throw new Error("Attempting to use a disconnected port object");
      const body = Buffer.from(JSON.stringify(message), "utf8");
      const header = Buffer.alloc(4);
      if (LE) header.writeUInt32LE(body.length);
      else header.writeUInt32BE(body.length);
      child.stdin.write(Buffer.concat([header, body]));
    },
    disconnect() {
      if (closed) return;
      closed = true; // Chrome: no onDisconnect for the side that disconnects
      child.stdin.end();
      setTimeout(() => child.kill("SIGTERM"), 1000).unref();
    },
  };
}

function runInPage(func, args, page) {
  const anchors = (page.anchors || []).map((a) => ({
    getAttribute: (name) => (name === "href" ? a.href : null),
    innerText: a.text || "",
  }));
  const document = { body: { innerText: page.text || "" }, documentElement: { textContent: page.text || "" },
    title: page.title || "", baseURI: page.href, querySelectorAll: () => anchors };
  const location = { href: page.href, origin: new URL(page.href).origin };
  return vm.runInNewContext(`(${func.toString()}).apply(null, args)`, { document, location, URL, args });
}

const chrome = {
  runtime: {
    id: cfg.extensionId,
    lastError: undefined,
    getURL: (p) => `chrome-extension://${cfg.extensionId}/${p}`,
    getManifest: () => JSON.parse(fs.readFileSync(path.join(EXT_DIR, "manifest.json"), "utf8")),
    connectNative: nativePort,
    onStartup: { addListener: (fn) => listeners.startup.push(fn) },
    onInstalled: { addListener: (fn) => listeners.installed.push(fn) },
    onMessage: { addListener: (fn) => listeners.message.push(fn) },
  },
  storage: {
    local: {
      async get(keys) {
        const out = {};
        for (const key of [].concat(keys)) if (key in storage) out[key] = storage[key];
        return JSON.parse(JSON.stringify(out));
      },
      async set(values) {
        Object.assign(storage, JSON.parse(JSON.stringify(values)));
        persist();
      },
    },
  },
  alarms: {
    async create() {},
    async clear() { return true; },
    onAlarm: { addListener: (fn) => listeners.alarm.push(fn) },
  },
  tabs: {
    async query(query) {
      return (query.active ? tabs.filter((t) => t.active) : tabs).map((t) => ({ ...t }));
    },
    async get(id) {
      const tab = tabs.find((t) => t.id === id);
      if (!tab) throw new Error(`No tab with id: ${id}.`);
      return { ...tab };
    },
    async create({ url, active }) {
      const id = Math.max(...tabs.map((t) => t.id)) + 1;
      if (active) tabs.forEach((t) => { if (t.windowId === 1) t.active = false; });
      const tab = { id, windowId: 1, active: Boolean(active), incognito: false,
        status: "complete", url, title: "" };
      tabs.push(tab);
      return { ...tab };
    },
    async update(id, { active }) {
      const tab = tabs.find((t) => t.id === id);
      if (!tab) throw new Error(`No tab with id: ${id}.`);
      if (active) {
        tabs.forEach((t) => { if (t.windowId === tab.windowId) t.active = false; });
        tab.active = true;
      }
      return { ...tab };
    },
  },
  permissions: {
    async contains({ origins }) { return origins.every((o) => granted.has(o)); },
    // Chrome-shaped (R3): the removal applies at once; onRemoved follows later
    // (held while hold_events is on, as when Chrome's network service is slow).
    async remove({ origins }) {
      if (chromeRefusesRemoval) return false; // R5 / AR5: "not removed", grant kept
      origins.forEach((o) => granted.delete(o));
      await new Promise((resolve) => setImmediate(resolve));
      announceRemoved(origins);
      return true;
    },
    onRemoved: { addListener: (fn) => listeners.permissionRemoved.push(fn) },
  },
  webNavigation: {
    async getFrame({ tabId, frameId }) {
      const tab = tabs.find((t) => t.id === tabId);
      if (!tab || frameId !== 0) return null;
      return { documentId: `doc-${tabId}`, documentLifecycle: "active", frameType: "outermost_frame", url: tab.url };
    },
  },
  scripting: {
    async executeScript(details) {
      const hold = holds.get(details.target.tabId);
      if (hold && !hold.used) {
        hold.used = true;
        emit({ event: "held", tab: details.target.tabId });
        await hold.promise;
      }
      const page = pages[String(details.target.tabId)];
      if (!page) throw new Error("Cannot access contents of the page");
      return [{ documentId: `doc-${details.target.tabId}`, frameId: 0, result: runInPage(details.func, details.args, page) }];
    },
  },
};

let holdEvents = false;
const heldEvents = [];
let chromeRefusesRemoval = false;
function announceRemoved(origins) {
  const fire = () => listeners.permissionRemoved.forEach((fn) => fn({ origins, permissions: [] }));
  if (holdEvents) heldEvents.push(fire);
  else fire();
}

const context = vm.createContext({
  chrome, console, URL, crypto: webcrypto, setTimeout, clearTimeout,
  importScripts: (name) => vm.runInContext(fs.readFileSync(path.join(EXT_DIR, name), "utf8"), context),
});
vm.runInContext(fs.readFileSync(path.join(EXT_DIR, "worker.js"), "utf8"), context);
listeners.startup.forEach((fn) => fn()); // "Chrome started"

const popupSender = { id: cfg.extensionId, url: chrome.runtime.getURL("popup.html") };
const popup = (message) => new Promise((resolve) => {
  if (listeners.message[0](message, popupSender, resolve) !== true) resolve(null);
});

function shutdown() {
  for (const child of children) child.kill("SIGTERM");
  process.exit(0);
}

readline.createInterface({ input: process.stdin }).on("line", async (line) => {
  const command = JSON.parse(line);
  try {
    if (command.cmd === "pause" || command.cmd === "resume") {
      emit({ event: "ack", cmd: command.cmd, result: await popup({ kind: "set_paused", paused: command.cmd === "pause" }) });
    } else if (command.cmd === "status") {
      emit({ event: "status", ...(await popup({ kind: "get_status" })) });
    } else if (command.cmd === "navigation") {
      emit({ event: "ack", cmd: "navigation",
        result: await popup({ kind: "set_navigation", allowed: command.allowed === true }) });
    } else if (command.cmd === "grant") {
      granted.add(command.pattern);
      emit({ event: "ack", cmd: "grant" });
    } else if (command.cmd === "revoke") { // outside the extension (chrome://extensions)
      granted.delete(command.pattern);
      announceRemoved([command.pattern]);
      emit({ event: "ack", cmd: "revoke" });
    } else if (command.cmd === "popup_revoke") { // the popup's Revoke button (R3)
      emit({ event: "ack", cmd: "popup_revoke", result: await popup({ kind: "revoke_site", pattern: command.pattern }) });
    } else if (command.cmd === "popup_allow") { // the popup's Allow, after Chrome granted (R5)
      emit({ event: "ack", cmd: "popup_allow", result: await popup({ kind: "allow_site", pattern: command.pattern }) });
    } else if (command.cmd === "chrome_refuses_removal") {
      chromeRefusesRemoval = command.value === true;
      emit({ event: "ack", cmd: "chrome_refuses_removal" });
    } else if (command.cmd === "hold_events") {
      holdEvents = true;
      emit({ event: "ack", cmd: "hold_events" });
    } else if (command.cmd === "release_events") {
      holdEvents = false;
      const held = heldEvents.splice(0);
      held.forEach((fire) => fire());
      emit({ event: "ack", cmd: "release_events", released: held.length });
    } else if (command.cmd === "hold") {
      let release;
      const promise = new Promise((resolve) => { release = resolve; });
      holds.set(command.tab, { promise, release, used: false });
      emit({ event: "ack", cmd: "hold" });
    } else if (command.cmd === "release") {
      for (const hold of holds.values()) hold.release();
      holds.clear();
      emit({ event: "ack", cmd: "release" });
    } else if (command.cmd === "alarm") {
      listeners.alarm.forEach((fn) => fn({ name: "lumina-companion-reconnect" }));
      emit({ event: "ack", cmd: "alarm" });
    } else if (command.cmd === "exit") {
      shutdown();
    }
  } catch (error) {
    emit({ event: "error", message: String(error) });
  }
}).on("close", shutdown);
