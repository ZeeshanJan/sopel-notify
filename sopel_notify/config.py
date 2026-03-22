# sopel_notify/config.py

from sopel.config.types import StaticSection, ValidatedAttribute

class NotifySection(StaticSection):
    notify_channel = ValidatedAttribute('notify_channel', str)
    watch_online = ValidatedAttribute('watch_online', bool, default=True)
    watch_offline = ValidatedAttribute('watch_offline', bool, default=True)
    watch_nickchange = ValidatedAttribute('watch_nickchange', bool, default=True)
    ison_interval = ValidatedAttribute('ison_interval', int, default=120)