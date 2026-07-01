# Lumina + Telegram — Setup Guide (Beta)

This connects Lumina to Telegram so you can message her from your phone — check on
things, ask her to send you a file, whatever you'd normally do sitting at the desktop,
just from wherever you are.

**This is a beta feature.** Right now it runs as a separate script you start by hand in
a terminal, and a couple of settings need to be edited directly in `config.py`. A future
update will fold all of this into the Settings UI — bot token, chat ID, and which persona
answers you, all in one tab, no terminal required. Until then, this guide gets you
running today.

**A note on trust:** Telegram is treated as a fully trusted channel — same as sitting at
your own keyboard. Lumina will act on whatever you send her with no extra confirmation
step. That's by design (it's what makes this useful for quick remote requests), but it
means this setup is for *your own* private bot that only *you* talk to — not something
to hand out to friends or family members as a shared assistant. (Multi-user support for
things like Discord is on the roadmap, with very different trust rules — see the project
handoffs if you're curious.)

---

## Step 1 — Create your bot

1. Open Telegram and search for **@BotFather** (it's Telegram's official bot for making
   other bots — look for the blue checkmark).
2. Send it the message `/newbot`.
3. It'll ask for a **display name** — this is what shows up in your chat list. Anything
   works, e.g. "Lumina."
4. Then it'll ask for a **username** — this one has to be unique across all of Telegram
   and must end in `bot`, e.g. `MyLuminaBot` or `Lumina_Skynet_Bot`. Keep trying names
   until one's free.
5. BotFather will reply with a message containing your **bot token** — a string that
   looks like `123456789:AAFakeExampleTokenStringHere123`. Copy this exactly. Treat it
   like a password — anyone who has it can control your bot.

## Step 2 — Get your personal chat ID

This is how Lumina knows it's really you messaging her, not a stranger who found your
bot's username.

1. In Telegram, search for **@userinfobot** and start a chat with it.
2. Send it any message (even just "hi").
3. It replies instantly with your info, including a line like `Id: 987654321`. That
   number is your chat ID. Write it down.

## Step 3 — Install the dependency

On the machine running Lumina, in the same Python environment Lumina normally runs in:

```bash
pip install python-telegram-bot --break-system-packages
```

## Step 4 — Save your bot token

From your `lumina` directory, run:

```bash
python -c "from core.secrets import set_secret; set_secret('telegram_bot_token', 'PASTE_YOUR_TOKEN_HERE')"
```

Replace `PASTE_YOUR_TOKEN_HERE` with the actual token from Step 1 (keep the quotes
around it). This saves the token to `~/.config/lumina/credentials.json`, kept separate
from your other settings on purpose — it's a real credential, not a regular preference,
so it's deliberately not stored alongside config.py.

## Step 5 — Set your chat ID

Open `config.py` in your Lumina directory and find this line near the bottom of the
"Agent behavior" section:

```python
TELEGRAM_OWNER_CHAT_ID = None
```

Change `None` to your actual numeric chat ID from Step 2 — no quotes, just the number:

```python
TELEGRAM_OWNER_CHAT_ID = 987654321
```

Save the file.

## Step 6 — Start the bridge

In a terminal, from your `lumina` directory:

```bash
python -m comms.telegram_bridge
```

You should see it log that it's started and is polling. Leave this terminal window open
— this is what's actually listening for your messages. (Your main Lumina desktop app
doesn't need to be running for this to work; they're independent.)

## Step 7 — Test it

Open Telegram, find the bot you created in Step 1, and send it a message — something
simple like "hey, you there?"

You should get a real response back within a few seconds. If you don't hear back at all,
double check Steps 4 and 5 — a missing token or unset chat ID is the most common cause,
and the terminal log from Step 6 will usually tell you which one.

---

## Stopping the bridge

Press `Ctrl+C` in the terminal where it's running. There's no harm in leaving it running
continuously if you want always-on access, but it's not required to be on for the rest
of Lumina to work normally.

## Known limitations (beta)

- Setup currently requires editing `config.py` and running a one-line Python command —
  this moves into Settings UI in a future update.
- Telegram doesn't yet respect whichever persona is active on your desktop, or let you
  pick a different one for this channel — you'll always get the default Lumina
  configuration for now. Per-channel persona selection is planned.
- The bridge runs as a separate process you start manually — it doesn't yet auto-start
  alongside the main app.

## Troubleshooting

**"telegram_bot_token not set"** — Step 4 didn't save correctly, or you're running the
bridge from a different Python environment than the one you ran `set_secret` in. Try:
```bash
cat ~/.config/lumina/credentials.json
```
to confirm the token actually saved.

**"TELEGRAM_OWNER_CHAT_ID not set in config.py — refusing to start unsafe"** — Step 5
wasn't saved, or you're still on `None`. The bridge deliberately won't start without a
real chat ID set — this is intentional, not a bug.

**Bot doesn't respond, no error in terminal** — most likely your chat ID doesn't match
what's in `config.py`. Double-check Step 2's number against what you entered in Step 5,
digit for digit.
