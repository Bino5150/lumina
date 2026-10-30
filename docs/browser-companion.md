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

Twelve tools, in four groups:

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

**Inert fixture interaction** (`chrome_interact`) — focus a plain text
field, set a native checkbox or activate a native disclosure on an explicitly
approved local test document. This has a separate owner grant and mission,
described below. It does not authorize ordinary website controls.

**None of the following exist in this build:** arbitrary button clicks,
typing, filling or submitting forms, posting or sending
anything, screenshots, cookie/password access, or any debugger/CDP access.
There is no selector argument and no page-script tool: Lumina names an
element only by a reference the extension itself handed her for that exact
page.

## Inert fixture interactions (F0)

Prepare an inert page whose controls have no external effects. The supplied
`chrome_companion/fixtures/inert.html` has no scripts, persistence, external
resources or form submission. For example, serve that directory with
`python -m http.server 8765 --bind 127.0.0.1 --directory chrome_companion/fixtures`
and open `http://127.0.0.1:8765/inert.html` in a disposable Chrome profile.
The fixture must use exactly `http://127.0.0.1:<non-default-port>/…`.
Loopback is a restriction, **not evidence of safety**; another local page can
still have consequential event handlers. Only attest a fixture you prepared
and verified. Unknown ordinary website consequences remain unsupported.

1. Allow site reading and Navigation Allow in the extension popup.
2. On the fixture tab, click **I verified this inert fixture — allow interactions**.
   This grants only this connection, exact URL, tab, window, Chrome document
   and extension-private document identity. It does not persist across PAUSE,
   reconnect or restart. Stop fixture interactions, Navigation Allow changes
   and site revoke withdraw the grant. Regranting a site cannot revive it.
3. Begin an authenticated owner request with `test browser fixture <exact URL>`.
   Only this command adds the fixture class to the current mission, capped at
   30 operations and the existing 30-minute lifetime. Tool, page, email and
   peer text cannot create or expand it.
4. Use `chrome_observe`, then pass its `tab_id`, `window_id`, `document_id`,
   `gen` and target `ref` to `chrome_interact`. Fixed actions are `focus`
   (plain text/search input or textarea), `set_checked` (native checkbox,
   boolean `value`) and `set_expanded` (native details/summary, boolean `value`).
   No selector, script, arbitrary button, keypress or submission is accepted.
5. Re-observe after each operation. A generation is consumed before possible
   dispatch; even a new operation ID cannot reuse it after a lost answer.

Interaction targets require the very node observed. Re-rendered lookalikes,
changed documents/URLs, DOM changes, intervening trusted input, hidden,
disabled, readonly, sensitive or obscured controls require re-observation or
refusal. Frames and shadow-root controls are unsupported. There is no scrolling
or alternate targeting to make a refused operation succeed. An already desired
checkbox/disclosure state is observed without another click.

Starting a navigation invalidates the fixture's prior observations, even if
the navigation later fails or is canceled. Wait for the tab to settle and
observe again; reading an old document during a pending load does not make
its controls eligible for an action. A same-document history/fragment change
also requires a fresh observation. Navigation in another tab or a child
frame does not revoke this main-document observation.
Refusals identify a pending navigation, incomplete load or stale document
when known. Wait for navigation/loading to settle before observing again;
waiting may not resolve the condition. If execution is uncertain, observe
actual state to reconcile it and do not retry that action automatically.

The worker watches navigation in the attested tab, and the fixed actuator
also checks lifecycle changes inside the observed document immediately before
acting. These checks cannot cancel an already admitted browser task across
processes. If lifecycle notification is delayed until after admission, an
effect may occur; a detected transition after possible dispatch yields only
an ambiguous receipt, with no automatic replay. This remains an inert,
owner-prepared fixture feature, not an atomic navigation/action API for
consequential websites.

Receipts include the operation ID, kind, ref, observation generation and
bounded boolean before/after state. A pre-dispatch refusal is
`failed_before_dispatch`. A verified local effect is
`browser_local_effect_observed`; `load_confirmed` stays false because no
navigation was established. An injection/transport error, changed binding or
owner-control withdrawal after possible dispatch is `ambiguous_after_dispatch`.
Re-observe to reconcile; never retry automatically. A local state read proves
only that state at the observation instant, not a server commit or durable
application outcome. An already dispatched effect cannot be recalled by PAUSE
or revoke; the receipt must retain ambiguity if those controls overtake it.

### F1 and future consequential actions

F1 fill, clear, replace and select are gated on independent F0 review and
testing. They are not implemented in this F0 candidate. Text entry may invoke
page handlers; neither filling nor focusing authorizes submission or Enter.

F2 is design-only and has no production actuator. A future consequential
operation must preserve the exact proposed content (and attachment hashes),
exact target/account/origin, consequence, action class, mission/scope, current
tab/window/document/element binding, expiry and unique operation identity.
An authenticated owner approval must bind that exact proposal at a trusted
final-admission boundary. Existing site grants, rendered page claims, tool
results and peer messages cannot approve it. Editing the content or target
voids approval. Writable same-UID files are not a sufficient approval boundary;
the dormant site-action/Authority Plane contracts need independent acceptance
before integration. No F0 fixture grant promotes into external-write authority.

Before dispatch, repeat all permission, provenance and binding checks and
atomically spend approval/operation identity. After possible dispatch, record
an unknown outcome until independent service evidence establishes the exact
effect, where available. No retry/replay without verified service idempotency
or reconciliation. A browser acknowledgement or changed page cannot confirm
a publish, message, purchase, upload, deletion or permission change. Live
consequential activation remains a separate owner decision.

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
- That targeting is not trusted on its own: in testing, Chrome 155 ran such
  code in the tab's *newer* document after it had navigated away (even back
  to the same address). So the extension also gives every page it reads a
  private identity of its own, and the final check and navigation refuse in
  any page other than the one Lumina actually read. The same identity binds
  `chrome_navigate`'s element references.

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

After an action is sent, the companion watches the tab for the outcome for
as long as the request allows (it stops a moment early so its answer still
arrives in time). A page that is simply slow to load is therefore confirmed
rather than reported as uncertain, and watching never repeats the action. A
page that still hasn't finished by then is reported as ambiguous — and the
receipt still names the tab the action went to, so it is never mistaken for
another tab's answer.

`chrome_follow_link` and `chrome_navigate` report the URL and document
identity Chrome actually loaded (after a redirect, the URL it landed on,
never the one requested). Link and address navigation only claim success
when the document was replaced.

Back/forward can also confirm a history move within the same page, as used
by single-page apps. This requires Chrome to report a back/forward traversal
on that exact main document after dispatch, with a changed URL that agrees
with both the tab and frame. The verified document ID then stays the same;
Lumina still requests a fresh observation of the landing. A URL change alone,
a missing traversal event, or a same-URL history entry stays ambiguous. This
does not confirm that the app finished updating its content, or repeat the
history move.
Those event listeners exist only while the back/forward request is in flight
and retain only its requested tab's main-frame landing URL until it ends.

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
