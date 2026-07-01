"""
Discord bridge — owner=False, no exceptions, no path to True ever exposed
to a message handler. See LUMINA_SECURITY_HARDENING_BLUEPRINT.md Part 4 (C3).

Two things are hardcoded in this file and must never be sourced from data
a user can edit:
  1. DISCORD_PERSONA_PATH - which file gets loaded. Not looked up by name,
     not selectable by any message content. Always this exact path.
  2. DISCORD_TOOLS_PROFILE - what the bot is allowed to do. This is a
     Python constant, not a field read out of the persona JSON. The
     persona file's own "tools_profile" key exists for documentation only
     and is explicitly stripped before the file's contents are applied -
     see load_discord_persona() below.

Everything else in the persona file - name, avatar, system_prompt, TTS -
is intentionally free for anyone running this bot to customize. That's
identity, not authority, and those are different things here on purpose.

Run standalone: python -m comms.discord_bridge
"""
import asyncio
import json
import logging

import discord

import config
from core.headless import run_headless_turn
from core.secrets import get_secret
from core.rate_limiter import check as rate_check
from core.personas import DISCORD_TEMPLATE_PATH as DISCORD_PERSONA_PATH

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("discord_bridge")

DISCORD_TOOLS_PROFILE = "Discord-Safe"


def load_discord_persona() -> dict:
    """Identity fields only. tools_profile/tools_enabled are intentionally
    dropped here - even if a user edits those keys into this file via the
    Settings UI, this function throws them away before they ever reach
    apply_persona(). Tool access is decided by DISCORD_TOOLS_PROFILE below,
    every single call, not by anything in this file."""
    with open(DISCORD_PERSONA_PATH, "r") as f:
        persona = json.load(f)
    persona.pop("tools_profile", None)
    persona.pop("tools_enabled", None)
    return persona


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info(f"Discord bridge connected as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if client.user not in message.mentions:
        return  # mention-gated - not every message in every channel

    allowed, retry_after = rate_check(message.author.id)
    if not allowed:
        log.info(f"[DISCORD] Rate-limited user_id={message.author.id}, "
                 f"retry_after={retry_after:.0f}s")
        return  # silent drop, no per-message notice — a throttled user
        # spamming a channel doesn't need a bot reply confirming it noticed

    text = message.content
    for mention in message.mentions:
        text = text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    text = text.strip()
    if not text:
        return

    channel_id = f"discord-{message.channel.id}"
    persona = load_discord_persona()

    async with message.channel.typing():
        # asyncio.to_thread, not a direct call — run_headless_turn() blocks
        # on local LLM inference, which can easily exceed 10s on a 4B model.
        # Calling it directly here froze the whole event loop, including
        # the heartbeat coroutine discord.py needs to keep the gateway
        # connection alive — that's what caused the "heartbeat blocked for
        # more than 10 seconds" reconnect loop seen live. Running it in a
        # thread lets the event loop keep servicing the heartbeat while
        # inference happens in the background.
        result = await asyncio.to_thread(
            run_headless_turn,
            task=text,
            channel_id=channel_id,
            owner=False,
            persona=persona,
            force_tools_profile=DISCORD_TOOLS_PROFILE,
        )
    reply = result["response"] if result["success"] else f"[Lumina error: {result['error']}]"
    await message.reply(reply[:2000])  # Discord message length cap


def main():
    token = get_secret("discord_bot_token")
    if not token:
        log.error("discord_bot_token not set — see core/secrets.py, set_secret().")
        return
    log.info("Discord bridge starting...")
    client.run(token)


if __name__ == "__main__":
    main()
