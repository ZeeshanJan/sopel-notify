# sopel_notify/__init__.py
"""Tell the owner when watched nicks come online, go offline, join, leave or
change nick.

How it knows who is online, best first: MONITOR or WATCH when the server
offers them (the server then tells the bot at once), otherwise it asks with
ISON every `check_seconds`. People in a channel with the bot are also followed
through their JOIN/PART/KICK/QUIT/NICK lines, which arrive at once either way.

Everything lives in the bot's database, so the web UI can show and edit it:

    notify_watchlist  one row per watched nick (key: IRC-lowercase nick): a
                      note, and what the bot last saw (online or not, since
                      when, host, channels shared with the bot)
    notify_events     what happened; `sent` is 1 when the owner was told, 0
                      while it is saved for when they come back, NULL when
                      that kind of message is switched off
    notify_settings   the switches in DEFAULTS, as JSON values
    notify_state      `version` (raised by every writer; the bot re-reads the
                      watchlist and settings when it changes), `heartbeat`
                      (last time the bot confirmed its view), `mode`

Messages go to the owner ([core] owner). While the owner is offline they are
saved and sent as one summary when the owner is back.
"""
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from sopel import plugin
from sopel.tools import get_logger
from sqlalchemy import (
    Column, Integer, MetaData, String, Table, Text, case, delete, insert, inspect, select, text, update,
)
from sqlalchemy.exc import SQLAlchemyError

from .config import NotifySection

LOGGER = get_logger("notify")  # goes to the bot's own log
STATE_KEY = "notify_watch_state"
PREFIX = "[notify] "

# Older versions wrote to their own file through the "sopel_notify" logger;
# logging keeps loggers across plugin reloads, so close that file here.
_old = logging.getLogger("sopel_notify")
for _handler in list(_old.handlers):
    _old.removeHandler(_handler)
    _handler.close()

metadata = MetaData()
watchlist_table = Table(
    "notify_watchlist", metadata,
    Column("nick", String(64), primary_key=True),
    Column("added_at", Integer, nullable=False),
    Column("note", Text, nullable=False, server_default=""),
    Column("shown", String(64)),            # the nick as written or last seen
    Column("status", String(8)),            # "online", "offline", NULL = never checked
    Column("status_since", Integer),
    Column("last_mask", Text),
    Column("channels", Text, nullable=False, server_default=""),  # "#a #b"
)
events_table = Table(
    "notify_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", Integer, nullable=False, index=True),
    Column("nick", String(64), nullable=False, index=True),
    Column("shown", String(64), nullable=False),
    Column("kind", String(16), nullable=False),  # online offline join part kick nick
    Column("channel", String(64)),
    Column("detail", Text),
    Column("mask", Text),
    Column("sent", Integer),
)
settings_table = Table(
    "notify_settings", metadata,
    Column("name", String(32), primary_key=True),
    Column("value", Text, nullable=False),
)
state_table = Table(
    "notify_state", metadata,
    Column("name", String(32), primary_key=True),
    Column("value", Text, nullable=False),
)

# Settings and their defaults. The UI's lib/notify.ts mirrors this list.
DEFAULTS = {
    "online": True,          # came online (connected to the network)
    "offline": True,         # went offline (quit)
    "joins": True,           # joined a channel the bot is in
    "parts": True,           # left, or was kicked from, such a channel
    "nick_changes": True,
    "delivery": "message",   # "message" or "notice"
    "hold_when_away": True,  # save messages while the owner is offline
    "check_seconds": 60,     # ISON polling, only when MONITOR/WATCH are missing
    "keep_days": 30,         # how long notify_events keeps rows
}
KIND_SETTING = {
    "online": "online", "offline": "offline", "join": "joins",
    "part": "parts", "kick": "parts", "nick": "nick_changes",
}
NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}$")
TICK_SECONDS = 2
JOIN_QUIET_SECONDS = 3   # wait this long for more joins before telling
JOIN_MAX_SECONDS = 10
NICK_CHANGE_TTL = 30     # a logoff/logon this soon after a NICK is that nick change
HEARTBEAT_SECONDS = 30
AWAY_SUMMARY_LINES = 10

# Files the plugin used to keep in its own folder; imported once, then moved
# to <bot home>/backups/sopel-notify/.
PLUGIN_DIR = os.path.dirname(__file__)
LEGACY_WATCHLIST = os.path.join(PLUGIN_DIR, "users.json")
LEGACY_LOG = os.path.join(PLUGIN_DIR, "notify-debug.log")


