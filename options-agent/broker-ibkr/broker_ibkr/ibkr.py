"""IBKR implementation of the broker API, over IB Gateway's socket API.

Talks to an IB Gateway (or TWS) running next to it. Places orders only on a
paper account unless IBKR_ALLOW_LIVE=true; IBKR paper account ids start "DU".
Every order that opens risk must also pass the hard limits in limits.py.
"""

import asyncio
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ib_async import IB, ComboLeg, Contract, LimitOrder, Stock, Trade

from . import models, occ
from .limits import Exposure, check_opening, is_close, max_loss

log = logging.getLogger(__name__)

# IBKR reports "no value" as the largest double, and "no quote" as -1.
_UNSET = 1e300


class NotReady(Exception):
    """IB Gateway is not connected."""


class Refused(Exception):
    """The request is well formed but cannot be carried out."""


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 4002  # IB Gateway's paper port; its live port is 4001
    # Keep this fixed: IBKR ties an order to the client id that placed it, and
    # that is how the adapter finds its own orders again after a restart.
    client_id: int = 17
    account: str = ""  # empty: the one account the login manages
    market_data_type: int = 3  # 1 live, 3 delayed; delayed until you pay for OPRA
    allow_live: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("IBKR_HOST", cls.host),
            port=int(os.getenv("IBKR_PORT", str(cls.port))),
            client_id=int(os.getenv("IBKR_CLIENT_ID", str(cls.client_id))),
            account=os.getenv("IBKR_ACCOUNT", cls.account),
            market_data_type=int(os.getenv("IBKR_MARKET_DATA_TYPE", str(cls.market_data_type))),
            allow_live=os.getenv("IBKR_ALLOW_LIVE") == "true",
        )


@dataclass
class _Assessment:
    """An order translated for IBKR, and what the limits make of it."""

    contract: Contract
    order: LimitOrder
    opens: bool
    max_loss: float | None  # opening orders only; math.inf if unlimited
    refused: str | None


