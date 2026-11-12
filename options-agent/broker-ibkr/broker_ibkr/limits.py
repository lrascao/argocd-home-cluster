"""Hard limits on what the agent can open.

A managed agent has no code of its own between the model and this adapter,
so these are the only limits that hold whatever the model does: its
instructions are requests, these are refusals. They apply to orders that open
risk. An order whose every leg reduces a position already held is a close and
is always allowed, kill switch included.
"""

import logging
import math
import os
from datetime import date

from . import models

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
) -> str | None:
    """Why the limits refuse an order that opens risk, or None if they allow it.

    open_risk is the worst loss of what is already open and working, without
    this order; opened_today counts opening orders already placed today.
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
    if order_loss > limits.max_loss_per_order:
        return f"its max loss ${order_loss:,.2f} is over the per-order limit ${limits.max_loss_per_order:,.2f}"
    if math.isinf(open_risk):
        return "positions already open have an unlimited loss; close them first"
    if open_risk + order_loss > limits.max_open_risk:
        return (
            f"open risk would be ${open_risk + order_loss:,.2f}, "
            f"over the limit ${limits.max_open_risk:,.2f} (${open_risk:,.2f} already open)"
        )
    if opened_today >= limits.max_opening_orders_per_day:
        return f"{opened_today} opening orders already today, the daily limit"
    if daily_pnl is None:
        return "today's P&L is not known yet; try again in a minute"
    if daily_pnl <= -limits.max_daily_loss:
        return f"today's P&L {daily_pnl:,.2f} has reached the daily loss limit {limits.max_daily_loss:,.2f}"
    return None
