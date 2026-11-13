from datetime import date, datetime

from ib_async import Contract, LimitOrder, OrderStatus, Trade, TradeLogEntry

from broker_ibkr.ibkr import _parse_ref, build_order, make_ref, order_view
from broker_ibkr.models import DESK, OrderRequest, OrderState

SHORT = "SPY   261218P00450000"
LONG = "SPY   261218P00440000"
REF = "o:20261111:wf-1:open"


def qualified(con_id: int, local_symbol: str) -> Contract:
    return Contract(secType="OPT", conId=con_id, symbol="SPY", localSymbol=local_symbol, exchange="SMART", currency="USD")


def request(legs, limit_price):
    return OrderRequest.model_validate(
        {"client_order_id": "wf-1:open", "legs": legs, "quantity": 2, "limit_price": limit_price}
    )


def test_a_spread_is_one_combo_order_priced_at_its_net():
    req = request([{"symbol": SHORT, "side": "sell"}, {"symbol": LONG, "side": "buy"}], -1.25)
    contract, order = build_order(req, [qualified(1, SHORT), qualified(2, LONG)], "DU123", REF)
    assert (contract.secType, contract.symbol, contract.exchange) == ("BAG", "SPY", "SMART")
    assert [(leg.conId, leg.action, leg.ratio) for leg in contract.comboLegs] == [(1, "SELL", 1), (2, "BUY", 1)]
    assert (order.action, order.totalQuantity, order.lmtPrice) == ("BUY", 2, -1.25)
    assert (order.orderType, order.tif, order.orderRef, order.account) == ("LMT", "DAY", REF, "DU123")


def test_a_single_sold_option_is_priced_positive():
    req = request([{"symbol": SHORT, "side": "sell"}], -2.10)
    contract, order = build_order(req, [qualified(1, SHORT)], "DU123", REF)
    assert contract.conId == 1
    assert (order.action, order.lmtPrice) == ("SELL", 2.10)


def test_parse_ref_keeps_colons_in_the_client_order_id():
    assert _parse_ref(REF) == ("o", "20261111", DESK, "wf-1:open")
    assert _parse_ref("c:20261111:x") == ("c", "20261111", DESK, "x")


def test_a_strategy_s_order_carries_its_id_and_reads_back():
    ref = make_ref("o", "put-spread", date(2026, 11, 13), "20261113-1430-1")
    assert ref == "o.put-spread:20261113:20261113-1430-1"
    assert _parse_ref(ref) == ("o", "20261113", "put-spread", "20261113-1430-1")


def test_the_desk_s_orders_keep_the_format_without_a_strategy():
    # So orders placed before there were strategies still read as the desk's.
    ref = make_ref("c", DESK, date(2026, 11, 13), "test-1")
    assert ref == "c:20261113:test-1"
    assert _parse_ref(ref).owner == DESK


def test_parse_ref_ignores_orders_the_adapter_did_not_place():
    for ref in [
        "",
        "manual",
        "x:20261111:id",
        "o:2026-11-11:id",
        "o:20261111",
        "o.Put_Spread:20261111:id",  # not a strategy id
        "o.desk:20261111:id",  # the desk never names itself
        "x.put-spread:20261111:id",
    ]:
        assert _parse_ref(ref) is None


def test_order_view_reports_a_sold_option_as_a_credit():
    trade = Trade(
        contract=Contract(secType="OPT"),
        order=LimitOrder("SELL", 1, 2.10, orderRef=REF, permId=42),
        orderStatus=OrderStatus(status="Filled", filled=1, avgFillPrice=2.05),
    )
    view = order_view(trade)
    assert (view.client_order_id, view.strategy, view.state) == ("wf-1:open", DESK, OrderState.FILLED)
    assert (view.limit_price, view.avg_fill_price, view.broker_order_id) == (-2.10, -2.05, "42")


def test_order_view_says_which_strategy_placed_it():
    trade = Trade(
        contract=Contract(secType="BAG"),
        order=LimitOrder("BUY", 1, -0.95, orderRef="o.iron-condor:20261113:20261113-1015-1"),
        orderStatus=OrderStatus(status="Submitted"),
    )
    view = order_view(trade)
    assert (view.client_order_id, view.strategy) == ("20261113-1015-1", "iron-condor")


def test_order_view_surfaces_why_ibkr_rejected_it():
    trade = Trade(
        contract=Contract(secType="BAG"),
        order=LimitOrder("BUY", 1, -1.25, orderRef=REF),
        orderStatus=OrderStatus(status="Inactive"),
        log=[TradeLogEntry(datetime.now(), "Inactive", "Order rejected - reason: no trading permission", 201)],
    )
    view = order_view(trade)
    assert view.state is OrderState.REJECTED
    assert view.message == "Order rejected - reason: no trading permission"
    assert (view.limit_price, view.avg_fill_price, view.broker_order_id) == (-1.25, None, None)


def test_order_view_treats_any_other_status_as_working():
    trade = Trade(
        contract=Contract(secType="BAG"),
        order=LimitOrder("BUY", 1, 0.80, orderRef="c:20261111:wf-2:close"),
        orderStatus=OrderStatus(status="PreSubmitted"),
    )
    assert order_view(trade).state is OrderState.WORKING
