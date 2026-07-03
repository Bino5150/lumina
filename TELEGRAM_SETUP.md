# Lumina + Telegram — Setup Guide (Beta)

This connects Lumina to Telegram so you can message her from your phone — check on
things, ask her to send you a file, whatever you'd normally do sitting at the desktop,
just from wherever you are.

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

## Step 4 — Enter your token and chat ID

Open Lumina, go to **Settings → Communications → Telegram**, and paste in:
- Your **bot token** from Step 1
- Your **chat ID** from Step 2

Hit Save. That's it — the token is stored securely in
`~/.config/lumina/credentials.json` (not in your regular settings file), and your chat
ID is saved to your preferences.

*(Prefer the terminal? See [Advanced / Manual Setup](#advanced--manual-setup) below.)*

## Step 5 — Start the bridge

Right below the token/chat ID fields is a **Bridge** toggle. Click **Start** — it flips
to "● Running" and Lumina starts listening for your messages immediately, right from
the Settings tab. No terminal required.

The bridge stays off by default every time Lumina launches — you turn it on when you
want it, same as before. If you'd rather not touch the GUI, running it from a terminal
still works exactly the same too (see [Advanced](#advanced--manual-setup)).

## Step 6 — Test it

Open Telegram, find the bot you created in Step 1, and send it a message — something
simple like "hey, you there?"

You should get a real response back within a few seconds. If you don't hear back at all,
double check Step 4 and that the toggle actually shows "Running" — a missing token,
unset chat ID, or a bridge that's still off is the most common cause.

---

## Stopping the bridge

Click **Stop** in the Communications tab. If you started it from a terminal instead,
press `Ctrl+C` there. There's no harm in leaving it running continuously if you want
always-on access, but it's not required to be on for the rest of Lumina to work
normally.

## Known limitations (beta)

- Telegram doesn't yet respect whichever persona is active on your desktop, or let you
  pick a different one for this channel — you'll always get the default Lumina
  configuration for now. Per-channel persona selection is planned.

## Troubleshooting

**"telegram_bot_token not set"** — Step 4 didn't save correctly. Confirm with:
```bash
cat ~/.config/lumina/credentials.json
```

**"Owner chat ID not set — configure it above first"** (shown when clicking Start) —
your chat ID isn't saved yet. Go back to Step 4.

**Bot doesn't respond, no error shown** — most likely your chat ID doesn't match what
you entered in Step 2 or 4. Double-check it digit for digit.

**Bridge toggle shows an import error** — `python-telegram-bot` isn't installed in the
environment Lumina's running in.