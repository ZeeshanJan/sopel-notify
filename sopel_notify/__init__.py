# sopel_notify/__init__.py
from sopel import plugin
import os
import json
from .config import NotifySection
from .defaults import DEFAULTS

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "users.json")

# Load watchlist from JSON file
def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r") as f:
            data = json.load(f)
            return set(user.lower() for user in data.get("users", []))
    return set()

# Save watchlist to JSON file
def save_watchlist():
    data = {"users": list(WATCHLIST)}
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

WATCHLIST = load_watchlist()


def setup(bot):
    bot.config.define_section('notify', NotifySection, require_extra=False)


# Helper: get channel for notifications
def get_notify_channel(bot):
    return getattr(bot.config.notify, "notify_channel", DEFAULTS["notify_channel"])


def get_notify_target(bot):
    return bot.config.core.owner
# -------------------
# Event notifications
# -------------------
@plugin.event('JOIN')
@plugin.rule('.*')
def on_join(bot, trigger):
    if not getattr(bot.config.notify, "watch_online", DEFAULTS["watch_online"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has joined {trigger.sender}!", get_notify_target(bot))

@plugin.event('PART')
@plugin.rule('.*')
def on_part(bot, trigger):
    if not getattr(bot.config.notify, "watch_offline", DEFAULTS["watch_offline"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has left {trigger.sender}!", get_notify_target(bot))

@plugin.event('QUIT')
@plugin.rule('.*')
def on_quit(bot, trigger):
    if not getattr(bot.config.notify, "watch_offline", DEFAULTS["watch_offline"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has quit the network!", get_notify_target(bot))

@plugin.event('NICK')
@plugin.rule('.*')
def on_nick_change(bot, trigger):
    if not getattr(bot.config.notify, "watch_nickchange", DEFAULTS["watch_nickchange"]):
        return
    old_nick = trigger.nick.lower()
    new_nick = trigger.args[0].lower()
    if old_nick in WATCHLIST or new_nick in WATCHLIST:
        bot.say(f"{trigger.nick} changed nick to {trigger.args[0]}", get_notify_target(bot))

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
    save_watchlist()
    bot.say(f"Added {nick_to_add} to watchlist.", trigger.nick)

@plugin.commands('notify-del')
@plugin.thread(True)
def del_notify(bot, trigger):
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
    save_watchlist()
    bot.say(f"Removed {nick_to_del} from watchlist.", trigger.nick)

@plugin.commands('notify-list')
@plugin.thread(True)
def list_notify(bot, trigger):
    if not is_admin(bot, trigger.nick):
        bot.say("You are not authorized to use this command.", trigger.nick)
        return
    if not WATCHLIST:
        bot.say("Watchlist is empty.", trigger.nick)
        return
    bot.say("Watchlist: " + ", ".join(sorted(WATCHLIST)), trigger.nick)