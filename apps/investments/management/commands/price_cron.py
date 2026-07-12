"""Long-running scheduler for the compose `cron` sidecar: run `fetch_eod_prices` once a day on
weekdays. Self-contained (no external cron/scheduler) — it sleeps until the next target time, runs
the fetch, and repeats. Restart-safe: on restart it simply recomputes the next run, and the fetch is
idempotent, so a missed or repeated day is harmless.

    python manage.py price_cron --hour 23 --minute 10   # 23:10 UTC, Mon–Fri

Time is UTC (no DST surprises). Trigger a fetch out-of-band any time with `fetch_eod_prices`.
"""

import datetime
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


def next_run(now: datetime.datetime, hour: int, minute: int) -> datetime.datetime:
    """The next weekday (Mon–Fri) at hour:minute strictly after `now` (same tz as `now`)."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += datetime.timedelta(days=1)
    while candidate.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        candidate += datetime.timedelta(days=1)
    return candidate


class Command(BaseCommand):
    help = "Scheduler sidecar: run fetch_eod_prices daily on weekdays at the given UTC time."

    def add_arguments(self, parser):
        parser.add_argument("--hour", type=int, default=23, help="UTC hour (0–23). Default 23.")
        parser.add_argument("--minute", type=int, default=10, help="UTC minute (0–59). Default 10.")

    def handle(self, *args, **opts):
        hour, minute = opts["hour"], opts["minute"]
        self.stdout.write(
            self.style.SUCCESS(f"price_cron up — fetching weekdays at {hour:02d}:{minute:02d} UTC")
        )
        while True:
            now = datetime.datetime.now(datetime.UTC)
            nxt = next_run(now, hour, minute)
            self.stdout.write(f"Next fetch: {nxt.isoformat()}")
            time.sleep(max((nxt - now).total_seconds(), 1))
            try:
                call_command("fetch_eod_prices")
            except Exception as exc:  # noqa: BLE001 - never let one failure kill the scheduler
                self.stderr.write(f"fetch_eod_prices failed: {exc}")
            time.sleep(61)  # step past the target minute before recomputing the next run
