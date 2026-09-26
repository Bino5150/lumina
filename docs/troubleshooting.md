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

- Data directory: `~/.local/share/lumina` by default (override with
  `LUMINA_DATA_DIR`) — chat history, MemPalace, Knowledge Base, prefs, and
  the Flight Recorder database all live here.
- Credentials: `~/.config/lumina/credentials.json`, mode `0600` (override
  with `LUMINA_SECRETS_PATH`) — never inside the data directory, never in
  a Memory Backup archive.

## Diagnosing a bad run

The Flight Recorder (see [Security & Authority](security.md#flight-recorder))
is the most reliable source of truth for "what actually happened" during
an agent run — it separates machine-recorded evidence from model
narration, so if a tool call's behavior is ambiguous, that's the first
place to look rather than trusting either the model's account or your own
memory of the session.

## Telegram setup

[TELEGRAM_SETUP.md](../TELEGRAM_SETUP.md) has a full walkthrough,
including a troubleshooting table for the most common setup error
strings. The GUI path (Settings → Communications) is faster if it works;
this file covers the manual/legacy path and BotFather token creation.

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
