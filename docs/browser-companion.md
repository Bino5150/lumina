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

Eleven tools, in three groups:

**Read-only** (`chrome_status`, `chrome_list_tabs`, `chrome_get_active_tab`,
`chrome_get_url_title`, `chrome_extract_visible_text`, `chrome_get_links`) —
list tabs, read the active tab, and read bounded visible text/links from
sites you've allowed.

**Navigation** (`chrome_open_owner_url`, `chrome_switch_tab`,
`chrome_follow_link`) — open a URL you typed yourself, switch to an
already-open tab, or open one link Lumina already read on a page. This is
deliberately narrow: owner URL opening requires the exact URL you typed in
this turn; tab switching requires the exact tab ID, window ID, and current
URL; link following requires a link delivered from an allowed document and
verified again just before navigation.

**Generic browsing** (`chrome_observe`, `chrome_navigate`) — see any
allowed page the way a person scans it, and move around while Lumina carries
out your request: follow a link she observed, open an address on one of your
sites, or go back/forward. These work the same on every website; no
site-specific support is needed or consulted. See
[Generic browsing](#generic-browsing-within-your-request) below.

**None of the following exist in this build:** clicking buttons or other
page controls, typing, filling or submitting forms, posting or sending
anything, screenshots, cookie/password access, or any debugger/CDP access.
There is no selector argument and no page-script tool: Lumina names an
element only by a reference the extension itself handed her for that exact
page.

## Following a link you already read (Stage 2)

`chrome_follow_link` opens one link inside a site you have already allowed.
It is the only companion action whose destination comes from a page, so it
is worth being precise about why that is still safe:

- Lumina names the link by the **exact text and href** delivered by
  `chrome_get_links` in this connection, on the **exact tab and document**
  (`document_id`) she read it on. The extension then re-reads the live document and navigates
  only to the href *that document reports* — never to the href Lumina
  passed in. A model that invents a URL gets `target_not_found`, because
  nothing on the page matches the invented claim.
- The link must be **unique** on that page. Two links with the same text
  and destination are `target_ambiguous`; Lumina does not get to pick one.
- It must be on the **same origin** as the page you authorized. A link
  pointing off-origin is `cross_origin_target`, however the page describes
  it.
- If the link has vanished, moved, or the document has been replaced since
  she read it, the action fails without navigating. It is never retried
  against a different target, and the origin is never widened to make the
  action succeed.
- Chrome targets fixed extension code to that exact source document for the
  final check and URL navigation. This is not a synthetic click. No click
  event handler runs because of Lumina.

The selected link must be same-origin and HTTP(S). A server may process a GET
with side effects, and a redirect after dispatch may land on another origin.
If the resulting URL and new document cannot be verified as the selected
destination, the receipt is `ambiguous_after_dispatch`. Stage 2 does not
type, fill forms, submit forms, or perform generic site actions.

Page text still cannot mint authority. A page claiming "the owner approved
this" changes nothing: the owner still has to have allowed the origin, and
still has to have granted Navigation Allow for this connection.

## Generic browsing within your request

`chrome_observe` lists the visible links, buttons, fields and toggles of an
allowed page — the ones in view first — each with a short **reference**
(`e12`), its accessible name, and a little context (which form, list item
or section it sits in), plus the headings and scroll position. Two buttons
that both say "Next" are two references with different context; Lumina picks
one. It can filter by name (`query`), scroll first (`scroll`: up, down, top,
bottom — the viewport only), or return only what is new since an earlier look
(`since_gen`), which keeps long sessions small. The value of a password,
one-time-code or card field is never sent; she only learns that the field
has a value.

`chrome_navigate` moves the browser:

- `ref` — follow a link reference from `chrome_observe`, on that exact page.
  As with `chrome_follow_link`, the reference is a claim, not a destination:
  the extension finds the very element it registered, checks that it is still
  there, still visible and still the same link, and navigates only to the
  address that live link holds. If the page re-rendered it, exactly one
  identical replacement is accepted; none is `stale_target`, two or more is
  `target_ambiguous` — never "the first one".
- `url` — open an address in a new tab (or, given the exact current page,
  in the same tab).
- `back` / `forward` — the tab's own history, from the exact current page.

**Where it may go.** Generic navigation needs an owner **mission**, which
exists only while Lumina is carrying out a request **you** sent her directly
(your own chat message, or your own Telegram — see the note below). It
covers looking and navigating only. Its sites are:

1. sites you allowed in the extension popup (checked live by Chrome and the
   extension at the moment of navigation, so a revoke takes effect at once),
   and
2. sites you named by address in that same message (`https://example.org/…`,
   `www.example.org`, or `example.org`).

Nothing a page, an email or a tool result says can add a site. A link to any
other site is `outside_mission_scope`; ask Lumina to continue there by
allowing the site in the popup or naming it in your message. The mission
also ends with your request, on any reconnect of the extension, after 30
minutes, or after 60 navigations, and the popup's **Allow navigation this
session** switch is still required. History moves go wherever the tab's own
history leads; the receipt then says where it landed (and never names a
restricted page).

**What it refuses before moving anything.** Restricted surfaces (password,
payment and account-security pages, non-web schemes), sites you revoked, a
link that only jumps within the current page, and addresses whose path looks
like an account action — sign-out/log-out, unsubscribe, deactivate, delete.
That last check is a tripwire over the address, not a guarantee: any website
can attach a side effect to an ordinary-looking link, and a GET request
reaches the site's server. Lumina reports what the browser did, never that a
site did or did not change something.

After a successful navigation the same tool call returns a fresh
`chrome_observe` of the page she landed on, so she rarely needs an extra
step to see it.

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
5. A page may propose the *destination* of an already-authorized origin,
   but only for a link delivered by `chrome_get_links` on the same document,
   still present and unique at dispatch, and only if the owner allowed that
   origin. Proposing a destination is not approving it.
6. Generic navigation (`chrome_navigate`) additionally needs the mission of
   your own current request. Its site list comes only from your popup
   allowances and the addresses in your own message; the extension checks
   every destination against both again before it moves.

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

`chrome_follow_link` and `chrome_navigate` report the URL and the new
document identity Chrome actually loaded (after a redirect, the URL it
landed on, never the one requested), and only claims success when the tab reached the
destination *and* the document was replaced. A same-document load that
never replaced the page is reported ambiguous, not as a navigation.

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
