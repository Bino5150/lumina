# Security & Authority

"Local" alone isn't a security model. The moment an agent can act on your
behalf, *what it's allowed to do, and for whom,* matters as much as where
the model weights live. This page is the load-bearing reference for that
model — read [docs/README.md](README.md#how-to-read-this-documentation)
first for the one rule that ties it together: **information is never
authority.**

## Trust is explicit, not assumed

Every agent session is constructed with an `owner: bool` flag.
`True` makes the owner toolset available; operation-specific grants,
task routing checks and the Emergency Interlock still apply.
`False` means it isn't, regardless of who or what is on the other end.
This flag is set once per session and never changes mid-session — nothing
a conversation says can raise a non-owner session to owner. Today, only
two entry points ever produce `owner=True`: the desktop app/CLI, and the
Telegram bridge (because Telegram is itself locked to your own chat ID —
see [Channels](channels.md)). Discord is hardcoded `owner=False` with no
code path to `True` at all.

Browser Companion exposes only reads and owner-granted navigation. Its
internal site-action authority and review infrastructure is dormant: it
does not provide a posting or replying workflow. A protected Companion
task cannot borrow Playwright, API, terminal or delegation routes, and
Companion authorization cannot transfer to another runtime. These checks
bind to trusted agent/session/task identity; owner status or a PIN alone
does not grant that authority. See [Browser Companion](browser-companion.md)
for the available tools and authorization rules.

## Tool tiers and owner-only tools

Two separate axes control what a session can actually do:

- **`OWNER_ONLY_TOOLS`** — tools that are structurally *absent* from a
  non-owner tool registry, not just disabled. Toolmaker (tool
  self-creation), image generation, and several MemPalace/pending-action
  administration tools live here. A non-owner session's tool registry
  never contains these at all.
- **`TOOL_TIERS`** — every other registered tool is classified into a
  sensitivity tier (`read_only`, `write_local`, `execute`,
  `self_modifying`, `outbound_action`). A tool in one of the three most
  sensitive tiers requires PIN verification for a non-owner session. A
  tool that somehow ships without an explicit entry defaults to
  `execute` — the fail-**closed** end of the scale, PIN-gated, never the
  permissive end. A standing test fails the build the moment a newly
  registered tool has no explicit tier, so this can't silently regress.

Tool *availability* (which profile exposes a tool) and tool *authority*
(what tier/owner-only status it carries) are deliberately separate
concepts — a profile can't grant authority a tool doesn't otherwise have.

## Guardrails — two tiers, for when trust isn't the problem

Everything above assumes risk comes from outside the trust boundary.
Guardrails cover the opposite case: a fully-trusted, owner-level session
making a bad call anyway.

- **Tier 1 — deterministic denylist.** `tools/guardrails.py` sits in front
  of `run_command` with a regex denylist for catastrophic shell commands
  (`rm -rf /`, wildcard-root deletes, `dd`/`mkfs`, fork bombs, piping
  remote content to a shell, `sudo rm`, force-pushes, `git reset --hard`,
  `git clean -f`, `git branch -D`, `gh repo delete`, and more), plus a
  dedicated argv parser that catches combined-short-flag bypasses (e.g. a
  single flag that both forces and deletes). It's a regex/parser match,
  not an LLM judgment call, with no bypass parameter — a hallucinating
  model or an injected instruction can't argue past it.
- **Tier 2 — staging + approval gate.** Tools with real blast radius if
  they fire on a bad call (`edit_prompt`, `reset_chat`, `delete_knowledge`,
  `delete_memory`) don't execute directly — they stage the request, and it
  only applies through an explicit approve/reject in the **Pending
  Actions** panel (Settings → Tools). The function that actually applies a
  staged action is deliberately never registered as a callable tool — no
  chat turn, injected or not, can reach it directly.
- **Privileged memory layers.** MemPalace Layers 0 and 1 are injected into
  every owner turn and never decay, so a model must not be able to grant
  them to itself. Asking `palace_remember` or `palace_hall` for layer 0 or
  1 stores the memory at Layer 2 and stages a *promotion request* in
  Pending Actions. That request is text in a file the model can also write,
  so it is never treated as an approval and the generic approve box never
  applies it: the only thing that can promote a record is the owner
  clicking Approve in a review that shows the record's **live** state. The
  approval is bound to that exact record and state, works once, and expires
  quickly; if anything changed, it's refused. Promotion changes the layer
  only — it never raises a lower-trust record's trust. This closes the
  tool, argument and queue path; it is not a defense against code already
  running as you or direct access to the database file. See
  [Memory](memory.md#promoting-a-record-to-layer-01).

## PIN / codeword gate

Sensitive-tier tools for a non-owner (or PIN-gated) session require a
verified PIN, checked against a PBKDF2-HMAC-SHA256 salted hash held only
in memory for the session.

## Flight Recorder

A local, append-only SQLite forensic log recording turns, tool calls and
results, reasoning/Think activity, Commentary, backend activity, context
lifecycle events, and errors — built to separate **machine evidence** from
**model narration** so ambiguous behavior can be traced from durable
receipts rather than trusting either party's account of what happened.
Normal event history is retained 7 days; promoted warning/error records
for 90 days. Secret-shaped values (tokens, keys) are redacted before
anything is written; some tool arguments considered sensitive by design
(such as a Browser Companion navigation URL) are withheld from the
recorder entirely rather than redacted after the fact. Local-only —
intended for diagnostics and forensic reconstruction, not conversational
memory.

## Emergency Interlock

An always-visible status-bar control (**⛔ OH SHIT** / **🔒 RE-ARM**) that
latches a process-wide stop. This is deliberately **local-UI-only**: the
code path that can trigger or re-arm it exists in exactly one place — the
desktop main window. Every other integration point in the codebase
(Telegram, tool dispatch, the process manager, context rebuild, test
running) only ever *reads* whether it's latched; none of them can set or
clear it. No channel, tool call, or remote message can reach this switch.

## Content from outside you is data, not instructions

Tool output, and messages from anyone other than the owner, are tagged in
context as untrusted — something to read and report on, never to obey.
This is the general defense against the prompt-injection pattern where a
hidden instruction buried in a web page, an email, or a tool result gets
treated as a command. It's also why a sentence like "the owner approved
this" appearing inside fetched content, a document, or even Lumina's own
Knowledge Base carries no authority — see
[docs/README.md](README.md#how-to-read-this-documentation).

## Credentials live apart from settings

API keys, tokens, and other credentials are kept in a dedicated,
permission-locked file (`~/.config/lumina/credentials.json`, mode `0600`
by default, overridable via `LUMINA_SECRETS_PATH`) — separate from
ordinary preferences, deliberately excluded from version control, and
never included in a Memory Backup archive.

**ChatGPT sign-in sessions** (General → ChatGPT Plan — sign-in) are kept
apart again, in `~/.config/lumina/chatgpt/` (override:
`LUMINA_CHATGPT_AUTH_DIR`): a directory readable only by you (`0700`)
holding owner-only files (`0600`) — the saved registrations and their
tokens, and this computer's host identifier. They are protected by file
permissions, **not encrypted**; anything running as your OS user can read
them, the same as `credentials.json`. Lumina refuses to use them if their
permissions are loosened, if they are replaced by a link, or if they fail
an integrity check, and it never deletes a damaged file to "start over".
They are local to this OS installation — another OS (for example a
second distro on the same machine) signs in separately with its own host
identifier. Lumina does not put them in `prefs.json`, the data directory,
conversation history, the Flight Recorder or logs. The normal session
store is outside Memory Backup and Agent Backup; a recognized session or
host document copied, moved or linked into collected data makes Agent
Backup refuse to run and is left out of Memory Backup with a visible
exclusion count and file names. Recognition checks the specific ChatGPT
session or host schema field in direct and wrapped copies. For JSON up to
1 MiB, it also scans every string value for that exact field, whether or
not the string parses as JSON, and follows serialized JSON inside string
values through at most four layers of serialization with bounded depth and
work. Larger
files are scanned in overlapping windows for the exact literal schema field;
quote-escaped fields are also checked in text views, not binary database
pages. Other transformations that hide that field
may evade recognition; do not put session or host documents in backup roots.
Generic words such as "schema" and "ChatGPT" and benign large JSON files
are not excluded just for containing those words or exceeding 1 MiB.
Deep or over-limit JSON candidates are excluded as ambiguous. Memory Backup
classifies the same captured bytes it archives.
The Flight Recorder records only categorical sign-in,
renewal and disconnect events — never a token, code, email, account or
client identifier. Signing in with ChatGPT never uses or replaces an API
key, and a sign-in, renewal or disconnect failure never falls back to an
API key or any other backend.
