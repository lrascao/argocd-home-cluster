"""Hard limits on what the agents can open.

A managed agent has no code of its own between the model and this adapter,
so these are the only limits that hold whatever the model does: its
instructions are requests, these are refusals. They apply to orders that open
risk. An order whose every leg reduces a position already held is a close and
is always allowed, kill switch included.

Two layers: the account's limits (limits.env), on every strategy's orders
together, and each strategy's own (strategies/<id>.toml). Money limits are in
euros. A loss is worked out in dollars from the legs and converted at IBKR's
own EUR/USD rate (eur_per); without one, nothing opens.
"""

import logging
import math
import os
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from . import models, occ
from .models import DESK

log = logging.getLogger(__name__)

# One option leg's exposure: right ("C" or "P"), strike, and signed shares
# (contracts x multiplier; positive long, negative short).
Exposure = tuple[str, float, float]

_REQUIRED = (
    "LIMIT_UNDERLYINGS",
    "LIMIT_MAX_LOSS_PER_ORDER",
    "LIMIT_MAX_OPEN_RISK",
    "LIMIT_MAX_OPENING_ORDERS_PER_DAY",
    "LIMIT_MAX_DAILY_LOSS",
    "LIMIT_MIN_DAYS_TO_EXPIRY",
    "LIMIT_MAX_DAYS_TO_EXPIRY",
)


def from_env() -> models.Limits | None:
    """The configured limits, or None if any is missing: then nothing opens."""
    missing = [k for k in _REQUIRED if not os.getenv(k, "").strip()]
    if missing:
        log.warning("limits not configured, nothing will open: set %s", ", ".join(missing))
        return None
    env = os.environ
    return models.Limits(
        underlyings=[s.strip().upper() for s in env["LIMIT_UNDERLYINGS"].split(",") if s.strip()],
        max_loss_per_order=float(env["LIMIT_MAX_LOSS_PER_ORDER"]),
        max_open_risk=float(env["LIMIT_MAX_OPEN_RISK"]),
        max_opening_orders_per_day=int(env["LIMIT_MAX_OPENING_ORDERS_PER_DAY"]),
        max_daily_loss=float(env["LIMIT_MAX_DAILY_LOSS"]),
        min_days_to_expiry=int(env["LIMIT_MIN_DAYS_TO_EXPIRY"]),
        max_days_to_expiry=int(env["LIMIT_MAX_DAYS_TO_EXPIRY"]),
        kill_switch=env.get("LIMIT_KILL_SWITCH") == "true",
    )


def max_loss(legs: list[Exposure], cash: float) -> float:
    """The worst loss at expiry, in dollars, of option legs sharing one expiry.

    cash is what opening them cost: positive paid, negative received. The
    payoff is piecewise linear with kinks only at the strikes, so its lowest
    point is at zero or at a strike, unless more calls are sold than bought,
    when it falls without bound as the price rises: math.inf.
    """
    if sum(q for right, _, q in legs if right == "C") < 0:
        return math.inf

    def payoff(s: float) -> float:
        value = sum(q * (max(s - k, 0.0) if right == "C" else max(k - s, 0.0)) for right, k, q in legs)
        return value - cash

    worst = min(payoff(s) for s in [0.0, *(k for _, k, _ in legs)])
    return max(0.0, -worst)


def positions_risk(positions: Iterable[tuple[occ.OptionSymbol, float, float]]) -> float:
    """The worst loss at expiry, in dollars, of option positions, each given
    as (symbol, signed shares, cash paid to open it). Legs offset each other
    only within one root and expiry, so a calendar is overstated, never
    understated."""
    legs: dict[tuple[str, date], list[Exposure]] = defaultdict(list)
    cash: dict[tuple[str, date], float] = defaultdict(float)
    for s, shares, paid in positions:
        key = (s.root, s.expiry)
        legs[key].append((s.right, float(s.strike), shares))
        cash[key] += paid
    return sum(max_loss(legs[k], cash[k]) for k in legs)


