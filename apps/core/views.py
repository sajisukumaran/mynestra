"""Core views: the health page and the dev-only /styleguide (UI-gate review surface)."""

from django.conf import settings
from django.db import connection
from django.http import Http404
from django.shortcuts import render


def health(request):
    """Report app status and DB reachability (SELECT 1). Returns 503 if the DB is unreachable."""
    db_ok = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            db_ok = cursor.fetchone() == (1,)
    except Exception:
        db_ok = False

    context = {
        "app_name": "MyNestra",
        "environment": settings.ENVIRONMENT,
        "schema": getattr(connection, "schema_name", "public"),
        "db_ok": db_ok,
    }
    return render(request, "health.html", context, status=200 if db_ok else 503)


def styleguide(request):
    """Dev-only component gallery — the UI-gate review surface (DESIGN §7.5). 404 in prod."""
    if not settings.DEBUG:
        raise Http404()

    # Static demo data for the data-viz components (c-donut / c-bar-list). Uses the real
    # donut_segments helper so the gallery exercises the same arc math the dashboard does.
    from decimal import Decimal

    from apps.investments.services import Slice, donut_segments

    demo_slices = [
        Slice("Equity", Decimal("62000"), "teal"),
        Slice("Fixed income", Decimal("18000"), "blue"),
        Slice("Cash", Decimal("9000"), "slate"),
        Slice("Real assets", Decimal("11000"), "amber"),
    ]
    demo_total = sum((s.value for s in demo_slices), Decimal("0"))
    bar_items = [
        {"label": "Fidelity", "value": Decimal("54000"), "tint": "teal"},
        {"label": "Vanguard", "value": Decimal("31000"), "tint": "violet"},
        {"label": "Schwab", "value": Decimal("15000"), "tint": "emerald"},
    ]
    context = {
        "donut_segments": donut_segments(demo_slices),
        "donut_total": demo_total,
        "bar_items": bar_items,
        "bar_total": sum((b["value"] for b in bar_items), Decimal("0")),
    }
    return render(request, "styleguide/index.html", context)
