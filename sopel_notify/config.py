# sopel_notify/config.py
from sopel.config.types import StaticSection, ValidatedAttribute

class NotifySection(StaticSection):
    watch_online = ValidatedAttribute("watch_online", bool, default=True)
    watch_offline = ValidatedAttribute("watch_offline", bool, default=True)
    watch_nickchange = ValidatedAttribute("watch_nickchange", bool, default=True)
    notify_channel = ValidatedAttribute("notify_channel", str, default="#notify")

def configure(config):
    section = config.define_section("notify", NotifySection)
    section.watch_online = True
    section.watch_offline = True
    section.watch_nickchange = True
    section.notify_channel = "#notify"