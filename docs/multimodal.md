# Multimodal

Voice, speech input, vision, and image generation all live together under
Settings → **🖼 Multimodal**, even though the underlying file is still
named `tts_tab.py` from when it was TTS-only.

## Voice (TTS)

The TTS layer is a fully abstracted backend system:

- **Kokoro** — the default. Fast neural TTS, runs locally, can offload to CPU.
- **Voicebox** (via Chatterbox Turbo/Qwen3 TTS, Docker) — voice cloning, CPU-viable.
- **Chatterbox** — in-process voice cloning, CPU or GPU.
- **Supertonic** — wired and ready.
- **ElevenLabs** — cloud subscription TTS.
- **Piper** — listed as a lightweight edge option, but **not currently
  functional**: its `speak()` call only logs and never produces audio, and
  its own self-test always reports failure. Don't pick it expecting sound
  yet.

Voice cloning with reference audio for personas requires Chatterbox Turbo
or Voicebox. Output uses a producer/consumer pipeline — text is chunked
into sentence-sized segments, generation and playback run in parallel, so
long responses don't have awkward gaps between paragraphs. Markdown is
stripped before synthesis. A phonetic-correction map fixes commonly
mispronounced proper nouns.

## Speech Input (STT)

Speech-to-text runs locally and offline via faster-whisper, with a simple
energy/silence threshold to detect when you've stopped talking. The real,
shipped way to use it today is the **push-to-talk mic button** in the
chat window — press it, talk, it transcribes on release.

**Wake-word ("Hey Lumina") hands-free listening does not exist in this
build.** No wake-word detection code is wired anywhere in the codebase;
`openwakeword` is listed in `requirements.txt` as a dependency for future
work, not something currently in use. Treat any hands-free claim as
aspirational until this page changes.

## Vision

Images are read through a **Bounded Vision Lane**: the current turn's
image is routed to a fresh, short-lived specialist model call rather than
being folded into your main conversation history — so a large image
doesn't permanently sit in your primary context, and the routing decision
is scoped to exactly one turn.

There is no tool that lets Lumina pull image bytes into the conversation
by path — `view_image` deliberately only confirms a path points at a
valid image file and tells the model to ask you to attach it in the chat
window instead. The only ways an image actually enters a conversation are
through the chat window itself: drag-and-drop, or the 📎 **Attach Images**
button. This is a deliberate boundary, not an oversight — it exists so
pixel data can't leak into context through a filesystem-reading tool
path.

You can pin a specific model to the vision route in Settings, instead of
relying on the active backend's default.

## Image Generation

Image generation is a **shipped, owner-only feature** with a deliberate
two-call structure — this is not "coming soon."

1. `estimate_image_generation(...)` — stages a draft with a cost estimate.
   No spend happens here.
2. `generate_image(draft_id)` — consumes that exact draft. There is no
   `confirmed=True` shortcut; a single tool call can never both propose
   and pay for a generation. The draft is bound to the exact conversation
   and turn that staged it, so a draft from one conversation can't be
   confirmed from another.

Approval happens in the chat window itself via an in-line approve button
on the pending draft.

Higgsfield API credentials (Key ID/Key Secret) are entered in Settings and
stored in the same protected credentials file used for every other
secret — never in `prefs.json`, never logged.

## Capability Router

A shared mechanism (`core/capability_router.py`) lets you pin a specific
model to a specific capability route (vision, image generation) instead
of using whatever the active backend defaults to. The same enum that
defines these routes also declares four more —
`audio_understanding`, `speech_synthesis`, `video_understanding`,
`video_generation` — that exist only as names in the code today: no
adapter, no Settings row, nothing consuming them yet. They are not
features you can use.
