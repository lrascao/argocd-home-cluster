"""The broker API every adapter serves.

The agent only ever sees these shapes. Moving from IBKR to another broker
means writing another adapter that serves the same models under the same
App ID, not changing the agent.
"""

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import occ

# Who an order belongs to: a strategy, named by its id, or the desk, which is
# whoever holds the admin token (a person, or the read-only desk agent).
DESK = "desk"
# A strategy id: also its file name in strategies/, and part of every orderRef
# it places, so no dots or colons.
STRATEGY_ID = r"^[a-z0-9][a-z0-9-]{0,23}$"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Leg(BaseModel):
    symbol: str  # OCC option symbol, padded or not; normalized on the way in
    side: Side
    ratio: int = Field(default=1, ge=1, le=10)

    @field_validator("symbol")
    @classmethod
    def _occ(cls, v: str) -> str:
        return occ.normalize(v)


class OrderRequest(BaseModel):
    # The caller's own id for this order. Sending the same id again returns the
    # existing order instead of placing a second one, so a workflow can retry
    # safely. Make it unique per order, e.g. the workflow instance id + step.
    client_order_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.:-]+$")
    legs: list[Leg] = Field(min_length=1, max_length=4)
    quantity: int = Field(ge=1, le=100)
    # Net price per unit of the order, per share (not times the multiplier).
    # Positive pays a debit, negative receives a credit.
    limit_price: Decimal = Field(max_digits=8, decimal_places=2)

    @model_validator(mode="after")
    def _check(self) -> "OrderRequest":
        if self.limit_price == 0:
            raise ValueError("limit_price must not be zero")
        if len({leg.symbol for leg in self.legs}) != len(self.legs):
            raise ValueError("each leg must be a different option")
        if len(self.legs) == 1:
            leg = self.legs[0]
            if leg.ratio != 1:
                raise ValueError("a single-leg order has ratio 1; use quantity")
            if (leg.side is Side.BUY) != (self.limit_price > 0):
                raise ValueError("a single buy pays (positive limit_price), a single sell receives (negative)")
        return self


class OrderState(str, Enum):
    WORKING = "working"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Order(BaseModel):
    client_order_id: str
    strategy: str  # who placed it: a strategy's id, or "desk"
    broker_order_id: str | None  # assigned by the broker once it accepts the order
    state: OrderState
    broker_status: str  # the broker's own status word, for debugging
    quantity: float
    filled: float
    limit_price: float | None  # same sign convention as the request
    avg_fill_price: float | None
    message: str | None  # the broker's last error or warning, e.g. why it rejected


class OrderPreview(BaseModel):
    # False when every leg reduces a position already held: a close, which the
    # limits always allow.
    opens: bool
    max_loss: float | None  # USD at expiry, at the limit price; None if unlimited
    max_loss_eur: float | None  # the same at IBKR's EUR/USD rate, which the limits use
    refused: str | None  # why the limits would refuse it; None if they allow it
    initial_margin_change: float | None
    maintenance_margin_change: float | None
    commission_min: float | None
    commission_max: float | None
    commission_currency: str | None
    warning: str | None


class Quote(BaseModel):
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    close: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    implied_vol: float | None = None
    error: str | None = None


class Chain(BaseModel):
    underlying: str
    root: str  # the OCC root to build symbols with; an underlying can have several
    multiplier: int
    expirations: list[date]
    # Every strike listed for any expiration. Not every strike exists for every
    # expiration: quote a symbol to confirm it before trading it.
    strikes: list[float]


class Position(BaseModel):
    symbol: str  # OCC symbol for an option, ticker for a stock
    sec_type: str
    quantity: float  # negative is short
    avg_price: float | None  # per share, multiplier removed; None if unknown
    multiplier: int
    # Who holds it, from the adapter's ledger: a strategy's id, or "desk". None
    # is the part of IBKR's position the ledger does not account for: a trade
    # made outside the adapter, an assignment, a fill the adapter missed.
    strategy: str | None


