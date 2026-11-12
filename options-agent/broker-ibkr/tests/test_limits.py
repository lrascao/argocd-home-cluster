import math
from datetime import date

import pytest

from broker_ibkr import limits
from broker_ibkr.models import Limits, Side

TODAY = date(2026, 11, 11)
EXPIRY = date(2026, 12, 18)  # 37 days out

LIMITS = Limits(
    underlyings=["SPY", "QQQ"],
    max_loss_per_order=1000,
    max_open_risk=3000,
    max_opening_orders_per_day=4,
    max_daily_loss=500,
    min_days_to_expiry=7,
    max_days_to_expiry=60,
    kill_switch=False,
)


# --- max_loss ---------------------------------------------------------------


def test_a_put_credit_spread_loses_its_width_less_the_credit():
    # Sell the 450 put, buy the 440 put, for a 1.25 credit, one contract.
    legs = [("P", 450.0, -100.0), ("P", 440.0, 100.0)]
    assert limits.max_loss(legs, -125.0) == pytest.approx(875.0)


def test_a_call_debit_spread_loses_what_it_cost():
    legs = [("C", 500.0, 100.0), ("C", 510.0, -100.0)]
    assert limits.max_loss(legs, 400.0) == pytest.approx(400.0)


def test_an_iron_condor_loses_its_wider_side_less_the_credit():
    legs = [("P", 440.0, 100.0), ("P", 450.0, -100.0), ("C", 520.0, -100.0), ("C", 535.0, 100.0)]
    assert limits.max_loss(legs, -200.0) == pytest.approx(1300.0)


def test_a_bought_option_loses_its_premium():
    assert limits.max_loss([("C", 500.0, 200.0)], 600.0) == pytest.approx(600.0)


def test_a_sold_put_can_lose_down_to_zero():
    assert limits.max_loss([("P", 450.0, -100.0)], -300.0) == pytest.approx(44700.0)


def test_a_sold_call_without_a_bought_one_is_unlimited():
    assert math.isinf(limits.max_loss([("C", 500.0, -100.0)], -300.0))
    assert math.isinf(limits.max_loss([("C", 500.0, -200.0), ("C", 510.0, 100.0)], -300.0))


def test_a_trade_that_cannot_lose_reports_zero():
    assert limits.max_loss([("C", 500.0, 100.0)], -50.0) == 0.0


# --- is_close ---------------------------------------------------------------


def test_selling_what_is_held_is_a_close():
    assert limits.is_close([(1, Side.SELL, 2)], {1: 2.0}, set())


def test_buying_back_a_short_spread_is_a_close():
    assert limits.is_close([(1, Side.BUY, 1), (2, Side.SELL, 1)], {1: -1.0, 2: 1.0}, set())


@pytest.mark.parametrize(
    "legs, held, working",
    [
        ([(1, Side.SELL, 3)], {1: 2.0}, set()),  # more than is held
        ([(1, Side.BUY, 1)], {1: 2.0}, set()),  # adds to a long
        ([(1, Side.SELL, 1)], {}, set()),  # nothing held
        ([(1, Side.SELL, 1), (2, Side.BUY, 1)], {1: 1.0}, set()),  # second leg opens
        ([(1, Side.SELL, 1)], {1: 1.0}, {1}),  # another order already works it
    ],
)
def test_anything_else_opens(legs, held, working):
    assert not limits.is_close(legs, held, working)


# --- check_opening ----------------------------------------------------------


def check(**overrides):
    args = {
        "underlying": "SPY",
        "expiries": {EXPIRY},
        "today": TODAY,
        "order_loss": 875.0,
        "open_risk": 1000.0,
        "opened_today": 1,
        "daily_pnl": -100.0,
    } | overrides
    lim = args.pop("limits", LIMITS)
    return limits.check_opening(lim, **args)


def test_an_order_inside_every_limit_passes():
    assert check() is None


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"limits": None}, "no limits are configured"),
        ({"limits": LIMITS.model_copy(update={"kill_switch": True})}, "kill switch"),
        ({"underlying": "TSLA"}, "not an allowed underlying"),
        ({"expiries": {EXPIRY, date(2027, 1, 15)}}, "share one expiry"),
        ({"expiries": {date(2026, 11, 13)}}, "2 days to expiry"),
        ({"expiries": {date(2027, 3, 19)}}, "days to expiry is outside"),
        ({"order_loss": math.inf}, "unlimited"),
        ({"order_loss": 1000.01}, "per-order limit"),
        ({"open_risk": math.inf}, "already open have an unlimited loss"),
        ({"open_risk": 2200.0}, "open risk would be"),
        ({"opened_today": 4}, "daily limit"),
        ({"daily_pnl": None}, "not known yet"),
        ({"daily_pnl": -500.0}, "daily loss limit"),
    ],
)
def test_refusals_say_why(overrides, reason):
    assert reason in check(**overrides)


def test_limits_come_from_the_environment(monkeypatch):
    env = {
        "LIMIT_UNDERLYINGS": " spy, qqq ,",
        "LIMIT_MAX_LOSS_PER_ORDER": "1000",
        "LIMIT_MAX_OPEN_RISK": "3000",
        "LIMIT_MAX_OPENING_ORDERS_PER_DAY": "4",
        "LIMIT_MAX_DAILY_LOSS": "500",
        "LIMIT_MIN_DAYS_TO_EXPIRY": "7",
        "LIMIT_MAX_DAYS_TO_EXPIRY": "60",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LIMIT_KILL_SWITCH", raising=False)
    assert limits.from_env() == LIMITS

    monkeypatch.setenv("LIMIT_KILL_SWITCH", "true")
    assert limits.from_env().kill_switch

    monkeypatch.delenv("LIMIT_MAX_DAILY_LOSS")
    assert limits.from_env() is None
