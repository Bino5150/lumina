# Settings

Settings is a tabbed panel (`ui/settings/panel.py`). Current tabs, in order:

| Tab | Covers |
|---|---|
| ⚙ General | Backend selection, reasoning effort, context compaction, dreaming toggles, global agent behavior prompt, and the backend-specific **ChatGPT Plan** account and model controls (see below) |
| 👤 User Profile | Your own bio and Lumina's curated notes about you — see [My Human](memory.md#my-human) |
| 🎭 Personas | Create/edit/import/export personas — see [Personas & Skills](personas-and-skills.md) |
| 📡 Communications | Telegram, Discord, and your public bio — see [Channels](channels.md) |
| 🧠 Memory | Browse/edit flat memory and MemPalace entries |
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
- ChatGPT Plan — foreground text chat using a connected ChatGPT account, separate from the OpenAI API-key backend

**Cloud, OpenAI-compatible**
- OpenRouter, DeepSeek, Groq, and any other OpenAI-compatible endpoint via the generic Custom slot

Each backend remembers its own context window and memory-injection limits
independently, and a backend change takes effect immediately — no
restart. Endpoints are provider-owned for every backend except llama.cpp,
LM Studio, Ollama, vLLM, OmniRoute, and Custom — those six are the
intended route for a gateway or a corporate proxy; pointing a fixed cloud
provider at a different URL is refused rather than honored.

## Reasoning Control

Every backend that supports it gets a per-backend, per-model **Reasoning
Effort** control in General. The underlying contract
(`core/backends/reasoning.py`'s `ReasoningCapabilities`) describes, per
model: which effort levels exist, whether reasoning is mandatory, and
whether a token budget is supported instead of a named level. The combo
box in Settings has to handle six distinct states depending on what's
currently known about the selected model — a static list of efforts, a
model with no reasoning control at all, a model where reasoning is
mandatory, or an OpenRouter model whose capabilities haven't been
discovered yet.

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

In this version the ChatGPT Plan backend handles foreground text chat only.
It cannot use tools, vision, utilities, Reforge, subagents or scheduled work.
Requests use the connected account's OAuth session, never an OpenAI API key,
and do not automatically fall back to another backend. Plan or enabled
ChatGPT credits may be consumed; check ChatGPT Settings → Usage for the
account's allowance. **Save All Settings** saves backend and model choices
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
