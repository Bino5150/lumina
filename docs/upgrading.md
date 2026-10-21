# Upgrading Lumina

## Upgrading to the C1 Runaway Suspension release

This release adds a safety record that pauses work when a turn runs away.
“C1” is the name of this release's first stage of that protection. The
desktop may open a **Runaway suspension** window the first time you use it.
You do not need to understand its internals to upgrade.

**Keep your existing data folder. Back it up before updating. Never delete
or replace a folder, `suspension.db`, or `soul_identity.json` to get past a
startup prompt.** A new, empty folder can make your history appear lost
and hide earlier safety records.

C1 initialization creates a separate safety record. It does not
intentionally replace conversations, the Memory Palace (MemPalace), Skills,
preferences or persona history. Missing history is a reason to stop and
check the selected folder, not a reason to initialize again.

### 1. Find the data folder you already use

The installation folder containing `main.py` and the persistent data folder
are different things. Record both paths before upgrading. The data folder
normally contains `memory/lumina.db` (history and other stored knowledge)
and `memory/prefs.json` (preferences).

**Current launch methods select different defaults.** The following table
describes the public release, regardless of what you named its checkout:

| Launch method | Persistent data folder selected |
|---|---|
| `python main.py` (desktop), including an IDE that runs it | `~/.local/share/lumina-release` when `LUMINA_DATA_DIR` is unset; an explicitly set, nonempty `LUMINA_DATA_DIR` takes precedence. |
| `python main.py --cli` | Same selection as the desktop through `main.py`. It cannot initialize C1. |
| `bash start_lumina.sh`, or an alias/shortcut that runs that script | Always `~/.local/share/lumina-release`. The script **overwrites** `LUMINA_DATA_DIR`, even if you set it first. |
| `python -m comms.telegram_bridge` or `python -m comms.discord_bridge` | Uses a nonempty `LUMINA_DATA_DIR` if set. Otherwise uses the operating system's `platformdirs` location for `lumina`, without passing through `main.py`. |
| An alias, desktop shortcut, service or IDE configuration you created | Inspect its command and environment; apply the corresponding row above. A name such as `lumina` alone does not establish which folder it uses. |

For standalone bridges on Linux, the unset default is
`~/.local/share/lumina`, or `$XDG_DATA_HOME/lumina` if that base is configured.
On macOS it is normally `~/Library/Application Support/lumina`; on Windows
it is under the user's Local AppData directory as `lumina`.
`main.py` and the shell launcher instead use the literal
`~/.local/share/lumina-release` default, including on those platforms.
Do not set `LUMINA_DATA_DIR` to an empty string; use a full folder path.

To identify your installation:

1. Inspect your usual shortcut or launcher. On Linux/macOS, `type lumina`
   shows what that shell name runs, and `printenv LUMINA_DATA_DIR` shows an
   override in that terminal. No output from `printenv` means it is unset
   there; a service or IDE can have its own environment.
2. In your file manager, enable hidden files and inspect the likely data
   folders, including any custom location you previously selected. Also
   preserve an older installation's in-repository `memory/`, `tools/` and
   `projects/` if present. Very old versions kept some state there.
3. Compare with your known history and backup records. File presence or a
   recent timestamp alone is not proof that a folder is the right one.
   If both `lumina` and `lumina-release` folders exist and you cannot tell
   which is yours, stop and ask for help before launching either.

If you already have a Flight Recorder diagnostic, its `runtime.startup`
event from `main.py` records `data_dir`. That can help confirm what a prior
launch used; you do not need to edit a database or run Lumina to find out.

**Existing users of `~/.local/share/lumina`:** the public launcher does not
automatically select or migrate that folder to `lumina-release`. Switching
to that launcher can therefore show an apparently fresh installation.
After the backup below, use the direct desktop entry point with your
verified existing folder. For example, from your Lumina installation on
Linux, if this is the folder you confirmed:

