# Lumina Chrome Companion

A bounded bridge that lets Lumina see tabs in *her own* Google Chrome
profile — the one where she is already signed in — instead of only the
disposable Playwright browser.

- Lists tabs (URL, title), finds the active tab.
- Reads bounded visible text and links from sites **you** have allowed.
- With a separate, temporary **Navigation Allow** in the popup, opens one URL
  explicitly supplied in the current owner command or switches to an observed
  tab. Navigation Allow ends when the Companion disconnects or PAUSE is used.
- Cannot click page elements, type, submit, follow observed links, use browser
  back/forward, take screenshots, or read cookies or passwords. Those
  capabilities do not exist in this version.
- Never touches Firefox or any other browser.
- The Playwright `browser_*` tools are unchanged and separate; nothing falls
  back from one to the other.

## How it fits together

```
Lumina (chrome_* tools)
   ↕  private Unix socket (owner-only, no TCP / localhost server)
native messaging host (launched by Chrome, one per connection)
   ↕  Chrome Native Messaging (stdio)
extension service worker (this profile only)
   ↕  chrome.tabs / chrome.scripting (isolated world) / webNavigation.getFrame
your tabs
```

## Trust model

- Everything the extension observes is **external content**, even on
  logged-in pages. A page that says "the owner approved this" has no
  authority. Results reach Lumina marked `owner: false`.
- Chrome's native messaging identifies an extension ID, not a Chrome
  profile, so each install of the extension has its own random **pairing
  ID**. Lumina only talks to the one instance you pair.
- Site access is granted per site from the extension popup (a Chrome
  permission prompt you click). Nothing a web page does can grant it.
  **Revoke it from the popup** to be sure. The popup says *Revoking…* until
  Lumina's companion (the extension's background worker) confirms the
  revoke, and says *Revoked* only once it has. From the moment the companion
  receives your revoke, every read still in progress is discarded, even if
  you allow the site again straight away. Your click
  itself cannot reach the companion instantly (the popup and the companion
  run separately), so a read that finished in the instant between your
  click and the companion receiving it may already have reached Lumina.
  A revoke from the popup also blocks the site in Lumina's companion
  itself, durably: it stays blocked across restarts, and even if the site
  is allowed again in Chrome, until you click **Allow Lumina to read this
  site** in the popup. Chrome's settings can take Lumina's access away, but
  they cannot give it back. *Revoked* also means Chrome was seen to no
  longer grant the site; if Chrome could not remove its permission, or did
  not say, the popup says so instead (the site is still blocked).
  If the companion does not answer at all, the popup removes the site
  access in Chrome itself and says the companion did not confirm. That
  fallback is only as strong as removing access in Chrome's settings,
  described next.
  Removing access in Chrome's own settings (`chrome://extensions`, the
  toolbar's extensions menu) stops new reads too, and voids a read in
  progress if the access is still removed when the read ends, or once
  Chrome has told the extension. Chrome tells extensions late, though, and
  offers no way to learn of a removal that was already undone. So if you
  remove access there and restore it within moments, a read that was
  already running may still complete. (Chrome's separate, browser-wide
  "restricted sites" list is not a way to withdraw Lumina's access: in
  testing on Chrome 154 it did not stop this extension's reads. Use the
  popup, or PAUSE.)
- **Navigation Allow is separate from Read Allow.** It is a grant for the
  current Companion connection only, and must be clicked again after PAUSE,
  disconnect, extension restart, or Lumina restart. It does not let a page
  select a destination. To open a URL, begin a fresh owner message with
  `open`, `visit`, `go to`, or `navigate to` followed by the URL (for example,
  `open https://github.com/example`). Lumina may dispatch that exact URL once
  for that owner event. Replaying the same owner event cannot open it again.
  `switch_tab` needs the tab ID, window ID, and exact current URL; Chrome
  checks them again before selecting the tab. Neither action falls back to
  Playwright.
- Revoke in the popup also hides that site's URL and title from Companion
  tab listings. An opaque tab ID may remain visible. It refuses navigation
  to or selection of a revoked site until the owner clicks Allow for that
  site again. PAUSE stops all reads and actions. Unpair and uninstall retire
  the current connection immediately.
- Each action has a unique operation ID. A response distinguishes a browser
  local effect from an ambiguous outcome after dispatch. A timeout or lost
  answer after dispatch stays ambiguous and is never retried automatically.
  A reported tab load is an observation inside Chrome, not proof of any
  remote site's commit or resulting account change.
- Restricted surfaces are never read: every non-`http(s)` scheme
  (`chrome://`, `chrome-extension://`, `file://`, `data:`, …), incognito
  tabs, and password/payment/account-security hosts such as
  `passwords.google.com`, `pay.google.com`, `accounts.google.com` — in any
  equivalent spelling (`ACCOUNTS.GOOGLE.COM.` is still `accounts.google.com`).
- A page read is bound to one document. If the tab navigates or reloads
  before the read finishes — even back to the same URL — the result is
  discarded as stale, never returned.
- **PAUSE LUMINA** in the popup disconnects immediately (a read in
  progress is dropped, and none starts), is remembered across restarts, and
  can only be undone from the popup. If PAUSE and RESUME are pressed in
  quick succession, the last one pressed is the one that holds.
- Diagnostics record metadata only (operation, timing, sizes, hashed
  origin) — never page text, titles, full URLs or pairing IDs.

## Setup (once)

Use the same data directory as the Lumina build you run (`LUMINA_DATA_DIR`;
the default install uses `~/.local/share/lumina`).

1. In the Chrome profile Lumina should use: `chrome://extensions` →
   enable **Developer mode** → **Load unpacked** → select
   `chrome_companion/extension` from this repository. Copy the extension ID
   Chrome shows.
2. Register the native host:

   ```bash
   python scripts/chrome_companion_setup.py --data-dir ~/.local/share/lumina install --extension-id <EXTENSION_ID>
   ```

   The installer refuses (and does not "fix") a Chrome directory that other
   users can write, or an unexpected link or file where it writes. It
   checks every directory on the way there, including those a linked
   (relocated) profile passes through, and writes only into the directories
   it actually opened and checked.

3. Open the **Lumina Companion** popup in Chrome, click **Copy** next to the
   pairing ID, then:

   ```bash
   python scripts/chrome_companion_setup.py --data-dir ~/.local/share/lumina pair <PAIRING_ID>
   ```

4. Restart Lumina. The popup should show **Connected to Lumina**.
5. On a site you want Lumina to read, open the popup and click
   **Allow Lumina to read this site**.
6. To permit the two navigation actions during this connection, click
   **Allow navigation this session** in the popup. **Stop navigation** or
   **PAUSE LUMINA** withdraws it.

`status`, `unpair` and `uninstall` are also available. Reloading the
extension keeps its pairing; removing it creates a new pairing ID.

Chrome 137+ no longer loads extensions from the command line, so step 1 is
manual by design.
