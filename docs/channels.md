# Channels

Two remote channels are live today. Both share the same underlying trust
architecture (see [Security & Authority](security.md)) — they just sit at
very different points on it.

## Telegram — full trust, your pocket

Message Lumina from your phone and she responds with the exact same
toolset, memory access, and permissions she has on your desktop —
filesystem, code execution, browser automation, all of it. This works
because the channel is locked to a single chat ID at the code level:
anyone else who finds the bot's username gets silently ignored, no reply,
no acknowledgment. The emergency-stop interlock still applies — several
checkpoints in the Telegram dispatch path check (never set) whether it's
latched, closing Telegram ingress during/after an emergency stop.

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

## Public bio

Both channels share one more thing: what a non-owner conversation partner
learns about you comes from a separate **public bio** field (Settings →
Communications), not from your private User Profile bio or Lumina's
curated notes about you — see [My Human](memory.md#my-human).
