# Settings

Settings is a tabbed panel (`ui/settings/panel.py`). Current tabs, in order:

| Tab | Covers |
|---|---|
| ⚙ General | Backend selection, reasoning effort, context compaction, dreaming toggles, global agent behavior prompt, and the separate **ChatGPT Plan — sign-in** section (see below) |
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
switching backends is a dropdown in General, not a code change. Fourteen
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

## ChatGPT Plan — sign-in

The bottom of the General page has its own **ChatGPT Plan — sign-in**
section. **Continue with ChatGPT** opens your system browser. For a first
registration, **Add another account**, or registration again after an invalid
client, enter the one-time code shown in Lumina into the local browser page;
it then opens OpenAI's sign-in page. You sign
in and approve there, and Lumina never sees
your OpenAI password, browser cookies or another app's tokens. When you
return, check the verified **Account to confirm** shown in Lumina and choose
**Confirm this account** before the new authorization is saved. A previously
connected account is shown separately while a new authorization waits. If the account is unexpected,
cancel and start a fresh sign-in. The section then shows whether **plan
permission** was granted (signing in and granting plan use are separate
OpenAI permissions), and the session state, with **Reconnect**,
**Enable plan permission**, **Add another account** and **Disconnect**
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

In this version that is all it does: it holds a ChatGPT session on this
computer. Using your ChatGPT plan to power chat is **not available yet** —
the ChatGPT-plan backend is not in the backend list, nothing is sent to a
model, and no plan usage is consumed. The section is independent of
**Save All Settings**: saving General never starts, renews or ends a
ChatGPT sign-in, and an OpenAI API key never stands in for it.

**Disconnect** stops use, asks OpenAI to end the renewable session, and
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
