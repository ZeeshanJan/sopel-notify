# sopel_notify/__init__.py
from sopel import plugin
import os
import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from .config import NotifySection
from .defaults import DEFAULTS

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "users.json")
PLUGIN_LOG_FILE = os.path.join(os.path.dirname(__file__), "notify-debug.log")
STATE_KEY = "notify_watch_state"


def _build_logger():
    logger = logging.getLogger("sopel_notify")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        handler = logging.FileHandler(PLUGIN_LOG_FILE)
    except Exception:
        # Temporary fallback for testing when Sopel can't write in plugin dir.
        fallback_log = os.path.join(tempfile.gettempdir(), "notify-debug.log")
        try:
            handler = logging.FileHandler(fallback_log)
        except Exception:
            logger.addHandler(logging.NullHandler())
            return logger
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("notify logger initialized: %s", handler.baseFilename)
    return logger


LOGGER = _build_logger()


def _log_exception(context):
    LOGGER.exception("%s", context)


def _state(bot):
    state = bot.memory.get(STATE_KEY)
    if state is None:
        state = {
            "monitor_supported": None,
            "ison_initialized": False,
            "online": set(),
            "last_ison_poll": 0.0,
            "hostmasks": {},
            "pending_whois": {},
        }
        bot.memory[STATE_KEY] = state
    return state


def _chunked(items, size):
    seq = list(items)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _monitor_add(bot, nicks):
    nick_list = [n for n in nicks if n]
    for chunk in _chunked(nick_list, 20):
        bot.write(("MONITOR", "+", ",".join(chunk)))


def _monitor_del(bot, nicks):
    nick_list = [n for n in nicks if n]
    for chunk in _chunked(nick_list, 20):
        bot.write(("MONITOR", "-", ",".join(chunk)))


def _request_ison(bot):
    if not WATCHLIST:
        return
    bot.write(("ISON", " ".join(sorted(WATCHLIST))))


def _format_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _notify_network_presence(bot, nick, hostmask, is_online, source):
    watch_key = nick.lower()
    if watch_key not in WATCHLIST:
        return
    if is_online and not _cfg_value(bot, "watch_online"):
        return
    if not is_online and not _cfg_value(bot, "watch_offline"):
        return
    action = "connected to the network" if is_online else "disconnected from the network"
    stamp = _format_now()
    mask = hostmask or f"{nick}!?@?"
    bot.say(
        f"[{stamp}] {nick} ({mask}) has {action} ({source}).",
        get_notify_target(bot),
    )


def _queue_whois_presence(bot, nick, is_online, source, fallback_hostmask=None):
    state = _state(bot)
    key = nick.lower()
    state["pending_whois"][key] = {
        "nick": nick,
        "is_online": is_online,
        "source": source,
        "fallback_hostmask": fallback_hostmask,
    }
    bot.write(("WHOIS", nick))


def _flush_pending_whois(bot, nick, hostmask=None):
    state = _state(bot)
    key = nick.lower()
    pending = state["pending_whois"].pop(key, None)
    if not pending:
        return
    if hostmask:
        state["hostmasks"][key] = hostmask
    fallback_mask = pending.get("fallback_hostmask")
    cached_mask = hostmask or state["hostmasks"].get(key) or fallback_mask
    if fallback_mask and key not in state["hostmasks"]:
        state["hostmasks"][key] = fallback_mask
    _notify_network_presence(
        bot,
        pending["nick"],
        cached_mask,
        pending["is_online"],
        pending["source"],
    )


def _sync_watchlist_tracking(bot):
    state = _state(bot)
    tracked_online = state["online"]
    tracked_online.intersection_update(WATCHLIST)
    if state["monitor_supported"] is True:
        _monitor_add(bot, WATCHLIST)

# Load watchlist from JSON file
def load_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                return set(user.lower() for user in data.get("users", []))
    except Exception:
        _log_exception("Failed to load watchlist")
    return set()

# Save watchlist to JSON file
def save_watchlist():
    data = {"users": list(WATCHLIST)}
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

WATCHLIST = load_watchlist()


def setup(bot):
    bot.config.define_section('notify_watch', NotifySection)
    LOGGER.info("notify plugin setup completed")
    _state(bot)


@plugin.event("001")
@plugin.rule(".*")
def on_welcome(bot, trigger):
    try:
        LOGGER.info("network connected; initializing presence tracking")
        state = _state(bot)
        state["ison_initialized"] = False
        state["online"].clear()
        state["pending_whois"].clear()
        if WATCHLIST:
            _monitor_add(bot, WATCHLIST)
        _request_ison(bot)
        state["last_ison_poll"] = time.time()
    except Exception:
        _log_exception("Presence initialization failed")


@plugin.interval(15)
def presence_poll(bot):
    try:
        state = _state(bot)
        if state["monitor_supported"] is True:
            return
        interval = _cfg_value(bot, "ison_interval")
        if not isinstance(interval, int):
            interval = DEFAULTS["ison_interval"]
        interval = max(15, interval)
        now = time.time()
        if now - state["last_ison_poll"] < interval:
            return
        state["last_ison_poll"] = now
        _request_ison(bot)
    except Exception:
        _log_exception("ISON polling failed")


