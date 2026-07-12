"""Fetch end-of-day prices for auto-tracked securities across every tenant and upsert them as
`SecurityPrice` rows.

Idempotent by design — the `(security, as_of)` unique constraint plus `update_or_create` mean a
re-run (a double-fire, a weekend, a holiday re-writing the last close) just overwrites the same row.
Each distinct ticker is fetched once per run even when several households hold it; the result is
written into every tenant that holds it. A CD / money-market fund, a security with no ticker, an
inactive one, or one with price-tracking turned off is skipped.

Scheduled once a day after market close (see the compose `cron` sidecar). Run by hand:
    python manage.py fetch_eod_prices [--provider stooq] [--symbol AAPL] [--schema demo] [--dry-run]
"""

from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context

from apps.investments import pricing


class Command(BaseCommand):
    help = "Fetch EOD prices for auto-tracked securities and store them (all tenants)."

    def add_arguments(self, parser):
        parser.add_argument("--provider", default=None,
                            help="Override the configured price provider for this run.")
        parser.add_argument("--symbol", default=None,
                            help="Fetch only this ticker (case-insensitive).")
        parser.add_argument("--schema", default=None,
                            help="Limit to a single tenant schema.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Fetch and report, but write nothing.")

    def handle(self, *args, **opts):
        from apps.tenants.models import Tenant

        provider = pricing.get_provider(opts["provider"])
        provider_name = (opts["provider"] or "").strip().lower() or self._configured()

        tenants = Tenant.objects.exclude(schema_name="public")
        if opts["schema"]:
            tenants = tenants.filter(schema_name=opts["schema"])

        # 1) Gather the tickers each tenant needs (per-tenant securities live in its own schema).
        want: dict[str, set[str]] = {}
        for tenant in tenants:
            with schema_context(tenant.schema_name):
                syms = self._trackable_symbols(opts["symbol"])
            if syms:
                want[tenant.schema_name] = syms

        symbols = sorted({s for syms in want.values() for s in syms})
        if not symbols:
            self.stdout.write("No auto-tracked securities to price.")
            return

        # 2) Fetch each distinct ticker once. One bad symbol never aborts the run.
        quotes: dict[str, pricing.PriceQuote] = {}
        errors: dict[str, str] = {}
        for sym in symbols:
            try:
                quote = provider(sym)
            except pricing.PriceError:
                raise  # misconfiguration (missing key / dep) — surface it, don't swallow
            except Exception as exc:  # noqa: BLE001 - network/parse hiccup on one symbol
                errors[sym] = str(exc) or exc.__class__.__name__
                continue
            if quote is None:
                errors[sym] = "no data returned"
            else:
                quotes[sym] = quote

        # 3) Upsert into every tenant that holds each priced ticker.
        written = 0
        for schema, syms in want.items():
            with schema_context(schema):
                written += self._write(syms, quotes, provider_name, opts["dry_run"])

        verb = "Would write" if opts["dry_run"] else "Wrote"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {written} price(s) via '{provider_name}' — "
            f"{len(quotes)}/{len(symbols)} tickers priced across {len(want)} tenant(s)."
        ))
        for sym, msg in sorted(errors.items()):
            self.stdout.write(self.style.WARNING(f"  {sym}: {msg}"))

    @staticmethod
    def _configured() -> str:
        from django.conf import settings
        return (settings.INVESTMENTS_PRICE_PROVIDER or "stooq").strip().lower()

    @staticmethod
    def _auto_tracked(symbol_filter):
        from apps.investments.models import Security, SecurityKind

        qs = (
            Security.objects.filter(is_active=True, track_price=True)
            .exclude(symbol="")
            .exclude(kind__in=[SecurityKind.CD, SecurityKind.MONEY_MARKET])
        )
        if symbol_filter:
            qs = qs.filter(symbol__iexact=symbol_filter)
        return qs

    def _trackable_symbols(self, symbol_filter) -> set[str]:
        return {s.symbol.strip().upper() for s in self._auto_tracked(symbol_filter)}

    def _write(self, syms, quotes, provider_name, dry_run) -> int:
        from apps.investments.models import SecurityPrice

        n = 0
        for sec in self._auto_tracked(None):
            key = sec.symbol.strip().upper()
            if key not in syms:
                continue
            quote = quotes.get(key)
            if quote is None:
                continue
            if not dry_run:
                SecurityPrice.objects.update_or_create(
                    security=sec, as_of=quote.as_of,
                    defaults={"price": quote.price, "source": f"auto:{provider_name}"},
                )
            n += 1
        return n
