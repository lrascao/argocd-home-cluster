from datetime import date, datetime, timedelta, timezone

import pytest

from broker_ibkr import ledger
from broker_ibkr.models import DESK

TODAY = date(2026, 11, 13)
SHORT = "SPY   261218P00450000"
LONG = "SPY   261218P00440000"
EXPIRED = "SPY   261106P00450000"
T0 = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)

_n = 0


def fill(owner, con_id, symbol, quantity, price, minutes=0):
    global _n
    _n += 1
    return ledger.Fill(
        exec_id=f"e{_n}",
        time=T0 + timedelta(minutes=minutes),
        owner=owner,
        con_id=con_id,
        symbol=symbol,
        quantity=quantity,
        price=price,
        multiplier=100.0,
        client_order_id=f"c{_n}",
    )


def held(fills, today=TODAY):
    return {(h.owner, h.con_id): (h.quantity, h.avg_price) for h in ledger.holdings(fills, today)}


# --- holdings ---------------------------------------------------------------


def test_a_spread_s_legs_are_held_at_their_fill_prices():
    fills = [fill("put-spread", 1, SHORT, -1, 2.50), fill("put-spread", 2, LONG, 1, 1.25)]
    assert held(fills) == {("put-spread", 1): (-1, 2.50), ("put-spread", 2): (1, 1.25)}


def test_adding_moves_the_average_and_reducing_does_not():
    fills = [
        fill("put-spread", 1, SHORT, -1, 2.00, 0),
        fill("put-spread", 1, SHORT, -1, 3.00, 1),
        fill("put-spread", 1, SHORT, 1, 1.00, 2),
    ]
    assert held(fills) == {("put-spread", 1): (-1, pytest.approx(2.50))}


def test_going_through_zero_starts_again_at_the_fill_price():
    fills = [fill("put-spread", 1, SHORT, -1, 2.00, 0), fill("put-spread", 1, SHORT, 2, 0.50, 1)]
    assert held(fills) == {("put-spread", 1): (1, 0.50)}


def test_fills_count_in_time_order_whatever_order_they_come_in():
    late = fill("put-spread", 1, SHORT, 2, 0.50, 1)
    early = fill("put-spread", 1, SHORT, -1, 2.00, 0)
    assert held([late, early]) == {("put-spread", 1): (1, 0.50)}


def test_a_closed_position_is_gone():
    fills = [fill("put-spread", 1, SHORT, -1, 2.50, 0), fill("put-spread", 1, SHORT, 1, 1.00, 1)]
    assert held(fills) == {}


def test_owners_holding_the_same_contract_are_kept_apart():
    fills = [fill("put-spread", 1, SHORT, -1, 2.50), fill(DESK, 1, SHORT, -2, 2.40)]
    assert held(fills) == {("put-spread", 1): (-1, 2.50), (DESK, 1): (-2, 2.40)}


def test_an_option_past_its_expiry_drops_out():
    fills = [fill("put-spread", 3, EXPIRED, -1, 2.50)]
    assert held(fills, date(2026, 11, 6)) == {("put-spread", 3): (-1, 2.50)}  # its last day
    assert held(fills, date(2026, 11, 7)) == {}


# --- reconcile --------------------------------------------------------------


def holding(owner, con_id, quantity, symbol=SHORT):
    return ledger.Holding(owner, con_id, symbol, quantity, 2.50, 100.0)


def test_when_the_ledger_matches_ibkr_nothing_is_blocked():
    rec = ledger.reconcile([holding("put-spread", 1, -1), holding("iron-condor", 2, 1)], {1: -1, 2: 1}, {1: SHORT})
    assert (rec.unattributed, rec.blocked, rec.everyone) == ({}, {}, None)


def test_two_owners_of_one_contract_add_up():
    rec = ledger.reconcile([holding("put-spread", 1, -1), holding(DESK, 1, -2)], {1: -3}, {1: SHORT})
    assert (rec.unattributed, rec.blocked, rec.everyone) == ({}, {}, None)


def test_a_strategy_whose_position_ibkr_no_longer_has_is_blocked():
    # An early assignment: the short put is gone at IBKR, the ledger still has it.
    rec = ledger.reconcile([holding("put-spread", 1, -1)], {}, {})
    assert rec.unattributed == {1: 1}
    assert rec.blocked == {"put-spread": f"{SHORT}: IBKR holds 0, the ledger -1"}
    assert rec.blocking("put-spread") == rec.blocked["put-spread"]
    assert rec.blocking("iron-condor") is None


def test_a_live_option_no_one_accounts_for_blocks_every_strategy_but_not_the_desk():
    # A fill the adapter missed, or a trade made in IBKR's own app.
    rec = ledger.reconcile([], {7: -1}, {7: SHORT})
    assert rec.unattributed == {7: -1}
    assert rec.everyone == f"IBKR holds -1 of {SHORT} that no one in the ledger accounts for"
    assert rec.blocking("put-spread") == rec.blocking("iron-condor") == rec.everyone
    assert rec.blocking(DESK) is None


def test_a_stock_or_an_expired_option_no_one_accounts_for_blocks_no_one():
    # Shares from an assignment; an expired option IBKR has not cleared yet.
    rec = ledger.reconcile([], {8: 100, 9: -1}, {})
    assert rec.unattributed == {8: 100, 9: -1}
    assert (rec.blocked, rec.everyone) == ({}, None)


def test_the_desk_is_never_blocked():
    rec = ledger.reconcile([holding(DESK, 1, -1)], {}, {})
    assert (rec.blocked, rec.blocking(DESK)) == ({}, None)


# --- the SQLite file --------------------------------------------------------


def test_a_fill_is_recorded_once_and_survives_a_restart(tmp_path):
    path = str(tmp_path / "ledger.db")
    f = fill("put-spread", 1, SHORT, -1, 2.50)
    book = ledger.Ledger(path)
    assert book.add([f]) == [f]
    assert book.add([f]) == []  # IBKR reports the day's fills again on every connect
    assert ledger.Ledger(path).all() == [f]


def test_a_naive_time_is_taken_as_utc(tmp_path):
    f = fill("put-spread", 1, SHORT, -1, 2.50)
    naive = ledger.Fill(**{**f.__dict__, "exec_id": "naive", "time": T0.replace(tzinfo=None)})
    book = ledger.Ledger(str(tmp_path / "ledger.db"))
    book.add([naive])
    assert book.all()[0].time == T0
