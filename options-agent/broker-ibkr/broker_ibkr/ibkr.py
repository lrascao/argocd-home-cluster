"""IBKR implementation of the broker API, over IB Gateway's socket API.

Talks to an IB Gateway (or TWS) running next to it. Places orders only on a
paper account unless IBKR_ALLOW_LIVE=true; IBKR paper account ids start "DU".
Every order that opens risk must also pass the hard limits in limits.py: the
account's, and those of the strategy placing it.

Several strategies share the one account. Every order carries its owner in
its orderRef, the ledger (ledger.py) records whose fills are whose, and each
caller sees only its own orders and positions (View).
"""

import asyncio
import logging
import math
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import NamedTuple

from ib_async import IB, ComboLeg, Contract, LimitOrder, Stock, Trade

from . import ledger, models, occ, strategies
from .limits import Exposure, check_opening, check_strategy, conflict, eur_per, is_close, max_loss, positions_risk
from .models import DESK

log = logging.getLogger(__name__)

# IBKR reports "no value" as the largest double, and "no quote" as -1.
_UNSET = 1e300


class NotReady(Exception):
    """IB Gateway is not connected."""


class Refused(Exception):
    """The request is well formed but cannot be carried out."""


class NotFound(Exception):
    pass


class Forbidden(Exception):
    """The caller may not see or do this."""


@dataclass(frozen=True)
class View:
    """Who is asking. owner is who orders are placed as: a strategy's id, or
    DESK. A strategy sees only its own orders and positions; the desk sees
    everyone's, unless it names a strategy to act as, and then sees only that
    strategy's."""

    owner: str
    sees_all: bool = False


