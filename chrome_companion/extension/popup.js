// Lumina Chrome Companion popup (BROWSER-COMPANION-01A).
// Shows the worker's live state (asked directly, not inferred), the current
// tab's access state, and the owner's local controls: grant/revoke site
// access (a Chrome permission prompt behind a real click -- never a page
// instruction) and PAUSE/RESUME. Pause is a local browser control, not an
// approval channel.
"use strict";

const STATE_LABELS = {
  READY: ["Connected to Lumina", "ready"],
  IDENTIFIED: ["Connecting to Lumina…", "pending"],
  CONNECTING: ["Connecting…", "pending"],
  DISCONNECTED: ["Disconnected", ""],
  PAUSED: ["Paused — Lumina is disconnected", "paused"],
};
const DETAILS = {
  lumina_unreachable: "Lumina isn't running, or her Chrome companion hub is off.",
  host_not_installed: "Native host not registered. Run the installer (scripts/chrome_companion_setup.py install).",
  host_forbidden: "Native host is registered for a different extension ID. Re-run the installer with this extension's ID.",
  host_exited: "The native host exited during startup.",
  unpaired: "This Chrome profile isn't paired with Lumina yet (see Pairing ID below).",
  wrong_instance: "Lumina is paired with a different Chrome profile or extension install.",
  wrong_origin: "Lumina's install record names a different extension ID.",
  not_installed: "Lumina's companion isn't installed for this data dir.",
  paused: "Paused by you.",
  disconnected: "Connection lost; reconnecting automatically.",
  protocol_violation: "Dropped a malformed message; reconnecting with a fresh connection.",
  storage_unavailable: "Extension storage unavailable; staying paused (fail closed).",
};

// What a revoke from this popup established (R4 / AR4). Strict only on the
// worker's acknowledgement; the fallback says it is not. "Revoked." only when
// the worker also saw Chrome's grant gone (R5 / AR5); otherwise the note says
// what Chrome did not do. Either way the companion keeps the site blocked
// until the owner allows it here again (R5.1).
const REVOKE_NOTES = {
  waiting: ["Waiting for Lumina's companion to confirm…", "muted"],
  revoked: ["Revoked. Lumina's companion confirmed it and discarded any read still in progress.", "muted"],
  chrome_refused: ["Companion access blocked. Chrome's site permission could not be removed — try again.", "warn"],
  chrome_unconfirmed: ["Companion access blocked. Chrome did not confirm removing its site permission — try again.",
    "warn"],
  block_not_saved: ["Companion access blocked for now, but the block could not be saved — try again.", "warn"],
  allow_unconfirmed: ["Chrome allows this site, but Lumina's companion did not confirm — it may keep the site "
    + "blocked.", "warn"],
  unconfirmed: ["Site access removed in Chrome, but Lumina's companion did not confirm the revoke — "
    + "a read already in progress may still have reached Lumina.", "warn"],
  failed: ["Site access could not be removed — try again.", "warn"],
};

let currentPattern = null;
let lastStatus = null;
let revoking = false;
let revokeNote = null;

function $(id) {
  return document.getElementById(id);
}

function formatId(id) {
  return typeof id === "string" ? id.match(/.{1,4}/g).join("-") : "unavailable";
}

async function workerStatus() {
  try {
    return await chrome.runtime.sendMessage({ kind: "get_status" });
  } catch {
    return null;
  }
}

