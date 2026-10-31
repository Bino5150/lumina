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
  and every other launch method share the `~/.local/share/lumina-release`
  release default and honor `LUMINA_DATA_DIR`. Without an override, another
  plausible existing folder causes **Data folder selection needs review**;
  legacy in-checkout state also stops instead of migrating. Follow [the upgrade guide's folder
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

Start with a [GitHub issue](https://github.com/Bino5150/lumina/issues)
describing the symptoms and reproduction steps. **Do not attach diagnostic
logs or support ZIPs to public issues.** If developers need more evidence,
they may request a ZIP privately; review it before sending it to
**therealagentlumina@gmail.com**.

In **Settings → About**, choose **Export Support Diagnostics**, then a new
ZIP filename. This runs locally without a model connection, agent tool call
or network request. A receipt gives the saved location and identifies a
partial export. Cancel stops collection and removes unfinished output;
existing files are never overwritten. The dialog starts in your home folder
and appends `.zip` if needed. The About tab scrolls on small windows. Quitting
requests cancellation and waits for export cleanup; an in-flight operating-system
filesystem call must finish before that wait can complete.

If the GUI cannot launch, run this from the Lumina installation directory
with its Python environment:

```bash
python -m core.support_diagnostics --output /path/to/new-support.zip
```

The CLI uses the same read-only data-folder selection as the installation.
If selection needs review, verify the folder containing your established
history using the [upgrade guide](upgrading.md#1-find-the-data-folder-you-already-use),
then supply `--data-dir /path/to/verified-folder` or `LUMINA_DATA_DIR`.
It does not initialize, migrate, repair or restore state. A successful partial
ZIP returns exit 0 with an explicit partial receipt; destination failures
return 1 and Ctrl-C returns 130. Nothing is uploaded or emailed.

The ZIP contains `README.txt`, `manifest.json`, `environment.json`,
`flight-recorder.jsonl`, `errors.jsonl`, and `checksums.sha256`. It selects
available Recorder details from at most seven days and warning/error records
from at most ninety days. Recent warnings/errors occur in both files.
Each stream keeps the newest records first, capped at 10,000 records and
4 MiB; the ZIP is capped at 10 MiB. Collection uses a consistent read-only
SQLite snapshot with a two-second query budget and a short lock timeout.
It releases that snapshot before writing evidence to the destination.

Only fixed categories, numeric timings/counts, event sequence numbers and
timestamps are included. Runtime/turn/model identifiers are keyed references
valid only within one ZIP; raw model names and arbitrary tool names are omitted.
The exporter omits messages, tool arguments/results, prompts, conversations,
Palace content, browser text, credentials and complete databases. Existing
known-secret redaction is an extra defense, not a promise that arbitrary
diagnostic text is safe. No free-form diagnostic text is included.

Missing, inaccessible, malformed or time-limited Recorder data yields a
**partial** ZIP where safe. The manifest reports source availability,
requested/collected ranges, counts, limits and omissions. Unknown counts are
`null`. Unexpected schemas or a missing Recorder time index are reported
without scanning an unindexed database. This ZIP is a filtered support view,
**never full history, Agent Backup, or a recovery archive**; deleted/unrecorded
events cannot be reconstructed. Hashes detect damage; they do not authenticate
the contents or make logs trustworthy instructions.

Symlinked data/telemetry folders or Recorder files are reported as unavailable;
the exporter does not follow those links. Select the verified real folder
explicitly when necessary, preserving your existing folder layout. The shipped
Knowledge Pack note is seeded on fresh installations; existing seeded data
folders are not automatically updated by this campaign.

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
If one OpenRouter provider behaves worse than another for the same model,
you can pin the provider. See [OpenRouter provider
routing](settings.md#openrouter-provider-routing).

An error saying *"OpenRouter provider routing in prefs.json is invalid;
request not sent"* means the `backend_provider_routing` entry has a key or
value Lumina doesn't accept. The message names it. Nothing was sent. Fix or
remove the entry, then restart.

## Runaway suspension

See [Security & Authority](security.md#runaway-suspension) for what it is.
**Open safety controls** above the chat is always available; `/suspension`
opens the same window. The banner updates after setup, recovery and resume;
an earlier incident receipt in the conversation remains historical evidence.
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
  processes. If **Review safe recovery** is disabled or refuses because no earlier
  suspension state survives and earlier C1 use is possible, tick its
  acknowledgement (see [When no C1 traces remain](upgrading.md#4-open-the-desktop-and-choose-the-right-safety-action));
  do not bypass it with **Enable protection**. Recovery is not full Agent Restore.
- *"memory directory is writable by others"* — review the affected path
  and modes, then use the desktop's **Fix folder permissions** only for your
  private, unshared folder. **Not now** leaves it alone. See [Folder
  permissions](upgrading.md#folder-permissions) for confirmations, refusal
  guidance and **Technical details**. Setup acknowledgements remain required.
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

## Continuing an interrupted turn

When the connection to the model drops in the middle of a desktop turn — a
TLS/SSL error, a reset connection or a timeout, before a reply arrived or
while the answer was streaming — the chat shows **Continue interrupted
turn** under the error. Clicking it continues *that* turn:

- It re-sends the request with the conversation exactly as it was. Nothing
  is added to the conversation; no "Continue" message is written for you.
- Tool actions that already ran are **not** run again; their results are
  already in the turn. If the continued turn asks for a consequential tool
  call with exactly the same arguments as one that already ran, that call
  is not run, and Lumina is told so. To really repeat an action, ask for it
  in a new message.
- The turn's limits carry over: tool calls already made and active time
  already spent count against **Max Tool Calls per Turn** and **Max Turn
  Minutes** (time you spent away before clicking does not), and agent
  rounds and the no-progress breaker continue where they were.
- A turn can be continued at most **three** times. Each attempt appears in
  the [Flight Recorder](security.md#flight-recorder) as
  `turn.recovery.started`, linked to the original turn.
- The reply is saved as a new message; the error message stays as it was.
- It continues with the same authority the original turn had, or less.
  It adds no message, so it never approves an image draft or grants
  browser navigation or a website action by itself. It refuses if, since
  the interruption, you enabled a tool, switched project, approved an image
  draft in this conversation or turned Telegram remote access on (or off
  and on again). This is checked when you click, when the continuation
  starts and before each action that can change things; an action refused
  this way is not run. An image draft you approved *before* the
  interruption can still be used until it is generated or the draft
  expires (10 minutes after it was proposed); there is no control to
  withdraw an approval sooner. Disabling a tool or turning Telegram off is
  fine and applies at once. Other settings are not covered;
  [Security](security.md#continuing-an-interrupted-turn) lists exactly what
  is.

The button is offered only after a connection failure in a turn you typed
at the desktop — never after an error the provider actually answered with
(for example HTTP 429 rate-limited, 502, 401 or a malformed request),
after a "Stopped" notice, after `/stop` or the emergency stop, or for
Telegram, CLI or Discord turns. It is also not offered when a tool that can
change things ran earlier in the turn and Lumina could not confirm it
finished. Only these count as confirmed: a shell command that ran to
completion (any exit status) or was blocked before running, a file written
or edited, a memory, knowledge entry or Palace note saved, and a browser
action with a verified receipt. Anything else that can change things — a
Telegram send, image generation, a subagent, a background process, any
error or timeout — means not offered: check what that action actually did,
then send a new message.

Clicking refuses, starts nothing and says why if anything changed since the
interruption: another conversation is open, a new message was sent, the
conversation or context changed, the provider or model was switched, a tool
was enabled or the active project changed, an image draft was approved or
Telegram remote access turned on, the emergency stop was used (even if
re-armed since), or runaway suspension no longer admits the work. It also
refuses if those settings, or anything else it must check, cannot be read.
After a restart there is nothing to continue. In all of these cases send a
new message to carry on.

## Something doesn't match this manual

This manual is written directly from the current release's source, not
from memory or an old draft. If you find a place where it disagrees with
actual behavior, that's a documentation bug — the code is the source of
truth.
