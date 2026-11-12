"""OCC option symbols, the one identifier every broker adapter speaks.

An OCC (OSI) symbol is the option root padded to six characters, the expiry
as YYMMDD, C or P, and the strike times 1000 as eight digits:
"AAPL  261218C00250000" is the AAPL 250 call expiring 2026-12-18. Each
adapter translates it to its broker's own contract id.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_OCC = re.compile(r"^([A-Z0-9]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True)
class OptionSymbol:
    root: str
    expiry: date
    right: str  # "C" or "P"
    strike: Decimal

    def __str__(self) -> str:
        return f"{self.root:<6}{self.expiry:%y%m%d}{self.right}{int(self.strike * 1000):08d}"


def parse(symbol: str) -> OptionSymbol:
    """Parse an OCC symbol, padded or not. Raises ValueError otherwise."""
    m = _OCC.match(symbol.strip().upper())
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    root, yy, mm, dd, right, strike = m.groups()
    return OptionSymbol(root, date(2000 + int(yy), int(mm), int(dd)), right, Decimal(strike) / 1000)


def is_option(symbol: str) -> bool:
    return _OCC.match(symbol.strip().upper()) is not None


def normalize(symbol: str) -> str:
    """The canonical padded form, which is also IBKR's localSymbol."""
    return str(parse(symbol))
