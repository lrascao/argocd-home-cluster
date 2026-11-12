import pytest
from pydantic import ValidationError

from broker_ibkr.models import OrderRequest

SHORT_PUT = {"symbol": "SPY261218P00450000", "side": "sell"}
LONG_PUT = {"symbol": "SPY261218P00440000", "side": "buy"}


def request(**overrides):
    return {"client_order_id": "wf-1:open", "legs": [SHORT_PUT, LONG_PUT], "quantity": 1, "limit_price": -1.25} | overrides


def test_a_credit_spread_is_valid_and_its_symbols_are_normalized():
    req = OrderRequest.model_validate(request())
    assert [leg.symbol for leg in req.legs] == ["SPY   261218P00450000", "SPY   261218P00440000"]


def test_a_single_buy_pays_and_a_single_sell_receives():
    OrderRequest.model_validate(request(legs=[LONG_PUT], limit_price=1.25))
    OrderRequest.model_validate(request(legs=[SHORT_PUT], limit_price=-1.25))


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit_price": 0},
        {"limit_price": 1.255},
        {"quantity": 0},
        {"legs": []},
        {"legs": [SHORT_PUT, SHORT_PUT]},
        {"legs": [{"symbol": "SPY", "side": "buy"}]},
        {"legs": [SHORT_PUT], "limit_price": 1.25},
        {"legs": [LONG_PUT], "limit_price": -1.25},
        {"legs": [LONG_PUT | {"ratio": 2}], "limit_price": 1.25},
        {"client_order_id": "has space"},
        {"client_order_id": "x" * 33},
    ],
)
def test_rejects(overrides):
    with pytest.raises(ValidationError):
        OrderRequest.model_validate(request(**overrides))
