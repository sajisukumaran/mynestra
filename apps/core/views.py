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
    return render(request, "styleguide/index.html")
