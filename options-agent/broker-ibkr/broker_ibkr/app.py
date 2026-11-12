"""The broker adapter's two faces, over the same IBKRBroker.

- /mcp: the MCP server a Catalyst managed agent uses, through an MCPServer
  resource and an access grant.
- /v1: the same calls as plain REST, for people and scripts (curl, the first
  paper session in README.md).

Both need `Authorization: Bearer $BROKER_TOKEN` when BROKER_TOKEN is set;
/healthz never does. Everything is async on purpose: ib_async is not
thread-safe, and FastAPI runs plain `def` routes on a thread pool.
"""

import asyncio
import contextlib
import hmac
import logging
import os
from decimal import Decimal

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import limits, models
from .ibkr import IBKRBroker, NotFound, NotReady, Refused, Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

broker = IBKRBroker(Settings.from_env(), limits.from_env())

_TOKEN = os.getenv("BROKER_TOKEN", "")


def _request(client_order_id: str, legs: list[models.Leg], quantity: int, limit_price: float) -> models.OrderRequest:
    return models.OrderRequest(
        client_order_id=client_order_id, legs=legs, quantity=quantity, limit_price=Decimal(str(limit_price))
    )


# --- MCP: what the agent sees. The docstrings are its manual. ---------------

mcp = FastMCP(
    "broker",
    instructions=(
        "Options trading on an Interactive Brokers account. Options are named by OCC symbols. "
        "Every order that opens risk must pass hard limits that get_status lists."
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
async def get_status() -> models.Health:
    """Whether the broker is connected to IBKR, whether the account is paper,
    whether orders are enabled, and the hard limits on orders that open risk.
    Call this first in every run."""
    return broker.health()


@mcp.tool()
async def get_account() -> models.Account:
    """Net liquidation value, available funds and buying power, in the
    account's base currency."""
    return await broker.account_summary()


@mcp.tool()
async def get_positions() -> list[models.Position]:
    """Every position held: options by OCC symbol, stocks by ticker. quantity
    is negative for a short position; avg_price is per share."""
    return broker.positions()


@mcp.tool()
async def get_quotes(symbols: list[str]) -> list[models.Quote]:
    """Bid, ask, last and close, and greeks for options, for 1 to 50 symbols:
    stock tickers or OCC option symbols. A symbol that does not exist comes
    back with error "no such contract". Never trade a symbol you have not
    quoted."""
    if not 1 <= len(symbols) <= 50:
        raise Refused("ask for between 1 and 50 symbols")
    return await broker.quotes(symbols)


@mcp.tool()
async def get_option_chain(underlying: str) -> list[models.Chain]:
    """The OCC roots, expirations and strikes listed for a US stock or ETF.
    Not every strike exists at every expiration: build the symbol, then quote
    it to confirm. An OCC symbol is the root padded to 6 characters, the
    expiry as YYMMDD, C or P, and the strike times 1000 in 8 digits:
    "SPY   261218P00450000" is the SPY 450 put expiring 2026-12-18."""
    return await broker.chains(underlying)


@mcp.tool()
async def preview_order(
    client_order_id: str, legs: list[models.Leg], quantity: int, limit_price: float
) -> models.OrderPreview:
    """What place_order would do with the same arguments, without placing
    anything: whether the order opens risk or only closes positions, its
    maximum loss in USD, the margin and commission IBKR expects, and, if the
    hard limits would refuse it, why."""
    return await broker.preview(_request(client_order_id, legs, quantity, limit_price))


@mcp.tool()
async def place_order(client_order_id: str, legs: list[models.Leg], quantity: int, limit_price: float) -> models.Order:
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
    """
    return await broker.place(_request(client_order_id, legs, quantity, limit_price))


@mcp.tool()
async def get_order(client_order_id: str) -> models.Order:
    """One order by the client_order_id it was placed with: its state
    (working, filled, cancelled, rejected), fills, and IBKR's last message."""
    return broker.order(client_order_id)


@mcp.tool()
async def list_open_orders() -> list[models.Order]:
    """Every order still working, including ones placed in earlier runs."""
    return broker.open_orders()


@mcp.tool()
async def cancel_order(client_order_id: str) -> models.Order:
    """Cancel a working order. Always allowed."""
    return broker.cancel(client_order_id)


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
    if _TOKEN and request.url.path != "/healthz":
        given = request.headers.get("authorization", "")
        if not hmac.compare_digest(given.encode(), f"Bearer {_TOKEN}".encode()):
            return JSONResponse({"detail": "missing or wrong bearer token"}, status_code=401)
    return await call_next(request)


_STATUS = {NotReady: 503, Refused: 422, NotFound: 404}


async def _error(_: Request, e: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(e)}, status_code=_STATUS[type(e)])


for _exc in _STATUS:
    app.add_exception_handler(_exc, _error)


@app.get("/healthz")
async def healthz() -> dict:
    """The process is up. Whether IB Gateway is connected is /v1/health."""
    return {"ok": True}


v1 = APIRouter(prefix="/v1")


@v1.get("/health")
async def health() -> models.Health:
    return broker.health()


@v1.get("/account")
async def account() -> models.Account:
    return await broker.account_summary()


@v1.get("/positions")
async def positions() -> list[models.Position]:
    return broker.positions()


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
async def preview(req: models.OrderRequest) -> models.OrderPreview:
    """What the order would do, without placing it: opens or closes, max loss,
    margin, commission, and why the limits would refuse it."""
    return await broker.preview(req)


@v1.post("/orders")
async def place(req: models.OrderRequest) -> models.Order:
    return await broker.place(req)


@v1.get("/orders")
async def open_orders() -> list[models.Order]:
    return broker.open_orders()


@v1.get("/orders/{client_order_id}")
async def get_order_rest(client_order_id: str) -> models.Order:
    return broker.order(client_order_id)


@v1.delete("/orders/{client_order_id}")
async def cancel(client_order_id: str) -> models.Order:
    return broker.cancel(client_order_id)


app.include_router(v1)
# Last, at the root: FastAPI's own routes above match first, and the MCP app
# serves its one route, /mcp.
app.mount("/", mcp_app)
