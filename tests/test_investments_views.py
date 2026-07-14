"""Investments screens: dashboard, accounts list/detail, account + transaction capture, securities,
the member gate, and the Expert-only Accounting tab. Drives the authenticated tenant client."""

from django_tenants.utils import schema_context

from apps.investments.models import InvestmentAccount, InvestmentTransaction, Lot, Security
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Portfolios", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _brokerage(name="Fidelity"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    return org


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def test_dashboard_and_list_render_empty(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(_url(tenant)).status_code == 200
    body = client.get(_url(tenant, "accounts/")).content.decode()
    assert "No accounts yet" in body


def test_non_member_is_denied(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    outsider = make_user("outsider@example.com")
    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


def test_account_create_form_renders_with_brokerage(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _brokerage()
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/new/")).content.decode()
    assert "New account" in body and "Fidelity" in body


def test_create_account_inline_institution_and_opening_cash(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "accounts/new/"),
        {
            "new_institution_name": "Vanguard",
            "new_institution_city": "Valley Forge",
            "registration": "roth_ira",
            "nickname": "My Roth",
            "currency": "USD",
            "is_active": "on",
            "opening_balance": "5000",
            "opening_date": "2026-01-02",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        acct = InvestmentAccount.objects.get(nickname="My Roth")
        assert acct.registration == "roth_ira"
        assert acct.gl_account.parent.code == "1220"           # retirement group header
        assert acct.institution.categories.filter(name="Brokerage").exists()
        assert acct.cash_balance == __import__("decimal").Decimal("5000")


def test_add_buy_then_sell_via_views(make_tenant, make_user, client):
    from decimal import Decimal

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="ACME", name="Acme",
                                      currency=Currency.objects.get(code="USD"))
        aid, sid = acct.pk, sec.pk
    client.force_login(owner)

    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "opening", "date": "2026-01-02", "amount": "10000"})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05", "security": sid,
        "quantity": "10", "price": "50", "amount": "500", "fee": "0"})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "sell", "date": "2026-03-01", "security": sid,
        "quantity": "4", "price": "70", "amount": "280", "fee": "0"})

    with schema_context(tenant.schema_name):
        assert Lot.objects.filter(account_id=aid, open=True).count() == 1
        sell = InvestmentTransaction.objects.get(account_id=aid, txn_type="sell")
        assert sell.realized_gain == Decimal("80")

    # Detail page shows the holding + drill-down works.
    body = client.get(_url(tenant, f"accounts/{aid}/")).content.decode()
    assert "ACME" in body and "Holdings" in body
    assert client.get(_url(tenant, f"accounts/{aid}/holdings/{sid}/")).status_code == 200


def test_duplicate_named_trade_inputs_are_disable_guarded(make_tenant, make_user, client):
    """Regression: the transaction form includes every per-type block, so some input names appear
    twice in one <form> (Price/unit + option Premium share both name="price"; split + merger both
    name="split_ratio_new/old"; option open/close + exercise both name="contracts"). A browser
    submits ALL of them, and Django's POST.get() takes the LAST — so a hidden, empty duplicate
    silently clobbered the typed value (the 'price blank on reopen' bug). Each such input must carry
    a :disabled guard so only the active txn-type's input is submitted."""
    import re

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="VZ", name="Verizon",
                                      currency=Currency.objects.get(code="USD"))
        aid, sid = acct.pk, sec.pk
    client.force_login(owner)
    # A priced buy — the exact shape that lost its price before the fix.
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2006-12-27", "security": sid,
        "quantity": "21", "price": "37.4457", "amount": "786.36", "fee": "9.99"})

    body = client.get(_url(tenant, f"accounts/{aid}/")).content.decode()
    for field in ("price", "contracts", "split_ratio_new", "split_ratio_old"):
        tags = re.findall(rf'<input name="{field}"[^>]*>', body)
        assert len(tags) >= 2, f"{field}: expected duplicate inputs across type blocks"
        assert all(":disabled=" in t for t in tags), f"{field}: an input lacks a :disabled guard"