def eur_per(rates: dict[str, float], currency: str, eurusd: float | None = None) -> float | None:
    """Euros per unit of currency. First from IBKR's ExchangeRate account
    values, which give each currency in the account's base currency (the base
    itself at 1), but only for currencies the account holds. Failing that, for
    dollars, from eurusd, the market's dollars per euro. None if neither."""
    if currency == "EUR":
        return 1.0
    unit, eur = rates.get(currency), rates.get("EUR")
    if unit and eur:
        return unit / eur
    if currency == "USD" and eurusd:
        return 1 / eurusd
    return None


def is_close(legs: list[tuple[int, models.Side, float]], held: dict[int, float], working: set[int]) -> bool:
    """Whether an order only reduces positions already held, so cannot open risk.

    legs are (contract id, side, contracts). Every leg must trade against a
    position at least its size, and no other order may be working on that
    contract, or two closes could together flip a position the other way.
    """

    def reduces(con_id: int, side: models.Side, size: float) -> bool:
        pos = held.get(con_id, 0.0)
        if con_id in working or size > abs(pos):
            return False
        return (pos > 0 and side is models.Side.SELL) or (pos < 0 and side is models.Side.BUY)

    return all(reduces(*leg) for leg in legs)


def check_opening(
    limits: models.Limits | None,
    *,
    underlying: str,
    expiries: set[date],
    today: date,
    order_loss: float,
    open_risk: float,
    opened_today: int,
    daily_pnl: float | None,
    eur_per_usd: float | None,
) -> str | None:
    """Why the account's limits refuse an order that opens risk, or None if
    they allow it.

    order_loss is the order's worst loss and open_risk that of everything
    already open and working on the account, without this order, both in
    dollars; opened_today counts opening orders already placed today;
    daily_pnl is IBKR's P&L for the day, in euros.
    """
    if limits is None:
        return "no limits are configured, so nothing may be opened"
    if limits.kill_switch:
        return "the kill switch is on: only closing orders are allowed"
    if underlying not in limits.underlyings:
        return f"{underlying} is not an allowed underlying ({', '.join(limits.underlyings) or 'none'})"
    if len(expiries) != 1:
        return "all legs of an opening order must share one expiry"
    dte = (next(iter(expiries)) - today).days
    if not limits.min_days_to_expiry <= dte <= limits.max_days_to_expiry:
        return f"{dte} days to expiry is outside {limits.min_days_to_expiry}-{limits.max_days_to_expiry}"
    if math.isinf(order_loss):
        return "the loss is unlimited: more calls sold than bought"
    if eur_per_usd is None:
        return _NO_RATE
    loss = order_loss * eur_per_usd
    if loss > limits.max_loss_per_order:
        return (
            f"its max loss {_eur(loss)} (${order_loss:,.2f}) is over the per-order limit "
            f"{_eur(limits.max_loss_per_order)}"
        )
    if math.isinf(open_risk):
        return "positions already open have an unlimited loss; close them first"
    risk = open_risk * eur_per_usd
    if risk + loss > limits.max_open_risk:
        return (
            f"open risk would be {_eur(risk + loss)}, "
            f"over the limit {_eur(limits.max_open_risk)} ({_eur(risk)} already open)"
        )
    if opened_today >= limits.max_opening_orders_per_day:
        return f"{opened_today} opening orders already today, the daily limit"
    if daily_pnl is None:
        return "today's P&L is not known yet; try again in a minute"
    if daily_pnl <= -limits.max_daily_loss:
        return f"today's P&L {_eur(daily_pnl)} has reached the daily loss limit {_eur(limits.max_daily_loss)}"
    return None