# --------------------------------------------------------------------- state

def _new_state():
    return {
        "lock": threading.RLock(),
        "watch": {},          # key -> {"shown", "note"}
        "settings": dict(DEFAULTS),
        "version": None,
        # Per connection:
        "mode": None,         # "monitor", "watch" or "ison" once tracking started
        "connected_at": None,
        "motd_seen": False,
        "features": set(),    # MONITOR/WATCH, read from the server's 005 lines
        "status": {},         # key -> True/False; missing = not known yet
        "masks": {},          # key -> nick!user@host
        "shown": {},          # key -> nick as the server spells it
        "pending": {},        # key -> a fresh "online" and joins, told together
        "pending_whois": {},  # key -> nick, for ISON's "came online"
        "nick_changes": [],   # (old key, new key, expires)
        "ison_queue": [],     # the key sets of the ISON lines sent, oldest first
        "last_ison": 0.0,
        "last_heartbeat": 0.0,
        "last_prune": 0.0,
        "channels": {},       # key -> "#a #b" as last written
    }


def _state(bot):
    state = bot.memory.get(STATE_KEY)
    if not isinstance(state, dict) or "lock" not in state:
        state = _new_state()
        bot.memory[STATE_KEY] = state
    return state


def _guarded(context):
    """Run a handler under the state lock; log (not raise) its errors."""
    def wrap(fn):
        def inner(bot, *args):
            try:
                with _state(bot)["lock"]:
                    return fn(bot, *args)
            except Exception:
                LOGGER.exception("notify: %s failed", context)
        inner.__name__ = fn.__name__
        inner.__doc__ = fn.__doc__
        return inner
    return wrap


def _key(bot, nick):
    return str(bot.make_identifier(nick).lower())


def _owner_key(bot):
    owner = bot.config.core.owner
    return _key(bot, owner) if owner else None


def _now():
    return int(time.time())


# ------------------------------------------------------------------ database

def _add_missing_columns(engine):
    """Older versions made notify_watchlist with just nick and added_at."""
    have = {column["name"] for column in inspect(engine).get_columns("notify_watchlist")}
    with engine.begin() as conn:
        for column in watchlist_table.columns:
            if column.name in have:
                continue
            kind = column.type.compile(engine.dialect)
            default = " NOT NULL DEFAULT ''" if column.server_default is not None else ""
            conn.execute(text(f'ALTER TABLE notify_watchlist ADD COLUMN "{column.name}" {kind}{default}'))
            LOGGER.info("notify: added column notify_watchlist.%s", column.name)


def _read_version(conn):
    value = conn.execute(select(state_table.c.value).where(state_table.c.name == "version")).scalar()
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _set_state(conn, name, value):
    done = conn.execute(update(state_table).where(state_table.c.name == name).values(value=str(value)))
    if not done.rowcount:
        conn.execute(insert(state_table).values(name=name, value=str(value)))


def _bump_version(conn):
    _set_state(conn, "version", _read_version(conn) + 1)


def _clean_setting(name, value):
    default = DEFAULTS[name]
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if name == "delivery":
        return value if value in ("message", "notice") else default
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    if name == "check_seconds":
        return min(600, max(15, value))
    return min(3650, max(1, value))  # keep_days


def _import_config_settings(bot, conn):
    """First run: start from [notify_watch] in the config file."""
    section = getattr(bot.config, "notify_watch", None)
    if section is None:
        return
    values = {
        "online": section.watch_online, "joins": section.watch_online,
        "offline": section.watch_offline, "parts": section.watch_offline,
        "nick_changes": section.watch_nickchange, "check_seconds": section.ison_interval,
    }
    for name, value in values.items():
        conn.execute(insert(settings_table).values(name=name, value=json.dumps(_clean_setting(name, value))))