async function renderStatus() {
  const status = await workerStatus();
  lastStatus = status;
  const [label, dotClass] = STATE_LABELS[status && status.state] || ["Unknown", ""];
  $("state").textContent = label;
  $("dot").className = `dot ${dotClass}`;
  $("detail").textContent = status && status.lastError ? DETAILS[status.lastError] || status.lastError : "";
  const paused = status && status.state === "PAUSED";
  $("pause").textContent = paused ? "RESUME LUMINA" : "PAUSE LUMINA";
  $("pause").classList.toggle("resume", Boolean(paused));
  const ready = status && status.state === "READY";
  // An unpacked extension can load this new popup from disk while Chrome
  // keeps the previous service worker running. Its get_status response has
  // no navigationAllowed field; show a reload instruction, never a button
  // that the old worker cannot answer.
  const navigationWorker = status && typeof status.navigationAllowed === "boolean";
  $("navigation").hidden = !ready || !navigationWorker;
  $("navigation").textContent = status && status.navigationAllowed
    ? "Stop navigation" : "Allow navigation this session";
  $("navigation-state").textContent = !ready ? "Connect and resume Companion first."
    : !navigationWorker ? "Reload Lumina Chrome Companion at chrome://extensions to enable navigation."
    : status.navigationAllowed ? "Allowed for this connection only."
      : "Navigation is off; site reading is separate.";
  $("instance").textContent = formatId(status && status.instanceId);
  $("pair-hint").hidden = !(status && (status.lastError === "unpaired" || status.lastError === "wrong_instance"));
  const action = status && status.lastAction;
  $("action").textContent = action
    ? `${action.op}${action.host ? ` · ${action.host}` : ""}${action.tab_id !== null ? ` · tab ${action.tab_id}` : ""} · ${new Date(action.at).toLocaleTimeString()}`
    : "none";
}

