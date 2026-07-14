"""Tax-lot engine: buys create lots, sells consume them (FIFO + specific), splits scale, return of
capital reduces basis, and edits/deletes replay correctly. Realized gains are lot-accurate."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments.exceptions import InsufficientShares
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    LotConsumption,
    Security,
)
from apps.investments.services import apply_transaction, cost_basis, ensure_gl_account, holdings
from apps.organizations.models import Organization

D = Decimal
JAN = datetime.date(2026, 1, 2)


def _setup():
    org = Organization.objects.create(name="Broker")
    acct = InvestmentAccount.objects.create(
        institution=org, nickname="Taxable", registration="taxable_individual",
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    sec = Security.objects.create(
        symbol="ACME", name="Acme", currency=Currency.objects.get(code="USD"))
    return acct, sec


def _add(acct, ttype, date, *, security=None, qty="0", price="0", amount="0", fee="0", **extra):
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date, security=security,
        quantity=D(qty), price=D(price), amount=D(amount), fee=D(fee), **extra,
    )
    apply_transaction(txn, is_new=True)
    txn.refresh_from_db()
    return txn


def test_buy_creates_lot_with_capitalized_commission(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500", fee="10")
        lot = Lot.objects.get(account=acct, security=sec)
        assert lot.remaining_quantity == D("10")
        assert lot.cost_basis == D("510")  # commission capitalized into basis
        assert cost_basis(acct) == D("510")


def test_fifo_partial_sell_realizes_gain_and_reduces_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        sell = _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
                    security=sec, qty="4", price="70", amount="280")
        assert sell.realized_gain == D("80")  # proceeds 280 − cost 200
        lot = Lot.objects.get(account=acct, security=sec)
        assert lot.remaining_quantity == D("6")
        assert lot.cost_basis == D("300")


def test_rebuild_query_count_does_not_grow_with_register_size(make_tenant):
    """The rebuild replays in memory and bulk-writes its lots + consumptions, so its query count is
    flat regardless of how many transactions the account holds — this is what keeps adding a
    transaction fast on a large register (regression guard for the RebuildStore bulk path; the old
    per-lot store fired ~2 queries per transaction)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.investments.services import rebuild_account_lots

    def _rebuild_queries(n, sym):
        org = Organization.objects.create(name=f"B{sym}")
        acct = InvestmentAccount.objects.create(
            institution=org, nickname="T", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(
            symbol=sym, name="X", currency=Currency.objects.get(code="USD"))
        base = datetime.date(2020, 1, 1)
        InvestmentTransaction.objects.bulk_create([
            InvestmentTransaction(
                account=acct, txn_type=InvTxnType.BUY, date=base + datetime.timedelta(days=i),
                security=sec, quantity=D("1"), price=D("10"), amount=D("10"))
            for i in range(n)
        ])
        with CaptureQueriesContext(connection) as ctx:
            rebuild_account_lots(acct)
        return len(ctx.captured_queries)

    with schema_context(make_tenant().schema_name):
        small = _rebuild_queries(10, "PERFA")
        large = _rebuild_queries(120, "PERFB")
    # 12x the transactions must not mean materially more queries.
    assert large <= small + 3, f"rebuild query count grew with register size: {small} -> {large}"


def _engine_state(acct):
    """Everything the lot engine owns, keyed without DB pks (a rebuild renumbers rows)."""
    lots = sorted(
        (lot.security_id, lot.acquired_date, lot.original_quantity, lot.remaining_quantity,
         lot.original_cost, lot.cost_basis, lot.open, lot.source_txn_id)
        for lot in Lot.objects.filter(account=acct)
    )
    cons = sorted(
        (c.sale_txn_id, c.quantity, c.cost, c.proceeds)
        for c in LotConsumption.objects.filter(sale_txn__account=acct)
    )
    gains = {t.id: t.realized_gain for t in acct.transactions.all()}
    return lots, cons, gains


def test_append_fast_path_matches_full_rebuild(make_tenant):
    """The append fast path must leave lots / consumptions / realized gains EXACTLY as a full
    replay would — a rebuild right after a series of appended posts is a strict no-op."""
    from apps.investments.services import rebuild_account_lots

    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN,
             security=sec, qty="10", price="10", amount="100", fee="5")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 2),
             security=sec, qty="10", price="20", amount="200")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, datetime.date(2026, 3, 2),
             security=sec, qty="2", amount="50")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 4, 2),
             security=sec, qty="12", price="30", amount="360")
        _add(acct, InvTxnType.SPLIT, datetime.date(2026, 5, 2),
             security=sec, split_ratio_new=D("2"), split_ratio_old=D("1"))
        _add(acct, InvTxnType.RETURN_OF_CAPITAL, datetime.date(2026, 6, 2),
             security=sec, amount="30")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 7, 2),
             security=sec, qty="5", price="18", amount="90")

        before = _engine_state(acct)
        result = rebuild_account_lots(acct)
        assert result.resell_ids == [] and result.resync_out_ids == []
        assert _engine_state(acct) == before


