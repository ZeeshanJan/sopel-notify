# sopel_notify/config.py

from sopel.config.types import BooleanAttribute, StaticSection, ValidatedAttribute

class NotifySection(StaticSection):
    notify_channel = ValidatedAttribute('notify_channel', str)
    watch_online = BooleanAttribute('watch_online', default=True)
    watch_offline = BooleanAttribute('watch_offline', default=True)
    watch_nickchange = BooleanAttribute('watch_nickchange', default=True)
    ison_interval = ValidatedAttribute('ison_interval', int, default=120)