class IBKRBroker:
    def __init__(self, settings: Settings, limits: models.Limits | None) -> None:
        self.settings = settings
        self.limits = limits
        self.ib = IB()
        self.account = ""
        self._pnl = None  # IBKR's live P&L for the account, once subscribed
        self._symbols: dict[int, occ.OptionSymbol] = {}  # contract id -> OCC symbol
        self._place_lock = asyncio.Lock()

    @property
    def paper(self) -> bool:
        return self.account.startswith("DU")

    @property
    def orders_enabled(self) -> bool:
        return self.ib.isConnected() and (self.paper or self.settings.allow_live)

    async def run(self) -> None:
        """Stay connected to IB Gateway. It restarts once a day, so reconnect."""
        while True:
            if not self.ib.isConnected():
                try:
                    await self._connect()
                except Exception as e:  # any failure: log it and try again shortly
                    log.warning("connecting to IB Gateway at %s:%s failed: %s", self.settings.host, self.settings.port, e)
            await asyncio.sleep(5)

    async def _connect(self) -> None:
        s = self.settings
        await self.ib.connectAsync(s.host, s.port, clientId=s.client_id, account=s.account)
        accounts = self.ib.managedAccounts()
        account = s.account or (accounts[0] if len(accounts) == 1 else "")
        if account not in accounts:
            self.ib.disconnect()
            raise RuntimeError(f"set IBKR_ACCOUNT to one of {accounts}")
        self.account = account
        self.ib.reqMarketDataType(s.market_data_type)
        # The daily loss limit reads this. Reuse a subscription that survived a
        # dropped connection rather than asking for a second one.
        existing = self.ib.pnl(account)
        self._pnl = existing[0] if existing else self.ib.reqPnL(account)
        log.info("connected: account=%s paper=%s orders_enabled=%s", account, self.paper, self.orders_enabled)

    def _require_connected(self) -> None:
        if not self.ib.isConnected():
            raise NotReady("not connected to IB Gateway")

    def health(self) -> models.Health:
        return models.Health(
            connected=self.ib.isConnected(),
            account_id=self.account or None,
            paper=self.paper if self.account else None,
            orders_enabled=self.orders_enabled,
            limits=self.limits,
        )

    async def account_summary(self) -> models.Account:
        self._require_connected()
        values = {v.tag: v for v in await self.ib.accountSummaryAsync(self.account)}

        def num(tag: str) -> float | None:
            v = values.get(tag)
            return _num(v.value) if v else None

        base = values.get("NetLiquidation")
        return models.Account(
            account_id=self.account,
            paper=self.paper,
            currency=base.currency if base else "",
            net_liquidation=num("NetLiquidation"),
            available_funds=num("AvailableFunds"),
            buying_power=num("BuyingPower"),
            excess_liquidity=num("ExcessLiquidity"),
        )

    def positions(self) -> list[models.Position]:
        self._require_connected()
        out = []
        for p in self.ib.positions(self.account):
            c = p.contract
            multiplier = int(float(c.multiplier or 1))
            out.append(
                models.Position(
                    symbol=_symbol_of(c),
                    sec_type=c.secType,
                    quantity=p.position,
                    avg_price=p.avgCost / multiplier,
                    multiplier=multiplier,
                )
            )
        return out

    async def quotes(self, symbols: list[str]) -> list[models.Quote]:
        self._require_connected()
        contracts = [_contract_for(s) for s in symbols]
        await self.ib.qualifyContractsAsync(*contracts)  # sets conId on each one it finds
        known = [c for c in contracts if c.conId]
        tickers = {t.contract.conId: t for t in await self.ib.reqTickersAsync(*known)} if known else {}
        out = []
        for c in contracts:
            t = tickers.get(c.conId) if c.conId else None
            if t is None:
                error = "no market data" if c.conId else "no such contract"
                out.append(models.Quote(symbol=_symbol_of(c), error=error))
                continue
            g = t.modelGreeks
            out.append(
                models.Quote(
                    symbol=_symbol_of(c),
                    bid=_price(t.bid),
                    ask=_price(t.ask),
                    last=_price(t.last),
                    close=_price(t.close),
                    delta=_num(g.delta) if g else None,
                    gamma=_num(g.gamma) if g else None,
                    theta=_num(g.theta) if g else None,
                    vega=_num(g.vega) if g else None,
                    implied_vol=_num(g.impliedVol) if g else None,
                )
            )
        return out

    async def chains(self, underlying: str) -> list[models.Chain]:
        self._require_connected()
        stock = Stock(underlying.upper(), "SMART", "USD")
        await self.ib.qualifyContractsAsync(stock)
        if not stock.conId:
            raise NotFound(f"no US stock or ETF {underlying!r}")
        chains = await self.ib.reqSecDefOptParamsAsync(stock.symbol, "", stock.secType, stock.conId)
        out = [
            models.Chain(
                underlying=stock.symbol,
                root=c.tradingClass,
                multiplier=int(float(c.multiplier or 100)),
                expirations=sorted(_ib_date(e) for e in c.expirations),
                strikes=sorted(c.strikes),
            )
            for c in chains
            if c.exchange == "SMART"
        ]
        if not out:
            raise NotFound(f"no options listed on {underlying!r}")
        return out

    async def preview(self, req: models.OrderRequest) -> models.OrderPreview:
        self._require_connected()
        a = await self._assess(req)
        state = await self.ib.whatIfOrderAsync(a.contract, a.order)
        return models.OrderPreview(
            opens=a.opens,
            max_loss=a.max_loss if a.max_loss is not None and not math.isinf(a.max_loss) else None,
            refused=a.refused,
            initial_margin_change=_num(state.initMarginChange),
            maintenance_margin_change=_num(state.maintMarginChange),
            commission_min=_num(state.minCommission),
            commission_max=_num(state.maxCommission),
            commission_currency=state.commissionCurrency or None,
            warning=state.warningText or None,
        )

    async def place(self, req: models.OrderRequest) -> models.Order:
        self._require_connected()
        if not self.orders_enabled:
            raise Refused(f"account {self.account} is not a paper account; set IBKR_ALLOW_LIVE=true to trade it")
        async with self._place_lock:
            # The agent retries a call whose reply it never saw. The same
            # client_order_id returns the order already placed instead of
            # placing it twice.
            existing = self._find(req.client_order_id)
            if existing is not None:
                return order_view(existing)
            a = await self._assess(req)
            if a.refused:
                log.info("refused %s: %s", req.client_order_id, a.refused)
                raise Refused(a.refused)
            log.info("placing %s (opens=%s, max_loss=%s)", req.model_dump_json(), a.opens, a.max_loss)
            trade = self.ib.placeOrder(a.contract, a.order)
            await _wait_acknowledged(trade)
        view = order_view(trade)
        log.info("order %s is %s: %s", view.client_order_id, view.broker_status, view.message or "")
        return view

    def order(self, client_order_id: str) -> models.Order:
        self._require_connected()
        trade = self._find(client_order_id)
        if trade is None:
            raise NotFound(f"no order {client_order_id!r}")
        return order_view(trade)

    def cancel(self, client_order_id: str) -> models.Order:
        # Allowed on any account and under any limit: cancelling only reduces risk.
        self._require_connected()
        trade = self._find(client_order_id)
        if trade is None:
            raise NotFound(f"no order {client_order_id!r}")
        if not trade.isDone():
            log.info("cancelling %s", client_order_id)
            self.ib.cancelOrder(trade.order)
        return order_view(trade)

    def open_orders(self) -> list[models.Order]:
        self._require_connected()
        return [order_view(t) for t in self.ib.openTrades() if _parse_ref(t.order.orderRef)]

    async def _assess(self, req: models.OrderRequest) -> _Assessment:
        """Translate the order, decide whether it opens risk, and check the limits."""
        legs = await self._qualify_legs(req)
        symbols = [self._symbols[c.conId] for c in legs]
        held = {p.contract.conId: p.position for p in self.ib.positions(self.account)}
        working = {con_id for t in self.ib.openTrades() for con_id in _con_ids(t)}
        opens = not is_close(
            [(c.conId, leg.side, leg.ratio * req.quantity) for leg, c in zip(req.legs, legs)], held, working
        )
        today = datetime.now(timezone.utc).date()
        loss = refused = None
        if opens:
            expiries = {s.expiry for s in symbols}
            loss = math.inf
            if len(expiries) == 1:
                mult = float(legs[0].multiplier or 100)
                exposure = [
                    (s.right, float(s.strike), _sign(leg.side) * leg.ratio * req.quantity * mult)
                    for leg, s in zip(req.legs, symbols)
                ]
                loss = max_loss(exposure, float(req.limit_price) * req.quantity * mult)
            try:
                open_risk = await self._open_risk()
            except ValueError as e:
                raise Refused(f"could not work out the risk already open: {e}") from e
            refused = check_opening(
                self.limits,
                underlying=legs[0].symbol,
                expiries=expiries,
                today=today,
                order_loss=loss,
                open_risk=open_risk,
                opened_today=self._opened_on(today),
                daily_pnl=_num(self._pnl.dailyPnL) if self._pnl else None,
            )
        ref = f"{'o' if opens else 'c'}:{today:%Y%m%d}:{req.client_order_id}"
        contract, order = build_order(req, legs, self.account, ref)
        return _Assessment(contract, order, opens, loss, refused)

    async def _qualify_legs(self, req: models.OrderRequest) -> list[Contract]:
        legs = [Contract(secType="OPT", localSymbol=leg.symbol, exchange="SMART", currency="USD") for leg in req.legs]
        await self.ib.qualifyContractsAsync(*legs)
        unknown = [leg.symbol for leg, c in zip(req.legs, legs) if not c.conId]
        if unknown:
            raise Refused(f"no such option contracts: {unknown}")
        if len({c.symbol for c in legs}) != 1:
            raise Refused("all legs must be options on the same underlying")
        for c in legs:
            self._symbols[c.conId] = occ.parse(c.localSymbol)
        return legs

    async def _open_risk(self) -> float:
        """The worst loss of everything open: option positions, grouped by
        underlying and expiry, plus every working order that opens risk."""
        legs: dict[tuple[str, date], list[Exposure]] = defaultdict(list)
        cash: dict[tuple[str, date], float] = defaultdict(float)
        for p in self.ib.positions(self.account):
            c = p.contract
            if c.secType != "OPT" or not occ.is_option(c.localSymbol):
                continue
            s = occ.parse(c.localSymbol)
            key = (c.symbol, s.expiry)
            legs[key].append((s.right, float(s.strike), p.position * float(c.multiplier or 100)))
            cash[key] += p.position * p.avgCost  # avgCost is per contract, multiplier included
        risk = sum(max_loss(legs[k], cash[k]) for k in legs)
        for t in self.ib.openTrades():
            ref = _parse_ref(t.order.orderRef)
            if ref is not None and ref[0] == "o":
                risk += max_loss(*await self._exposure(t))
        return risk

    async def _exposure(self, trade: Trade) -> tuple[list[Exposure], float]:
        """What the unfilled part of a working order would add, and its cost."""
        side = 1.0 if trade.order.action == "BUY" else -1.0
        qty = trade.remaining()
        if trade.contract.secType == "BAG":
            mult = 100.0
            pairs = [
                (await self._symbol_for(leg.conId), (1.0 if leg.action == "BUY" else -1.0) * leg.ratio)
                for leg in trade.contract.comboLegs
            ]
        else:
            mult = float(trade.contract.multiplier or 100)
            pairs = [(occ.parse(trade.contract.localSymbol), 1.0)]
        legs = [(s.right, float(s.strike), side * ratio * qty * mult) for s, ratio in pairs]
        return legs, side * (_num(trade.order.lmtPrice) or 0.0) * qty * mult

    async def _symbol_for(self, con_id: int) -> occ.OptionSymbol:
        """The OCC symbol of a combo leg, which IBKR names only by contract id."""
        if con_id not in self._symbols:
            c = Contract(conId=con_id, exchange="SMART")
            await self.ib.qualifyContractsAsync(c)
            self._symbols[con_id] = occ.parse(c.localSymbol)  # ValueError if it did not resolve
        return self._symbols[con_id]

    def _opened_on(self, day: date) -> int:
        tag = f"{day:%Y%m%d}"
        return sum(1 for t in self.ib.trades() if (r := _parse_ref(t.order.orderRef)) and r[0] == "o" and r[1] == tag)

    def _find(self, client_order_id: str) -> Trade | None:
        # trades() holds this session's orders plus the open and completed ones
        # IB Gateway sends on connect, so a restart does not lose track of them.
        found = [t for t in self.ib.trades() if (r := _parse_ref(t.order.orderRef)) and r[2] == client_order_id]
        return found[-1] if found else None


