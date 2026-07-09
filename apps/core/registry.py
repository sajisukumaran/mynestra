"""Module registry (DESIGN §9): the launcher renders a live infolet per enabled module.

A module opts in by declaring a ``module`` dict + a ``launcher_counts()`` method on its AppConfig;
the launcher reads them and renders a tile with up to three live counts. Modules that are known but
not yet built render as muted "coming soon" tiles (``COMING_SOON``). Adding module 2+ is a matter of
declaring metadata on its AppConfig — the launcher needs no edits.
"""

from django.apps import apps as django_apps

# Known-but-not-yet-built modules → muted launcher tiles (DESIGN §7.4), in display order.
COMING_SOON = [
    {"name": "Banking", "description": "Accounts, cards & bills", "glyph": "landmark"},
    {"name": "Health", "description": "Records, visits & medications", "glyph": "activity"},
    {"name": "Documents", "description": "Files, IDs & certificates", "glyph": "file-text"},
    {"name": "Reminders", "description": "Tasks & important dates", "glyph": "calendar-days"},
    {"name": "Vehicles", "description": "Cars, service & insurance", "glyph": "car"},
    {"name": "Travel", "description": "Trips, bookings & documents", "glyph": "plane"},
]


def enabled_modules():
    """AppConfigs that declare a ``module`` dict, sorted by their ``order``."""
    configs = [c for c in django_apps.get_app_configs() if getattr(c, "module", None)]
    return sorted(configs, key=lambda c: c.module.get("order", 100))