def check_strategy(
    strategy: models.Strategy | None,
    *,
    error: str | None,
    blocked: str | None,
    rights: set[str],
    legs: int,
    expiries: set[date],
    today: date,
    order_loss: float,
    open_risk: float,
    opened_today: int,
    eur_per_usd: float | None,
) -> str | None:
    """Why a strategy's own limits refuse an order that opens risk, or None.

    error is why its file did not load; blocked is why the ledger and IBKR
    disagree in a way that stops it (ledger.Reconciliation.blocking). order_loss and open_risk are in dollars,
    open_risk being the strategy's own positions and working opening orders;
    opened_today counts its own opening orders today.
    """
    if strategy is None:
        return f"this strategy has no file that loads in strategies/ ({error or 'none found'}), so it opens nothing"
    if strategy.kill_switch:
        return "this strategy's kill switch is on: only closing orders are allowed"
    if blocked:
        return f"the ledger does not match IBKR ({blocked}); this strategy opens nothing until someone sorts it out"
    if legs != strategy.legs:
        return f"this strategy opens {strategy.legs}-leg orders, not {legs}"
    if not_allowed := rights - set(strategy.rights):
        return f"this strategy opens only {_rights(strategy.rights)}, not {_rights(not_allowed)}"
    if len(expiries) != 1:
        return "all legs of an opening order must share one expiry"
    dte = (next(iter(expiries)) - today).days
    if not strategy.min_days_to_expiry <= dte <= strategy.max_days_to_expiry:
        return (
            f"{dte} days to expiry is outside this strategy's "
            f"{strategy.min_days_to_expiry}-{strategy.max_days_to_expiry}"
        )
    if math.isinf(order_loss):
        return "the loss is unlimited: more calls sold than bought"
    if eur_per_usd is None:
        return _NO_RATE
    loss = order_loss * eur_per_usd
    if loss > strategy.max_loss_per_order_eur:
        return (
            f"its max loss {_eur(loss)} (${order_loss:,.2f}) is over this strategy's "
            f"per-order limit {_eur(strategy.max_loss_per_order_eur)}"
        )
    if math.isinf(open_risk):
        return "this strategy's positions have an unlimited loss; close them first"
    risk = open_risk * eur_per_usd
    if risk + loss > strategy.budget_eur:
        return (
            f"this strategy's open risk would be {_eur(risk + loss)}, "
            f"over its budget {_eur(strategy.budget_eur)} ({_eur(risk)} already open)"
        )
    if opened_today >= strategy.max_opening_orders_per_day:
        return f"{opened_today} opening orders already today, this strategy's daily limit"
    return None


def conflict(
    owner: str,
    contracts: dict[int, str],
    holders: dict[int, set[str]],
    working: dict[int, set[str]],
    unattributed: dict[int, float],
) -> str | None:
    """Why an opening order by owner would share a contract with another
    owner, or None. A contract belongs to one owner at a time, or each
    strategy's trades would net against another's at IBKR and neither would
    know what it holds.

    contracts are the order's legs (contract id -> symbol); holders and
    working give, per contract, the owners holding it in the ledger and the
    owners with an adapter order working on it; unattributed is what IBKR
    holds that no owner accounts for, which the desk owns until someone says
    otherwise.
    """
    for con_id, symbol in contracts.items():
        if others := sorted(holders.get(con_id, set()) - {owner}):
            return f"{symbol} is held by {', '.join(others)}, and a contract belongs to one strategy at a time"
        if others := sorted(working.get(con_id, set()) - {owner}):
            return f"{', '.join(others)} has an order working on {symbol}"
        if owner != DESK and unattributed.get(con_id):
            return (
                f"IBKR holds {unattributed[con_id]:g} of {symbol} that the ledger does not account for; "
                "it must be attributed first (POST /v1/ledger/adjustments)"
            )
    return None


_NO_RATE = "IBKR has not sent the EUR/USD rate yet, and the limits are in euros; try again in a minute"


def _eur(x: float) -> str:
    return f"€{x:,.2f}"


def _rights(rights: Iterable[str]) -> str:
    names = {"C": "calls", "P": "puts"}
    return " and ".join(names[r] for r in sorted(rights))
