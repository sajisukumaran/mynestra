"""Module registry (DESIGN §9): the launcher renders a live infolet per enabled module.

A module opts in by declaring a ``launcher_module`` dict + a ``launcher_counts()`` method on its
AppConfig; the launcher reads them and renders a tile with up to three live counts. Modules that are
known but not yet built render as muted "coming soon" tiles (``COMING_SOON``). Adding module 2+ is a
matter of declaring metadata on its AppConfig — the launcher needs no edits.

(The attribute is ``launcher_module``, not ``module``: Django's AppConfig already binds ``.module``
to the app's imported Python module.)
"""

from django.apps import apps as django_apps

# Known-but-not-yet-built modules → muted launcher tiles (DESIGN §7.4), in display order.
COMING_SOON = [
    {"name": "Documents", "description": "Files, IDs & certificates", "glyph": "file-text"},
    {"name": "Reminders", "description": "Tasks & important dates", "glyph": "calendar-days"},
    {"name": "Travel", "description": "Trips, bookings & documents", "glyph": "plane"},
]


def enabled_modules(tenant=None):
    """AppConfigs that declare a ``launcher_module`` dict, sorted by ``order``.

    A module may set ``requires_expert: True`` in its ``launcher_module`` metadata (e.g. Finance);
    such tiles are hidden unless the given tenant is in Expert accounting mode. Passing no tenant
    returns every declared module (used where mode is irrelevant)."""
    mode = getattr(tenant, "accounting_mode", "expert")  # no tenant → show everything
    configs = [
        c
        for c in django_apps.get_app_configs()
        if getattr(c, "launcher_module", None)
        and (mode == "expert" or not c.launcher_module.get("requires_expert"))
    ]
    return sorted(configs, key=lambda c: c.launcher_module.get("order", 100))
