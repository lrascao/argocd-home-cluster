"""The broker adapter's two faces, over the same IBKRBroker.

- /mcp: the MCP server the Catalyst managed agents use, each through its own
  MCPServer resource and access grant.
- /v1: the same calls as plain REST, for people and scripts (curl, the first
  paper session in README.md).

Every call but /healthz needs a bearer token, and the token says who is
calling (strategies.py): a strategy's token (STRATEGY_TOKENS) is that
strategy, which sees and trades only its own orders and positions; the admin
token (BROKER_TOKEN) is the desk, which sees everything and, on REST, can act
as a strategy with ?strategy=<id>. Everything is async on purpose: ib_async is
not thread-safe, and FastAPI runs plain `def` routes on a thread pool.
"""

import asyncio
import contextlib
import logging
import os
from decimal import Decimal

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import limits, models, strategies
from .ibkr import Forbidden, IBKRBroker, NotFound, NotReady, Refused, Settings, View
from .ledger import Ledger
from .models import DESK

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

auth = strategies.Auth.from_env()
broker = IBKRBroker(
    Settings.from_env(),
    limits.from_env(),
    Ledger(os.getenv("LEDGER_PATH", "ledger.db")),
    os.getenv("STRATEGIES_DIR", ""),
)


def _request(client_order_id: str, legs: list[models.Leg], quantity: int, limit_price: float) -> models.OrderRequest:
    return models.OrderRequest(
        client_order_id=client_order_id, legs=legs, quantity=quantity, limit_price=Decimal(str(limit_price))
    )


# --- MCP: what the agents see. The docstrings are their manual. -------------


def _view(ctx: Context) -> View:
    """Who is calling, from the bearer token the MCPServer sent. The MCP
    server runs each call in a task of its own, so the HTTP request is read
    from the call's context, not from anything the middleware set."""
    request = ctx.request_context.request
    who = auth.identify(request.headers.get("authorization", "")) if request is not None else None
    if who is None:
        raise Forbidden("missing or wrong bearer token")
    return View(who, sees_all=who == DESK)


