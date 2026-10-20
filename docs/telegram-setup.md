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
change it. It starts **off** on a new install.

**While it's on, Lumina listens whenever the desktop app is running.** Turning it on starts
listening right away, and every time Lumina opens with it on, she starts listening by
herself; there's no separate Start button. When the desktop app isn't running, nothing
listens: your messages wait at Telegram (see "Late messages" below).

The switch is the master control for Telegram, in both directions:

- **On:** Lumina listens for your messages and may send to your Telegram.
- **Off:** nothing is received and nothing is sent. Off wins over everything:
  - a restart;
  - Lumina sending you an alert (sending never turns listening on);
  - a standalone bridge (`python -m comms.telegram_bridge` won't start while it's off,
    and shuts itself down if you turn it off while it runs);
  - another computer sharing the same Lumina data, such as Kali.

## Step 6 — Test it

With remote access on, open Telegram, find the bot you created in Step 1, and send it a
message, something simple like "hey, you there?"

You should get a real response back within a few seconds. If you don't hear back, check
the status line under the switch first. It reads **Remote access: ON · ● Listening** when
everything is working; anything else is explained under Troubleshooting below.

### Who answers: replies and new messages

- **A reply (swipe-reply) to a message Lumina sent from your desktop conversation** goes
  back into that same desktop conversation. If it's busy, your reply waits its turn.
- **A reply to one of the Telegram assistant's own answers** continues with it.
- **A new message (not a reply)** goes to Lumina's Telegram assistant, which has your full
  owner access but not the desktop conversation's history. If exactly one desktop
  conversation has messaged you recently, a new message goes there instead.
- **A reply Lumina can't place is refused, never guessed.** If you reply to a message
  this session doesn't know (sent before Lumina restarted, or by a background job), to one
  more than a day old, or to a conversation that's no longer available, you get a short
  reply saying it was not run. Send it again as a new message (not a reply).

### Late messages

Lumina never acts on stale instructions:

- **Sent while remote access was off:** never run (see "Turning remote access off").
- **Sent while the desktop app was closed:** Telegram holds it for about a day. When Lumina
  next opens, a message that's more than 3 minutes old by then is **not run**. You get a
  short reply naming when it was sent, so you know which one to send again.
- **A timestamp ahead of the computer's clock** by more than 2 minutes is refused too,
  because a wrong clock could make an old message look new. Check the computer's clock.
- At most five of these replies are sent each time Lumina starts listening, then one
  summary line; older ones beyond that are skipped quietly.
- **Edited messages never run anything.** Send a new message instead.
- Each message runs at most once, even if Telegram delivers it twice.

### Slash commands over Telegram

Lumina's [operator commands](operator-commands.md) (`/status`, `/btw`, `/compact`,
`/stop`, `/context`, …) are desktop-only. Over Telegram:

- **A message that would read as one of them** (it starts with `/`, even after spaces)
  gets a short fixed reply saying so. Nothing runs, and the message isn't sent to the
  model.
- **Telegram's own bot commands**, such as `/start`, are still ignored.
- **`/resume`** is reserved for [runaway suspension](security.md#runaway-suspension)
  recovery over Telegram, which isn't active yet. From your own private chat it replies
  that it isn't active and changes nothing; resume from the desktop (`/suspension`). A
  late `/resume` is refused like any late message. From anywhere else, or as a forwarded
  or edited message, it's ignored.
- **While your work is suspended**, your Telegram messages get a short notice with the
  time and reason instead of a reply. Nothing runs until you resume at the desktop.

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

**Closing Lumina stops listening.** A message that arrives while Lumina is closing gets a
short reply saying it was not run.

**One listener per bot.** Only one Lumina on a computer listens to a bot at a time (for
example, if two Lumina installs share the same bot token). The second one shows "Another
Lumina is using this bot" and stays quiet. If two computers listen to the same bot at
once, the later one notices within about two minutes and stops.

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

The status line under the switch says what's going on:

| Status | Meaning |
|---|---|
| **● Listening** | Working. |
| **Starting (no network, retrying)** | Lumina opened before the network was up. She keeps retrying (every 30 seconds at most) and starts listening once Telegram is reachable. |
| **Not configured — set the bot token and chat ID above** | Step 4 isn't complete, or the chat ID isn't a number. Save both; listening starts once they're saved. |
| **Bot token looks malformed — paste it again from BotFather** | The saved token isn't shaped like a bot token (a number, a colon, then a long code). |
| **Bot token rejected — check it with BotFather** | Telegram refused the token (wrong, or revoked). Save a current one, then turn remote access off and on. |
| **Another Lumina is using this bot** | Another Lumina is already listening to this bot. Close it, then turn remote access off and on here. |
| **Stopped after an error — turn it off and on to retry** | Something unexpected stopped the listener; the terminal has the details (token redacted). |

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

**The listener never runs** — `python-telegram-bot` may not be installed in the
environment Lumina's running in.