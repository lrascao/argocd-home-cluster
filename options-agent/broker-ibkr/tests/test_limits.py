import math
from datetime import date

import pytest

from broker_ibkr import limits, occ
from broker_ibkr.models import DESK, Limits, Side, Strategy

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


# --- positions_risk ---------------------------------------------------------


def opt(symbol: str) -> occ.OptionSymbol:
    return occ.parse(symbol)


def test_positions_risk_offsets_legs_within_one_expiry():
    # A put spread held at a 1.25 credit: short 450, long 440, one contract.
    rows = [(opt("SPY261218P00450000"), -100.0, -250.0), (opt("SPY261218P00440000"), 100.0, 125.0)]
    assert limits.positions_risk(rows) == pytest.approx(875.0)


def test_positions_risk_adds_up_separate_expiries_and_roots():
    spread = [(opt("SPY261218P00450000"), -100.0, -250.0), (opt("SPY261218P00440000"), 100.0, 125.0)]
    later = [(opt("SPY270115P00450000"), -100.0, -250.0), (opt("SPY270115P00440000"), 100.0, 125.0)]
    other = [(opt("QQQ261218P00400000"), -100.0, -250.0), (opt("QQQ261218P00395000"), 100.0, 125.0)]
    assert limits.positions_risk(spread + later + other) == pytest.approx(875.0 + 875.0 + 375.0)


def test_positions_risk_of_nothing_is_zero():
    assert limits.positions_risk([]) == 0.0


# --- eur_per ----------------------------------------------------------------


def test_eur_per_usd_on_a_euro_account():
    assert limits.eur_per({"EUR": 1.0, "USD": 0.86}, "USD") == pytest.approx(0.86)


def test_eur_per_usd_on_a_dollar_account():
    assert limits.eur_per({"USD": 1.0, "EUR": 1.16}, "USD") == pytest.approx(1 / 1.16)


def test_eur_per_falls_back_to_the_market_rate_while_the_account_holds_no_dollars():
    # IBKR sends an ExchangeRate only for currencies the account holds.
    assert limits.eur_per({"EUR": 1.0}, "USD", eurusd=1.16) == pytest.approx(1 / 1.16)
    assert limits.eur_per({}, "USD", eurusd=1.16) == pytest.approx(1 / 1.16)


def test_the_account_rate_comes_before_the_market_rate():
    assert limits.eur_per({"EUR": 1.0, "USD": 0.86}, "USD", eurusd=1.25) == pytest.approx(0.86)


def test_eur_per_is_none_without_either_rate():
    assert limits.eur_per({"EUR": 1.0}, "USD") is None
    assert limits.eur_per({}, "GBP", eurusd=1.16) is None


def test_a_euro_is_a_euro():
    assert limits.eur_per({}, "EUR") == 1.0


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
        "eur_per_usd": 1.0,
    } | overrides
    lim = args.pop("limits", LIMITS)
    return limits.check_opening(lim, **args)


def test_an_order_inside_every_limit_passes():
    assert check() is None


def test_losses_are_converted_to_euros_before_the_limits():
    # $1,100 is over the €1,000 per-order limit at par, not at 0.86.
    assert "per-order limit" in check(order_loss=1100.0)
    assert check(order_loss=1100.0, eur_per_usd=0.86) is None
    # $3,500 open plus $875 is over €3,000 at par, €3,762.50 at 0.86, and
    # under it at 0.65.
    assert "€3,762.50" in check(open_risk=3500.0, eur_per_usd=0.86)
    assert check(open_risk=3500.0, eur_per_usd=0.65) is None


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
        ({"eur_per_usd": None}, "EUR/USD rate"),
    ],
)
def test_refusals_say_why(overrides, reason):
    assert reason in check(**overrides)


# --- check_strategy ---------------------------------------------------------

PUT_SPREAD = Strategy(
    id="put-spread",
    description="test",
    rules="test",
    budget_eur=2500,
    max_loss_per_order_eur=500,
    max_opening_orders_per_day=6,
    min_days_to_expiry=30,
    max_days_to_expiry=45,
    rights=["P"],
    legs=2,
)


def check_s(**overrides):
    args = {
        "error": None,
        "blocked": None,
        "rights": {"P"},
        "legs": 2,
        "expiries": {EXPIRY},
        "today": TODAY,
        "order_loss": 400.0,
        "open_risk": 1000.0,
        "opened_today": 2,
        "eur_per_usd": 0.9,
    } | overrides
    strategy = args.pop("strategy", PUT_SPREAD)
    return limits.check_strategy(strategy, **args)


def test_an_order_inside_the_strategy_s_limits_passes():
    assert check_s() is None


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


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"strategy": None, "error": "put-spread.toml: bad"}, "put-spread.toml: bad"),
        ({"strategy": PUT_SPREAD.model_copy(update={"kill_switch": True})}, "kill switch"),
        ({"blocked": "SPY 261218P00450000: IBKR holds 0, the ledger -1"}, "the ledger does not match IBKR"),
        ({"legs": 4}, "2-leg orders, not 4"),
        ({"rights": {"C", "P"}}, "only puts, not calls"),
        ({"expiries": {EXPIRY, date(2027, 1, 15)}}, "share one expiry"),
        ({"expiries": {date(2026, 12, 4)}}, "outside this strategy's 30-45"),
        ({"order_loss": math.inf}, "unlimited"),
        ({"eur_per_usd": None}, "EUR/USD rate"),
        ({"order_loss": 600.0}, "€540.00 ($600.00) is over this strategy's per-order limit €500.00"),
        ({"open_risk": math.inf}, "unlimited loss"),
        ({"open_risk": 2400.0}, "over its budget €2,500.00 (€2,160.00 already open)"),
        ({"opened_today": 6}, "this strategy's daily limit"),
    ],
)
def test_strategy_refusals_say_why(overrides, reason):
    assert reason in check_s(**overrides)


# --- conflict ---------------------------------------------------------------

SHORT, LONG = 1, 2
CONTRACTS = {SHORT: "SPY   261218P00450000", LONG: "SPY   261218P00440000"}


def test_a_contract_nobody_else_touches_is_free():
    assert limits.conflict("put-spread", CONTRACTS, {SHORT: {"put-spread"}}, {LONG: {"put-spread"}}, {}) is None


def test_a_contract_another_strategy_holds_is_refused():
    reason = limits.conflict("put-spread", CONTRACTS, {LONG: {"iron-condor"}}, {}, {})
    assert reason.startswith("SPY   261218P00440000 is held by iron-condor")


def test_a_contract_another_strategy_works_is_refused():
    reason = limits.conflict("iron-condor", CONTRACTS, {}, {SHORT: {"put-spread", "iron-condor"}}, {})
    assert reason == "put-spread has an order working on SPY   261218P00450000"


def test_the_desk_holds_contracts_like_a_strategy():
    assert "held by desk" in limits.conflict("put-spread", CONTRACTS, {SHORT: {DESK}}, {}, {})
    assert "held by put-spread" in limits.conflict(DESK, CONTRACTS, {SHORT: {"put-spread"}}, {}, {})


def test_a_position_nobody_accounts_for_stops_strategies_but_not_the_desk():
    assert "must be attributed first" in limits.conflict("put-spread", CONTRACTS, {}, {}, {SHORT: -1.0})
    assert limits.conflict(DESK, CONTRACTS, {}, {}, {SHORT: -1.0}) is None
