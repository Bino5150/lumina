# Browser Companion

A bounded bridge that lets Lumina see tabs in **your own** Chrome profile
— the one where you're already signed in — instead of only the
disposable Playwright browser (`browser.py`, unchanged and separate; the
two never fall back to each other).

For install/pairing steps, see [chrome_companion/README.md](../chrome_companion/README.md)
(CLI-only setup today — `scripts/chrome_companion_setup.py`; no Settings
tab for this yet). This page covers the authorization model — what it can
do, and exactly what has to be true before it can do it.

## What it can do

Eight tools, in two groups:

**Read-only** (`chrome_status`, `chrome_list_tabs`, `chrome_get_active_tab`,
`chrome_get_url_title`, `chrome_extract_visible_text`, `chrome_get_links`) —
list tabs, read the active tab, and read bounded visible text/links from
sites you've allowed.

**Navigation** (`chrome_open_owner_url`, `chrome_switch_tab`) — open a URL
or switch to an already-open tab. This is deliberately narrow: **it can
only open a URL you yourself typed in your own message this turn**, and
can only switch to a tab by its exact tab ID, window ID, and current URL.

**None of the following exist in this build:** clicking page elements,
typing, form submission, following a link observed on a page or in a
tool result, browser back/forward, screenshots, cookie/password access,
or any debugger/CDP access. There is no `open_observed_link` tool and no
generic page-action tool of any kind.

## Why navigation is safe from prompt injection

A page, an email, a tool result, or anything else that *isn't* your own
typed chat message cannot make Lumina navigate anywhere. The mechanism:

1. A navigation grant is minted only when your raw message, read as owner,
   *starts with* `open`, `visit`, `go to`, or `navigate to` followed by a
   URL — e.g. `open https://github.com/example`. It's scoped to that exact
   turn and that exact URL; it cannot outlive the turn, and replaying the
   same message can't mint a second grant.
2. `chrome_open_owner_url`'s own `url` argument is never trusted directly —
   the tool has to successfully claim the turn's grant, and the argument
   must match it exactly. A model hallucinating a different URL, or
   content elsewhere in context suggesting one, cannot substitute in.
3. Separately, a human has to click **Allow navigation this session** in
   the Chrome extension's own popup — a toggle Lumina/the hub has no code
   path to set. It resets on disconnect, PAUSE, or restart, so it isn't a
   standing grant.
4. The same per-site allow/deny list and restricted-host denylist
   (password/payment/account-security domains, non-`http(s)` schemes) that
   gates reads also gates navigation.

One nuance worth knowing: because Telegram is treated as a fully-trusted,
owner-originated channel (see [Channels](channels.md)), an
owner-authenticated Telegram message that starts with "open <url>" can
mint the same grant a desktop-typed command can. This is consistent with
Telegram's existing full-trust design, not a separate hole. Discord can
never reach this — Discord sessions are hardcoded non-owner and have no
path to owner status at all.

## Receipts are truthful, not optimistic

Every navigation action returns one of exactly three states:
`dispatched`, `browser_local_effect_observed`, or
`ambiguous_after_dispatch`. If the extension loses the connection, PAUSEs,
or the tab is revoked mid-flight after Chrome was told to act, the result
is reported as ambiguous — never silently retried, and never reported as
a false success or false failure. A reported tab load is an observation
inside Chrome, not proof that a destination site actually committed
anything.

## Revoke, unpair, and uninstall

Revoking a site from the extension popup blocks it durably — across
restarts, and even if Chrome's own permission is re-granted — until you
explicitly click **Allow** again in the popup. A revoked site's tab
listings also hide its URL and title. Unpairing or uninstalling the
extension actively retires any live connection immediately: an in-flight
action gets force-retired and reported as ambiguous, it isn't just left to
time out.

## What's never sent anywhere else

Every Browser Companion result — reads and navigation alike — is tagged
`owner: false` / `external_untrusted` before it ever reaches the model,
exactly like content from any other outside source (see [Security &
Authority](security.md)). Diagnostics record only metadata (operation,
timing, sizes, a hashed origin) — never page text, titles, full URLs, or
pairing IDs. A navigation URL specifically is withheld from the Flight
Recorder, not just redacted.
