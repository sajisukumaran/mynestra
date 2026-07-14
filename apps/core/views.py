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

    # Static demo data for the data-viz components (c-donut / c-bar-list / c-line-chart). Uses the
    # real donut_segments / line_chart_points helpers so the gallery exercises the same math the
    # dashboard does.
    import datetime
    from decimal import Decimal

    from apps.investments.services import Slice, donut_segments, line_chart_points
    from apps.loans.services import loan_chart_points

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
    # (date, invested, market) samples — the same shape services.value_over_time returns.
    line_series = [
        (datetime.date(2026, 1, 1), Decimal("40000"), Decimal("40000")),
        (datetime.date(2026, 2, 1), Decimal("42000"), Decimal("43500")),
        (datetime.date(2026, 3, 1), Decimal("42000"), Decimal("41800")),
        (datetime.date(2026, 4, 1), Decimal("47000"), Decimal("49200")),
        (datetime.date(2026, 5, 1), Decimal("47000"), Decimal("52100")),
        (datetime.date(2026, 6, 1), Decimal("50000"), Decimal("56400")),
    ]
    line_vals = [v for _, inv, mkt in line_series for v in (inv, mkt)]
    # Loan paydown: balance to date (solid) + projected payoff (dashed), through the real helper.
    loan_actual = [
        (datetime.date(2024, 1, 1), Decimal("30000")),
        (datetime.date(2024, 7, 1), Decimal("26500")),
        (datetime.date(2025, 1, 1), Decimal("22800")),
        (datetime.date(2025, 7, 1), Decimal("18900")),
        (datetime.date(2026, 1, 1), Decimal("14800")),
    ]
    loan_projected = [
        (datetime.date(2026, 1, 1), Decimal("14800")),
        (datetime.date(2026, 7, 1), Decimal("10500")),
        (datetime.date(2027, 1, 1), Decimal("6000")),
        (datetime.date(2027, 7, 1), Decimal("1300")),
        (datetime.date(2027, 9, 1), Decimal("0")),
    ]
    loan_vals = [v for _, v in loan_actual + loan_projected]
    context = {
        "donut_segments": donut_segments(demo_slices),
        "donut_total": demo_total,
        "bar_items": bar_items,
        "bar_total": sum((b["value"] for b in bar_items), Decimal("0")),
        "line_geo": line_chart_points(
            line_series, min_v=min(line_vals), max_v=max(line_vals),
            start=line_series[0][0], end=line_series[-1][0],
        ),
        "line_market": line_series[-1][2],
        "line_invested": line_series[-1][1],
        "line_gain": line_series[-1][2] - line_series[-1][1],
        "loan_geo": loan_chart_points(
            loan_actual, loan_projected, min_v=min(loan_vals), max_v=max(loan_vals),
            start=loan_actual[0][0], end=loan_projected[-1][0],
        ),
        "loan_balance": loan_actual[-1][1],
        "loan_payoff": loan_projected[-1][0],
        "loan_interest": Decimal("2400"),
    }
    return render(request, "styleguide/index.html", context)