@plugin.event("421")
@plugin.rule(".*")
def on_unknown_command(bot, trigger):
    try:
        args = [a.upper() for a in trigger.args]
        if "MONITOR" in args:
            state = _state(bot)
            if state["monitor_supported"] is not False:
                state["monitor_supported"] = False
                LOGGER.info("MONITOR not supported by server; using ISON fallback")
    except Exception:
        _log_exception("MONITOR capability detection failed")


def _parse_monitor_list(raw):
    if not raw:
        return []
    cleaned = raw.lstrip(":")
    return [item for item in cleaned.split(",") if item]


@plugin.event("730")
@plugin.rule(".*")
def on_monitor_online(bot, trigger):
    try:
        state = _state(bot)
        state["monitor_supported"] = True
        entries = _parse_monitor_list(trigger.args[-1] if trigger.args else "")
        for entry in entries:
            nick = entry.split("!", 1)[0]
            key = nick.lower()
            if key not in WATCHLIST:
                continue
            if key in state["online"]:
                continue
            state["online"].add(key)
            _queue_whois_presence(bot, nick, True, "MONITOR+WHOIS", entry)
    except Exception:
        _log_exception("MONITOR online handler failed")


@plugin.event("731")
@plugin.rule(".*")
def on_monitor_offline(bot, trigger):
    try:
        state = _state(bot)
        state["monitor_supported"] = True
        entries = _parse_monitor_list(trigger.args[-1] if trigger.args else "")
        for entry in entries:
            nick = entry.split("!", 1)[0]
            key = nick.lower()
            if key not in WATCHLIST:
                continue
            if key not in state["online"]:
                continue
            state["online"].discard(key)
            _notify_network_presence(bot, nick, None, False, "MONITOR")
    except Exception:
        _log_exception("MONITOR offline handler failed")


@plugin.event("303")
@plugin.rule(".*")
def on_ison_reply(bot, trigger):
    try:
        state = _state(bot)
        if state["monitor_supported"] is True:
            return
        payload = (trigger.args[-1] if trigger.args else "").lstrip(":")
        online_now = set(n.lower() for n in payload.split() if n)
        online_now.intersection_update(WATCHLIST)
        if not state["ison_initialized"]:
            state["online"] = set(online_now)
            state["ison_initialized"] = True
            LOGGER.info("ISON baseline captured: %d online", len(online_now))
            return

        became_online = online_now - state["online"]
        became_offline = state["online"] - online_now
        for nick in sorted(became_online):
            _queue_whois_presence(bot, nick, True, "ISON+WHOIS")
        for nick in sorted(became_offline):
            _notify_network_presence(
                bot,
                nick,
                state["hostmasks"].get(nick),
                False,
                "ISON",
            )
        state["online"] = online_now
    except Exception:
        _log_exception("ISON reply handler failed")


@plugin.event("311")
@plugin.rule(".*")
def on_whois_user(bot, trigger):
    try:
        # 311 <me> <nick> <user> <host> * :<realname>
        if len(trigger.args) < 5:
            return
        nick = trigger.args[1]
        user = trigger.args[2]
        host = trigger.args[3]
        _flush_pending_whois(bot, nick, f"{nick}!{user}@{host}")
    except Exception:
        _log_exception("WHOIS user handler failed")


@plugin.event("318")
@plugin.rule(".*")
def on_whois_end(bot, trigger):
    try:
        # 318 <me> <nick> :End of /WHOIS list.
        if len(trigger.args) < 2:
            return
        nick = trigger.args[1]
        _flush_pending_whois(bot, nick)
    except Exception:
        _log_exception("WHOIS end handler failed")


@plugin.event("401")
@plugin.rule(".*")
def on_no_such_nick(bot, trigger):
    try:
        # 401 <me> <nick> :No such nick/channel
        if len(trigger.args) < 2:
            return
        nick = trigger.args[1]
        _flush_pending_whois(bot, nick)
    except Exception:
        _log_exception("WHOIS no-such-nick handler failed")


def _cfg_value(bot, name):
    cfg = getattr(bot.config, "notify_watch", None)
    if cfg is None:
        cfg = getattr(bot.config, "notify", None)
    if cfg is None:
        return DEFAULTS[name]
    return getattr(cfg, name, DEFAULTS[name])


# Helper: get channel for notifications
def get_notify_channel(bot):
    return _cfg_value(bot, "notify_channel")


def get_notify_target(bot):
    return bot.config.core.owner


def _format_event_timestamp(trigger):
    event_time = getattr(trigger, "time", None)
    if event_time is None:
        event_time = datetime.now(timezone.utc)
    return event_time.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_hostmask(trigger):
    hostmask = getattr(trigger, "hostmask", None)
    if hostmask:
        return hostmask
    user = getattr(trigger, "user", None) or "?"
    host = getattr(trigger, "host", None) or "?"
    return f"{trigger.nick}!{user}@{host}"