def _load(bot):
    """Read the watchlist and settings into memory; return the keys added and removed."""
    state = _state(bot)
    with bot.db.engine.begin() as conn:
        version = _read_version(conn)
        rows = conn.execute(select(watchlist_table.c.nick, watchlist_table.c.shown, watchlist_table.c.note)).all()
        settings = dict(DEFAULTS)
        for name, value in conn.execute(select(settings_table.c.name, settings_table.c.value)):
            if name in DEFAULTS:
                try:
                    settings[name] = _clean_setting(name, json.loads(value))
                except ValueError:
                    pass
        watch = {}
        for nick, shown, note in rows:
            key = _key(bot, nick)
            # Rows written with Python's lower() rather than IRC's ("[x]" vs "{x}").
            if key != nick and key not in watch:
                conn.execute(update(watchlist_table).where(watchlist_table.c.nick == nick).values(nick=key))
            watch[key] = {"shown": shown or nick, "note": note or ""}
    before = set(state["watch"])
    state["watch"] = watch
    state["settings"] = settings
    state["version"] = version
    return set(watch) - before, before - set(watch)


def _shared_channels(bot, key):
    names = [str(name) for name, channel in list(bot.channels.items()) if key in channel.users]
    return " ".join(sorted(names, key=str.lower))


def _save_status(bot, key, changed=False):
    """Write what the bot knows about a watched nick, for the UI.

    changed: its online/offline status may have changed (sets status_since
    unless the stored status was already the same).
    """
    state = _state(bot)
    if key not in state["watch"]:
        return
    online = state["status"].get(key)
    values = {"channels": _shared_channels(bot, key) if online else ""}
    state["channels"][key] = values["channels"]
    if changed and online is not None:
        new = "online" if online else "offline"
        values["status"] = new
        values["status_since"] = case(
            (watchlist_table.c.status == new, watchlist_table.c.status_since), else_=_now())
    if state["masks"].get(key):
        values["last_mask"] = state["masks"][key]
    if state["shown"].get(key):
        values["shown"] = state["shown"][key]
    with bot.db.engine.begin() as conn:
        conn.execute(update(watchlist_table).where(watchlist_table.c.nick == key).values(**values))


# ------------------------------------------------------------------- telling

def _send(bot, line):
    """Message the owner, and only the owner: never a channel."""
    owner = bot.config.core.owner
    if not owner or owner[0] in "#&+!":
        return
    if _state(bot)["settings"]["delivery"] == "notice":
        bot.notice(line[:400], owner)  # notice() does not split long text
    else:
        bot.say(line, owner)


def _and(items):
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _who(nick, mask):
    return f"{nick} ({mask})" if mask and "!" in mask and not mask.endswith("!?@?") else nick


def _tell(bot, events, line_for):
    """Save events, then message the owner about the wanted ones now, or hold them
    while the owner is offline. line_for(wanted events) -> the message."""
    state = _state(bot)
    settings = state["settings"]
    wanted = [event for event in events if settings[KIND_SETTING[event["kind"]]]]
    if not wanted:
        sent = None
    elif settings["hold_when_away"] and state["status"].get(_owner_key(bot)) is False:
        sent = 0
    else:
        sent = 1
    now = _now()
    rows = [{"at": now, "channel": None, "detail": None, "mask": None, **event,
             "sent": sent if event in wanted else None} for event in events]
    if rows:
        with bot.db.engine.begin() as conn:
            conn.execute(insert(events_table), rows)
    if sent == 1:
        _send(bot, PREFIX + line_for(wanted))


def _describe(event):
    """One line about one saved event (for the away summary)."""
    nick, kind = event["shown"], event["kind"]
    when = datetime.fromtimestamp(event["at"], timezone.utc).strftime("%d %b %H:%M UTC")
    channel, detail = event.get("channel"), event.get("detail")
    if kind == "online":
        what = f"{_who(nick, event.get('mask'))} came online"
    elif kind == "offline":
        what = f"{nick} went offline" + (f" ({detail})" if detail else "")
    elif kind == "join":
        what = f"{nick} joined {channel}"
    elif kind == "part":
        what = f"{nick} left {channel}" + (f" ({detail})" if detail else "")
    elif kind == "kick":
        what = f"{nick} was kicked from {channel}" + (f" {detail}" if detail else "")
    else:
        what = f"{nick} is now known as {detail}"
    return f"{when}: {what}"


def _send_held(bot):
    """The owner is back: send what was saved while they were away."""
    with bot.db.engine.begin() as conn:
        rows = [dict(row._mapping) for row in conn.execute(
            select(events_table).where(events_table.c.sent == 0).order_by(events_table.c.id))]
        if not rows:
            return
        conn.execute(update(events_table).where(events_table.c.sent == 0).values(sent=1))
    _send(bot, f"{PREFIX}While you were away ({len(rows)}):")
    for row in rows[:AWAY_SUMMARY_LINES]:
        _send(bot, PREFIX + _describe(row))
    if len(rows) > AWAY_SUMMARY_LINES:
        _send(bot, f"{PREFIX}…and {len(rows) - AWAY_SUMMARY_LINES} more on the UI's Notify page.")