async function renderTab() {
  currentPattern = null;
  $("grant").hidden = true;
  $("revoke").hidden = true;
  $("tab-origin").textContent = "";
  let tab;
  try {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch {
    tab = null;
  }
  if (!tab) {
    $("tab-state").textContent = "No tab";
    return;
  }
  const verdict = LuminaPolicy.classifyUrl(tab.url);
  if (!verdict.readable) {
    $("tab-state").textContent = "Restricted — Lumina never reads this page";
    return;
  }
  $("tab-origin").textContent = verdict.origin;
  currentPattern = verdict.pattern;
  let granted = false;
  try {
    granted = await chrome.permissions.contains({ origins: [verdict.pattern] });
  } catch {
    granted = false;
  }
  const blocked = await siteBlocked(verdict.pattern);
  if (blocked === true) {
    // Revoked from this popup (R5.1): the companion keeps the site blocked
    // whatever Chrome grants. Only Allow (the owner's explicit grant) lifts
    // it; Revoke, while Chrome grants the site, removes Chrome's grant.
    $("tab-state").textContent = granted ? "Blocked by Lumina's companion — Chrome allows this site"
      : "No site access";
    $("grant").hidden = false;
    $("revoke").hidden = !granted;
  } else {
    $("tab-state").textContent = !granted ? "No site access"
      : blocked === false ? "Readable by Lumina (read-only)" : "Allowed in Chrome — Lumina's companion is not answering";
    $("grant").hidden = granted;
    $("revoke").hidden = !granted;
  }
  if (revoking) showRevoking();
  showRevokeNote();
}

// REVOKING…: the click is not the revoke. Nothing is claimed, and no other
// site control is offered, until the worker answers.
function showRevoking() {
  $("tab-state").textContent = "Revoking…";
  $("grant").hidden = true;
  $("revoke").hidden = true;
}

function showRevokeNote() {
  const [text, tone] = REVOKE_NOTES[revokeNote] || ["", "muted"];
  $("revoke-note").textContent = text;
  $("revoke-note").className = tone;
}

// true / false from the worker; null if it did not answer.
async function siteBlocked(pattern) {
  try {
    const answer = await chrome.runtime.sendMessage({ kind: "site_state", pattern });
    return answer && typeof answer.blocked === "boolean" ? answer.blocked : null;
  } catch {
    return null;
  }
}

async function render() {
  await Promise.all([renderStatus(), renderTab()]);
}

// Grant is the owner's explicit authorization (R5.1): once Chrome grants the
// site, the worker is told to lift the block a revoke left on it -- the only
// way that block is ever lifted.
$("grant").addEventListener("click", () => {
  if (!currentPattern || revoking) return;
  const pattern = currentPattern;
  revokeNote = null;
  // Called directly in the click handler so Chrome sees the user gesture.
  chrome.permissions.request({ origins: [pattern] })
    .then((granted) => (granted === true ? allowSite(pattern) : undefined))
    .catch(() => {})
    .finally(() => void render());
});

async function allowSite(pattern) {
  let result;
  try {
    result = await chrome.runtime.sendMessage({ kind: "allow_site", pattern });
  } catch {
    result = undefined;
  }
  if (!result || result.ok !== true) revokeNote = "allow_unconfirmed";
}

// Revoke goes THROUGH the worker (R3 / B2-R3): it voids every read in
// progress before it asks Chrome to remove the grant, so no read that
// overlaps this revoke can publish -- even after a quick re-grant (see the
// top of worker.js).
//
// The click itself cannot void anything (R4 / AR4): this popup and the
// worker are separate contexts, and the message takes time to arrive. So the
// popup shows REVOKING… from the click, and claims the revoke only when the
// worker's answer acknowledges it (invalidated: true: every read in progress
// was voided the moment the worker received the message) AND reports
// Chrome's grant seen gone (ok: true, chrome_access: false -- R5 / AR5). An
// acknowledged revoke leaves the site blocked by the companion until the
// owner allows it here (R5.1); if Chrome did not complete it, the note says
// so. Only if no acknowledgement came at all --
// no worker was running, or it died on the way -- does the popup remove the
// grant itself, so the owner's revoke is never blocked; it then says the
// companion did not confirm, because that is only as strong as a revoke in
// Chrome's settings.
$("revoke").addEventListener("click", async () => {
  if (!currentPattern || revoking) return;
  const pattern = currentPattern;
  revoking = true;
  revokeNote = "waiting";
  showRevoking();
  showRevokeNote();
  let result;
  try {
    result = await chrome.runtime.sendMessage({ kind: "revoke_site", pattern });
  } catch {
    result = undefined;
  }
  if (result && result.invalidated === true) {
    // "Revoked." needs BOTH: reads invalidated, and Chrome's grant seen gone.
    revokeNote = result.ok === true && result.chrome_access === false ? "revoked"
      : result.error === "site_block_not_saved" ? "block_not_saved"
      : result.chrome_access === true ? "chrome_refused" : "chrome_unconfirmed";
  } else {
    try {
      await chrome.permissions.remove({ origins: [pattern] });
    } catch {
      // judged below by what Chrome now holds
    }
    let granted = true;
    try {
      granted = await chrome.permissions.contains({ origins: [pattern] });
    } catch {
      granted = true;
    }
    revokeNote = granted ? "failed" : "unconfirmed";
  }
  revoking = false;
  await render();
});

$("pause").addEventListener("click", async () => {
  const wantPause = !(lastStatus && lastStatus.state === "PAUSED");
  let result = null;
  try {
    result = await chrome.runtime.sendMessage({ kind: "set_paused", paused: wantPause });
  } catch {
    result = null;
  }
  if (!result || !result.ok) {
    $("detail").textContent = wantPause ? "PAUSE could not be saved — try again." : "RESUME could not be saved.";
    return;
  }
  await render();
});

$("navigation").addEventListener("click", async () => {
  if (!lastStatus || lastStatus.state !== "READY") return;
  if (typeof lastStatus.navigationAllowed !== "boolean") return;
  const allowed = !lastStatus.navigationAllowed;
  try {
    const result = await chrome.runtime.sendMessage({ kind: "set_navigation", allowed });
    if (!result || result.ok !== true) {
      $("navigation-state").textContent = "Companion did not confirm. Reload the extension at chrome://extensions and try again.";
      return;
    }
  } catch {
    $("navigation-state").textContent = "Companion did not confirm. Reload the extension at chrome://extensions and try again.";
    return;
  }
  await renderStatus();
});

$("copy").addEventListener("click", async () => {
  if (!lastStatus || !lastStatus.instanceId) return;
  try {
    await navigator.clipboard.writeText(formatId(lastStatus.instanceId));
    $("copy").textContent = "Copied";
  } catch {
    $("copy").textContent = "Copy failed";
  }
});

chrome.storage.onChanged.addListener(() => void renderStatus());
void render();
