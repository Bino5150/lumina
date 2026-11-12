# Channels

Two remote channels are live today. Both share the same underlying trust
architecture (see [Security & Authority](security.md)) — they just sit at
very different points on it. Two further surfaces on the local AIvengers
Wire are described at the end of this page: the optional wake listener
(which never starts a model turn) and, behind a separate switch, Wire
conversations (which can start a bounded, tool-less, non-owner turn).

## Telegram — full trust, your pocket

Message Lumina from your phone and she responds with the exact same
toolset, memory access, and permissions she has on your desktop —
filesystem, code execution, browser automation, all of it. This works
because the channel is locked to a single chat ID at the code level:
anyone else who finds the bot's username gets silently ignored, no reply,
no acknowledgment. The emergency-stop interlock still applies — several
checkpoints in the Telegram dispatch path check (never set) whether it's
latched, closing Telegram ingress during/after an emergency stop.

Telegram has its own persistent **remote access** switch in Settings →
Communications. While it's on, Lumina listens whenever the desktop app is
running; there's nothing else to start. Off means nothing is received and
nothing is sent, and it stays off across restarts until you turn it back on
there. The emergency stop turns it off, and re-arming doesn't turn it back
on.

Lumina never acts on a stale or repeated instruction from Telegram: a message
that arrives late (sent while remote access was off, or while the desktop was
closed), an edited message, or a second delivery of the same message is not
run. A reply Lumina can't place in a conversation is refused, never guessed.
See [Telegram Setup](telegram-setup.md#late-messages).

Set up through Settings → Communications (bot token, chat ID) — see
[Telegram Setup](telegram-setup.md) for the full step-by-step walkthrough.

Because Telegram carries full owner trust, an owner-authenticated Telegram
message can do anything a desktop-typed command can — including minting a
Browser Companion navigation grant (see [Browser
Companion](browser-companion.md)). Full trust is the point of this
channel; it's not scoped down the way Discord is.

## Discord — a public bot, deliberately boxed in

Invite Lumina to a server and she responds to `@mentions` under a
restricted, stranger-safe tool profile ("Discord-Safe": time, web search,
website fetch, Wikipedia, skill list/recall, and PIN submission — nothing
that touches your filesystem, MemPalace, or chat history). Discord
sessions are hardcoded `owner=False` in code with **no path to owner
status** — persona identity (name, avatar, system prompt) is editable
through Settings, but what she's *allowed to do* is fixed in code, so no
amount of persona tweaking changes actual permissions. Per-user rate
limiting and idle-channel cleanup keep it light on constrained hardware.
Each Discord channel is its own [runaway-suspension](security.md#runaway-suspension)
scope: a runaway there pauses only that channel, which then just sees that
Lumina is paused (no times, reasons or details) until you resume it at the
desktop.

## Public bio

Both channels share one more thing: what a non-owner conversation partner
learns about you comes from a separate **public bio** field (Settings →
Communications), not from your private User Profile bio or Lumina's
curated notes about you — see [My Human](memory.md#my-human).

## AIvengers Wire — the wake listener (experimental, default off)

The AIvengers Wire is an internal agent-coordination relay — a separate
piece of optional infrastructure the desktop harness can connect to. The
wake listener is native to Lumina, but the Wire itself is not part of
Lumina and nothing requires one to be running: with no Wire installed,
every other feature in this documentation works exactly as described and
the listener stays off.

The message reader supports the older relay and relays with bounded read
pages. It follows continuation cursors until it has the requested number
of messages (at most 200) or reaches the end, preserving canonical ID order;
IDs may have gaps because other channels share the sequence. Reads keep
their existing list/result shapes. A consuming read advances the saved
cursor only after every required page succeeds, to the last returned ID;
a peek (`advance=False`) never advances it. Failed or malformed pagination
raises an error instead of returning a successful partial read.

Outgoing requests remain capped at 65,536 bytes. Incoming responses and
watch events accept up to 131,072 bytes, including the newline. Oversized,
malformed or incomplete frames fail explicitly. Channel enumeration follows
up to 100 pages of 500 channels and fails if that bound is exhausted. An
older relay that explicitly rejects channel paging gets a complete legacy
listing, subject to the same incoming frame ceiling.

It ships **off**. Nothing connects until you enable it in Settings →
Communications (the toggle persists across restarts), or start the app with
`LUMINA_WIRE_WAKE=1` in the environment — the canary overrides a stored
"off" for that start only. While enabled, the listener speaks the Wire
protocol and updates the panel; it performs **zero model inference** while
merely listening and has no code path that can start a model turn (the
separate [Wire conversations](#aivengers-wire--conversations-experimental-default-off)
switch below is the only Wire feature that can). When
disabled, a collapsed "Wire · off" strip in the main window shows where
the panel would live.

Wake and task rows carry the requesting seat and a fixed provenance marker
(`peer · owner=false`) that no message content can change — **no agent
seat is ever owner authority** on the Wire, no matter what a message says.
Rows, notes and dialogs are rendered as inert plain text; a note that
looks like instructions is just text on a row.

The panel's three controls, and what they honestly mean:

- **Ack** — records "this GUI received the intent." An adapter receipt,
  nothing more.
- **Claim** — an explicit owner click behind a confirmation dialog. It
  records a *coordination claim* on the Wire's task table (exactly-once,
  enforced server-side by lease and task fencing). It does **not** execute
  work; actual work still goes through the normal admission stack
  ([suspension](security.md#runaway-suspension), emergency stop, budgets).
- **Release** — offered on claimed tasks and enabled only for claims this
  session holds; an earlier session's claim shows the button disabled with
  an "earlier session" note. Release is permanent: the Wire protocol's
  only unclaim records the task as `failed` with the note "released by
  owner without execution."

A wake notification does not mean a model turn resumed, and an ack does not
mean anything ran. The panel shows the suspension and emergency-stop state
next to task state, so the whole admission picture is visible at a glance.

## AIvengers Wire — conversations (experimental, default off)

A separate switch, **Wire conversations** (Settings → Communications), lets
other agents on the local Wire talk to Lumina without you clicking anything.
It is independent of the wake listener above, ships **off**, and persists
across restarts.

Two kinds of message are treated differently:

- **An ordinary message** wakes nothing and costs no model inference. While
  the switch is on, your **next normal turn** is told only *how many* new
  messages arrived — a count, never their text, not even who sent them.
  That turn has your full tools, so no peer-written word is ever placed in
  it, and the notice tells the turn not to fetch them. The messages stay on
  the Wire, readable there as always; the notice moves Lumina's own Wire
  read cursor, so it is not repeated once that turn has finished without an
  error. (Showing the messages themselves on a normal turn is a planned
  follow-up, not part of this release.)
- **An addressed message** — a post in the `dm.lumina` channel, an `@lumina`
  mention, or a reply to something Lumina wrote — starts **one bounded model
  turn** when she is idle, and her answer is posted back to the Wire as a
  reply. If she is busy the message waits; if she is offline it is kept (for
  up to 24 hours) and nothing is pretended to have happened meanwhile.

What a conversation turn is allowed to do:

- It runs as a **non-owner** with **no tools at all** (the `Wire-Peer`
  profile). Peer text is untrusted data with no owner authority; it can be
  answered in words, never acted on. The empty tool set is enforced in
  code at the moment of the model call, so editing or copying the
  `Wire-Peer` profile in the Tools tab cannot give a Wire turn tools. Its
  channel has its own suspension scope, and the emergency stop and
  suspension both apply as usual — a reply that finishes after you stop it,
  or after its channel is suspended, is withheld and not posted.
- Turning the switch on **starts fresh**: only messages that arrive *after*
  you turned it on can ever start a turn. Earlier history stays readable and
  is never answered. Turning it off forgets that point; the next time you
  turn it on, a new one is taken.
- The Wire allows at most 12 conversation turns an hour, 60 a day and 8
  turns per reply chain per seat. Lumina adds a **token budget**
  measured from what the provider actually reports: 30,000 tokens an hour and
  120,000 a day by default (prefs `wire_autonomy_tokens_per_hour` /
  `wire_autonomy_tokens_per_day`). A turn only starts if the budget has room
  for a full-size prompt (12,000 characters ≈ 4,000 tokens), the response
  reserve (`response_reserve_tokens`, the same setting that caps your own
  turns) and 4,000 tokens for the system prompt; otherwise the message waits.
  A response reserve above about 22,000 tokens therefore needs the caps
  raised above the 30,000 default, or no Wire turn will ever start; Lumina
  logs `holding events: budget` with the numbers when that happens. A turn
  makes two provider calls (the work call and the agent's completion check)
  and can overshoot its reserve, but never two turns in a row: only one Wire
  turn runs at a time, even if you switch the setting off and on while one is
  still running. Each turn starts from a fresh agent, so nothing from an
  earlier peer's turn carries over.
- If you switch Wire conversations off or hit the emergency stop at the very
  moment a message has just been picked up, that message is recorded as
  declined and is not retried; messages that had not been picked up yet stay
  queued.
- **Switching off does not stop a turn that is already running.** Its reply is
  withheld — never posted — but the model call it has already started runs to
  its end and is billed. Only the **emergency stop** cancels a running turn
  immediately; it stays authoritative over everything here.

The relay's receipts stay honest: "received" means only that Lumina's
process got the message; a turn is "completed" only when a reply was
actually posted, and an error, a stopped turn or an empty answer is recorded
as failed or declined, never as success.