class Account(BaseModel):
    account_id: str
    paper: bool
    currency: str  # the account's base currency; US options settle in USD
    net_liquidation: float | None
    available_funds: float | None
    buying_power: float | None
    excess_liquidity: float | None


class Limits(BaseModel):
    """Hard limits on the whole account's orders that open risk, every
    strategy's together. Closing orders are always allowed."""

    underlyings: list[str]
    max_loss_per_order: float  # EUR
    max_open_risk: float  # EUR, open positions plus working opening orders
    max_opening_orders_per_day: int  # UTC day, whatever became of the orders
    max_daily_loss: float  # EUR, from IBKR's daily P&L for the account
    min_days_to_expiry: int
    max_days_to_expiry: int
    kill_switch: bool  # on: nothing opens, closes still go through


class Health(BaseModel):
    connected: bool
    account_id: str | None
    paper: bool | None
    orders_enabled: bool
    caller: str  # who the token says is asking: a strategy's id, or "desk"
    eur_per_usd: float | None  # IBKR's rate; until it is known, nothing opens
    limits: Limits | None  # None: not configured, so nothing opens


class Strategy(BaseModel):
    """One strategy, from strategies/<id>.toml. The numbers are hard limits on
    its own orders that open risk, inside the account's. The rules are what
    its agent is asked to do, and hold only as far as the numbers do."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=STRATEGY_ID)
    description: str
    rules: str
    budget_eur: float = Field(gt=0)  # its open risk: positions plus working opening orders
    max_loss_per_order_eur: float = Field(gt=0)
    max_opening_orders_per_day: int = Field(ge=0)
    min_days_to_expiry: int = Field(ge=0)
    max_days_to_expiry: int = Field(ge=0)
    rights: list[Literal["C", "P"]] = Field(min_length=1)  # what its opening orders may hold
    legs: int = Field(ge=1, le=4)  # how many legs each opening order has
    # The least net credit per share an opening order may ask: its
    # limit_price must be this or more negative. None for a strategy that
    # opens for a debit.
    min_credit: float | None = Field(default=None, gt=0)
    kill_switch: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Strategy":
        if self.id == DESK:
            raise ValueError(f"{DESK!r} is not a strategy id")
        if self.min_days_to_expiry > self.max_days_to_expiry:
            raise ValueError("min_days_to_expiry is over max_days_to_expiry")
        return self


class StrategyStatus(BaseModel):
    id: str
    strategy: Strategy | None  # None if its file is missing or does not load
    error: str | None  # why it did not load
    open_risk_eur: float | None  # its positions and working opening orders
    budget_left_eur: float | None
    opening_orders_today: int  # UTC day, counting refused and cancelled ones
    # Why it may open nothing right now, whatever the order: no file, its kill
    # switch, or the ledger not matching IBKR's positions.
    blocked: str | None


class Adjustment(BaseModel):
    """A fill entered by hand, to make the ledger match IBKR: a position IBKR
    holds that nobody owns, or one a strategy no longer holds (an early
    assignment). It is recorded like a fill, so it moves the average price."""

    strategy: str = Field(pattern=STRATEGY_ID)  # "desk" matches the pattern too
    symbol: str  # OCC option symbol
    quantity: float  # contracts: positive adds a long or takes off a short
    price: float = Field(ge=0)  # per share; 0 for an expiry or an assignment

    @field_validator("symbol")
    @classmethod
    def _occ(cls, v: str) -> str:
        return occ.normalize(v)

    @field_validator("quantity")
    @classmethod
    def _nonzero(cls, v: float) -> float:
        if v == 0:
            raise ValueError("quantity must not be zero")
        return v


class LedgerReport(BaseModel):
    holdings: list[Position]  # what each owner holds, by the ledger
    unattributed: list[Position]  # IBKR's positions less the ledger's, where they differ
    blocked: dict[str, str]  # strategy -> why its holdings do not match IBKR's
    everyone_blocked: str | None  # why no strategy may open: a live option no one accounts for
