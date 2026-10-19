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

## Step 5 — Turn on remote access

Right below the token/chat ID fields is the **Telegram remote access** switch. Click
**Turn on**. The setting is saved, so it stays on (or off) across restarts until you
change it.

> **For now, turning it on doesn't start listening by itself.** Automatic listening
> (Lumina listens whenever she's running and remote access is on) arrives with the next
> update. Until then Lumina can't receive your Telegram messages; this gap is temporary.
> With remote access on, she can still send you messages and files.

The switch is the master control for Telegram, in both directions:

- **On:** Lumina may send to your Telegram, and may listen once listening is running.
- **Off:** nothing is received and nothing is sent. Off wins over everything:
  - a restart;
  - Lumina sending you an alert (sending never turns listening on);
  - a standalone bridge (`python -m comms.telegram_bridge` won't start while it's off,
    and shuts itself down if you turn it off while it runs);
  - another computer sharing the same Lumina data, such as Kali.

## Step 6 — Test it

Once listening is running, open Telegram, find the bot you created in Step 1, and send
it a message, something simple like "hey, you there?"

You should get a real response back within a few seconds. If you don't hear back, check
these first:

- Step 4;
- that the switch shows **Remote access: ON**;
- the listener line under it.

A missing token or an unset chat ID is the most common cause.

### Slash commands over Telegram

Lumina's [operator commands](operator-commands.md) (`/status`, `/btw`, `/compact`,
`/stop`, `/context`, …) are desktop-only. Over Telegram:

- **A message that would read as one of them** (it starts with `/`, even after spaces)
  gets a short fixed reply saying so. Nothing runs, and the message isn't sent to the
  model.
- **Telegram's own bot commands**, such as `/start`, are still ignored.
- **`/resume`** is reserved for a future runaway-suspension recovery feature. From your
  own private chat it currently replies that it isn't active and changes nothing. From
  anywhere else, or as a forwarded or edited message, it's ignored.

---

## Turning remote access off

Click **Turn off** in the Communications tab. Lumina stops listening, sends nothing to
Telegram, and keeps it that way across restarts until you turn it back on. Everything
else in Lumina keeps working normally.

Use it whenever you need to close the channel without stopping Lumina, for example if
you lose your phone. Turning it off cuts Telegram's access to Lumina. It doesn't sign the
lost phone out of your Telegram account; do that in Telegram's own device settings.

**The emergency stop turns remote access off too.** Re-arming Lumina doesn't turn it
back on. You turn it on yourself in the Communications tab, when you're ready. The
switch then shows "turned off by the emergency stop".

**Messages you sent while it was off never run.** Telegram holds undelivered messages
for about a day and hands them over when Lumina next listens. Anything sent while remote
access was off gets a short reply saying so, and is not acted on; send it again if you
still want it.

## Your bot token and the terminal

Telegram's Bot API puts your bot token in the address of every request the bridge
makes, including the poll for new messages that runs every few seconds. Lumina keeps it
out of the terminal and logs:

- Individual Telegram requests aren't logged. Request lines for other services (model
  providers, for example) are unchanged.
- Problems still show up, with the token replaced by `[REDACTED]`. A token Telegram
  rejects logs `[TELEGRAM] Bridge stopped on an error:` followed by
  ``InvalidToken: The token `[REDACTED]` was rejected by the server.`` Network
  failures log `Exception happened while polling for updates.`
- A standalone bridge (`python -m comms.telegram_bridge`) that stops on an error exits
  with status 1.
- A failed `send_telegram_message` or `send_telegram_file` reports the error with the
  token redacted, so it doesn't reach the conversation, chat history or the Flight
  Recorder.

If your token was ever exposed (pasted into a chat, shared in a screenshot or terminal
log, or printed by a Lumina version before this protection), revoke it: send
`/revoke` to @BotFather, pick your bot, then enter the new token in Step 4.

## Known limitations (beta)

- Telegram doesn't yet respect whichever persona is active on your desktop, or let you
  pick a different one for this channel — you'll always get the default Lumina
  configuration for now. Per-channel persona selection is planned.

## Troubleshooting

**"telegram_bot_token not set"** — Step 4 didn't save correctly. To check whether a
token is saved without printing it:
```bash
python3 -c "import json, os; d = json.load(open(os.path.expanduser('~/.config/lumina/credentials.json'))); print('token saved' if d.get('telegram_bot_token') else 'no token saved')"
```

**"InvalidToken: The token `[REDACTED]` was rejected by the server"** — the saved token
is wrong or was revoked. Copy a current one from @BotFather and save it again in Step 4.

**"Owner chat ID not set — configure it above first"** —
your chat ID isn't saved yet. Go back to Step 4.

**Bot doesn't respond, no error shown** — most likely your chat ID doesn't match what
you entered in Step 2 or 4. Double-check it digit for digit.

**The listener never runs** (once listening is available) — `python-telegram-bot` may not
be installed in the environment Lumina's running in.