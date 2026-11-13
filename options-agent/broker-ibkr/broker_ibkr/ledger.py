"""Which strategy holds what.

IBKR keeps one set of positions for the account and knows nothing of
strategies. The adapter tags every order with the strategy that placed it, in
the order's orderRef, and this ledger records every option fill of those
orders. What a strategy holds is the sum of its own fills.

The ledger is checked against IBKR on every order that opens risk. They
disagree when IBKR moves a position without a fill the adapter sees: an early
assignment, a trade made in IBKR's own app, a fill while the adapter was down
that IBKR no longer reports when it comes back (it reports the current day's),
or a lost ledger. Where they disagree on a contract a strategy holds, that
strategy opens nothing. Where IBKR holds a live option no one in the ledger
does, no strategy opens anything: it may be one whose fills were missed, and
it would then be holding positions it does not know about. Either lasts until
someone makes the two agree, with an adjustment (POST /v1/ledger/adjustments).
The desk is never blocked.

SQLite, on a small volume of its own: the adapter is the only writer, and one
pod at most runs it.
"""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import occ
from .models import DESK


@dataclass(frozen=True)
class Fill:
    exec_id: str  # IBKR's execution id, or "adjustment:..." for one entered by hand
    time: datetime  # UTC
    owner: str  # a strategy's id, or DESK
    con_id: int
    symbol: str  # OCC, padded
    quantity: float  # contracts: positive bought, negative sold
    price: float  # per share
    multiplier: float
    client_order_id: str


@dataclass(frozen=True)
class Holding:
    owner: str
    con_id: int
    symbol: str
    quantity: float  # contracts, negative short
    avg_price: float  # per share
    multiplier: float


@dataclass(frozen=True)
class Reconciliation:
    # contract id -> IBKR's position less what the ledger's owners hold of it,
    # where that is not zero
    unattributed: dict[int, float]
    # strategy -> why what it holds does not match IBKR
    blocked: dict[str, str]
    # why no strategy may open: IBKR holds a live option no one accounts for
    everyone: str | None = None

    def blocking(self, owner: str) -> str | None:
        """Why owner may open nothing until the ledger matches IBKR, or None."""
        return None if owner == DESK else self.blocked.get(owner) or self.everyone


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    exec_id TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    owner TEXT NOT NULL,
    con_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    multiplier REAL NOT NULL,
    client_order_id TEXT NOT NULL
)
"""

_COLUMNS = "exec_id, time, owner, con_id, symbol, quantity, price, multiplier, client_order_id"


class Ledger:
    def __init__(self, path: str) -> None:
        # Autocommit: each fill is on disk once add() returns.
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.execute(_SCHEMA)

    def add(self, fills: Iterable[Fill]) -> list[Fill]:
        """Record the fills not recorded before, and return those."""
        new = []
        for f in fills:
            cur = self._db.execute(
                f"INSERT OR IGNORE INTO fills ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f.exec_id,
                    _utc(f.time).isoformat(),
                    f.owner,
                    f.con_id,
                    f.symbol,
                    f.quantity,
                    f.price,
                    f.multiplier,
                    f.client_order_id,
                ),
            )
            if cur.rowcount:
                new.append(f)
        return new

    def all(self) -> list[Fill]:
        rows = self._db.execute(f"SELECT {_COLUMNS} FROM fills").fetchall()
        return [Fill(r[0], datetime.fromisoformat(r[1]), *r[2:]) for r in rows]


def holdings(fills: Iterable[Fill], today: date) -> list[Holding]:
    """What each owner holds of each contract, from its fills in time order,
    at IBKR's average cost: adding to a position moves the average, reducing
    it does not. Options past their expiry drop out, since IBKR reports no
    fill when an option expires or is assigned."""
    pos: dict[tuple[str, int], tuple[float, float, str, float]] = {}
    for f in sorted(fills, key=lambda f: (_utc(f.time), f.exec_id)):
        key = (f.owner, f.con_id)
        qty, avg = pos[key][:2] if key in pos else (0.0, 0.0)
        new = qty + f.quantity
        if qty == 0 or (qty > 0) == (f.quantity > 0):
            avg = (qty * avg + f.quantity * f.price) / new
        elif new != 0 and (new > 0) != (qty > 0):
            avg = f.price  # went through zero: what is left was opened at this price
        pos[key] = (new, avg, f.symbol, f.multiplier)
    out = []
    for (owner, con_id), (qty, avg, symbol, mult) in pos.items():
        if abs(qty) < 1e-9:
            continue
        if occ.is_option(symbol) and occ.parse(symbol).expiry < today:
            continue
        out.append(Holding(owner, con_id, symbol, qty, avg, mult))
    return out


def reconcile(held: list[Holding], ibkr: dict[int, float], live_options: dict[int, str]) -> Reconciliation:
    """Compare what the ledger says each owner holds with what IBKR holds,
    contract by contract. ibkr is IBKR's positions (contract id -> quantity),
    live_options the symbols of those that are options not yet expired."""
    owned: dict[int, float] = defaultdict(float)
    holders: dict[int, list[Holding]] = defaultdict(list)
    for h in held:
        owned[h.con_id] += h.quantity
        holders[h.con_id].append(h)
    unattributed: dict[int, float] = {}
    blocked: dict[str, str] = {}
    everyone = None
    for con_id in sorted(owned.keys() | ibkr.keys()):
        diff = ibkr.get(con_id, 0.0) - owned.get(con_id, 0.0)
        if abs(diff) < 1e-9:
            continue
        unattributed[con_id] = diff
        strategies = [h for h in holders.get(con_id, []) if h.owner != DESK]
        for h in strategies:
            blocked.setdefault(
                h.owner, f"{h.symbol}: IBKR holds {ibkr.get(con_id, 0.0):g}, the ledger {owned[con_id]:g}"
            )
        if not strategies and con_id in live_options and everyone is None:
            everyone = f"IBKR holds {diff:g} of {live_options[con_id]} that no one in the ledger accounts for"
    return Reconciliation(unattributed, blocked, everyone)


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