```bash
LUMINA_DATA_DIR="$HOME/.local/share/lumina" python main.py
```

This selects the folder; it does not copy or merge data. Do not pass this
override to `start_lumina.sh`: the script ignores it. Keep using the same
explicit folder in future launch configurations. The inconsistent defaults
are a release issue; this guide does not change them.

### 2. Stop everything and preserve your installation

1. Finish or cancel work and wait for running tools to finish. Quit every
   Lumina desktop and CLI using this folder. Stop standalone Telegram and
   Discord bridges, background helpers and services using it, and disable
   automatic restarts while you back up. Include another machine if it
   shares the folder. Confirm those processes have exited; closing one
   window is not enough.
2. Copy the **entire existing data folder** to a separate, private backup
   location outside the installation and data folders. Keep every file and
   subfolder, including database companion files, C1 records and recovery
   evidence. Keep the original in place. Check that the copy completed
   without errors and contains the expected files before continuing.
3. Separately preserve the installation folder and your user-created
   overlays: `personas/`, `skills/`, `tool_profiles/`, `assets/avatars/`,
   `assets/voices/` and `projects/`, plus local configuration and any legacy
   state. Some of these are outside the data folder. Preserve personal
   modifications rather than replacing them with freshly shipped copies.
4. Credentials and sign-in sessions live separately; consult
   [Security](security.md#credentials-live-apart-from-settings). Keep any
   private backups private. Another machine may need fresh sign-in and
   local configuration.

**Incomplete State Export is not yet a complete disaster-recovery solution.**
It omits C1 safety state and some preferences and other state; a successful
archive check does not prove it can recover your whole installation.
**Backup Memory (Legacy)** also has limited coverage, including no source
overlays. Complete **Verified Agent Backup** is currently unavailable, and
full **Agent Restore is not implemented**. Do not rely on an export as your
only pre-upgrade copy. See [Agent Backup and Incomplete State
Export](agent-backup.md) for the exact limits. An offline copy preserves
files; it is not a promise of automatic or verified restoration.

### 3. Update the application, keeping personal changes

For an existing Git installation, use your existing checkout. Inspect
`git status` first. If it shows changes you do not understand, preserve
them and stop for help rather than discarding them. On a clean checkout
tracking the public release, `git pull --ff-only` updates without creating
a merge. If it refuses, stop for review; do not reset or overwrite files.
Install the updated requirements in your usual Python environment with
`pip install -r requirements.txt`. Keep your existing `config.py` and
personal overlays; do not copy `config.example.py` over them as an upgrade
step.

For a clean installation with no previous Lumina data to retain, follow
[Getting Started](getting-started.md), then continue here before chatting.
For another machine or data folder, read [Moving installations](#moving-to-another-machine-or-data-folder)
first; an empty destination does not establish a first installation.

Very old in-repository state can be moved automatically by the existing
startup migration into the selected data folder when destination files do
not exist. It does not merge two populated data folders. That is why both
the installation and data folder must be preserved **before** launching.

### 4. Open the desktop and choose the right safety action

Launch the desktop against the folder confirmed in step 1. CLI, Telegram
and Discord **cannot perform first-time C1 initialization, repair, recovery
or resume**. The desktop must establish a usable safety store for that
same folder first. A bridge using a different default still will not work.

If the safety record is not ready, the desktop opens **Runaway suspension**.
You can also type `/suspension` into the desktop chat to open these controls.
There is no additional confirmation dialog behind **Initialize**,
**Repair identity** or **Recover…**; read the screen before clicking.

| Your situation | What to do |
|---|---|
| Brand-new installation, or an older installation that you know has **never used C1 in this folder** | If offered, tick **This is the first launch with runaway suspension for this data folder**, then click **Initialize**. This creates the safety record, with no work suspended initially. |
| C1 already worked in this folder and no warning appears | Keep the existing safety record. Open `/suspension` if you want to review its status. Do not initialize again. |
| C1 existed here before, traces remain, or you are unsure whether it existed | Choose **Recover…**, rather than declaring a first initialization. Follow the recovery instructions below. If no traces remain at all, Recover needs its own acknowledgement first; see **When no C1 traces remain** below. |
| The store fails its checks and **Repair identity** is offered | This can repair a missing or unreadable identity file when the safety record itself is intact. It preserves every suspension. If it fails, preserve the error and review recovery; do not delete the identity file. |
| The store is missing with traces, damaged, replaced, or does not match its identity | After confirming the folder and preserving a backup, use **Recover…**. An unexpected identity mismatch warrants stopping for help before recovery. |

The C1 files are `memory/suspension.db` and `memory/soul_identity.json`.
Their presence, companion files or `memory/suspension_evidence/` indicate
that earlier safety state may exist. Their absence **cannot prove** that
C1 was never initialized. Use what you know about this particular folder,
including earlier backups and launches, rather than the age of the app
installation alone. Do not edit these files to change the screen's choices.

**When no C1 traces remain.** If none of those files exist, the screen
offers both **Initialize** and **Recover…**. If you know or suspect earlier
C1 use in this folder, do not tick the first-launch checkbox. Instead, first
make sure you have selected the right folder (step 1) and preserved your
backups (step 2). Then tick **I can't confirm this is the first launch.
Earlier suspensions, other Lumina processes and the outcome of earlier work
are unknown; start with my work and every channel suspended for review**
and click **Recover…**. The two checkboxes cancel each other out, and
**Recover…** stays disabled until the acknowledgement is ticked.

No earlier safety record survives, so this restores nothing. It creates a
new safety record with a new identity, so no work started under an earlier
record can continue. Your own work (`owner`) starts suspended, and every
channel starts suspended the first time it is used. The result begins **New
safety record started; nothing was restored** and names a receipt in
`memory/suspension_evidence/` recording that no earlier evidence survived.
Keep that receipt. If the new record is ever lost, it is the trace that
keeps a later launch from looking like a first run. Then review and resume
as described in step 5. The owner entry always needs the outcome-unknown
acknowledgement, and **After recovery, let channels whose state can't be
read start unsuspended** is not offered on this path.

### 5. If recovery is needed, review before resuming

**Recover… rebuilds the safety record, not your agent's memories.** It first
preserves available old safety files in `memory/suspension_evidence/`, then
rebuilds that record, retaining readable suspensions and stopping work whose
earlier safety status cannot be confirmed. Work from the old record cannot
continue under it. This is not full Agent Restore and cannot bring back
missing conversations or reconstruct missing safety history.

1. Keep every other process using this state stopped. A **Recovery waiting**
   message can mean another process still appears active, or the old process
   list cannot be read. Stop the listed processes and retry. The checkbox
   **Other Lumina processes listed above are stopped or may be ignored**
   is an explicit acknowledgement, not a way to stop them. Use it only
   after you have independently confirmed they are stopped. If you cannot
   establish that, stop and ask for help. Do not ignore a known live process.
2. Leave **After recovery, let channels whose state can't be read start
   unsuspended** unchecked while you investigate. That option relaxes the
   safety hold; it is not required to recover the record. Likewise, do not
   click **Unknown channels may start unsuspended** just to clear a warning.
   With the conservative default, an unknown channel can acquire a new
   suspension when first used; review it in these desktop controls.
3. After a successful recovery, the result begins **Recovered. Evidence
   kept in …** (or, when no traces remained, **New safety record started;
   nothing was restored**) and lists work suspended for review. The list uses `owner`
   for your desktop/Telegram work and channel names such as `channel:…`
   for separate public-channel work. Review each listed incident and its
   time, reason and earlier running work. Recovery may leave readable,
   previously clear work clear; it does not always suspend everything.
4. To permit new work for a reviewed entry, select it and click
   **Resume selected**. If earlier work is still running, wait for it to
   finish and try again. If its process stopped, check whether its actions
   actually completed before ticking **I accept that work whose process
   stopped may or may not have finished (outcome unknown)** and retrying.
   Recovery incidents may also require this acknowledgement.
5. Resume permits **new** work. It does not restart the interrupted task,
   resend a refused message, or apply a pending approval. Review the result
   before requesting that work again, to avoid repeating an action that
   already completed.

If repair or recovery fails, retain the error and any evidence directory
and stop for help. Do not repeatedly remove files, weaken permissions, or
switch to an older Lumina process to bypass the safety hold. Resume is
separate from the Emergency Interlock and Telegram remote access; it does
not turn either back on.

### 6. Check that the upgrade succeeded

1. After initialization, expect **Initialized. Runaway suspension is now
   active.** With no incidents, the window reads **Runaway suspension is
   active and nothing is suspended.** “Active” means the protection is
   working, not that your chat is paused. After recovery/repair, review and
   resume only the entries you understand; unresolved entries remain paused.
2. Close the controls with **Close**. Check several known conversations in
   the chat selector, your expected persona and preferences, and familiar
   entries under **Settings → Skills**. Compare with what you had before
   the upgrade; a familiar avatar alone is not enough.
3. Send a simple ordinary chat message to your configured backend. Then ask
   for a harmless read-only tool operation, for example listing recent chats
   (`list_recent_chats`), and confirm that the tool actually returns a
   result. Ask for a read-only recall of a known Memory Palace entry too;
   verify the retrieved content rather than an answer from conversation
   context alone. Do not test by causing a runaway or approving a
   destructive task.
4. On the next normal launch with that **same folder**, the first-initialization
   prompt should not return. If it does, or history is missing, stop work,
   quit the app and recheck the launch path and folder before choosing
   anything. Preserve both folders if you accidentally opened a different
   one; do not merge, delete or replace them to make the display look right.

Once the desktop checks pass, start other interfaces with that same explicit
`LUMINA_DATA_DIR`. For example, if you verified the older Linux folder above:

```bash
LUMINA_DATA_DIR="$HOME/.local/share/lumina" python main.py --cli
LUMINA_DATA_DIR="$HOME/.local/share/lumina" python -m comms.discord_bridge
```

Use only the interfaces you have configured. Telegram remote access must
also be enabled locally, and only one listener can handle a bot at a time;
see [Telegram Setup](telegram-setup.md). Do not run a standalone Telegram
listener alongside the desktop's listener for the same bot.

### Moving to another machine or data folder

A new checkout or empty destination does not mean your existing agent never
used C1. There is no shipped full Agent Restore or automatic cross-folder
merge. Do not unpack Incomplete State Export over an installation and
declare it initialized: that archive deliberately omits C1 safety state.

Before a move, stop all processes on the source and destination and preserve
the complete source data and overlays as described above. Retain the
original and backups. Do not combine a source database with a destination's
safety files, overwrite a destination's existing restrictions, or copy only
`lumina.db` and call it a complete migration. Get a reviewed transfer plan
if a destination already has data or you only have an incomplete export.

For a transferred installation, explicitly select its intended data folder
at every entry point and review it on the desktop before enabling other
interfaces. **Initialize** is only for a genuine first C1 initialization;
use **Recover…** when earlier C1 state existed or is uncertain, including
the no-traces acknowledgement above. If the transferred record opens normally,
review existing suspensions rather than rebuilding it. Verify history,
preferences and local project/credential configuration before resuming
ordinary use. Stop if an unexpected identity or recovery prompt appears.

Launch selection is defined in [main.py](../main.py),
[start_lumina.sh](../start_lumina.sh) and [config.py](../config.py).
The labels and safety actions above come from
[the suspension dialog](../ui/suspension_dialog.py),
[its runtime](../core/suspension_runtime.py) and
[the safety store](../core/suspension.py).
