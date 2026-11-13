from pathlib import Path

import pytest

from broker_ibkr import strategies
from broker_ibkr.models import DESK

# The strategy files the cluster mounts.
SHIPPED = Path(__file__).resolve().parents[2] / "strategies"

GOOD = """
description = "test"
rules = "do it"
budget_eur = 1000
max_loss_per_order_eur = 200
max_opening_orders_per_day = 2
min_days_to_expiry = 30
max_days_to_expiry = 45
rights = ["P"]
legs = 2
"""


# --- tokens -----------------------------------------------------------------


def test_tokens_map_to_their_strategies():
    assert strategies.parse_tokens(" put-spread=aaa , iron-condor=bbb,") == {"aaa": "put-spread", "bbb": "iron-condor"}
    assert strategies.parse_tokens("") == {}


@pytest.mark.parametrize(
    "value",
    [
        "s3cret",  # no id
        "put-spread=",  # no token
        "Put_Spread=s3cret",  # not an id
        "desk=s3cret",  # the desk is not a strategy
        "put-spread=s3cret,iron-condor=s3cret",  # one token, two strategies
    ],
)
def test_malformed_tokens_are_refused_without_quoting_them(value):
    with pytest.raises(ValueError) as e:
        strategies.parse_tokens(value)
    assert "s3cret" not in str(e.value)


def test_the_token_says_who_is_calling():
    auth = strategies.Auth("admin", {"aaa": "put-spread"})
    assert auth.identify("Bearer admin") == DESK
    assert auth.identify("Bearer aaa") == "put-spread"
    assert auth.identify("Bearer nope") is None
    assert auth.identify("aaa") is None
    assert auth.identify("") is None


def test_without_an_admin_token_anyone_else_is_the_desk():
    # A laptop: no BROKER_TOKEN, as before there were strategies.
    auth = strategies.Auth("", {"aaa": "put-spread"})
    assert auth.identify("Bearer aaa") == "put-spread"
    assert auth.identify("") == DESK


# --- strategy files ---------------------------------------------------------


def test_the_shipped_strategies_load():
    loaded, errors = strategies.load(str(SHIPPED))
    assert errors == {}
    assert sorted(loaded) == ["iron-condor", "put-spread"]
    assert (loaded["put-spread"].legs, loaded["put-spread"].rights) == (2, ["P"])
    assert (loaded["iron-condor"].legs, sorted(loaded["iron-condor"].rights)) == (4, ["C", "P"])
    for s in loaded.values():
        assert not s.kill_switch
        assert s.rules.strip()


def test_the_file_name_is_the_id(tmp_path):
    (tmp_path / "test-1.toml").write_text(GOOD)
    loaded, errors = strategies.load(str(tmp_path))
    assert (loaded["test-1"].id, errors) == ("test-1", {})


@pytest.mark.parametrize(
    "name, text, reason",
    [
        ("typo.toml", GOOD + "budget = 5\n", "budget"),
        ("window.toml", GOOD.replace("min_days_to_expiry = 30", "min_days_to_expiry = 50"), "min_days_to_expiry"),
        ("rights.toml", GOOD.replace('["P"]', '["X"]'), "rights"),
        ("broken.toml", "budget_eur = \n", "broken.toml"),
        ("Bad_Name.toml", GOOD, "id"),
        ("desk.toml", GOOD, "desk"),
    ],
)
def test_a_file_that_does_not_load_says_why_and_the_others_still_do(tmp_path, name, text, reason):
    (tmp_path / "good.toml").write_text(GOOD)
    (tmp_path / name).write_text(text)
    loaded, errors = strategies.load(str(tmp_path))
    assert list(loaded) == ["good"]
    assert reason in errors[Path(name).stem]


def test_no_directory_means_no_strategies(tmp_path):
    assert strategies.load("") == ({}, {})
    assert strategies.load(str(tmp_path / "missing")) == ({}, {})


def test_a_malformed_strategy_tokens_turns_off_the_strategies_not_the_desk(monkeypatch):
    # Both tokens empty: the shell that sealed them never had them set.
    monkeypatch.setenv("BROKER_TOKEN", "admin")
    monkeypatch.setenv("STRATEGY_TOKENS", "put-spread=,iron-condor=")
    auth = strategies.Auth.from_env()
    assert auth.identify("Bearer admin") == DESK
    assert auth.identify("Bearer ") is None