class Ref(NamedTuple):
    kind: str  # "o" if the order opened risk, "c" if it only closed
    day: str  # YYYYMMDD, the UTC day it was placed
    owner: str
    client_order_id: str


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
    def __init__(
        self, settings: Settings, limits: models.Limits | None, book: ledger.Ledger, strategies_dir: str = ""
    ) -> None:
        self.settings = settings
        self.limits = limits
        self.book = book
        self.strategies_dir = strategies_dir
        self.ib = IB()
        self.account = ""
        self._pnl = None  # IBKR's live P&L for the account, once subscribed
        self._symbols: dict[int, occ.OptionSymbol] = {}  # contract id -> OCC symbol
        self._recorded: set[str] = set()  # execution ids already offered to the ledger
        self._place_lock = asyncio.Lock()

    @property
    def paper(self) -> bool:
        return self.account.startswith("DU")

    @property
    def orders_enabled(self) -> bool:
        return self.ib.isConnected() and (self.paper or self.settings.allow_live)

    async def run(self) -> None:
        """Stay connected to IB Gateway, which restarts once a day, and keep
        the ledger up with the fills IBKR reports."""
        while True:
            if not self.ib.isConnected():
                try:
                    await self._connect()
                except Exception as e:  # any failure: log it and try again shortly
                    log.warning("connecting to IB Gateway at %s:%s failed: %s", self.settings.host, self.settings.port, e)
            elif self.account:
                try:
                    await self._sync_fills()
                except Exception:
                    log.exception("recording fills in the ledger failed")
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

    def health(self, caller: str) -> models.Health:
        connected = self.ib.isConnected()
        return models.Health(
            connected=connected,
            account_id=self.account or None,
            paper=self.paper if self.account else None,
            orders_enabled=self.orders_enabled,
            caller=caller,
            eur_per_usd=self._eur_per("USD") if connected and self.account else None,
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

    async def positions(self, view: View) -> list[models.Position]:
        """What the view's owner holds, by the ledger. The desk also sees what
        IBKR holds that the ledger does not account for (strategy None)."""
        self._require_connected()
        await self._sync_fills()
        held, rec = self._state()
        out = [_position_of(h) for h in held if view.sees_all or h.owner == view.owner]
        if view.sees_all or view.owner == DESK:
            out += self._unattributed(held, rec)
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

    async def preview(self, view: View, req: models.OrderRequest) -> models.OrderPreview:
        self._require_connected()
        a = await self._assess(view.owner, req)
        state = await self.ib.whatIfOrderAsync(a.contract, a.order)
        loss = a.max_loss if a.max_loss is not None and not math.isinf(a.max_loss) else None
        rate = self._eur_per("USD")
        return models.OrderPreview(
            opens=a.opens,
            max_loss=loss,
            max_loss_eur=loss * rate if loss is not None and rate is not None else None,
            refused=a.refused,
            initial_margin_change=_num(state.initMarginChange),
            maintenance_margin_change=_num(state.maintMarginChange),
            commission_min=_num(state.minCommission),
            commission_max=_num(state.maxCommission),
            commission_currency=state.commissionCurrency or None,
            warning=state.warningText or None,
        )

    async def place(self, view: View, req: models.OrderRequest) -> models.Order:
        self._require_connected()
        if not self.orders_enabled:
            raise Refused(f"account {self.account} is not a paper account; set IBKR_ALLOW_LIVE=true to trade it")
        async with self._place_lock:
            # The agent retries a call whose reply it never saw. The same
            # client_order_id returns the order already placed instead of
            # placing it twice. Ids are the caller's own: two strategies may
            # use the same one.
            existing = self._find(req.client_order_id, view.owner)
            if existing is not None:
                return order_view(existing)
            a = await self._assess(view.owner, req)
            if a.refused:
                log.info("refused %s's %s: %s", view.owner, req.client_order_id, a.refused)
                raise Refused(a.refused)
            log.info("%s placing %s (opens=%s, max_loss=%s)", view.owner, req.model_dump_json(), a.opens, a.max_loss)
            trade = self.ib.placeOrder(a.contract, a.order)
            await _wait_acknowledged(trade)
        placed = order_view(trade)
        log.info("order %s is %s: %s", a.order.orderRef, placed.broker_status, placed.message or "")
        return placed

    def order(self, view: View, client_order_id: str) -> models.Order:
        self._require_connected()
        trade = self._find(client_order_id, None if view.sees_all else view.owner)
        if trade is None:
            raise NotFound(f"no order {client_order_id!r}")
        return order_view(trade)

    def cancel(self, view: View, client_order_id: str) -> models.Order:
        # Allowed on any account and under any limit: cancelling only reduces risk.
        self._require_connected()
        trade = self._find(client_order_id, None if view.sees_all else view.owner)
        if trade is None:
            raise NotFound(f"no order {client_order_id!r}")
        if not trade.isDone():
            log.info("%s cancelling %s", view.owner, client_order_id)
            self.ib.cancelOrder(trade.order)
        return order_view(trade)

    def open_orders(self, view: View) -> list[models.Order]:
        self._require_connected()
        return [
            order_view(t)
            for t in self.ib.openTrades()
            if (r := _parse_ref(t.order.orderRef)) and (view.sees_all or r.owner == view.owner)
        ]

    async def strategy_status(self, owner: str) -> models.StrategyStatus:
        """A strategy's file, and how much of its limits it is using."""
        self._require_connected()
        await self._sync_fills()
        held, rec = self._state()
        loaded, errors = strategies.load(self.strategies_dir)
        s = loaded.get(owner)
        rate = self._eur_per("USD")
        try:
            risk = await self._owner_risk(owner, held)
        except ValueError as e:
            raise Refused(f"could not work out the risk already open: {e}") from e
        risk_eur = risk * rate if rate is not None and not math.isinf(risk) else None
        if s is None:
            error = errors.get(owner) or f"no file strategies/{owner}.toml"
            blocked = f"it has no strategy file that loads: {error}"
        else:
            error = None
            blocked = "its kill switch is on" if s.kill_switch else rec.blocking(owner)
        return models.StrategyStatus(
            id=owner,
            strategy=s,
            error=error,
            open_risk_eur=risk_eur,
            budget_left_eur=s.budget_eur - risk_eur if s is not None and risk_eur is not None else None,
            opening_orders_today=self._opened_on(_today(), owner),
            blocked=blocked,
        )

    async def strategy_statuses(self, view: View) -> list[models.StrategyStatus]:
        """Every strategy in strategies/ for the desk; a strategy's own otherwise."""
        if not view.sees_all:
            return [await self.strategy_status(view.owner)]
        loaded, errors = strategies.load(self.strategies_dir)
        return [await self.strategy_status(sid) for sid in sorted(loaded.keys() | errors.keys())]

    async def ledger_report(self) -> models.LedgerReport:
        self._require_connected()
        await self._sync_fills()
        held, rec = self._state()
        return models.LedgerReport(
            holdings=[_position_of(h) for h in held],
            unattributed=self._unattributed(held, rec),
            blocked=rec.blocked,
            everyone_blocked=rec.everyone,
        )

    async def adjust(self, adj: models.Adjustment) -> models.LedgerReport:
        """Record a fill by hand, so the ledger matches IBKR again."""
        self._require_connected()
        c = Contract(secType="OPT", localSymbol=adj.symbol, exchange="SMART", currency="USD")
        await self.ib.qualifyContractsAsync(c)
        if not c.conId:
            raise NotFound(f"no such option contract {adj.symbol!r}")
        fill = ledger.Fill(
            exec_id=f"adjustment:{uuid.uuid4().hex}",
            time=datetime.now(timezone.utc),
            owner=adj.strategy,
            con_id=c.conId,
            symbol=adj.symbol,
            quantity=adj.quantity,
            price=adj.price,
            multiplier=float(c.multiplier or 100),
            client_order_id="adjustment",
        )
        self.book.add([fill])
        log.info("ledger adjustment: %s %+g %s at %.2f", fill.owner, fill.quantity, fill.symbol, fill.price)
        return await self.ledger_report()

    async def _assess(self, owner: str, req: models.OrderRequest) -> _Assessment:
        """Translate the order, decide whether it opens risk, and check the limits."""
        legs = await self._qualify_legs(req)
        symbols = [self._symbols[c.conId] for c in legs]
        await self._sync_fills()
        held, rec = self._state()
        account = {p.contract.conId: p.position for p in self.ib.positions(self.account)}
        # What the owner holds. The desk also owns what nobody else does.
        mine: dict[int, float] = defaultdict(float, rec.unattributed if owner == DESK else {})
        for h in held:
            if h.owner == owner:
                mine[h.con_id] += h.quantity
        trades = [(t, _parse_ref(t.order.orderRef)) for t in self.ib.openTrades()]
        working = {con_id for t, _ in trades for con_id in _con_ids(t)}
        # A close must reduce what the owner holds and what the account holds:
        # a strategy buying back a short that IBKR no longer has would open a
        # long, whatever the ledger says.
        order_legs = [(c.conId, leg.side, leg.ratio * req.quantity) for leg, c in zip(req.legs, legs)]
        opens = not (is_close(order_legs, account, working) and is_close(order_legs, mine, working))
        today = _today()
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
            rate = self._eur_per("USD")
            try:
                open_risk = await self._open_risk()
                own_risk = await self._owner_risk(owner, held) if owner != DESK else 0.0
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
                daily_pnl=self._daily_pnl_eur(),
                eur_per_usd=rate,
            )
            if refused is None:
                holders: dict[int, set[str]] = defaultdict(set)
                for h in held:
                    holders[h.con_id].add(h.owner)
                busy: dict[int, set[str]] = defaultdict(set)
                for t, r in trades:
                    if r is not None:
                        for con_id in _con_ids(t):
                            busy[con_id].add(r.owner)
                contracts = {c.conId: str(s) for c, s in zip(legs, symbols)}
                refused = conflict(owner, contracts, holders, busy, rec.unattributed)
            if refused is None and owner != DESK:
                loaded, errors = strategies.load(self.strategies_dir)
                refused = check_strategy(
                    loaded.get(owner),
                    error=errors.get(owner),
                    blocked=rec.blocking(owner),
                    rights={s.right for s in symbols},
                    legs=len(legs),
                    expiries=expiries,
                    today=today,
                    order_loss=loss,
                    open_risk=own_risk,
                    opened_today=self._opened_on(today, owner),
                    eur_per_usd=rate,
                )
        ref = make_ref("o" if opens else "c", owner, today, req.client_order_id)
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
        """The worst loss of everything open on the account, in dollars: IBKR's
        option positions, plus every working order that opens risk."""
        rows = []
        for p in self.ib.positions(self.account):
            c = p.contract
            if c.secType != "OPT" or not occ.is_option(c.localSymbol):
                continue
            # avgCost is per contract, multiplier included
            rows.append((occ.parse(c.localSymbol), p.position * float(c.multiplier or 100), p.position * p.avgCost))
        return positions_risk(rows) + await self._working_risk(None)

    async def _owner_risk(self, owner: str, held: list[ledger.Holding]) -> float:
        """The same for one owner: its positions in the ledger, at what they
        cost, plus its working orders that open risk."""
        rows = [
            (occ.parse(h.symbol), h.quantity * h.multiplier, h.quantity * h.avg_price * h.multiplier)
            for h in held
            if h.owner == owner
        ]
        return positions_risk(rows) + await self._working_risk(owner)

    async def _working_risk(self, owner: str | None) -> float:
        risk = 0.0
        for t in self.ib.openTrades():
            r = _parse_ref(t.order.orderRef)
            if r is not None and r.kind == "o" and (owner is None or r.owner == owner):
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

    def _opened_on(self, day: date, owner: str | None = None) -> int:
        """Opening orders placed on a UTC day: the account's, or one owner's."""
        tag = f"{day:%Y%m%d}"
        return sum(
            1
            for t in self.ib.trades()
            if (r := _parse_ref(t.order.orderRef)) and r.kind == "o" and r.day == tag and owner in (None, r.owner)
        )

    def _find(self, client_order_id: str, owner: str | None) -> Trade | None:
        """The order placed under client_order_id by owner, or by anyone if
        owner is None, which is refused if two owners used that id."""
        # trades() holds this session's orders plus the open and completed ones
        # IB Gateway sends on connect, so a restart does not lose track of them.
        found = [
            (t, r)
            for t in self.ib.trades()
            if (r := _parse_ref(t.order.orderRef))
            and r.client_order_id == client_order_id
            and owner in (None, r.owner)
        ]
        owners = sorted({r.owner for _, r in found})
        if len(owners) > 1:
            raise Refused(f"{', '.join(owners)} each have an order {client_order_id!r}; name one with ?strategy=")
        return found[-1][0] if found else None

    async def _sync_fills(self) -> None:
        """Record in the ledger every option fill of the adapter's orders that
        IBKR has reported and the ledger does not have yet. IBKR reports the
        day's fills on connect and each new one as it happens."""
        new = []
        for f in self.ib.fills():
            e, c = f.execution, f.contract
            # A combo reports each leg's fill, and may report one for the
            # combo itself, at its net price: the legs are what is held.
            if e.execId in self._recorded or c.secType != "OPT" or (e.acctNumber and e.acctNumber != self.account):
                continue
            ref = _parse_ref(e.orderRef) or _parse_ref(self._order_ref(e.permId))
            if ref is None:  # not the adapter's order: a trade made in IBKR's own app
                self._recorded.add(e.execId)
                continue
            try:
                if occ.is_option(c.localSymbol):
                    symbol = occ.normalize(c.localSymbol)
                else:
                    symbol = str(await self._symbol_for(c.conId))
            except ValueError as err:
                log.warning("fill %s: cannot tell which option it is: %s", e.execId, err)
                continue
            new.append(
                ledger.Fill(
                    exec_id=e.execId,
                    time=e.time,
                    owner=ref.owner,
                    con_id=c.conId,
                    symbol=symbol,
                    quantity=(1.0 if e.side == "BOT" else -1.0) * float(e.shares),
                    price=float(e.price),
                    multiplier=float(c.multiplier or 100),
                    client_order_id=ref.client_order_id,
                )
            )
        for fill in self.book.add(new):
            log.info(
                "ledger: %s %+g %s at %.2f (order %s)",
                fill.owner,
                fill.quantity,
                fill.symbol,
                fill.price,
                fill.client_order_id,
            )
        self._recorded.update(f.exec_id for f in new)

    def _order_ref(self, perm_id: int) -> str:
        if perm_id:
            for t in self.ib.trades():
                if t.order.permId == perm_id:
                    return t.order.orderRef
        return ""

    def _state(self) -> tuple[list[ledger.Holding], ledger.Reconciliation]:
        """What each owner holds by the ledger, and how that compares with IBKR."""
        today = _today()
        held = ledger.holdings(self.book.all(), today)
        ibkr, live = {}, {}
        for p in self.ib.positions(self.account):
            c = p.contract
            ibkr[c.conId] = p.position
            if c.secType == "OPT" and occ.is_option(c.localSymbol) and occ.parse(c.localSymbol).expiry >= today:
                live[c.conId] = occ.normalize(c.localSymbol)
        return held, ledger.reconcile(held, ibkr, live)

    def _unattributed(self, held: list[ledger.Holding], rec: ledger.Reconciliation) -> list[models.Position]:
        ibkr = {p.contract.conId: p for p in self.ib.positions(self.account)}
        symbols = {h.con_id: h for h in held}
        out = []
        for con_id, qty in rec.unattributed.items():
            if (p := ibkr.get(con_id)) is not None:
                mult = int(float(p.contract.multiplier or 1))
                symbol, sec_type, avg = _symbol_of(p.contract), p.contract.secType, p.avgCost / mult
            else:  # the ledger has it and IBKR does not
                h = symbols[con_id]
                mult, symbol, sec_type, avg = int(h.multiplier), h.symbol, "OPT", None
            out.append(
                models.Position(
                    symbol=symbol, sec_type=sec_type, quantity=qty, avg_price=avg, multiplier=mult, strategy=None
                )
            )
        return out

    def _base_currency(self) -> str:
        for v in self.ib.accountValues(self.account):
            if v.tag == "NetLiquidation" and v.currency not in ("", "BASE"):
                return v.currency
        return ""

    def _eur_per(self, currency: str) -> float | None:
        """Euros per unit of currency, from IBKR's ExchangeRate account values,
        which IBKR refreshes every few minutes."""
        rates = {}
        for v in self.ib.accountValues(self.account):
            if v.tag == "ExchangeRate" and (r := _num(v.value)):
                rates[v.currency] = r
        if base := self._base_currency():
            rates[base] = 1.0
        return eur_per(rates, currency)

    def _daily_pnl_eur(self) -> float | None:
        """IBKR's P&L for the day, which is in the account's base currency."""
        pnl = _num(self._pnl.dailyPnL) if self._pnl else None
        base = self._base_currency()
        rate = self._eur_per(base) if base else None
        return pnl * rate if pnl is not None and rate is not None else None


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


