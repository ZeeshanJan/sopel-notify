@~/sopel/CLAUDE.md

# sopel-notify

A Sopel 8 plugin (entry point `notify_watch = sopel_notify`) that watches a
list of nicks and tells the bot's owner, by private message only (never in a
channel), when one comes online, goes offline, joins, leaves or is kicked
from a channel the bot is in, or changes nick. The web UI (sopel-ui, Notify
page) shows the watchlist, who is online, the activity and the switches.

## How it works

- All logic is in `sopel_notify/__init__.py` (a reload re-imports only that
  module); `config.py` keeps the old `[notify_watch]` config section, read
  once to seed the settings.
- Presence: MONITOR or WATCH when the server has them (instant), otherwise
  ISON every `check_seconds`, with WHOIS for the host. Sopel 8.0 only reads
  005 lines ending "are supported by this server"; DALnet says "available on
  this server", so the plugin reads 005 itself and also sends `WATCH S`
  (an answer, 603/606/607, means WATCH works). The owner is tracked too, so
  messages are saved (`sent = 0`) while they are offline and sent as a
  summary when they come back.
- JOIN/PART/KICK/QUIT/NICK lines from shared channels are used at once.
  A fresh "online" and the joins that follow within a few seconds are sent
  as one message. The first answer after connecting (or adding a nick) only
  sets the status, so a reconnect does not flood the owner.
- Data (bot's database): `notify_watchlist` (nick key in IRC lowercase, note,
  last status/since/host/channels), `notify_events` (kept `keep_days`),
  `notify_settings` (JSON values), `notify_state` (`version`: every writer
  raises it and a 2-second timer re-reads on change; `heartbeat`; `mode`).
- Commands (owner/admins, answered privately): `.notify-add <nick> [note]`,
  `.notify-del <nick>`, `.notify` / `.notify-list`.

## Working on it

- Nothing personal is kept in the repo folder; keep it that way.
- Test on a mock bot: set `bot.backend.connected = True`, feed `001`, a
  `005` line, `376`, then the numerics above, and call `sopel_notify.tick(bot)`
  by hand (interval jobs don't run in the mock).
