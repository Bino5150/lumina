# Operator Commands

A small set of `/` slash commands, typed directly into the chat box, give
you a direct line to specific runtime behavior instead of asking Lumina
to do it conversationally. This entire command family is **desktop-only
by design** — none of it is reachable from Telegram, Discord, or any
tool-call path; the code that dispatches these commands is never imported
by the agent/tool layer at all.

A command runs only when you type it into this window's chat box. Lumina
records where every message came in, and only text typed here counts.
What a message says never decides that.

- **Telegram:** a message that would read as a command, including one with
  leading spaces, gets a short fixed reply saying commands are
  desktop-only. It isn't run and isn't sent to the model. If it was a reply
  to a desktop chat, that chat also shows a notice that a command-like
  message was refused. Your ordinary Telegram messages work as before.
- **`/resume`** over Telegram is reserved for a later version of [runaway
  suspension](security.md#runaway-suspension) and isn't active yet. On the
  desktop, review and resume suspended work with `/suspension`.

| Command | Does |
|---|---|
| `/status` | Live operator status. |
| `/btw <question>` | Ask a side question without it becoming part of the main conversation thread. |
| `/compact` | Manually trigger context compaction now (see [Memory](memory.md#context-compaction)). |
| `/stop` | Cancel the current foreground turn. |
| `/stop all` | Cancel the current turn **and** trigger the Emergency Interlock (see [Security & Authority](security.md#emergency-interlock)). |
| `/context` | Usage help for the context family. |
| `/context status` | Show the current context-checkpoint state. |
| `/context reforge` | Run Reforge, a continuity-checkpoint reconstruction — see [`/context reforge`](memory.md#context-reforge). |
| `/context rebuild` | Backward-compatible alias for `/context reforge`; runs the same operation with the same protections. |
| `/suspension` | Open Suspension controls: review and resume suspended work, set up runaway suspension on first launch, or repair/recover its store (see [Runaway suspension](security.md#runaway-suspension)). |
