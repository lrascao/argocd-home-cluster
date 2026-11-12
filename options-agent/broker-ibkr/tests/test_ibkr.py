from datetime import datetime

from ib_async import Contract, LimitOrder, OrderStatus, Trade, TradeLogEntry

from broker_ibkr.ibkr import _parse_ref, build_order, order_view
from broker_ibkr.models import OrderRequest, OrderState

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
    assert _parse_ref(REF) == ("o", "20261111", "wf-1:open")
    assert _parse_ref("c:20261111:x") == ("c", "20261111", "x")


def test_parse_ref_ignores_orders_the_adapter_did_not_place():
    for ref in ["", "manual", "x:20261111:id", "o:2026-11-11:id", "o:20261111"]:
        assert _parse_ref(ref) is None


def test_order_view_reports_a_sold_option_as_a_credit():
    trade = Trade(
        contract=Contract(secType="OPT"),
        order=LimitOrder("SELL", 1, 2.10, orderRef=REF, permId=42),
        orderStatus=OrderStatus(status="Filled", filled=1, avgFillPrice=2.05),
    )
    view = order_view(trade)
    assert (view.client_order_id, view.state) == ("wf-1:open", OrderState.FILLED)
    assert (view.limit_price, view.avg_fill_price, view.broker_order_id) == (-2.10, -2.05, "42")


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