# -------------------
# Event notifications
# -------------------
@plugin.event('JOIN')
@plugin.rule('.*')
def on_join(bot, trigger):
    try:
        if not _cfg_value(bot, "watch_online"):
            return
        nick = trigger.nick.lower()
        if nick in WATCHLIST:
            stamp = _format_event_timestamp(trigger)
            mask = _format_hostmask(trigger)
            bot.say(
                f"[{stamp}] {trigger.nick} ({mask}) has joined {trigger.sender}.",
                get_notify_target(bot),
            )
    except Exception:
        _log_exception("JOIN handler failed")

@plugin.event('PART')
@plugin.rule('.*')
def on_part(bot, trigger):
    try:
        if not _cfg_value(bot, "watch_offline"):
            return
        nick = trigger.nick.lower()
        if nick in WATCHLIST:
            stamp = _format_event_timestamp(trigger)
            mask = _format_hostmask(trigger)
            bot.say(
                f"[{stamp}] {trigger.nick} ({mask}) has left {trigger.sender}.",
                get_notify_target(bot),
            )
    except Exception:
        _log_exception("PART handler failed")

@plugin.event('QUIT')
@plugin.rule('.*')
def on_quit(bot, trigger):
    try:
        state = _state(bot)
        if state["monitor_supported"] is True or state["ison_initialized"]:
            return
        if not _cfg_value(bot, "watch_offline"):
            return
        nick = trigger.nick.lower()
        if nick in WATCHLIST:
            stamp = _format_event_timestamp(trigger)
            mask = _format_hostmask(trigger)
            bot.say(
                f"[{stamp}] {trigger.nick} ({mask}) has disconnected from the network.",
                get_notify_target(bot),
            )
    except Exception:
        _log_exception("QUIT handler failed")

@plugin.event('NICK')
@plugin.rule('.*')
def on_nick_change(bot, trigger):
    try:
        if not _cfg_value(bot, "watch_nickchange"):
            return
        old_nick = trigger.nick.lower()
        new_nick = trigger.args[0].lower()
        if old_nick in WATCHLIST or new_nick in WATCHLIST:
            stamp = _format_event_timestamp(trigger)
            mask = _format_hostmask(trigger)
            bot.say(
                f"[{stamp}] {trigger.nick} ({mask}) changed nick to {trigger.args[0]}.",
                get_notify_target(bot),
            )
    except Exception:
        _log_exception("NICK handler failed")

# -------------------
# Admin commands
# -------------------
def is_admin(bot, nick):
    owner = bot.config.core.owner
    admins = bot.config.core.admins or []
    return nick.lower() == owner.lower() or nick.lower() in (a.lower() for a in admins)

@plugin.commands('notify-add')
@plugin.thread(True)
def add_notify(bot, trigger):
    try:
        LOGGER.info("notify-add called by=%s raw_args=%r", trigger.nick, trigger.group(2))
        if not is_admin(bot, trigger.nick):
            bot.say("You are not authorized to use this command.", trigger.nick)
            return
        args = trigger.group(2)
        if not args:
            bot.say("Usage: .notify-add <nick>", trigger.nick)
            return
        nick_to_add = args.strip().lower()
        if nick_to_add in WATCHLIST:
            bot.say(f"{nick_to_add} is already being watched.", trigger.nick)
            return
        WATCHLIST.add(nick_to_add)
        bot.say(f"Added {nick_to_add} to watchlist.", trigger.nick)
        save_watchlist()
        _sync_watchlist_tracking(bot)
        _request_ison(bot)
        LOGGER.info("notify-add success nick=%s", nick_to_add)
    except Exception:
        _log_exception("notify-add command failed")

@plugin.commands('notify-del')
@plugin.thread(True)
def del_notify(bot, trigger):
    try:
        LOGGER.info("notify-del called by=%s raw_args=%r", trigger.nick, trigger.group(2))
        if not is_admin(bot, trigger.nick):
            bot.say("You are not authorized to use this command.", trigger.nick)
            return
        args = trigger.group(2)
        if not args:
            bot.say("Usage: .notify-del <nick>", trigger.nick)
            return
        nick_to_del = args.strip().lower()
        if nick_to_del not in WATCHLIST:
            bot.say(f"{nick_to_del} is not in watchlist.", trigger.nick)
            return
        WATCHLIST.remove(nick_to_del)
        bot.say(f"Removed {nick_to_del} from watchlist.", trigger.nick)
        save_watchlist()
        state = _state(bot)
        state["online"].discard(nick_to_del)
        if state["monitor_supported"] is True:
            _monitor_del(bot, [nick_to_del])
        _request_ison(bot)
        LOGGER.info("notify-del success nick=%s", nick_to_del)
    except Exception:
        _log_exception("notify-del command failed")

@plugin.commands('notify-list')
@plugin.thread(True)
def list_notify(bot, trigger):
    try:
        LOGGER.info("notify-list called by=%s", trigger.nick)
        if not is_admin(bot, trigger.nick):
            bot.say("You are not authorized to use this command.", trigger.nick)
            return
        if not WATCHLIST:
            bot.say("Watchlist is empty.", trigger.nick)
            return
        bot.say("Watchlist: " + ", ".join(sorted(WATCHLIST)), trigger.nick)
    except Exception:
        _log_exception("notify-list command failed")