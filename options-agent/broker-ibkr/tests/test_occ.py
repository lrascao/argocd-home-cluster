from datetime import date
from decimal import Decimal

import pytest

from broker_ibkr import occ


@pytest.mark.parametrize("symbol", ["AAPL  261218C00250000", "AAPL261218C00250000", " aapl261218c00250000 "])
def test_normalize_pads_the_root_and_uppercases(symbol):
    assert occ.normalize(symbol) == "AAPL  261218C00250000"


def test_parse_reads_every_field():
    s = occ.parse("SPY   261218P00450500")
    assert s == occ.OptionSymbol("SPY", date(2026, 12, 18), "P", Decimal("450.5"))
    assert str(s) == "SPY   261218P00450500"


def test_root_may_be_six_characters_and_end_in_a_digit():
    assert occ.normalize("SPXW  261218C05000000") == "SPXW  261218C05000000"
    assert occ.parse("GOOGL1261218C00150000").root == "GOOGL1"
    assert occ.parse("AAPL1261218C00150000").root == "AAPL1"


@pytest.mark.parametrize(
    "symbol",
    ["SPY", "AAPL  261218X00250000", "AAPL  261318C00250000", "TOOLONGROOT261218C00250000", "AAPL  261218C0025000"],
)
def test_parse_rejects_what_is_not_an_occ_symbol(symbol):
    with pytest.raises(ValueError):
        occ.parse(symbol)


def test_is_option_tells_tickers_from_options():
    assert not occ.is_option("SPY")
    assert occ.is_option("SPY261218P00450000")
