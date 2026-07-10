"""Finance seed catalogs: chart of accounts + currencies are present, locked, and well-formed."""

from django_tenants.utils import schema_context

from apps.finance.coa import CHART_OF_ACCOUNTS, CURRENCIES
from apps.finance.models import Account, AccountType, Currency, Side


def test_chart_of_accounts_seeded_and_locked(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        assert Account.objects.filter(is_system=True).count() == len(CHART_OF_ACCOUNTS)
        # All five accounting elements present.
        assert set(Account.objects.values_list("type", flat=True)) == set(AccountType.values)
        # Header (rollup) accounts are non-postable.
        assert Account.objects.get(code="1000").is_postable is False
        # Normal side seeded from the account type.
        assert Account.objects.get(code="1120").normal_side == Side.DEBIT  # asset
        assert Account.objects.get(code="4100").normal_side == Side.CREDIT  # revenue
        # Stable role handles resolve to exactly one account each.
        for key in [
            "opening_balance_equity", "current_year_earnings", "net_worth",
            "fx_gain_loss", "suspense", "taxes_payable", "transfer_clearing",
            "credit_cards", "interest_expense",
        ]:
            assert Account.objects.filter(system_key=key).count() == 1


def test_currencies_seeded(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        assert Currency.objects.filter(is_system=True).count() == len(CURRENCIES)
        for code in ["USD", "EUR", "INR", "JPY"]:
            assert Currency.objects.filter(code=code).exists()
        assert Currency.objects.get(code="JPY").decimal_places == 0  # zero-decimal currency
        assert Currency.objects.get(code="USD").decimal_places == 2