def build_order(req: models.OrderRequest, legs: list[Contract], account: str, ref: str) -> tuple[Contract, LimitOrder]:
    """The one IBKR order that carries out the request.

    Several legs become a single combo (BAG) order. IBKR fills a combo's legs
    together or not at all, so a spread is never left half open, and prices it
    as the net of its legs, negative for a credit: the request's own sign
    convention. A single leg is a plain option order, which IBKR prices positive.
    """
    price = float(req.limit_price)
    common = {"tif": "DAY", "orderRef": ref, "account": account}
    if len(legs) == 1:
        return legs[0], LimitOrder(_action(req.legs[0].side), req.quantity, abs(price), **common)
    bag = Contract(
        secType="BAG",
        symbol=legs[0].symbol,
        exchange="SMART",
        currency=legs[0].currency,
        comboLegs=[
            ComboLeg(conId=c.conId, ratio=leg.ratio, action=_action(leg.side), exchange="SMART")
            for leg, c in zip(req.legs, legs)
        ],
    )
    return bag, LimitOrder("BUY", req.quantity, price, **common)


_DONE = {
    "Filled": models.OrderState.FILLED,
    "Cancelled": models.OrderState.CANCELLED,
    "ApiCancelled": models.OrderState.CANCELLED,
    "Inactive": models.OrderState.REJECTED,
}


