# sopel_notify/__init__.py
from sopel import module
import json
import os
from .defaults import DEFAULTS

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "users.json")

# Load watchlist from file
def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, "r") as f:
            data = json.load(f)
            return set(user.lower() for user in data.get("users", []))
    return set()

# Save watchlist to file
def save_watchlist():
    data = {"users": list(WATCHLIST)}
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)

WATCHLIST = load_watchlist()

def get_notify_channel(bot):
    return getattr(bot.config.notify, "notify_channel", DEFAULTS["notify_channel"])

# -------------------
# Notifications
# -------------------
@module.event('JOIN')
@module.rule('.*')
def on_user_join(bot, trigger):
    if not getattr(bot.config.notify, "watch_online", DEFAULTS["watch_online"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has joined the network!", get_notify_channel(bot))

@module.event('PART')
@module.rule('.*')
def on_user_part(bot, trigger):
    if not getattr(bot.config.notify, "watch_offline", DEFAULTS["watch_offline"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has left the network!", get_notify_channel(bot))

@module.event('QUIT')
@module.rule('.*')
def on_user_quit(bot, trigger):
    if not getattr(bot.config.notify, "watch_offline", DEFAULTS["watch_offline"]):
        return
    nick = trigger.nick.lower()
    if nick in WATCHLIST:
        bot.say(f"{trigger.nick} has quit the network!", get_notify_channel(bot))

@module.event('NICK')
@module.rule('.*')
def on_nick_change(bot, trigger):
    if not getattr(bot.config.notify, "watch_nickchange", DEFAULTS["watch_nickchange"]):
        return
    old_nick = trigger.nick.lower()
    new_nick = trigger.group(1).lower()  # new nick is captured
    if old_nick in WATCHLIST or new_nick in WATCHLIST:
        bot.say(f"{trigger.nick} changed nick to {trigger.args[0]}", get_notify_channel(bot))

# -------------------
# Admin commands
# -------------------
def is_admin(bot, nick):
    """Check if nick is in bot admins"""
    return nick.lower() in (admin.lower() for admin in bot.config.core.admins)

@module.commands('notify-add')
@module.example('.notify-add BK-')
def add_notify(bot, trigger):
    if not is_admin(bot, trigger.nick):
        bot.say("You are not authorized to use this command.", trigger.nick)
        return
    if not trigger.group(2):
        bot.say("Usage: .notify-add <nick>", trigger.nick)
        return
    nick_to_add = trigger.group(2).strip().lower()
    if nick_to_add in WATCHLIST:
        bot.say(f"{nick_to_add} is already being watched.", trigger.nick)
        return
    WATCHLIST.add(nick_to_add)
    save_watchlist()
    bot.say(f"Added {nick_to_add} to watchlist.", trigger.nick)

@module.commands('notify-del')
@module.example('.notify-del BK-')
def del_notify(bot, trigger):
    if not is_admin(bot, trigger.nick):
        bot.say("You are not authorized to use this command.", trigger.nick)
        return
    if not trigger.group(2):
        bot.say("Usage: .notify-del <nick>", trigger.nick)
        return
    nick_to_del = trigger.group(2).strip().lower()
    if nick_to_del not in WATCHLIST:
        bot.say(f"{nick_to_del} is not in watchlist.", trigger.nick)
        return
    WATCHLIST.remove(nick_to_del)
    save_watchlist()
    bot.say(f"Removed {nick_to_del} from watchlist.", trigger.nick)

@module.commands('notify-list')
@module.example('.notify-list')
def list_notify(bot, trigger):
    if not is_admin(bot, trigger.nick):
        bot.say("You are not authorized to use this command.", trigger.nick)
        return
    if not WATCHLIST:
        bot.say("Watchlist is empty.", trigger.nick)
        return
    bot.say("Watchlist: " + ", ".join(sorted(WATCHLIST)), trigger.nick)