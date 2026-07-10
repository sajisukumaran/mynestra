"""Finance posting service — double-entry integrity, numbering, reversal, idempotency, guards."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.finance.exceptions import (
    EmptyEntry,
    InvalidLine,
    PostedEntryImmutable,
    UnbalancedEntry,
    UnknownAccount,
)
from apps.finance.models import (
    Account,
    FiscalPeriod,
    FiscalYear,
    JournalEntry,
    JournalLine,
)
from apps.finance.services import LineInput, post_entry, resolve_period, reverse_entry, void_entry

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _opening(amount="1000"):
    """Dr Cash on Hand / Cr Opening Balance Equity — the canonical opening-balance entry."""
    return post_entry(
        date=JAN,
        description="Opening balance",
        entry_type=JournalEntry.EntryType.OPENING,
        lines=[
            LineInput(account="1110", debit=D(amount)),
            LineInput(account="opening_balance_equity", credit=D(amount)),
        ],
    )


def test_balanced_entry_posts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        entry = _opening("1000")
        assert entry.status == JournalEntry.Status.POSTED
        assert entry.entry_type == JournalEntry.EntryType.OPENING
        assert entry.entry_no == 1
        assert entry.fiscal_year == 2026
        assert entry.period.period_no == 1
        assert entry.posted_at is not None
        assert entry.lines.count() == 2
        line = entry.lines.get(account__code="1110")
        assert line.debit == D("1000") and line.base_debit == D("1000.00")


def test_unbalanced_entry_raises(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(UnbalancedEntry):
            post_entry(
                date=JAN,
                lines=[
                    LineInput("1110", debit=D("100")),
                    LineInput("opening_balance_equity", credit=D("90")),
                ],
            )
        assert JournalEntry.objects.count() == 0  # nothing persisted


def test_single_line_raises(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(EmptyEntry):
            post_entry(date=JAN, lines=[LineInput("1110", debit=D("100"))])


def test_line_must_have_exactly_one_side(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(InvalidLine):  # both sides
            post_entry(
                date=JAN,
                lines=[
                    LineInput("1110", debit=D("100"), credit=D("100")),
                    LineInput("opening_balance_equity", credit=D("100")),
                ],
            )
        with pytest.raises(InvalidLine):  # neither side
            post_entry(
                date=JAN,
                lines=[LineInput("1110"), LineInput("opening_balance_equity")],
            )
        with pytest.raises(InvalidLine):  # negative
            post_entry(
                date=JAN,
                lines=[
                    LineInput("1110", debit=D("-5")),
                    LineInput("opening_balance_equity", credit=D("-5")),
                ],
            )


def test_db_check_rejects_two_sided_line(make_tenant):
    """Belt-and-suspenders: the DB CHECK rejects a crafted both-sided line even if the service is
    bypassed."""
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        entry = _opening("500")
        account = Account.objects.get(code="1110")
        with pytest.raises(IntegrityError), transaction.atomic():
            JournalLine.objects.create(
                entry=entry,
                account=account,
                currency=entry.currency,
                debit=D("5"),
                credit=D("5"),
                base_debit=D("5"),
                base_credit=D("5"),
            )


def test_entry_no_sequential_per_fiscal_year(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        e1 = _opening("100")
        e2 = post_entry(
            date=datetime.date(2026, 3, 1),
            lines=[LineInput("1110", debit=D("50")), LineInput("4100", credit=D("50"))],
        )
        assert (e1.entry_no, e2.entry_no) == (1, 2)
        assert e1.fiscal_year == e2.fiscal_year == 2026
        # A new fiscal year restarts numbering at 1.
        e3 = post_entry(
            date=datetime.date(2027, 1, 5),
            lines=[LineInput("1110", debit=D("10")), LineInput("4100", credit=D("10"))],
        )
        assert e3.entry_no == 1 and e3.fiscal_year == 2027


def test_period_resolution_covers_all_months(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        for month in range(1, 13):
            period = resolve_period(datetime.date(2026, month, 10))
            assert period.period_no == month
            assert period.name == f"{datetime.date(2026, month, 1):%b} 2026"
        assert FiscalYear.objects.filter(year=2026).count() == 1
        assert FiscalPeriod.objects.filter(fiscal_year__year=2026).count() == 12


def test_draft_entry_is_unnumbered(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        draft = post_entry(
            date=JAN,
            status=JournalEntry.Status.DRAFT,
            lines=[LineInput("1110", debit=D("10")), LineInput("4100", credit=D("10"))],
        )
        assert draft.status == JournalEntry.Status.DRAFT
        assert draft.entry_no is None and draft.posted_at is None and draft.fiscal_year is None


def test_reverse_entry_mirrors_and_marks(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        entry = _opening("300")
        reversal = reverse_entry(entry)
        entry.refresh_from_db()
        assert entry.is_reversed is True
        assert reversal.reversal_of_id == entry.pk
        assert reversal.entry_type == JournalEntry.EntryType.REVERSAL
        orig = entry.lines.get(account__code="1110")
        rev = reversal.lines.get(account__code="1110")
        assert orig.debit == rev.credit and orig.credit == rev.debit
        assert orig.base_debit == rev.base_credit
        assert JournalEntry.objects.filter(pk=entry.pk).exists()  # original retained


def test_posted_entry_cannot_be_voided_but_draft_can(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        draft = post_entry(
            date=JAN,
            status=JournalEntry.Status.DRAFT,
            lines=[LineInput("1110", debit=D("10")), LineInput("4100", credit=D("10"))],
        )
        void_entry(draft)
        assert draft.status == JournalEntry.Status.VOID
        posted = _opening("50")
        with pytest.raises(PostedEntryImmutable):
            void_entry(posted)
        with pytest.raises(PostedEntryImmutable):
            reverse_entry(draft)  # a voided (non-posted) entry cannot be reversed


def test_idempotent_external_key(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        kwargs = {
            "date": JAN,
            "external_key": "banking:txn:42",
            "lines": [LineInput("1110", debit=D("75")), LineInput("4100", credit=D("75"))],
        }
        first = post_entry(**kwargs)
        second = post_entry(**kwargs)
        assert first.pk == second.pk
        assert JournalEntry.objects.filter(external_key="banking:txn:42").count() == 1
        assert JournalLine.objects.count() == 2  # not doubled


def test_account_resolution_by_code_key_and_unknown(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        entry = post_entry(
            date=JAN,
            lines=[
                LineInput("1110", debit=D("20")),  # by code
                LineInput("opening_balance_equity", credit=D("20")),  # by system_key
            ],
        )
        assert {line.account.code for line in entry.lines.all()} == {"1110", "3100"}
        with pytest.raises(UnknownAccount):
            post_entry(
                date=JAN,
                lines=[LineInput("9999", debit=D("1")), LineInput("4100", credit=D("1"))],
            )


def test_post_time_guards_reject_header_and_inactive_accounts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(InvalidLine):  # 1000 Assets is a header (non-postable)
            post_entry(
                date=JAN,
                lines=[LineInput("1000", debit=D("5")), LineInput("4100", credit=D("5"))],
            )
        account = Account.objects.get(code="1150")
        account.is_active = False
        account.save(update_fields=["is_active"])
        with pytest.raises(InvalidLine):
            post_entry(
                date=JAN,
                lines=[LineInput("1150", debit=D("5")), LineInput("4100", credit=D("5"))],
            )


def test_line_carries_optional_counterparty(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.contacts.models import Person
        from apps.organizations.models import Organization

        person = Person.objects.create(first_name="Dr", last_name="Smith")
        entry = post_entry(
            date=JAN,
            lines=[
                LineInput("5400", debit=D("60"), person=person),  # Health expense to Dr Smith
                LineInput("1110", credit=D("60")),
            ],
        )
        assert entry.lines.get(account__code="5400").person_id == person.pk
        org = Organization.objects.create(name="Clinic")
        with pytest.raises(InvalidLine):  # at most one counterparty
            post_entry(
                date=JAN,
                lines=[
                    LineInput("5400", debit=D("10"), person=person, organization=org),
                    LineInput("1110", credit=D("10")),
                ],
            )
