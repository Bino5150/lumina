# Settings

Settings is a tabbed panel (`ui/settings/panel.py`). Current tabs, in order:

| Tab | Covers |
|---|---|
| ⚙ General | Backend selection, reasoning effort, context limits, agent rounds and tool-call limits (see below), context compaction, dreaming toggles, global agent behavior prompt, and the backend-specific **ChatGPT Plan** account and model controls (see below) |
| 👤 User Profile | Your own bio and Lumina's curated notes about you — see [My Human](memory.md#my-human) |
| 🎭 Personas | Create/edit/import/export personas — see [Personas & Skills](personas-and-skills.md) |
| 📡 Communications | Telegram, Discord, and your public bio — see [Channels](channels.md) |
| 🧠 Memory | Browse/edit flat memory and MemPalace entries; separate Legacy Backup, eligibility-gated Verified Agent Backup, and acknowledged Incomplete State Export — see [Agent Backup](agent-backup.md) |
| 📚 Knowledge | Browse/edit Knowledge Base entries |
| 🧩 Skills | Browse Lumina's procedural skills — see [Personas & Skills](personas-and-skills.md#skills) |
| 🔧 Tools | Tool profiles, and the **Pending Actions**/**Pending Tools** approval queues (a Palace promotion request opens a review of the record's live state instead of a plain confirm) — see [Security & Authority](security.md) |
| 🗓 Scheduled Tasks | Inspect/cancel scheduled and background work |
| 🖼 Multimodal | Voice (TTS), Speech-to-text, Vision routing, Image Generation routing and credentials — see [Multimodal](multimodal.md) |
| 🔮 Oracle | Placeholder — a related, separate project, not yet available here |
| ✨ About | Version, links, update check |

(The Multimodal tab's file on disk is still named `tts_tab.py` with class
`MultimodalTab` — it grew from a TTS-only tab into the home for
Voice/STT/Vision/Image-Gen and hasn't been renamed yet. Functionally it's
one tab.)

## Agent rounds and tool-call limits

Four controls in **Settings → General** bound how much tool work one turn
can do. They are independent: hitting one never relaxes another.

| Setting | Default | What it limits |
|---|---|---|
| **Max Agent Rounds** | 20 | Model responses per turn that may request tools. Not a tool-call limit — one response can request many calls. (Called "Max Tool Iterations" before.) |
| **Max Tool Calls per Response** | 256 | A response requesting more calls than this is refused **whole**: none of its calls run, and the model is told to re-plan. |
| **Max Tool Calls per Turn** | 512 | Calls one turn may run in total. The model gets a one-time warning at 80%; at the limit the turn stops before the next call. |
| **Max Turn Minutes** | 60 | Wall-clock time per turn, checked before every model request and every tool call. Warning at 75%; a call already running finishes under its own timeout. |

Separately from these numbers, a response that was **cut off at its output
limit** (Response Tokens) while requesting tools is refused whole too — a
truncated batch is not the batch the model finished planning. A first
refusal is recoverable (the model gets its next round); a second refusal in
the same turn ends it **and suspends** that conversation's work (see
[Runaway suspension](security.md#runaway-suspension)). When any other limit
ends a turn, you see a short notice and nothing continues on its own: your
next message starts a fresh turn.

### No-progress breaker

The limits above count calls. A separate breaker watches whether the calls
are still getting anywhere. A call makes **no progress** when it repeats a
call this turn already made and gets back a result already seen, or when it
fails (an unknown tool, bad arguments, an error). Any call that brings back
something new resets the count.

| Consecutive no-progress calls | Warning to the model | Turn stops |
|---|---|---|
| Any tools | 10 | 20 |
| Tools that can change things (anything not read-only) | 3 | 5 |

The warning tells the model to stop repeating itself. When the turn stops,
you see a notice, and calls the model asked for but that never ran are
marked as stopped. Nothing continues on its own: the stop also **suspends**
that conversation's work until you review and resume it in Suspension
controls (`/suspension`; see [Runaway
suspension](security.md#runaway-suspension)).

The breaker ignores numbers, timestamps and IDs when comparing results, so
a clock or counter changing doesn't count as new information. Real browsing
(scroll, then read new content), editing and re-reading a file, and watching
a process whose output grows are all progress.

These four numbers have no Settings fields. They can be changed in
`prefs.json` (`no_progress_read_only_warn`, `no_progress_read_only_trip`,
`no_progress_write_warn`, `no_progress_write_trip`; restart to apply).
Lumina keeps each one within fixed bounds: read-only trip 5–64, write trip
2–16, and each warning below its trip. A value outside the bounds is pulled
back inside, and a value that isn't a number is ignored, so the breaker
can't be turned off.

Every refusal, warning and stop is recorded in the Flight Recorder
(`tool.batch_refused`, `turn.budget_warning`, `turn.budget_exhausted`,
`breaker.warning`, `breaker.tripped`), and each model response records
which provider served it and how it ended (`provider.response`), so a bad
run can be traced to its source.

## Backend Abstraction

Lumina's LLM layer is fully abstracted behind one shared interface —
switching backends is a dropdown in General, not a code change. Fifteen
ship out of the box:

**Local**
- llama.cpp (primary, recommended)
- LM Studio
- Ollama
- vLLM

**Self-hosted gateway**
- OmniRoute — routes through a local endpoint you run yourself to 200+ upstream providers, many free

**Cloud, native**
- Anthropic (Claude), Google (Gemini), OpenAI, Moonshot (Kimi), Alibaba (Qwen/DashScope)
- ChatGPT Plan — foreground text chat and enabled local tools using a connected ChatGPT account, separate from the OpenAI API-key backend

**Cloud, OpenAI-compatible**
- OpenRouter, DeepSeek, Groq, and any other OpenAI-compatible endpoint via the generic Custom slot

Each backend remembers its own context window and memory-injection limits
independently, and a backend change takes effect immediately — no
restart. Endpoints are provider-owned for every backend except llama.cpp,
LM Studio, Ollama, vLLM, OmniRoute, and Custom — those six are the
intended route for a gateway or a corporate proxy; pointing a fixed cloud
provider at a different URL is refused rather than honored.

### OpenRouter provider routing

OpenRouter serves most models through several upstream providers. By
default it picks one for you, and the same model can behave differently
from one provider to the next. To choose the provider yourself, add a
`backend_provider_routing` entry to `prefs.json` (there's no Settings field
for it). Quit Lumina before you edit, then restart. For example, to use only
Crusoe's endpoint, with no fallback to any other provider and a price cap:

```json
"backend_provider_routing": {
  "openrouter": {
    "only": ["crusoe/fp4"],
    "allow_fallbacks": false,
    "require_parameters": true,
    "max_price": {"prompt": 0.15, "completion": 0.5}
  }
}
```

The object is sent unchanged as OpenRouter's
[`provider` routing field](https://openrouter.ai/docs/guides/routing/provider-selection)
on every OpenRouter request: chat, tool work, the final reply, and
background jobs such as chat naming and dream summaries. Lumina accepts
`order`, `only`, `ignore`, `quantizations`, `allow_fallbacks`,
`require_parameters`, `sort` (`price`, `throughput` or `latency`),
`data_collection` (`allow` or `deny`) and `max_price` (`prompt`,
`completion`, `request`, `image`, in dollars per million tokens). Provider
names are OpenRouter's slugs, as shown on the model's page; a slug with a
suffix such as `crusoe/fp4` selects one endpoint of that provider.

With `allow_fallbacks` set to `false`, a request the chosen provider can't
serve fails with an error instead of going somewhere else. That includes a
provider that's down, rate-limited, or priced above `max_price`. A value
Lumina doesn't recognize stops the request before it's sent, with an error
naming the problem, rather than being ignored. Removing the entry (or
setting it to `{}`) goes back to OpenRouter's own choice. Other backends
ignore this setting.

Each request's routing is recorded with its `provider.dispatch` event in the
Flight Recorder. `provider.response` names the provider that actually
answered. If a pinned request is ever answered by a provider outside the pin,
Lumina records a `provider.pin_mismatch` warning.

## Reasoning Control

Every backend that supports it gets a per-backend, per-model **Reasoning
Effort** control in General. The underlying contract
(`core/backends/reasoning.py`'s `ReasoningCapabilities`) describes, per
model: which effort levels exist, whether reasoning is mandatory, and
whether a token budget is supported instead of a named level. The combo
box in Settings has to handle six distinct states depending on what's
currently known about the selected model — a static list of efforts, a
model with no reasoning control at all, a model where reasoning is
mandatory, or an OpenRouter or Anthropic model whose capabilities haven't
been discovered yet. For Anthropic, efforts the provider's model list
reports outrank Lumina's built-in table; models in that table work offline,
and any other Claude model shows its efforts after one successful
discovery.

If a backend doesn't declare any reasoning capability, the default is
**no override at all** — Lumina simply doesn't send a reasoning parameter,
rather than guessing. Your choice is saved per backend *and* per model, so
switching models on the same backend doesn't carry over a setting that
doesn't apply.

## ChatGPT Plan

Select **ChatGPT Plan** in General's Backend dropdown to show its account
controls and account-specific Model picker near the top of the page. The
model picker shows names supplied by the connected account's catalog and
saves the selected model slug. If that model is no longer listed, select
another one; Lumina does not substitute a model. **Reasoning Effort** stays
at **Provider Default**. The API Key, Server URL, custom model and Response
Tokens controls are hidden for this backend. The other local context
controls remain available.

**Continue with ChatGPT** opens your system browser. For a first
registration, **Add another account**, or registration again after an invalid
client, enter the one-time code shown in Lumina into the local browser page;
the adjacent **Copy** button copies that code only when you click it.
The browser then opens OpenAI's sign-in page. You sign
in and approve there, and Lumina never sees
your OpenAI password, browser cookies or another app's tokens. When you
return, check the verified **Account to confirm** shown in Lumina and choose
**Confirm this account** before the new authorization is saved. A previously
connected account is shown separately while a new authorization waits. If the account is unexpected,
cancel and start a fresh sign-in. The section then shows whether **plan
permission** was granted (signing in and granting plan use are separate
OpenAI permissions), and the session state, with **Reconnect**,
**Enable plan permission**, **Add another account** and **Unlink Account**
as they apply.
An identity sign-in without offline access or plan permission is shown as
connected without plan permission; it cannot renew a session or use a model.
If a refresh may have rotated but Lumina lost the response, the session
requires a fresh authorization rather than retrying the old refresh token.
Discovery, DNS, connection refusal and connect-timeout failures that prove the
refresh request was never sent leave the saved token retryable. After a request
may have been sent, Lumina conservatively requires reauthorization if the
outcome is unknown, including a post-send 5xx or lost response. This is the
current no-resend policy pending a clear vendor answer on rotating-token
retries after an ambiguous delivery.

The ChatGPT Plan backend offers enabled local tools during foreground text chat
through the native-tool WORK path and Responses function calls. This path is
enabled by default, including for ordinary text messages, and may use
additional provider rounds before completion. The full WORK-loop answer may
arrive after the work and completion gate finish instead of streaming to the
screen immediately. You can choose a smaller tool profile when it suits the
task. The effects of profile size and WORK rounds on latency and allowance
use have not yet been measured quantitatively. Tool profiles, PIN checks, owner-only tools,
Browser Companion grants, cancellation and emergency stop still apply. The
selected model and account must accept the function-tool request; a refused or
incomplete response does not run a tool. Vision, utilities, Reforge, subagents
and scheduled work remain unavailable on this lane.
After a ChatGPT Plan chat starts, it stays bound to that verified account,
backend and model. Start a new chat to use another account, backend or model;
an older chat with unbound history also needs a fresh chat for this path.
Requests use the connected account's OAuth session, never an OpenAI API key,
and do not automatically fall back to another backend. Plan or enabled
ChatGPT credits may be consumed from the account's applicable shared usage
allowances; this integration does not inherit separate accounting for ordinary
ChatGPT conversations. Check ChatGPT Settings → Usage for the account's
allowance. **Save All Settings** saves backend and model choices
but never starts, renews or ends a ChatGPT sign-in.

**Unlink Account** stops use, asks OpenAI to end the renewable session, and
removes the saved tokens. If OpenAI can't be reached to confirm, the
tokens are still removed locally and the section says remote disconnect
was not confirmed — you can also disconnect Lumina in ChatGPT Settings.
Where the session is stored and how it is protected:
[Security & Authority](security.md#credentials-live-apart-from-settings).

## Vision & Image Generation routing

The Multimodal tab's Vision and Image-Generation sections let you pin a
specific model to that capability instead of using the current backend's
default — see [Multimodal](multimodal.md) for what each route actually
does and the safety gates around image generation specifically.
