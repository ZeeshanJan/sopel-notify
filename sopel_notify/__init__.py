# sopel_notify/__init__.py
from sopel import module
import os
import json
from .defaults import DEFAULTS

# Store watchlist in Sopel's home directory
WATCHLIST_FILE = os.path.join(os.path.expanduser("~/.sopel"), "sopel_notify_users.json")

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

# Helper: get channel for notifications
def get_notify_channel(bot):
    return getattr(bot.config.notify, "notify_channel", DEFAULTS["notify_channel"])


def get_notify_target(bot, trigger):
    # Send to PM to owner/admin instead of a channel
    owner = bot.config.core.owner
    return owner
# -------------------
# Event notifications
# -------------------
@module.event('JOIN')
@module.rule('.*')
def on_join(bot, trigger):
    if not getattr(bot.config.notify, "watch_online", DEFAULTS["watch_online"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has joined the network!", get_notify_target(bot, trigger))

@module.event('PART')
@module.rule('.*')
def on_part(bot, trigger):
    if not getattr(bot.config.notify, "watch_offline", DEFAULTS["watch_offline"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has left the network!", get_notify_target(bot, trigger))

@module.event('QUIT')
@module.rule('.*')
def on_quit(bot, trigger):
    if not getattr(bot.config.notify, "watch_offline", DEFAULTS["watch_offline"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has quit the network!", get_notify_target(bot, trigger))

@module.event('NICK')
@module.rule('.*')
def on_nick_change(bot, trigger):
    if not getattr(bot.config.notify, "watch_nickchange", DEFAULTS["watch_nickchange"]):
        return
    old_nick = trigger.nick.lower()
    new_nick = trigger.group(1).lower() if trigger.group(1) else trigger.args[0].lower()
    if old_nick in WATCHLIST or new_nick in WATCHLIST:
        bot.say(f"{trigger.nick} changed nick to {trigger.args[0]}", get_notify_target(bot, trigger))

# -------------------
# Admin commands
# -------------------
def is_admin(bot, nick):
    return nick.lower() in (admin.lower() for admin in bot.config.core.admins)

@module.commands('notify-add')
@module.thread(True)
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

@module.commands('notify-del')
@module.thread(True)
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

@module.commands('notify-list')
@module.thread(True)
def list_notify(bot, trigger):
    if not is_admin(bot, trigger.nick):
        bot.say("You are not authorized to use this command.", trigger.nick)
        return
    if not WATCHLIST:
        bot.say("Watchlist is empty.", trigger.nick)
        return
    bot.say("Watchlist: " + ", ".join(sorted(WATCHLIST)), trigger.nick)