# ------------------------------------------------------------ presence model

def _recent_nick_change(state, old=None, new=None):
    now = time.time()
    state["nick_changes"] = [c for c in state["nick_changes"] if c[2] > now]
    return any((old is None or c[0] == old) and (new is None or c[1] == new) for c in state["nick_changes"])


def _owner_seen(bot, online):
    key = _owner_key(bot)
    if key is None:
        return
    state = _state(bot)
    was = state["status"].get(key)
    state["status"][key] = online
    if online and was is False:
        _send_held(bot)


def _set_online(bot, key, nick, mask=None, *, join=None):
    """`key` is online. Only a change from known-offline is news; the first
    answer after (re)connecting or adding a nick just sets the status."""
    state = _state(bot)
    if key == _owner_key(bot):
        _owner_seen(bot, True)
    if key not in state["watch"]:
        return
    if mask:
        state["masks"][key] = mask
    state["shown"][key] = nick
    was = state["status"].get(key)
    state["status"][key] = True
    fresh = was is False and not _recent_nick_change(state, new=key)
    if fresh or join:
        now = time.time()
        entry = state["pending"].setdefault(key, {"online": False, "joins": [], "since": now})
        entry.update(nick=nick, last=now, online=entry["online"] or fresh)
        if join and join not in entry["joins"]:
            entry["joins"].append(join)
    if was is not True:
        _save_status(bot, key, changed=True)


def _set_offline(bot, key, nick, *, detail=None):
    state = _state(bot)
    if key == _owner_key(bot):
        _owner_seen(bot, False)
    if key not in state["watch"]:
        return
    _flush_pending(bot, key)
    was = state["status"].get(key)
    state["status"][key] = False
    if was is False:
        return
    _save_status(bot, key, changed=True)
    if was is None or _recent_nick_change(state, old=key):
        return
    mask = state["masks"].get(key)
    _tell(bot, [{"nick": key, "shown": nick, "kind": "offline", "detail": detail, "mask": mask}],
          lambda _: f"{_who(nick, mask)} went offline" + (f" ({detail})." if detail else "."))


def _flush_pending(bot, key):
    """Tell about a fresh "online" and/or the joins collected for `key`."""
    entry = _state(bot)["pending"].pop(key, None)
    if not entry or not (entry["online"] or entry["joins"]):
        return
    nick = entry["nick"]
    mask = _state(bot)["masks"].get(key)
    events = []
    if entry["online"]:
        events.append({"nick": key, "shown": nick, "kind": "online", "mask": mask})
    events += [{"nick": key, "shown": nick, "kind": "join", "channel": channel, "mask": mask}
               for channel in entry["joins"]]

    def line(wanted):
        online = any(event["kind"] == "online" for event in wanted)
        joined = [event["channel"] for event in wanted if event["kind"] == "join"]
        if online and joined:
            return f"{_who(nick, mask)} came online and joined {_and(joined)}."
        if online:
            return f"{_who(nick, mask)} came online."
        return f"{_who(nick, mask)} joined {_and(joined)}."

    _tell(bot, events, line)
    _save_status(bot, key)


def _flush_due(bot):
    now = time.time()
    for key, entry in list(_state(bot)["pending"].items()):
        if now - entry["last"] >= JOIN_QUIET_SECONDS or now - entry["since"] >= JOIN_MAX_SECONDS:
            _flush_pending(bot, key)


# ------------------------------------------------------------------ tracking

def _targets(bot):
    """Everyone whose presence the bot asks the server about: the watchlist and the owner."""
    keys = set(_state(bot)["watch"])
    owner = _owner_key(bot)
    if owner:
        keys.add(owner)
    return keys