def test_invalid_transaction_save_is_rejected_and_messaged(make_tenant, make_user, client):
    """Fix for silent failure: a rejected save no longer vanishes — nothing is written and the
    detail page shows an error toast. Also guards negative money/quantity values."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="VZ", name="Verizon",
                                      currency=Currency.objects.get(code="USD"))
        aid, sid = acct.pk, sec.pk
    client.force_login(owner)

    # (a) Negative commission is rejected outright.
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05", "security": sid,
        "quantity": "10", "price": "50", "amount": "500", "fee": "-1"}, follow=True)
    assert "Couldn&#x27;t save" in resp.content.decode() or "Couldn't save" in resp.content.decode()
    with schema_context(tenant.schema_name):
        assert not InvestmentTransaction.objects.filter(account_id=aid).exists()

    # (b) A buy missing its security is rejected too (not silently dropped).
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05",
        "quantity": "10", "price": "50", "amount": "500"}, follow=True)
    assert "save" in resp.content.decode().lower()
    with schema_context(tenant.schema_name):
        assert not InvestmentTransaction.objects.filter(account_id=aid).exists()

    # (c) A valid buy still saves cleanly.
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05", "security": sid,
        "quantity": "10", "price": "50", "amount": "500", "fee": "0"})
    with schema_context(tenant.schema_name):
        assert InvestmentTransaction.objects.filter(account_id=aid, txn_type="buy").count() == 1


def test_dividend_dates_are_captured_and_metadata_only(make_tenant, make_user, client):
    """A dividend records its declaration / ex-dividend / record dates (the main date stays the
    payment date). Pure metadata — no GL or cash effect — and only for dividend types."""
    from decimal import Decimal

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="T", name="AT&T",
                                      currency=Currency.objects.get(code="USD"))
        aid, sid = acct.pk, sec.pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "opening", "date": "2007-01-01", "amount": "1000"})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "dividend", "date": "2007-02-01", "security": sid, "amount": "7.81",
        "declaration_date": "2007-01-10", "ex_dividend_date": "2007-01-20",
        "record_date": "2007-01-22"})

    import datetime
    with schema_context(tenant.schema_name):
        from apps.investments.services import cash_balance
        div = InvestmentTransaction.objects.get(account_id=aid, txn_type="dividend")
        assert div.date == datetime.date(2007, 2, 1)               # payment date unchanged
        assert div.declaration_date == datetime.date(2007, 1, 10)
        assert div.ex_dividend_date == datetime.date(2007, 1, 20)
        assert div.record_date == datetime.date(2007, 1, 22)
        assert cash_balance(acct) == Decimal("1007.81")            # dates don't touch the money

    # A non-dividend type never picks up dividend dates, even if the params are posted.
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "interest", "date": "2007-03-01", "amount": "2.00",
        "ex_dividend_date": "2007-02-25"})
    with schema_context(tenant.schema_name):
        interest = InvestmentTransaction.objects.get(account_id=aid, txn_type="interest")
        assert interest.ex_dividend_date is None


def test_transaction_form_blocks_invalid_submit_client_side(make_tenant, make_user, client):
    """The add/edit forms carry an Alpine guard — a formError getter, x-model on quantity/security,
    and a Save @click that prevents submitting an invalid form — so a mistake keeps the modal open
    with the entered values intact instead of bouncing to a lost-data redirect."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        aid = acct.pk
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{aid}/")).content.decode()
    assert "get formError()" in body
    assert 'x-model="quantity"' in body and 'x-model="security"' in body
    assert "attempted = true" in body   # Save click prevents an invalid submit
    assert "{#" not in body             # no leaked template comment


def test_security_picker_scopes_to_account_transacted_securities(make_tenant, make_user, client):
    """The register's Security picker for income/holding ops lists only securities THIS account has
    transacted (account_securities); acquisitions still offer the full master (securities)."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        usd = Currency.objects.get(code="USD")
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=usd)
        ensure_gl_account(acct)
        held = Security.objects.create(symbol="AAA", name="Held Co", currency=usd)
        Security.objects.create(symbol="BBB", name="Never Held Co", currency=usd)  # not held here
        aid, held_id = acct.pk, held.pk
    client.force_login(owner)
    # Transact AAA in this account so it becomes one of the account's securities.
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05", "security": held_id,
        "quantity": "10", "price": "50", "amount": "500", "fee": "0"})

    resp = client.get(_url(tenant, f"accounts/{aid}/"))
    account_secs = set(resp.context["account_securities"].values_list("symbol", flat=True))
    all_secs = set(resp.context["securities"].values_list("symbol", flat=True))
    assert "AAA" in account_secs and "BBB" not in account_secs   # scoped to this account
    assert {"AAA", "BBB"} <= all_secs                            # full master still has both
    # Two type-scoped security pickers render (account-list + full-list), guarded so one submits.
    assert resp.content.decode().count('name="security"') >= 2


def test_net_amount_and_reactive_bindings_render(make_tenant, make_user, client):
    """The net-amount readout is computed client-side from amount + commission, so amount/fee must
    be x-model bound and the net expression present (added on a buy, deducted on a sell)."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="VZ", name="Verizon",
                                      currency=Currency.objects.get(code="USD"))
        aid, sid = acct.pk, sec.pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05", "security": sid,
        "quantity": "10", "price": "50", "amount": "500", "fee": "9.99"})

    body = client.get(_url(tenant, f"accounts/{aid}/")).content.decode()
    assert 'x-model="amount"' in body and 'x-model="fee"' in body
    assert "Net amount" in body
    assert "['buy','buy_to_cover']" in body  # net adds commission for buys, subtracts for sells
    # No Django template comment leaked into the page: a multi-line {# #} renders its literal
    # delimiters as text (only {% comment %} spans lines), so the raw marker must never appear.
    assert "{#" not in body