def order_view(trade: Trade) -> models.Order:
    s = trade.orderStatus
    # IBKR prices a single sold option positive; report it as a credit.
    sign = -1.0 if trade.contract.secType != "BAG" and trade.order.action == "SELL" else 1.0
    limit = _num(trade.order.lmtPrice)
    perm_id = trade.order.permId or s.permId
    errors = [e.message for e in trade.log if e.errorCode]
    ref = _parse_ref(trade.order.orderRef)
    return models.Order(
        client_order_id=ref[2] if ref else trade.order.orderRef,
        broker_order_id=str(perm_id) if perm_id else None,
        state=_DONE.get(s.status, models.OrderState.WORKING),
        broker_status=s.status,
        quantity=float(trade.order.totalQuantity),
        filled=s.filled,
        limit_price=sign * limit if limit is not None else None,
        avg_fill_price=sign * s.avgFillPrice if s.filled else None,
        message=errors[-1] if errors else None,
    )


def _parse_ref(ref: str) -> tuple[str, str, str] | None:
    """An order's orderRef is "<o|c>:<YYYYMMDD>:<client_order_id>": whether it
    opened risk, the UTC day it was placed, and the caller's id. Kept in IBKR's
    own order, so the daily count and the idempotency survive a restart."""
    parts = ref.split(":", 2)
    if len(parts) == 3 and parts[0] in ("o", "c") and len(parts[1]) == 8 and parts[1].isdigit():
        return parts[0], parts[1], parts[2]
    return None


def _con_ids(trade: Trade) -> list[int]:
    if trade.contract.secType == "BAG":
        return [leg.conId for leg in trade.contract.comboLegs]
    return [trade.contract.conId]


async def _wait_acknowledged(trade: Trade, timeout: float = 5.0) -> None:
    """Give IBKR a moment to accept or reject the order, so the reply says which."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while trade.orderStatus.status in ("", "PendingSubmit", "ApiPending") and loop.time() < deadline:
        await asyncio.sleep(0.1)


def _action(side: models.Side) -> str:
    return "BUY" if side is models.Side.BUY else "SELL"


def _sign(side: models.Side) -> float:
    return 1.0 if side is models.Side.BUY else -1.0


def _contract_for(symbol: str) -> Contract:
    if occ.is_option(symbol):
        return Contract(secType="OPT", localSymbol=occ.normalize(symbol), exchange="SMART", currency="USD")
    return Stock(symbol.strip().upper(), "SMART", "USD")


def _symbol_of(c: Contract) -> str:
    if c.secType == "OPT" and occ.is_option(c.localSymbol):
        return occ.normalize(c.localSymbol)
    return c.symbol or c.localSymbol


def _ib_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or abs(f) >= _UNSET else f


def _price(v: object) -> float | None:
    f = _num(v)
    return f if f is not None and f >= 0 else None
