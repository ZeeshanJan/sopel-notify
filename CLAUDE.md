@~/sopel/CLAUDE.md

# sopel-notify

A Sopel 8 plugin (entry point `notify_watch = sopel_notify`) that watches a
list of nicks and reports when they come online, go offline or change nick,
to a channel set in `[notify_watch] notify_channel`.

## How it works

- Uses IRC `MONITOR` when the server supports it (`730`/`731` replies),
  otherwise polls with `ISON` every `ison_interval` seconds (`303`), and
  confirms with `WHOIS` (`311`/`318`/`401`).
- The watchlist is the `notify_watchlist` table in the bot's database
  (`.notify-add`, `.notify-del`, `.notify-list`, owner/admins). An old
  `sopel_notify/users.json` is imported once on start (only into an empty
  table) and moved, with the old `notify-debug.log`, to
  `<homedir>/backups/sopel-notify/`. Log lines go to the bot's own log
  (`sopel.externals.notify`).
- `config.py` holds the settings section; `defaults.py` the fallbacks.
- All logic is in `sopel_notify/__init__.py`.

## Working on it

- Nothing personal is kept in the repo folder any more; keep it that way.
- Test on a mock bot by feeding the numeric replies above as raw lines.
