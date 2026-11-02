# Channels

Two remote channels are live today. Both share the same underlying trust
architecture (see [Security & Authority](security.md)) — they just sit at
very different points on it. A third, internal surface — the optional
AIvengers Wire wake listener — is described at the end of this page.

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

It ships **off**. Nothing connects until you enable it in Settings →
Communications (the toggle persists across restarts), or start the app with
`LUMINA_WIRE_WAKE=1` in the environment — the canary overrides a stored
"off" for that start only. While enabled, the listener speaks the Wire
protocol and updates the panel; it performs **zero model inference** while
merely listening and has no code path that can start a model turn. When
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