def test_register_is_paginated_and_sortable(make_tenant, make_user, client):
    """The register paginates (50/page, newest first by default) and sorts by column; the active tab
    rides in ?tab= so a sort/page reload lands back on the Register tab. Balance stays chronological
    regardless of the display sort."""
    import datetime
    from decimal import Decimal

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account, register_page
        usd = Currency.objects.get(code="USD")
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=usd)
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="ZZ", name="Zed", currency=usd)
        base = datetime.date(2020, 1, 1)
        InvestmentTransaction.objects.bulk_create([
            InvestmentTransaction(
                account=acct, txn_type="buy", date=base + datetime.timedelta(days=i),
                security=sec, quantity=Decimal("1"), price=Decimal("10"),
                amount=Decimal("10") + i)
            for i in range(120)
        ])
        aid = acct.pk

        pg = register_page(acct, sort="date", direction="desc", page=1)
        assert len(pg["rows"]) == 50                       # one page
        assert pg["total"] == 120
        assert pg["page_obj"].paginator.num_pages == 3
        assert pg["rows"][0]["txn"].date > pg["rows"][-1]["txn"].date  # newest first
        # Page 2 continues where page 1 left off (no overlap, still date-desc).
        pg2 = register_page(acct, sort="date", direction="desc", page=2)
        assert pg["rows"][-1]["txn"].date > pg2["rows"][0]["txn"].date
        # Sort by cash ascending → most-negative (largest) buy first.
        pc = register_page(acct, sort="cash", direction="asc", page=1)
        assert pc["rows"][0]["txn"].signed_cash <= pc["rows"][1]["txn"].signed_cash

    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{aid}/?tab=register")).content.decode()
    assert "sortable" in body        # sortable column headers rendered
    assert "pager" in body           # pagination control rendered
    assert "tab=register" in body    # sort/page links carry the active tab
    assert ">120<" in body           # the Register tab badge shows the TOTAL, not the page size


