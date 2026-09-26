# Operator Commands

A small set of `/` slash commands, typed directly into the chat box, give
you a direct line to specific runtime behavior instead of asking Lumina
to do it conversationally. This entire command family is **desktop-only
by design** — none of it is reachable from Telegram, Discord, or any
tool-call path; the code that dispatches these commands is never imported
by the agent/tool layer at all.

| Command | Does |
|---|---|
| `/status` | Live operator status. |
| `/btw <question>` | Ask a side question without it becoming part of the main conversation thread. |
| `/compact` | Manually trigger context compaction now (see [Memory](memory.md#context-compaction)). |
| `/stop` | Cancel the current foreground turn. |
| `/stop all` | Cancel the current turn **and** trigger the Emergency Interlock (see [Security & Authority](security.md#emergency-interlock)). |
| `/context` | Usage help for the context family. |
| `/context status` | Show the current context-checkpoint state. |
| `/context rebuild` | Run a continuity-checkpoint reconstruction — see [`/context rebuild`](memory.md#context-rebuild). |
