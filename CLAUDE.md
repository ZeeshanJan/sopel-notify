@~/sopel/CLAUDE.md

# sopel-notify

A Sopel 8 plugin (entry point `notify_watch = sopel_notify`) that watches a
list of nicks and reports when they come online, go offline or change nick,
to a channel set in `[notify_watch] notify_channel`.

## How it works

- Uses IRC `MONITOR` when the server supports it (`730`/`731` replies),
  otherwise polls with `ISON` every `ison_interval` seconds (`303`), and
  confirms with `WHOIS` (`311`/`318`/`401`).
- The watchlist is `sopel_notify/users.json`, and the plugin writes a debug
  log to `sopel_notify/notify-debug.log`. Both are local and git-ignored.
- `config.py` holds the settings section; `defaults.py` the fallbacks.
- All logic is in `sopel_notify/__init__.py`.

## Working on it

- Don't commit the watchlist or log (they name real people).
- Test on a mock bot by feeding the numeric replies above as raw lines.