def _position_of(h: ledger.Holding) -> models.Position:
    return models.Position(
        symbol=h.symbol,
        sec_type="OPT",
        quantity=h.quantity,
        avg_price=h.avg_price,
        multiplier=int(h.multiplier),
        strategy=h.owner,
    )


def order_view(trade: Trade) -> models.Order:
    s = trade.orderStatus
    # IBKR prices a single sold option positive; report it as a credit.
    sign = -1.0 if trade.contract.secType != "BAG" and trade.order.action == "SELL" else 1.0
    limit = _num(trade.order.lmtPrice)
    perm_id = trade.order.permId or s.permId
    errors = [e.message for e in trade.log if e.errorCode]
    ref = _parse_ref(trade.order.orderRef)
    return models.Order(
        client_order_id=ref.client_order_id if ref else trade.order.orderRef,
        strategy=ref.owner if ref else DESK,
        broker_order_id=str(perm_id) if perm_id else None,
        state=_DONE.get(s.status, models.OrderState.WORKING),
        broker_status=s.status,
        quantity=float(trade.order.totalQuantity),
        filled=s.filled,
        limit_price=sign * limit if limit is not None else None,
        avg_fill_price=sign * s.avgFillPrice if s.filled else None,
        message=errors[-1] if errors else None,
    )


def make_ref(kind: str, owner: str, day: date, client_order_id: str) -> str:
    """An order's orderRef: "<o|c>[.<strategy>]:<YYYYMMDD>:<client_order_id>".
    Whether it opened risk, the strategy that placed it (none for the desk),
    the UTC day it was placed, and the caller's id. Kept in IBKR's own order,
    so the daily counts, the idempotency and who placed what survive a
    restart. Neither a strategy id nor the day holds a colon or a dot."""
    who = "" if owner == DESK else f".{owner}"
    return f"{kind}{who}:{day:%Y%m%d}:{client_order_id}"


def _parse_ref(ref: str) -> Ref | None:
    """make_ref read back; None for an order the adapter did not place."""
    parts = ref.split(":", 2)
    if len(parts) != 3:
        return None
    kind, dot, owner = parts[0].partition(".")
    if dot and not strategies.valid_id(owner):
        return None
    if kind in ("o", "c") and len(parts[1]) == 8 and parts[1].isdigit():
        return Ref(kind, parts[1], owner if dot else DESK, parts[2])
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


def _today() -> date:
    return datetime.now(timezone.utc).date()


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
