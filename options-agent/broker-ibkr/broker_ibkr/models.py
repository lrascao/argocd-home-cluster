"""The broker API every adapter serves.

The agent only ever sees these shapes. Moving from IBKR to another broker
means writing another adapter that serves the same models under the same
App ID, not changing the agent.
"""

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from . import occ


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
    avg_price: float  # per share, multiplier removed
    multiplier: int


class Account(BaseModel):
    account_id: str
    paper: bool
    currency: str  # the account's base currency; US options settle in USD
    net_liquidation: float | None
    available_funds: float | None
    buying_power: float | None
    excess_liquidity: float | None


class Limits(BaseModel):
    """Hard limits on orders that open risk. Closing orders are always allowed."""

    underlyings: list[str]
    max_loss_per_order: float  # USD
    max_open_risk: float  # USD, open positions plus working opening orders
    max_opening_orders_per_day: int  # UTC day, whatever became of the orders
    max_daily_loss: float  # account base currency, from IBKR's daily P&L
    min_days_to_expiry: int
    max_days_to_expiry: int
    kill_switch: bool  # on: nothing opens, closes still go through


class Health(BaseModel):
    connected: bool
    account_id: str | None
    paper: bool | None
    orders_enabled: bool
    limits: Limits | None  # None: not configured, so nothing opens
