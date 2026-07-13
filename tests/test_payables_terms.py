"""Payables PaymentTerm catalog: seeded terms + due-date computation across kinds + early-pay
discount deadlines."""

import datetime

from django_tenants.utils import schema_context

from apps.payables.models import PaymentTerm
from apps.payables.seed import PAYMENT_TERMS

BILL = datetime.date(2026, 3, 10)


def test_system_terms_seeded(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        assert PaymentTerm.objects.filter(is_system=True).count() == len(PAYMENT_TERMS)
        assert PaymentTerm.objects.filter(name="Net 30", is_system=True).exists()


def test_due_dates_across_kinds(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        assert PaymentTerm.objects.get(name="Due on receipt").due_date_for(BILL) == BILL
        net30 = PaymentTerm.objects.get(name="Net 30")
        assert net30.due_date_for(BILL) == datetime.date(2026, 4, 9)

        # Day-of-month: next occurrence (this month if not yet passed, else next month).
        first = PaymentTerm.objects.create(
            name="1st of month", kind=PaymentTerm.Kind.DAY_OF_MONTH, day_of_month=1
        )
        assert first.due_date_for(BILL) == datetime.date(2026, 4, 1)
        mid = PaymentTerm.objects.create(
            name="15th", kind=PaymentTerm.Kind.DAY_OF_MONTH, day_of_month=15
        )
        assert mid.due_date_for(BILL) == datetime.date(2026, 3, 15)

        # Specific date: the caller supplies the due date (None from the term).
        spec = PaymentTerm.objects.create(name="On a date", kind=PaymentTerm.Kind.SPECIFIC_DATE)
        assert spec.due_date_for(BILL) is None


def test_early_payment_discount(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        term = PaymentTerm.objects.get(name="2/10 Net 30")
        assert term.has_discount
        assert term.due_date_for(BILL) == datetime.date(2026, 4, 9)
        assert term.discount_deadline(BILL) == datetime.date(2026, 3, 20)

        net30 = PaymentTerm.objects.get(name="Net 30")
        assert not net30.has_discount
        assert net30.discount_deadline(BILL) is None
