# sopel_notify/__init__.py
from sopel import plugin
import os
import json
import logging
import tempfile
from .config import NotifySection
from .defaults import DEFAULTS

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "users.json")
PLUGIN_LOG_FILE = os.path.join(os.path.dirname(__file__), "notify-debug.log")


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
            bot.say(f"{trigger.nick} has joined {trigger.sender}!", get_notify_target(bot))
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
            bot.say(f"{trigger.nick} has left {trigger.sender}!", get_notify_target(bot))
    except Exception:
        _log_exception("PART handler failed")

@plugin.event('QUIT')
@plugin.rule('.*')
def on_quit(bot, trigger):
    try:
        if not _cfg_value(bot, "watch_offline"):
            return
        nick = trigger.nick.lower()
        if nick in WATCHLIST:
            bot.say(f"{trigger.nick} has quit the network!", get_notify_target(bot))
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
            bot.say(f"{trigger.nick} changed nick to {trigger.args[0]}", get_notify_target(bot))
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