def test_append_skips_full_rebuild_backdated_takes_it(make_tenant, monkeypatch):
    """An end-of-register post must bypass the wipe-and-replay; a backdated one must replay.
    A date TIE with the register's last day still counts as an append (the new id sorts last)."""
    import apps.investments.services as services

    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, qty="10", price="10", amount="100")

        calls = []
        real = services.rebuild_account_lots

        def spy(account):
            calls.append(account.pk)
            return real(account)

        monkeypatch.setattr(services, "rebuild_account_lots", spy)
        _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
             security=sec, qty="4", price="20", amount="80")
        assert calls == []  # later date → append fast path
        _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
             security=sec, qty="1", price="20", amount="20")
        assert calls == []  # same-date tie → still an append
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="1", price="10", amount="10")
        assert calls == [acct.pk]  # backdated → full replay


def test_append_post_query_count_flat_with_register_size(make_tenant):
    """Posting a NEW end-of-register transaction fires a flat number of queries no matter how many
    transactions the account already holds — the append fast path never replays the register."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.investments.services import rebuild_account_lots

    def _append_queries(n, sym):
        org = Organization.objects.create(name=f"A{sym}")
        acct = InvestmentAccount.objects.create(
            institution=org, nickname="T", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(
            symbol=sym, name="X", currency=Currency.objects.get(code="USD"))
        base = datetime.date(2020, 1, 1)
        InvestmentTransaction.objects.bulk_create([
            InvestmentTransaction(
                account=acct, txn_type=InvTxnType.BUY, date=base + datetime.timedelta(days=i),
                security=sec, quantity=D("1"), price=D("10"), amount=D("10"))
            for i in range(n)
        ])
        rebuild_account_lots(acct)  # materialize the lots the appended sell draws from
        txn = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.SELL, date=datetime.date(2026, 1, 1),
            security=sec, quantity=D("1"), price=D("20"), amount=D("20"))
        with CaptureQueriesContext(connection) as ctx:
            apply_transaction(txn, is_new=True)
        return len(ctx.captured_queries)

    with schema_context(make_tenant().schema_name):
        small = _append_queries(10, "APPA")
        large = _append_queries(120, "APPB")
    assert large <= small, f"append post query count grew with register size: {small} -> {large}"


def test_fifo_sell_spans_multiple_lots(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="10", amount="100")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, qty="10", price="20", amount="200")
        sell = _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
                    security=sec, qty="15", price="30", amount="450")
        # FIFO: 10 @10 (cost 100) + 5 @20 (cost 100) = 200 cost; proceeds 450 → gain 250.
        assert sell.realized_gain == D("250")
        assert cost_basis(acct) == D("100")  # 5 @20 remaining


def test_full_sale_closes_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="5", price="10", amount="50")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 2, 1),
             security=sec, qty="5", price="12", amount="60")
        assert not Lot.objects.filter(account=acct, open=True).exists()
        assert holdings(acct) == []


def test_oversell_raises(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="5", price="10", amount="50")
        with pytest.raises(InsufficientShares):
            _add(acct, InvTxnType.SELL, datetime.date(2026, 2, 1),
                 security=sec, qty="10", price="12", amount="120")


def test_editing_a_buy_updates_a_later_sells_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        buy = _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        sell = _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
                    security=sec, qty="10", price="70", amount="700")
        assert sell.realized_gain == D("200")
        # Correct the purchase cost upward; the sale's realized gain must shrink after replay.
        buy.amount = D("600")
        buy.save()
        apply_transaction(buy, is_new=False)
        sell.refresh_from_db()
        assert sell.realized_gain == D("100")


def test_deleting_an_unsold_buy_removes_the_lot(make_tenant):
    from apps.investments.services import remove_transaction

    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        buy = _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        assert cost_basis(acct) == D("500")
        remove_transaction(buy)
        assert cost_basis(acct) == D("0")
        assert not Lot.objects.filter(account=acct, open=True).exists()


def test_stock_split_scales_quantity_preserving_basis(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="100", amount="1000")
        _add(acct, InvTxnType.SPLIT, datetime.date(2026, 2, 1), security=sec,
             split_ratio_new=D("2"), split_ratio_old=D("1"))
        lot = Lot.objects.get(account=acct, security=sec)
        assert lot.remaining_quantity == D("20")      # doubled
        assert lot.cost_basis == D("1000")            # total basis unchanged
        assert lot.per_share_cost == D("50")          # halved


def test_return_of_capital_reduces_basis_then_realizes_excess(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="10", amount="100")
        # Within basis: reduces cost, no gain.
        roc1 = _add(acct, InvTxnType.RETURN_OF_CAPITAL, datetime.date(2026, 2, 1),
                    security=sec, amount="40")
        assert roc1.realized_gain == D("0")
        assert cost_basis(acct) == D("60")
        # Exceeds remaining basis (60): 60 reduces basis to 0, excess 20 is a realized gain.
        roc2 = _add(acct, InvTxnType.RETURN_OF_CAPITAL, datetime.date(2026, 3, 1),
                    security=sec, amount="80")
        assert roc2.realized_gain == D("20")
        assert cost_basis(acct) == D("0")


def test_dividend_reinvest_creates_a_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, datetime.date(2026, 2, 1),
             security=sec, qty="0.5", price="60", amount="30")
        assert cost_basis(acct) == D("530")  # 500 + 30 reinvested
        h = holdings(acct)[0]
        assert h.quantity == D("10.5")


def test_opening_holding_creates_lot_without_cash(make_tenant):
    from apps.investments.services import cash_balance

    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, security=sec, qty="8", price="25", amount="200")
        assert cost_basis(acct) == D("200")
        assert cash_balance(acct) == D("0")  # opening a holding brings in securities, not cash


def test_specific_lot_selection_by_source_buy(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        b1 = _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="10", amount="100")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, qty="10", price="20", amount="200")
        # Sell 5 specifically from the cheaper first lot → cost 50, proceeds 150, gain 100.
        sell = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.SELL, date=datetime.date(2026, 3, 1),
            security=sec, quantity=D("5"), price=D("30"), amount=D("150"),
            cost_basis_method="specific", lot_selection=[{"buy_txn": b1.pk, "qty": "5"}],
        )
        apply_transaction(sell, is_new=True)
        sell.refresh_from_db()
        assert sell.realized_gain == D("100")


def test_holdings_and_attach_totals_batch_queries_flat(make_tenant):
    """`holdings` reads prices in ONE query however many securities are held (no per-security
    latest_price N+1), and `attach_account_totals` stamps figures matching the per-account
    computations in a fixed number of grouped queries."""
    import datetime

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.investments.models import SecurityPrice
    from apps.investments.services import attach_account_totals, cash_balance, market_value

    def _built(n_secs, prefix):
        org = Organization.objects.create(name=f"H{prefix}")
        acct = InvestmentAccount.objects.create(
            institution=org, nickname="T", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        for i in range(n_secs):
            sec = Security.objects.create(
                symbol=f"{prefix}{i}", name="X", currency=Currency.objects.get(code="USD"))
            SecurityPrice.objects.create(
                security=sec, as_of=datetime.date(2026, 1, 10), price=D("12"))
            _add(acct, InvTxnType.BUY, JAN, security=sec, qty="2", price="10", amount="20")
        return acct

    with schema_context(make_tenant().schema_name):
        small = _built(2, "HA")
        large = _built(8, "HB")

        with CaptureQueriesContext(connection) as small_ctx:
            holdings(small)
        with CaptureQueriesContext(connection) as large_ctx:
            large_hold = holdings(large)
        assert len(large_ctx.captured_queries) <= len(small_ctx.captured_queries)
        assert all(h.price == D("12") for h in large_hold)  # batched price == latest price

        # Batch totals agree with the per-account figures, in 3 grouped queries.
        expected = {
            a.pk: (cash_balance(a), market_value(a))
            for a in InvestmentAccount.objects.filter(pk__in=[small.pk, large.pk])
        }
        fresh = list(InvestmentAccount.objects.filter(pk__in=[small.pk, large.pk]))
        with CaptureQueriesContext(connection) as ctx:
            attach_account_totals(fresh)
            got = {a.pk: (a.cash_balance, a.market_value) for a in fresh}
        assert got == expected
        assert len(ctx.captured_queries) <= 3