def _chunks(items, size):
    items = sorted(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


def _track(bot, added, removed):
    """Tell the server about watchlist changes."""
    state = _state(bot)
    removed = set(removed) - {_owner_key(bot)}
    if state["mode"] == "monitor":
        for chunk in _chunks(removed, 20):
            bot.write(("MONITOR", "-", ",".join(chunk)))
        for chunk in _chunks(added, 20):
            bot.write(("MONITOR", "+", ",".join(chunk)))
    elif state["mode"] == "watch":
        for chunk in _chunks([f"-{n}" for n in removed] + [f"+{n}" for n in added], 20):
            bot.write(("WATCH", *chunk))
    elif state["mode"] == "ison" and added:
        _poll_ison(bot)
    for key in removed:
        for name in ("status", "masks", "shown", "pending", "channels"):
            state[name].pop(key, None)


def _start(bot):
    state = _state(bot)
    features = state["features"] | {name for name in ("MONITOR", "WATCH") if name in bot.isupport}
    if "MONITOR" in features:
        state["mode"] = "monitor"
        bot.write(("MONITOR", "C"))
    elif "WATCH" in features:
        state["mode"] = "watch"
        bot.write(("WATCH", "C"))
    else:
        state["mode"] = "ison"
        # Servers that hide WATCH from their 005 lines (or after a reload,
        # when none come) still answer this; see on_watch_reply.
        bot.write(("WATCH", "S"))
    state["status"].clear()
    state["ison_queue"].clear()
    LOGGER.info("notify: watching %d nicks with %s", len(state["watch"]), state["mode"].upper())
    with bot.db.engine.begin() as conn:
        _set_state(conn, "mode", state["mode"])
        _set_state(conn, "heartbeat", _now())
    _track(bot, _targets(bot), set())


def _poll_ison(bot):
    state = _state(bot)
    state["last_ison"] = time.time()
    for chunk in _chunks(_targets(bot), 20):
        state["ison_queue"].append(set(chunk))
        bot.write(("ISON", " ".join(chunk)))
    del state["ison_queue"][:-50]


def _heartbeat(bot):
    """Tell the UI the bot's view is current, and refresh shared channels."""
    state = _state(bot)
    state["last_heartbeat"] = time.time()
    for key, online in list(state["status"].items()):
        if online and key in state["watch"] and _shared_channels(bot, key) != state["channels"].get(key):
            _save_status(bot, key)
    with bot.db.engine.begin() as conn:
        _set_state(conn, "heartbeat", _now())


def _prune(bot):
    state = _state(bot)
    state["last_prune"] = time.time()
    cutoff = _now() - state["settings"]["keep_days"] * 86400
    with bot.db.engine.begin() as conn:
        conn.execute(delete(events_table).where(events_table.c.at < cutoff))


# --------------------------------------------------------------------- setup

def _move_to_backups(bot, path):
    folder = os.path.join(bot.config.core.homedir, "backups", "sopel-notify")
    os.makedirs(folder, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.move(path, os.path.join(folder, f"{os.path.basename(path)}.{stamp}"))


def migrate_legacy_files(bot):
    """Import users.json once (only into an empty watchlist), then move the old files away."""
    if os.path.isfile(LEGACY_WATCHLIST):
        try:
            with open(LEGACY_WATCHLIST, encoding="utf-8") as f:
                nicks = {str(n) for n in json.load(f).get("users", [])}
            with bot.db.engine.begin() as conn:
                if conn.execute(select(watchlist_table).limit(1)).first():
                    LOGGER.warning("notify: users.json not imported, the database already has a watchlist")
                elif nicks:
                    now = _now()
                    conn.execute(insert(watchlist_table), [
                        {"nick": _key(bot, n), "shown": n, "added_at": now, "note": "", "channels": ""}
                        for n in sorted(nicks)])
                    LOGGER.info("notify: imported %d nicks from users.json", len(nicks))
            _move_to_backups(bot, LEGACY_WATCHLIST)
        except (OSError, ValueError, AttributeError, SQLAlchemyError):
            LOGGER.exception("notify: could not import users.json")
    if os.path.isfile(LEGACY_LOG):
        try:
            _move_to_backups(bot, LEGACY_LOG)
        except OSError:
            LOGGER.exception("notify: could not move the old debug log")


def setup(bot):
    bot.config.define_section("notify_watch", NotifySection)
    metadata.create_all(bot.db.engine)
    _add_missing_columns(bot.db.engine)
    with bot.db.engine.begin() as conn:
        if conn.execute(select(settings_table).limit(1)).first() is None:
            _import_config_settings(bot, conn)
    migrate_legacy_files(bot)
    bot.memory[STATE_KEY] = _new_state()
    state = _state(bot)
    with state["lock"]:
        _load(bot)
        # Reloaded while connected: no 001/MOTD will come, so start on the next tick.
        if bot.connection_registered:
            state["motd_seen"] = True
    LOGGER.info("notify plugin setup completed (%d nicks watched)", len(state["watch"]))


def shutdown(bot):
    state = bot.memory.get(STATE_KEY)
    if isinstance(state, dict) and state.get("mode") == "monitor":
        try:
            bot.write(("MONITOR", "C"))
        except Exception:
            pass


# -------------------------------------------------------------------- events

@plugin.event("001")
@plugin.rule(".*")
@plugin.priority("high")
@_guarded("connect")
def on_welcome(bot, trigger):
    state = _state(bot)
    for name in ("status", "masks", "shown", "pending", "pending_whois", "channels"):
        state[name].clear()
    state["nick_changes"].clear()
    state["ison_queue"].clear()
    state["features"].clear()
    state.update(mode=None, connected_at=time.time(), motd_seen=False)


@plugin.event("005")
@plugin.rule(".*")
@_guarded("ISUPPORT")
def on_isupport(bot, trigger):
    """Note MONITOR/WATCH support. Sopel itself only reads 005 lines that end
    "are supported by this server"; DALnet says "available on this server"."""
    state = _state(bot)
    _found_features(bot, {arg.split("=", 1)[0] for arg in trigger.args[1:-1]} & {"MONITOR", "WATCH"})


def _found_features(bot, found):
    state = _state(bot)
    if found - state["features"]:
        state["features"] |= found
        if state["mode"] == "ison":  # found out late: switch over
            _start(bot)


@plugin.event("603", "606", "607")  # answers to "WATCH S"
@plugin.rule(".*")
@_guarded("WATCH status")
def on_watch_reply(bot, trigger):
    _found_features(bot, {"WATCH"})


@plugin.event("376", "422")  # end of MOTD / no MOTD: the server's features are known by now
@plugin.rule(".*")
@_guarded("MOTD")
def on_motd_end(bot, trigger):
    _state(bot)["motd_seen"] = True


@plugin.interval(TICK_SECONDS)
@_guarded("tick")
def tick(bot):
    state = _state(bot)
    if not bot.connection_registered:
        return
    now = time.time()
    with bot.db.engine.connect() as conn:
        version = _read_version(conn)
    if version != state["version"]:  # changed from the UI
        added, removed = _load(bot)
        if state["mode"]:
            _track(bot, added, removed)
    if state["mode"] is None:
        if state["motd_seen"] or now - (state["connected_at"] or now) > 15:
            _start(bot)
        return
    _flush_due(bot)
    if state["mode"] == "ison" and now - state["last_ison"] >= state["settings"]["check_seconds"]:
        _poll_ison(bot)
    if now - state["last_heartbeat"] >= HEARTBEAT_SECONDS:
        _heartbeat(bot)
    if now - state["last_prune"] >= 3600:
        _prune(bot)


def _entries(trigger):
    """The nicks in a MONITOR reply: "a!u@h,b!u@h"."""
    return [item for item in (trigger.args[-1] if trigger.args else "").lstrip(":").split(",") if item]


@plugin.event("730")
@plugin.rule(".*")
@_guarded("MONITOR online")
def on_monitor_online(bot, trigger):
    for entry in _entries(trigger):
        nick = entry.split("!", 1)[0]
        _set_online(bot, _key(bot, nick), nick, entry if "!" in entry else None)


@plugin.event("731")
@plugin.rule(".*")
@_guarded("MONITOR offline")
def on_monitor_offline(bot, trigger):
    for entry in _entries(trigger):
        nick = entry.split("!", 1)[0]
        _set_offline(bot, _key(bot, nick), nick)


# WATCH replies: <me> <nick> <user> <host> <time> :text
@plugin.event("600", "604")  # logged on / is on (604 answers our "WATCH +nick")
@plugin.rule(".*")
@_guarded("WATCH online")
def on_watch_online(bot, trigger):
    if len(trigger.args) >= 4:
        nick, user, host = trigger.args[1:4]
        _set_online(bot, _key(bot, nick), nick, f"{nick}!{user}@{host}")


@plugin.event("601", "605")  # logged off / is off
@plugin.rule(".*")
@_guarded("WATCH offline")
def on_watch_offline(bot, trigger):
    if len(trigger.args) >= 2:
        _set_offline(bot, _key(bot, trigger.args[1]), trigger.args[1])


@plugin.event("303")
@plugin.rule(".*")
@_guarded("ISON reply")
def on_ison_reply(bot, trigger):
    state = _state(bot)
    if state["mode"] != "ison" or not state["ison_queue"]:
        return
    asked = state["ison_queue"].pop(0)
    online = {_key(bot, n): n for n in (trigger.args[-1] if trigger.args else "").lstrip(":").split() if n}
    for key in asked:
        if key not in online:
            nick = state["shown"].get(key) or state["watch"].get(key, {}).get("shown") or key
            _set_offline(bot, key, nick)
        elif state["status"].get(key) is False and key in state["watch"]:
            # Came online: ask for their host before telling.
            state["pending_whois"][key] = online[key]
            bot.write(("WHOIS", online[key]))
        else:
            _set_online(bot, key, online[key])


@plugin.event("311")  # WHOIS: <me> <nick> <user> <host> * :<real name>
@plugin.rule(".*")
@_guarded("WHOIS")
def on_whois_user(bot, trigger):
    if len(trigger.args) >= 4 and _key(bot, trigger.args[1]) in _state(bot)["pending_whois"]:
        nick = trigger.args[1]
        _state(bot)["masks"][_key(bot, nick)] = f"{nick}!{trigger.args[2]}@{trigger.args[3]}"


@plugin.event("318", "401")  # end of WHOIS / no such nick
@plugin.rule(".*")
@_guarded("WHOIS end")
def on_whois_end(bot, trigger):
    if len(trigger.args) < 2:
        return
    key = _key(bot, trigger.args[1])
    nick = _state(bot)["pending_whois"].pop(key, None)
    if nick is None:
        return
    if trigger.event == "318":
        _set_online(bot, key, nick)
    else:  # already gone again: leave it to the next check
        _state(bot)["status"].pop(key, None)


def _mask(trigger):
    return f"{trigger.nick}!{trigger.user or '?'}@{trigger.host or '?'}"


def _is_me(bot, trigger):
    return trigger.nick == bot.nick


@plugin.event("JOIN")
@plugin.rule(".*")
@_guarded("JOIN")
def on_join(bot, trigger):
    if _is_me(bot, trigger):
        return
    key = _key(bot, trigger.nick)
    if key == _owner_key(bot):
        _owner_seen(bot, True)
    if key in _state(bot)["watch"]:
        _set_online(bot, key, str(trigger.nick), _mask(trigger), join=str(trigger.sender))


@plugin.event("PART")
@plugin.rule(".*")
@_guarded("PART")
def on_part(bot, trigger):
    key = _key(bot, trigger.nick)
    state = _state(bot)
    if _is_me(bot, trigger) or key not in state["watch"]:
        return
    channel, nick, mask = str(trigger.sender), str(trigger.nick), _mask(trigger)
    state["masks"][key] = mask
    entry = state["pending"].get(key)
    if entry and channel in entry["joins"]:
        entry["joins"].remove(channel)  # joined and left at once: tell neither
        return
    _flush_pending(bot, key)
    reason = trigger.args[1] if len(trigger.args) > 1 and trigger.args[1] else None
    _tell(bot, [{"nick": key, "shown": nick, "kind": "part", "channel": channel, "detail": reason, "mask": mask}],
          lambda _: f"{nick} left {channel}" + (f" ({reason})." if reason else "."))
    _save_status(bot, key)


@plugin.event("KICK")
@plugin.rule(".*")
@_guarded("KICK")
def on_kick(bot, trigger):
    if len(trigger.args) < 2:
        return
    nick, channel = trigger.args[1], str(trigger.sender)
    key = _key(bot, nick)
    if key not in _state(bot)["watch"]:
        return
    _flush_pending(bot, key)
    reason = trigger.args[2] if len(trigger.args) > 2 and trigger.args[2] != nick else ""
    detail = f"by {trigger.nick}" + (f": {reason}" if reason else "")
    _tell(bot, [{"nick": key, "shown": nick, "kind": "kick", "channel": channel, "detail": detail}],
          lambda _: f"{nick} was kicked from {channel} {detail}.")
    _save_status(bot, key)


@plugin.event("QUIT")
@plugin.rule(".*")
@_guarded("QUIT")
def on_quit(bot, trigger):
    key = _key(bot, trigger.nick)
    if key in _state(bot)["watch"]:
        _state(bot)["masks"][key] = _mask(trigger)
    reason = trigger.args[0] if trigger.args and trigger.args[0] else None
    _set_offline(bot, key, str(trigger.nick), detail=reason)


@plugin.event("NICK")
@plugin.rule(".*")
@_guarded("NICK")
def on_nick_change(bot, trigger):
    if not trigger.args or _is_me(bot, trigger):
        return
    state = _state(bot)
    old_nick, new_nick = str(trigger.nick), trigger.args[0]
    old, new = _key(bot, old_nick), _key(bot, new_nick)
    if old == new:
        return
    state["nick_changes"].append((old, new, time.time() + NICK_CHANGE_TTL))
    owner = _owner_key(bot)
    if owner in (old, new):
        _owner_seen(bot, new == owner)
    watched = [key for key in (old, new) if key in state["watch"]]
    if not watched:
        return
    mask = f"{new_nick}!{trigger.user or '?'}@{trigger.host or '?'}"
    _flush_pending(bot, old)
    if old in state["watch"]:
        state["status"][old] = False
        _save_status(bot, old, changed=True)
    if new in state["watch"]:
        state["masks"][new] = mask
        state["shown"][new] = new_nick
        state["status"][new] = True
        _save_status(bot, new, changed=True)
    _tell(bot, [{"nick": watched[0], "shown": old_nick, "kind": "nick", "detail": new_nick, "mask": mask}],
          lambda _: f"{old_nick} is now known as {new_nick}.")


# ------------------------------------------------------------------ commands

def _change_watchlist(bot, add=None, remove=None, note=""):
    with bot.db.engine.begin() as conn:
        if add:
            conn.execute(insert(watchlist_table).values(
                nick=_key(bot, add), shown=add, added_at=_now(), note=note, channels=""))
        if remove:
            conn.execute(delete(watchlist_table).where(watchlist_table.c.nick == remove))
        _bump_version(conn)
    added, removed = _load(bot)
    if _state(bot)["mode"]:
        _track(bot, added, removed)


@plugin.commands("notify-add")
@plugin.example(".notify-add alice school friend")
@plugin.require_admin()
@_guarded("notify-add")
def add_notify(bot, trigger):
    """Watch a nick (owner and admins only). A note can follow the nick."""
    words = (trigger.group(2) or "").split(None, 1)
    if not words:
        bot.say("Usage: .notify-add <nick> [note]", trigger.nick)
        return
    nick, note = words[0], (words[1].strip() if len(words) > 1 else "")[:200]
    if not NICK_RE.match(nick):
        bot.say(f"{nick} is not a valid nick.", trigger.nick)
        return
    if _key(bot, nick) in _state(bot)["watch"]:
        bot.say(f"{nick} is already being watched.", trigger.nick)
        return
    _change_watchlist(bot, add=nick, note=note)
    bot.say(f"Now watching {nick}.", trigger.nick)


@plugin.commands("notify-del")
@plugin.example(".notify-del alice")
@plugin.require_admin()
@_guarded("notify-del")
def del_notify(bot, trigger):
    """Stop watching a nick (owner and admins only)."""
    nick = (trigger.group(3) or "").strip()
    if not nick:
        bot.say("Usage: .notify-del <nick>", trigger.nick)
        return
    if _key(bot, nick) not in _state(bot)["watch"]:
        bot.say(f"{nick} is not on the watchlist.", trigger.nick)
        return
    _change_watchlist(bot, remove=_key(bot, nick))
    bot.say(f"Stopped watching {nick}.", trigger.nick)


@plugin.commands("notify-list", "notify")
@plugin.require_admin()
@_guarded("notify-list")
def list_notify(bot, trigger):
    """Show the watchlist and who is online now (owner and admins only)."""
    state = _state(bot)
    if not state["watch"]:
        bot.say("The watchlist is empty. Add a nick with .notify-add <nick>", trigger.nick)
        return
    online, offline, unknown = [], [], []
    for key, info in sorted(state["watch"].items()):
        status = state["status"].get(key)
        nick = state["shown"].get(key) or info["shown"]
        (online if status else offline if status is False else unknown).append(nick)
    bot.say(f"Watching {len(state['watch'])}. Online: {', '.join(online) or 'nobody'}.", trigger.nick)
    if offline:
        bot.say("Offline: " + ", ".join(offline), trigger.nick)
    if unknown:
        bot.say("Not checked yet: " + ", ".join(unknown), trigger.nick)
