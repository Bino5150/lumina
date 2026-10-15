// BROWSER-COMPANION-COMPLETION-01 Slice 1 -- a small, deterministic DOM for
// running the worker's REAL injected page functions (observePage,
// resolveRefTarget, dispatchRefLink) in Node. Built from a JSON spec so the
// unit harness (worker_test.js) and the real worker+host+hub harness
// (e2e_driver.js) share it. It models what those functions read: tags,
// attributes, text, layout rects, visibility styles, open shadow roots,
// labels, and ONE persistent isolated world per document (the extension's
// globals survive between injections into the same document, as in Chrome).
//
// Spec node: { tag, attrs?, text?, rect?: {top,left,width,height}, style?:
//   {display, visibility, opacity, overflowY}, value?, checked?, disabled?,
//   children?: [node], shadow?: [node], label?: "text" }
// It is a model, not a browser: real-Chrome behaviour is evidenced separately.
"use strict";

const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

const DEFAULT_RECT = { top: 10, left: 10, width: 120, height: 24 };

class ShadowRoot {
  constructor(host) { this.host = host; this.children = []; this.parentNode = null; }
  querySelectorAll(selector) { return queryAll(this.children, selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  getElementById(id) { return this.querySelectorAll("*").find((el) => el.id === id) ?? null; }
}

class FakeElement {
  constructor(spec, doc) {
    this.doc = doc;
    this.tagName = String(spec.tag || "div").toUpperCase();
    this.nodeType = 1;
    this.attrs = { ...(spec.attrs || {}) };
    this.ownText = spec.text ?? "";
    this.rect = { ...DEFAULT_RECT, ...(spec.rect || {}) };
    this.styleSpec = { display: "block", visibility: "visible", opacity: "1", overflowY: "visible", ...(spec.style || {}) };
    this.children = [];
    this.parentNode = null;
    this.shadowRoot = null;
    this.value = spec.value;
    this.checked = spec.checked;
    this.disabled = spec.disabled === true;
    this.labelsList = [];
    this.scrollTop = 0;
    this.scrollHeight = spec.scrollHeight ?? this.rect.height;
    this.clientHeight = spec.clientHeight ?? this.rect.height;
  }

  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? String(this.attrs[name]) : null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  get id() { return this.attrs.id || ""; }
  // As in Chrome: an element that is not rendered answers its textContent;
  // a rendered one omits the text of children that are not rendered.
  get innerText() {
    if (!this.visibleSelfAndAncestors()) return this.textContent;
    const rendered = (node) => {
      const s = node.styleSpec;
      if (s.display === "none" || s.visibility !== "visible") return "";
      return [node.ownText, ...node.children.map(rendered)].filter((t) => t).join(" ");
    };
    return rendered(this);
  }
  get textContent() { return [this.ownText, ...this.children.map((c) => c.textContent)].filter((t) => t).join(" "); }
  get isContentEditable() { return this.attrs.contenteditable === "" || this.attrs.contenteditable === "true"; }
  get labels() { return this.labelsList; }
  get parentElement() { return this.parentNode instanceof FakeElement ? this.parentNode : null; }
  get previousElementSibling() {
    const siblings = this.parentNode ? this.parentNode.children : [];
    const i = siblings.indexOf(this);
    return i > 0 ? siblings[i - 1] : null;
  }
  get isConnected() {
    let node = this;
    for (let i = 0; i < 256 && node; i++) {
      if (node === this.doc.documentElement) return true;
      node = node instanceof ShadowRoot ? node.host : node.parentNode;
    }
    return false;
  }
  getRootNode() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.doc.documentElement ? this.doc : node;
  }
  getBoundingClientRect() {
    const r = this.rect;
    const scroll = this.doc.window.scrollY;
    return { top: r.top - scroll, left: r.left, width: r.width, height: r.height,
      bottom: r.top - scroll + r.height, right: r.left + r.width };
  }
  visibleSelfAndAncestors() {
    let node = this;
    for (let i = 0; i < 256 && node; i++) {
      if (node instanceof FakeElement) {
        const s = node.styleSpec;
        if (s.display === "none" || s.visibility !== "visible" || Number(s.opacity) === 0) return false;
      }
      node = node instanceof ShadowRoot ? node.host : node.parentNode;
    }
    return true;
  }
  checkVisibility() { return this.visibleSelfAndAncestors(); }
  closest(selector) {
    let node = this;
    while (node instanceof FakeElement) {
      if (matches(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelectorAll(selector) { return queryAll(this.children, selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
}

// --- a tiny selector engine: comma lists of compound selectors ------------
// tag | * , [attr] , [attr=value] , [attr='value'] , :not(compound)
function parseCompound(text) {
  const out = { tag: null, attrs: [], nots: [] };
  let rest = text.trim();
  const tag = rest.match(/^([a-zA-Z][a-zA-Z0-9]*|\*)/);
  if (tag) { out.tag = tag[1] === "*" ? null : tag[1].toLowerCase(); rest = rest.slice(tag[0].length); }
  while (rest.length) {
    let m = rest.match(/^\[([a-zA-Z-]+)(?:=(?:'([^']*)'|"([^"]*)"|([^\]]*)))?\]/);
    if (m) {
      out.attrs.push({ name: m[1], value: m[2] ?? m[3] ?? m[4] });
      rest = rest.slice(m[0].length);
      continue;
    }
    m = rest.match(/^:not\(([^)]*)\)/);
    if (m) { out.nots.push(parseCompound(m[1])); rest = rest.slice(m[0].length); continue; }
    throw new Error(`fake_dom: unsupported selector ${text}`);
  }
  return out;
}
function matchCompound(el, c) {
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  for (const a of c.attrs) {
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== undefined && el.getAttribute(a.name) !== a.value) return false;
  }
  return !c.nots.some((n) => matchCompound(el, n));
}
function matches(el, selector) {
  return selector.split(",").some((part) => matchCompound(el, parseCompound(part)));
}
function queryAll(children, selector) {
  const out = [];
  const walk = (nodes) => {
    for (const node of nodes) {
      if (matches(node, selector)) out.push(node);
      walk(node.children);
    }
  };
  walk(children);
  return out;
}

function build(spec, doc, parent) {
  const el = new FakeElement(spec, doc);
  el.parentNode = parent;
  for (const child of spec.children || []) el.children.push(build(child, doc, el));
  if (spec.shadow) {
    const root = new ShadowRoot(el);
    for (const child of spec.shadow) root.children.push(build(child, doc, root));
    el.shadowRoot = root;
  }
  return el;
}

// Wire <label for=id> and wrapping <label> to their fields.
function linkLabels(doc) {
  const all = doc.documentElement.querySelectorAll("*");
  for (const label of all.filter((el) => el.tagName === "LABEL")) {
    const target = label.getAttribute("for")
      ? all.find((el) => el.id === label.getAttribute("for"))
      : label.querySelector("input,select,textarea");
    if (target) target.labelsList.push(label);
  }
}

// One document with its own persistent isolated world.
function createDocument(pageSpec) {
  const win = { scrollY: 0, innerHeight: pageSpec.viewport?.height ?? 600, innerWidth: pageSpec.viewport?.width ?? 1000 };
  const doc = { window: win, title: pageSpec.title ?? "", baseURI: pageSpec.href };
  doc.documentElement = build({ tag: "html", children: pageSpec.nodes || [], rect: { top: 0, left: 0, width: win.innerWidth,
    height: pageSpec.scrollHeight ?? win.innerHeight } }, doc, null);
  doc.documentElement.scrollHeight = pageSpec.scrollHeight ?? win.innerHeight;
  doc.querySelectorAll = (selector) => queryAll([doc.documentElement], selector);
  doc.querySelector = (selector) => doc.querySelectorAll(selector)[0] ?? null;
  doc.getElementById = (id) => doc.querySelectorAll("*").find((el) => el.id === id) ?? null;
  doc.find = (id) => doc.getElementById(id);
  linkLabels(doc);
  return doc;
}

function createWorld(pageSpec, doc, navigate) {
  const location = { href: pageSpec.locationHref || pageSpec.href, origin: new URL(pageSpec.href).origin,
    assign: (url) => navigate(url) };
  const win = doc.window;
  const world = vm.createContext({
    document: doc, location, URL, WeakRef, WeakMap, Map, Set, crypto: webcrypto,
    get innerHeight() { return win.innerHeight; },
    get innerWidth() { return win.innerWidth; },
    get scrollY() { return win.scrollY; },
    scrollTo: (_x, y) => { win.scrollY = Math.max(0, Math.min(y, Math.max(0, doc.documentElement.scrollHeight - win.innerHeight))); },
    getComputedStyle: (el) => ({ ...el.styleSpec }),
  });
  return world;
}

// Run an injected function in the page's persistent isolated world.
function runInDomPage(func, args, page, navigate) {
  if (!page.dom) page.dom = createDocument(page);
  page.navigateHook = navigate;
  if (!page.world) page.world = createWorld(page, page.dom, (url) => page.navigateHook(url));
  page.world.args = args;
  return vm.runInContext(`(${func.toString()}).apply(null, args)`, page.world);
}

// Attach a new node (built from a spec) under ``parent`` -- e.g. a framework
// re-render replacing an element with an identical twin.
function appendSpec(doc, parent, spec) {
  const el = build(spec, doc, parent);
  parent.children.push(el);
  return el;
}

module.exports = { createDocument, runInDomPage, appendSpec, matches };
