# Troubleshooting

## Settings crashes on a fresh install

This used to be a real bug: `config.example.py` had drifted behind
`config.py`, and two Settings tabs read a couple of the missing keys
without a safe fallback, so opening **Settings → General** or **Settings
→ User Profile** raised `AttributeError` on a brand-new install. This is
fixed — `config.example.py` is now kept byte-identical to `config.py`'s
full public setting set, enforced by a standing test
(`tests/test_config_example_parity.py`). If you still hit an
`AttributeError` opening Settings after a fresh `cp config.example.py
config.py`, that's a regression worth reporting, not expected behavior.

## Where things live

- Data directory: chat history, MemPalace, Knowledge Base, prefs and the
  Flight Recorder live under the selected folder. Public `main.py`
  defaults to `~/.local/share/lumina-release` and honors a nonempty
  `LUMINA_DATA_DIR`; `start_lumina.sh` always forces that release folder.
  Standalone bridges instead default to the platform location (normally
  `~/.local/share/lumina` on Linux). Follow [the upgrade guide's folder
  check](upgrading.md#1-find-the-data-folder-you-already-use) before
  changing launch methods. If history looks missing, stop and preserve
  both folders; do not delete one or initialize again to fix the display.
- Credentials: `~/.config/lumina/credentials.json`, mode `0600` (override
  with `LUMINA_SECRETS_PATH`) — never inside the data directory, never in
  a Memory Backup archive.
- ChatGPT sign-in sessions: `~/.config/lumina/chatgpt/` (override with
  `LUMINA_CHATGPT_AUTH_DIR`) — owner-only files, local to this OS
  installation and outside normal backup roots. Recognized copies in backed-up
  data are excluded. If Settings reports the saved ChatGPT
  connection "could not be read safely", the files were damaged or their
  permissions/ownership changed; Lumina leaves them untouched. Restore the
  files, or move that directory aside to sign in from scratch (a moved-aside
  session can still be ended in ChatGPT Settings).

## Diagnosing a bad run

The Flight Recorder (see [Security & Authority](security.md#flight-recorder))
is the most reliable source of truth for "what actually happened" during
an agent run — it separates machine-recorded evidence from model
narration, so if a tool call's behavior is ambiguous, that's the first
place to look rather than trusting either the model's account or your own
memory of the session.

## "Stopped" notices and refused tool calls

A few short notices mean one of the tool-call limits in [Settings →
General](settings.md#agent-rounds-and-tool-call-limits) ended a turn. None
of them is a crash, and nothing continues on its own afterward. After the
first and last of these (a second refused batch, or the no-progress
breaker) the work is also **suspended**: resume it in Suspension controls
(`/suspension`) first (see [Runaway suspension](#runaway-suspension)). For
the others, send a new message to carry on:

- *"stopped after the model again produced a tool batch that could not be
  run safely"* — the model twice asked for more tool calls in one response
  than **Max Tool Calls per Response**, or twice produced a tool batch that
  was cut off at its output limit. None of those calls ran.
- *"this turn reached its tool-call limit"* — the turn used **Max Tool Calls
  per Turn**.
- *"this turn reached its time limit"* — the turn ran for **Max Turn
  Minutes**.
- *"the recent tool calls in this turn made no progress"* — the
  [no-progress breaker](settings.md#no-progress-breaker) stopped a run of
  calls that kept repeating calls with results already known, or kept
  failing.

### If the no-progress breaker stopped a turn that was doing real work

The breaker can be wrong in one known way. When comparing results it
ignores numbers, timestamps and IDs, so a result whose **only** change is a
number, such as a progress percentage or a byte count, looks like nothing
new. Twenty such checks in a row (five for a tool that can change things)
stop the turn.

To recover:

1. **Resume, then send a new message.** The stop suspended the work: open
   Suspension controls (`/suspension`) and resume it. Every turn then starts
   with a clean slate, so the model can carry on. It helps to say how:
   "check less often", "wait, then check once", or "use the result you
   already have".
2. **If it keeps happening for a workflow you trust**, raise the thresholds
   in `prefs.json` (see [Settings](settings.md#no-progress-breaker)) and
   restart. They can't go past their fixed maximums.
3. **If the chat now carries many repeated tool results**, `/context
   reforge` may help: it can shrink what every later request re-sends. It's
   optional and not guaranteed to fix anything.

`breaker.tripped` in the Flight Recorder shows which tools repeated, how
many calls ran and how long the streak was. Each `tool.result` records
whether that call counted as progress.

Inside a turn you may also see tool results reading *"Refused by Lumina
before execution"*. That is the first, recoverable refusal: the calls did
not run and the model was asked to re-plan.

An error reading *"an internal request ended on an assistant message and
was not sent"* means an internal safety check stopped a malformed request
before it reached the model provider. It should never happen in normal
use; the Flight Recorder (`provider.dispatch_refused`) has the details.

If a model keeps hitting these limits on one provider, look at
`provider.response` in the Flight Recorder: it names the serving provider
(for OpenRouter, the actual upstream provider) and how each response ended.

## Runaway suspension

See [Security & Authority](security.md#runaway-suspension) for what it is.
What the notices mean:

- *"runaway suspension is active since … Nothing was run"* — that work was
  suspended. Open Suspension controls (`/suspension`), review the incident
  and **Resume selected**. Your message wasn't sent; send it again after
  resuming.
- *"this work began before a runaway suspension and can no longer run"* —
  a task, scheduled task or queued message from before the suspension.
  It won't run, even after you resume; start it again if you still want it.
- *"Not yet: … earlier dispatch(es) are still running"* — work admitted
  before the suspension hasn't finished. Wait and resume again.
- *"some earlier work's process stopped, so its outcome is unknown"* — a
  Lumina process quit partway through. Check whether that work happened
  (the incident lists it), tick the acknowledgement and resume.
- *"Runaway suspension isn't ready for this data folder"* or a failed
  identity check — follow [the desktop upgrade and recovery steps](upgrading.md#4-open-the-desktop-and-choose-the-right-safety-action).
  They cover first initialization, damaged/missing state and active
  processes. If **Recover…** is disabled or refuses because no earlier
  suspension state survives and earlier C1 use is possible, tick its
  acknowledgement (see [When no C1 traces remain](upgrading.md#4-open-the-desktop-and-choose-the-right-safety-action));
  do not bypass it with **Initialize**. Recovery is not full Agent Restore.
- *"global suspension unconfirmed"* — a suspension couldn't be written
  down, so it holds in this desktop process only, and writing it is retried
  on the next request. Check Suspension controls; a restart before it is
  written would lose it.
- *"a runaway suspension is active, so staged items can't be applied right
  now"* — you approved a pending action, Palace promotion, pending tool or
  image draft while something is suspended. Nothing changed and the item is
  still pending: resume in Suspension controls, then approve it again. An
  image generation reporting `outcome: suspended` spent nothing.
- The **standalone Telegram or Discord bridge** refuses everything until the
  desktop has initialized runaway suspension for the same data folder.

## Telegram setup

[Telegram Setup](telegram-setup.md) has the full step-by-step walkthrough
— BotFather token creation through starting the bridge — plus a
troubleshooting table for the most common setup error strings.

## Backend connection errors

A `ConnectionError` naming a backend and a URL (e.g. "Cannot reach
llama.cpp at http://localhost:8080") means Lumina couldn't reach the
server that backend is configured to use — check that the server is
actually running and that the URL in Settings matches it.

## Something doesn't match this manual

This manual is written directly from the current release's source, not
from memory or an old draft. If you find a place where it disagrees with
actual behavior, that's a documentation bug — the code is the source of
truth.
