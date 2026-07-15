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
            "credit_cards", "interest_expense", "certificates_of_deposit",
            "substitute_dividend_expense",
            # Payables (module 6).
            "accounts_payable", "household_goods", "purchase_discounts",
            "shipping_expense", "sales_tax_paid",
            # Loans & Liabilities (module 7).
            "loans", "loans_mortgage", "loans_auto", "loans_personal", "loans_student",
            "loans_heloc", "loans_line_of_credit", "other_liabilities", "contingent_liabilities",
            "property_tax", "home_insurance",
            # Automobile (module 8).
            "vehicles", "refundable_deposits", "asset_disposal_gain_loss",
            "vehicle_insurance", "vehicle_registration", "vehicle_lease",
            # Insurance (Plan B): per-type premium expense children under the 5500 header.
            "health_insurance", "life_insurance", "umbrella_insurance",
            "renters_insurance", "other_insurance",
        ]:
            assert Account.objects.filter(system_key=key).count() == 1
        # 2300 is the AP control account (renamed from "Bills Payable").
        ap = Account.objects.get(system_key="accounts_payable")
        assert ap.code == "2300" and ap.name == "Accounts Payable"
        assert ap.type == AccountType.LIABILITY and ap.is_postable is True
        # Loans: the per-type leaves are now group headers (the module nests per-loan sub-accounts).
        for code in ["2210", "2220", "2230"]:
            acct = Account.objects.get(code=code)
            assert acct.is_postable is False and acct.type == AccountType.LIABILITY
            assert acct.parent.code == "2200"
        # Other + contingent liability headers, parented directly under Liabilities.
        for code in ["2900", "2950"]:
            acct = Account.objects.get(code=code)
            assert acct.is_postable is False and acct.parent.code == "2000"
        # Escrow expense accounts live under Housing.
        for code in ["5140", "5150"]:
            acct = Account.objects.get(code=code)
            assert acct.type == AccountType.EXPENSE and acct.parent.code == "5100"
        # Insurance: 5500 is now a group header with per-type children (Plan B).
        insurance = Account.objects.get(code="5500")
        assert insurance.is_postable is False and insurance.type == AccountType.EXPENSE
        for code in ["5510", "5520", "5530", "5540", "5590"]:
            acct = Account.objects.get(code=code)
            assert acct.type == AccountType.EXPENSE and acct.parent.code == "5500"
        # Automobile: 1420 Vehicles is now a group header (per-vehicle sub-accounts nest under it).
        vehicles = Account.objects.get(code="1420")
        assert vehicles.is_postable is False and vehicles.type == AccountType.ASSET
        assert vehicles.parent.code == "1400" and vehicles.system_key == "vehicles"
        # Refundable deposits parent directly under Assets; disposal gain/loss under Revenue.
        assert Account.objects.get(code="1320").parent.code == "1000"
        gain_loss = Account.objects.get(code="4930")
        assert gain_loss.type == AccountType.REVENUE and gain_loss.parent.code == "4000"
        # Vehicle running-cost expense homes live under Transportation.
        for code in ["5340", "5350", "5360"]:
            acct = Account.objects.get(code=code)
            assert acct.type == AccountType.EXPENSE and acct.parent.code == "5300"


def test_currencies_seeded(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        assert Currency.objects.filter(is_system=True).count() == len(CURRENCIES)
        for code in ["USD", "EUR", "INR", "JPY"]:
            assert Currency.objects.filter(code=code).exists()
        assert Currency.objects.get(code="JPY").decimal_places == 0  # zero-decimal currency
        assert Currency.objects.get(code="USD").decimal_places == 2