mcp = FastMCP(
    "broker",
    instructions=(
        "Options trading on an Interactive Brokers account shared by several strategies. "
        "Options are named by OCC symbols. You see and trade only your own strategy's orders and positions. "
        "Every order that opens risk must pass the account's hard limits, which get_status lists, "
        "and your strategy's, which get_strategy lists."
    ),
    # Stateless, plain JSON replies: no session to lose when the pod restarts.
    stateless_http=True,
    json_response=True,
    # FastMCP turns on a Host-header check when its host is loopback, which it
    # is by default, and every call through the Service would get 421. The
    # bearer token is the check here.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def get_status(ctx: Context) -> models.Health:
    """Whether the broker is connected to IBKR, whether the account is paper,
    whether orders are enabled, which strategy you are (caller), IBKR's
    EUR/USD rate, and the account's hard limits on orders that open risk,
    every strategy's together. Call this first in every run."""
    v = _view(ctx)
    return broker.health(v.owner)


@mcp.tool()
async def get_strategy(ctx: Context) -> models.StrategyStatus:
    """Your strategy: its rules, its own hard limits (budget, max loss per
    order, opening orders per day, days to expiry, which rights and how many
    legs an opening order has, kill switch), how much of its budget your
    positions and working opening orders use, and, if it may open nothing
    right now, why (blocked). Follow its rules."""
    v = _view(ctx)
    if v.owner == DESK:
        raise Refused("the desk is not a strategy: call list_strategies")
    return await broker.strategy_status(v.owner)


@mcp.tool()
async def list_strategies(ctx: Context) -> list[models.StrategyStatus]:
    """Every strategy trading the account, as get_strategy shows each one:
    its rules and limits, the risk it has open and what is left of its
    budget, its opening orders today, and whether it is blocked."""
    return await broker.strategy_statuses(_view(ctx))


@mcp.tool()
async def get_account() -> models.Account:
    """Net liquidation value, available funds and buying power, in the
    account's base currency."""
    return await broker.account_summary()


@mcp.tool()
async def get_positions(ctx: Context) -> list[models.Position]:
    """Your positions, by OCC symbol. quantity is negative for a short
    position; avg_price is what it was opened at, per share: positive for
    each leg, whether bought or sold. Other strategies' positions in the same
    account are not shown. To the desk, every strategy's, each with the
    strategy that holds it; strategy null is what IBKR holds that no strategy
    accounts for."""
    return await broker.positions(_view(ctx))


@mcp.tool()
async def get_quotes(symbols: list[str]) -> list[models.Quote]:
    """Bid, ask, last and close, and greeks for options, for 1 to 50 symbols:
    stock tickers or OCC option symbols. Quotes are delayed. A symbol that
    does not exist comes back with error "no such contract". The answer comes
    within a few seconds with what has arrived by then: an option quoted for
    the first time can come back without its delta, or with error "no quote
    yet", and asking for it again a few seconds later fills it in. Never
    trade a symbol you have not quoted."""
    if not 1 <= len(symbols) <= 50:
        raise Refused("ask for between 1 and 50 symbols")
    return await broker.quotes(symbols)


@mcp.tool()
async def get_option_chain(underlying: str) -> list[models.Chain]:
    """The expirations and strikes listed for a US stock or ETF, under its
    standard OCC root. Not every strike exists at every expiration: build the
    symbol, then quote it to confirm. An OCC symbol is the root padded to 6 characters, the
    expiry as YYMMDD, C or P, and the strike times 1000 in 8 digits:
    "SPY   261218P00450000" is the SPY 450 put expiring 2026-12-18."""
    return await broker.chains(underlying)


@mcp.tool()
async def preview_order(
    client_order_id: str, legs: list[models.Leg], quantity: int, limit_price: float, ctx: Context
) -> models.OrderPreview:
    """What place_order would do with the same arguments, without placing
    anything: whether the order opens risk or only closes your positions,
    its maximum loss in USD and in EUR, the margin and commission IBKR
    expects, and, if the hard limits would refuse it, why."""
    return await broker.preview(_view(ctx), _request(client_order_id, legs, quantity, limit_price))


@mcp.tool()
async def place_order(
    client_order_id: str, legs: list[models.Leg], quantity: int, limit_price: float, ctx: Context
) -> models.Order:
    """Place a limit order, good for the day.

    legs: 1 to 4 options on one underlying, each {symbol, side, ratio}, with
    symbol an OCC symbol and side "buy" or "sell". Several legs go as one
    order that fills completely or not at all.
    quantity: how many units of the legs, 1 to 100.
    limit_price: the net price per share for one unit, at most 2 decimals.
    Positive pays a debit, negative receives a credit.
    client_order_id: new for each order, up to 32 of A-Z a-z 0-9 _ . : -.
    Sending an id already used returns that order instead of placing another,
    so if a reply is lost, call get_order with the same id; never retry
    under a new id.

    An order is a close when every leg reduces a position you hold; a close
    is always allowed. A contract belongs to one strategy at a time, so an
    opening order on a contract another strategy holds or has an order
    working on is refused.
    """
    return await broker.place(_view(ctx), _request(client_order_id, legs, quantity, limit_price))


@mcp.tool()
async def get_order(client_order_id: str, ctx: Context) -> models.Order:
    """One of your orders by the client_order_id it was placed with: its
    state (working, filled, cancelled, rejected), fills, and IBKR's last
    message."""
    return broker.order(_view(ctx), client_order_id)


@mcp.tool()
async def list_open_orders(ctx: Context) -> list[models.Order]:
    """Your orders still working, including ones placed in earlier runs. To
    the desk, every strategy's."""
    return broker.open_orders(_view(ctx))


@mcp.tool()
async def cancel_order(client_order_id: str, ctx: Context) -> models.Order:
    """Cancel one of your working orders. Always allowed."""
    return broker.cancel(_view(ctx), client_order_id)


mcp_app = mcp.streamable_http_app()  # creates mcp.session_manager, run in lifespan


# --- The FastAPI app: auth, REST, and the MCP app mounted last. -------------


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(broker.run())
    try:
        # A mounted app's own lifespan never runs, so start MCP's here.
        async with mcp.session_manager.run():
            yield
    finally:
        task.cancel()
        broker.ib.disconnect()


app = FastAPI(title="broker", version="1", lifespan=lifespan)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if request.url.path != "/healthz" and auth.identify(request.headers.get("authorization", "")) is None:
        return JSONResponse({"detail": "missing or wrong bearer token"}, status_code=401)
    return await call_next(request)


_STATUS = {NotReady: 503, Refused: 422, NotFound: 404, Forbidden: 403}


async def _error(_: Request, e: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=_STATUS[type(e)])


for _exc in _STATUS:
    app.add_exception_handler(_exc, _error)


@app.get("/healthz")
async def healthz() -> dict:
    """The process is up. Whether IB Gateway is connected is /v1/health."""
    return {"ok": True}


def _caller(
    request: Request,
    strategy: str | None = Query(None, description="the desk's token only: act as this strategy, or as desk"),
) -> View:
    who = auth.identify(request.headers.get("authorization", ""))
    if who is None:
        raise Forbidden("missing or wrong bearer token")
    if strategy is None:
        return View(who, sees_all=who == DESK)
    if strategy == who:
        return View(who)
    if who != DESK:
        raise Forbidden("a strategy's token acts only as that strategy")
    if strategy != DESK and not strategies.valid_id(strategy):
        raise Refused(f"{strategy!r} is not a strategy id")
    return View(strategy)


def _desk(v: View = Depends(_caller)) -> View:
    if not v.sees_all:
        raise Forbidden("only the desk's token, acting as no strategy, may do this")
    return v


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
async def health(v: View = Depends(_caller)) -> models.Health:
    return broker.health(v.owner)


@v1.get("/strategies")
async def strategies_(v: View = Depends(_caller)) -> list[models.StrategyStatus]:
    """Every strategy for the desk; the caller's own for a strategy."""
    return await broker.strategy_statuses(v)


@v1.get("/ledger")
async def ledger_(_: View = Depends(_desk)) -> models.LedgerReport:
    """What each owner holds by the ledger, what IBKR holds that it does not
    account for, and which strategies that blocks."""
    return await broker.ledger_report()


@v1.post("/ledger/adjustments")
async def adjust(adj: models.Adjustment, _: View = Depends(_desk)) -> models.LedgerReport:
    """Record a fill by hand so the ledger matches IBKR: give a strategy a
    position IBKR holds that nobody owns, or take away one IBKR no longer
    holds. quantity is in contracts, positive to add a long or take off a
    short."""
    return await broker.adjust(adj)


@v1.get("/account")
async def account() -> models.Account:
    return await broker.account_summary()


@v1.get("/positions")
async def positions(v: View = Depends(_caller)) -> list[models.Position]:
    return await broker.positions(v)


@v1.get("/quotes")
async def quotes(symbol: list[str] = Query()) -> list[models.Quote]:
    """Quotes for stocks (ticker) and options (OCC symbol): ?symbol=SPY&symbol=SPY 261218P00450000"""
    if not 1 <= len(symbol) <= 50:
        raise Refused("ask for between 1 and 50 symbols")
    return await broker.quotes(symbol)


@v1.get("/chains/{underlying}")
async def chains(underlying: str) -> list[models.Chain]:
    return await broker.chains(underlying)


@v1.post("/orders/preview")
async def preview(req: models.OrderRequest, v: View = Depends(_caller)) -> models.OrderPreview:
    """What the order would do, without placing it: opens or closes, max loss,
    margin, commission, and why the limits would refuse it."""
    return await broker.preview(v, req)


@v1.post("/orders")
async def place(req: models.OrderRequest, v: View = Depends(_caller)) -> models.Order:
    return await broker.place(v, req)


@v1.get("/orders")
async def open_orders(v: View = Depends(_caller)) -> list[models.Order]:
    return broker.open_orders(v)


@v1.get("/orders/{client_order_id}")
async def get_order_rest(client_order_id: str, v: View = Depends(_caller)) -> models.Order:
    return broker.order(v, client_order_id)


@v1.delete("/orders/{client_order_id}")
async def cancel(client_order_id: str, v: View = Depends(_caller)) -> models.Order:
    return broker.cancel(v, client_order_id)


app.include_router(v1)
# Last, at the root: FastAPI's own routes above match first, and the MCP app
# serves its one route, /mcp.
app.mount("/", mcp_app)