def test_transaction_form_validates_exotic_types_client_side(make_tenant, make_user, client):
    """The client-side guard covers the corporate-action / exotic types too — spin-off, merger,
    split, in-kind — not just plain trades. Their fields are reactive (x-model) and both modals
    delegate to one shared window.txnFormError, so e.g. a spin-off missing its target or basis %
    keeps the modal open with an inline error instead of submitting and bouncing to a page toast."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        aid = acct.pk
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{aid}/")).content.decode()
    # One shared validator drives the add + edit modals so they can never drift.
    assert "window.txnFormError" in body
    assert "return window.txnFormError(this)" in body
    # Spin-off / merger / split fields must be reactive for the validator to see them.
    assert 'x-model="ratioNew"' in body and 'x-model="ratioOld"' in body
    assert 'x-model="basisPct"' in body and 'x-model="targetSec"' in body
    assert 'x-model="newSym"' in body and 'x-model="newName"' in body
    # In-kind incoming lots surface a validity count up to the parent scope.
    assert 'x-effect="inKindLots' in body
    # The validator enforces the spin-off basis range and the merger/spin-off target.
    assert "between 0 and 100" in body and "security received" in body
    assert "{#" not in body


def test_security_create_and_price(make_tenant, make_user, client):
    from decimal import Decimal

    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_url(tenant, "securities/new/"), {
        "symbol": "VTI", "name": "Vanguard Total Stock", "kind": "etf",
        "asset_class": "equity", "currency": "USD", "is_active": "on"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        sec = Security.objects.get(symbol="VTI")
    client.post(_url(tenant, f"securities/{sec.pk}/price/"), {
        "price": "255.50", "as_of": "2026-06-01", "source": "Broker"})
    with schema_context(tenant.schema_name):
        sec.refresh_from_db()
        assert sec.latest_price == Decimal("255.50")
    body = client.get(_url(tenant, "securities/")).content.decode()
    assert "VTI" in body


def test_expert_accounting_tab_appears_in_expert_mode(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    tenant.accounting_mode = "expert"
    tenant.save(update_fields=["accounting_mode"])
    with schema_context(tenant.schema_name):
        _brokerage()
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/new/")).content.decode()
    assert "Accounting" in body and "ledger node" in body


def test_signed_cash_sql_matches_property_for_every_type(make_tenant):
    """`signed_cash_sql` is the SQL twin of the `signed_cash` property — the pair must agree for
    EVERY transaction type, including the option-right-dependent exercise/assignment cases and
    security-less rows. This is the lockstep guard: edit one, you must edit the other."""
    import datetime
    from decimal import Decimal

    from apps.finance.models import Currency
    from apps.investments.models import InvTxnType, OptionRight, SecurityKind, signed_cash_sql

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        usd = Currency.objects.get(code="USD")
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Twin", registration="taxable_individual",
            currency=usd)
        stock = Security.objects.create(symbol="TWIN", name="Twin", currency=usd)
        put = Security.objects.create(
            symbol="TWINP", name="Twin put", currency=usd, kind=SecurityKind.OPTION,
            option_right=OptionRight.PUT, underlying=stock)
        call = Security.objects.create(
            symbol="TWINC", name="Twin call", currency=usd, kind=SecurityKind.OPTION,
            option_right=OptionRight.CALL, underlying=stock)

        day = datetime.date(2026, 1, 2)
        common = {"account": acct, "date": day, "quantity": Decimal("1"), "price": Decimal("1"),
                  "amount": Decimal("100"), "fee": Decimal("7")}
        rows = [InvestmentTransaction(txn_type=value, security=stock, **common)
                for value, _label in InvTxnType.choices]
        rows += [InvestmentTransaction(txn_type=t, security=s, **common)
                 for t in (InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN)
                 for s in (put, call, None)]
        rows.append(InvestmentTransaction(txn_type=InvTxnType.OPENING, security=None, **common))
        InvestmentTransaction.objects.bulk_create(rows)

        annotated = {t.pk: t.sc for t in acct.transactions.annotate(sc=signed_cash_sql())}
        for t in acct.transactions.select_related("security"):
            assert annotated[t.pk] == t.signed_cash, (t.txn_type, t.security_id)


def test_cash_balance_and_register_page_queries_flat_with_register_size(make_tenant):
    """`cash_balance` is ONE database SUM and `register_page` materializes only the requested page
    — neither's query count grows with the size of the register."""
    import datetime
    from decimal import Decimal

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.finance.models import Currency
    from apps.investments.models import InvTxnType
    from apps.investments.services import cash_balance, ensure_gl_account, register_page

    tenant = make_tenant()

    def _counts(n, sym):
        usd = Currency.objects.get(code="USD")
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(name=f"B{sym}"), nickname="T",
            registration="taxable_individual", currency=usd)
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol=sym, name="X", currency=usd)
        base = datetime.date(2020, 1, 1)
        InvestmentTransaction.objects.bulk_create([
            InvestmentTransaction(
                account=acct, txn_type=InvTxnType.BUY, date=base + datetime.timedelta(days=i),
                security=sec, quantity=Decimal("1"), price=Decimal("10"), amount=Decimal("10"))
            for i in range(n)
        ])
        with CaptureQueriesContext(connection) as cash_ctx:
            cash_balance(acct)
        with CaptureQueriesContext(connection) as reg_ctx:
            pg = register_page(acct, sort="date", direction="desc", page=1)
        assert len(pg["rows"]) == min(n, 50)
        return len(cash_ctx.captured_queries), len(reg_ctx.captured_queries)

    with schema_context(tenant.schema_name):
        cash_small, reg_small = _counts(10, "QFA")
        cash_large, reg_large = _counts(200, "QFB")
    assert cash_small == cash_large == 1
    assert reg_large <= reg_small
