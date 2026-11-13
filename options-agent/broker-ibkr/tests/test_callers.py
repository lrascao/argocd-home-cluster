import asyncio
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from broker_ibkr import callers, strategies
from broker_ibkr.models import DESK

ISSUER = "https://oidc.example/abc"
AUDIENCE = "broker.test"
APPS = "spiffe://td.example/ns/prj-1/"


def _key(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True) | {"kid": kid, "use": "sig", "alg": "RS256"}
    return private, public


KEY, PUBLIC = _key("k1")
# A key the issuer does not publish until the rotation test says so.
NEW_KEY, NEW_PUBLIC = _key("k2")


class Issuer:
    """The issuer's two documents, and how often its keys were fetched."""

    def __init__(self, *keys: dict, name: str = ISSUER) -> None:
        self.keys = list(keys)
        self.name = name
        self.fetches = 0
        self.down = False

    async def __call__(self, url: str):
        if self.down:
            raise httpx.ConnectError("down")
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {"issuer": self.name, "jwks_uri": f"{ISSUER}/jwks.json"}
        assert url == f"{ISSUER}/jwks.json"
        self.fetches += 1
        return {"keys": list(self.keys)}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _callers(issuer: Issuer, clock: Clock | None = None) -> callers.Callers:
    return callers.Callers(
        callers.Keys(ISSUER, fetch=issuer, clock=clock or Clock()),
        issuer=ISSUER,
        audience=AUDIENCE,
        trust_domain="td.example",
        namespace="prj-1",
        mcpserver_app_id="broker",
        desk_app_id="options-trader",
        strategy_app_prefix="options-",
        is_strategy=lambda sid: sid == "put-spread",
    )


def _token(app_id: str = "broker", *, key=KEY, kid: str = "k1", algorithm: str = "RS256", **claims) -> str:
    now = int(time.time())
    body = {"sub": APPS + app_id, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "nbf": now - 5, "exp": now + 3600}
    body = {k: v for k, v in (body | claims).items() if v is not None}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


def _identify(c: callers.Callers, token: str, caller: str = "options-put-spread") -> str | None:
    asyncio.run(c.prepare(token))
    return c.identify(token, caller)


# --- who is calling ------------------------------------------------------------


def test_through_the_mcpserver_the_desks_agent_is_the_desk_and_a_strategys_is_that_strategy():
    c = _callers(Issuer(PUBLIC))
    assert _identify(c, _token(), "options-trader") == DESK
    assert _identify(c, _token(), "options-put-spread") == "put-spread"


@pytest.mark.parametrize(
    "caller",
    [
        "options-iron-condor",  # no strategy file
        "options-put-spread-sandbox-guest",
        "options-put-spread/extra",
        "whatsapp-agent",
        "trader",
    ],
)
def test_any_other_agent_is_no_one(caller):
    assert _identify(_callers(Issuer(PUBLIC)), _token(), caller) is None


def test_no_caller_is_the_mcpservers_own_check_and_never_a_strategy():
    assert _identify(_callers(Issuer(PUBLIC)), _token(), "") == callers.MCPSERVER_CHECK
    assert not strategies.valid_id(callers.MCPSERVER_CHECK)
    # No check without the MCPServer's token.
    assert _identify(_callers(Issuer(PUBLIC)), _token("options-put-spread"), "") is None


@pytest.mark.parametrize(
    "app_id",
    [
        "options-put-spread",  # an agent's own token: it did not come through the MCPServer
        "broker-put-spread",  # another MCPServer's
    ],
)
def test_only_the_mcpservers_token_counts(app_id):
    assert _identify(_callers(Issuer(PUBLIC)), _token(app_id)) is None


@pytest.mark.parametrize(
    "subject",
    [
        "spiffe://td.example/ns/prj-2/broker",  # another project
        "spiffe://other.example/ns/prj-1/broker",  # another trust domain
        "spiffe://td.example/ns/prj-1/broker/extra",
        "broker",
    ],
)
def test_a_token_from_outside_the_project_counts_for_no_one(subject):
    assert _identify(_callers(Issuer(PUBLIC)), _token(sub=subject)) is None


@pytest.mark.parametrize(
    "token",
    [
        _token(key=NEW_KEY),  # signed with another key under k1's id
        _token(key=NEW_KEY, kid="k2"),  # a key the issuer does not publish
        _token(aud="someone-else"),
        _token(iss="https://oidc.example/other"),
        _token(iat=int(time.time()) - 7200, nbf=None, exp=int(time.time()) - 3600),  # expired
        _token(exp=None),
        _token(key="s" * 64, algorithm="HS256"),  # not the algorithm the key is for
        "a.b.c",
    ],
)
def test_a_token_that_does_not_check_out_is_no_one(token):
    assert _identify(_callers(Issuer(PUBLIC)), token) is None


def test_only_a_jwt_is_taken_for_one():
    assert callers.looks_like_one(_token())
    assert not callers.looks_like_one("0123456789abcdef")


# --- the issuer's keys ---------------------------------------------------------


def test_the_keys_are_fetched_once_and_ready_after_warming():
    issuer = Issuer(PUBLIC)
    c = _callers(issuer)
    asyncio.run(c.warm())
    for _ in range(3):
        assert _identify(c, _token()) == "put-spread"
    assert issuer.fetches == 1


def test_a_new_key_is_fetched_but_no_more_than_every_30_seconds():
    issuer, clock = Issuer(PUBLIC), Clock()
    c = _callers(issuer, clock)
    assert _identify(c, _token()) == "put-spread"
    # Sentry starts signing with a key the adapter has not seen.
    issuer.keys.append(NEW_PUBLIC)
    clock.now += 10
    assert _identify(c, _token(key=NEW_KEY, kid="k2")) is None
    assert issuer.fetches == 1
    clock.now += 21
    assert _identify(c, _token(key=NEW_KEY, kid="k2")) == "put-spread"
    assert issuer.fetches == 2


def test_the_keys_are_fetched_again_after_an_hour_and_a_dropped_key_stops_counting():
    issuer, clock = Issuer(PUBLIC, NEW_PUBLIC), Clock()
    c = _callers(issuer, clock)
    assert _identify(c, _token()) == "put-spread"
    issuer.keys = [NEW_PUBLIC]
    clock.now += 3601
    assert _identify(c, _token()) is None
    assert issuer.fetches == 2


def test_an_issuer_that_is_down_leaves_the_keys_there_were():
    issuer, clock = Issuer(PUBLIC), Clock()
    c = _callers(issuer, clock)
    assert _identify(c, _token()) == "put-spread"
    issuer.down = True
    clock.now += 3601
    assert _identify(c, _token()) == "put-spread"


def test_a_discovery_document_for_another_issuer_gives_no_keys():
    assert _identify(_callers(Issuer(PUBLIC, name="https://evil.example")), _token()) is None


# --- with the static tokens ----------------------------------------------------


def test_sentry_tokens_and_static_tokens_both_count():
    auth = strategies.Auth("admin", {"aaa": "iron-condor"}, _callers(Issuer(PUBLIC)))
    token = _token()
    for header, caller, who in [
        (f"Bearer {token}", "options-put-spread", "put-spread"),
        (f"Bearer {token}", "options-trader", DESK),
        (f"Bearer {token}", "whatsapp-agent", None),
        (f"Bearer {token}", "", callers.MCPSERVER_CHECK),
        ("Bearer aaa", "", "iron-condor"),
        ("Bearer admin", "", DESK),
        # Without the MCPServer's token the caller header counts for nothing.
        ("Bearer aaa", "options-put-spread", "iron-condor"),
        ("Bearer admin", "options-put-spread", DESK),
        ("Bearer nope", "options-put-spread", None),
        (token, "options-put-spread", None),  # no "Bearer "
    ]:
        asyncio.run(auth.prepare(header))
        assert auth.identify(header, caller) == who, (header[:20], caller)


def test_caller_tokens_are_off_unless_fully_configured(monkeypatch):
    for name in ("CALLER_TOKEN_ISSUER", "CALLER_TOKEN_AUDIENCE", "CALLER_TRUST_DOMAIN", "CALLER_NAMESPACE"):
        monkeypatch.delenv(name, raising=False)
    assert callers.Callers.from_env(lambda _: True) is None
    monkeypatch.setenv("CALLER_TOKEN_ISSUER", ISSUER)
    assert callers.Callers.from_env(lambda _: True) is None  # no audience, trust domain or namespace
    monkeypatch.setenv("CALLER_TOKEN_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("CALLER_TRUST_DOMAIN", "td.example")
    monkeypatch.setenv("CALLER_NAMESPACE", "prj-1")
    assert isinstance(callers.Callers.from_env(lambda _: True), callers.Callers)
    monkeypatch.setenv("CALLER_TOKEN_ISSUER", "http://oidc.example/abc")
    assert callers.Callers.from_env(lambda _: True) is None


def test_a_strategy_exists_if_it_has_a_file(tmp_path):
    (tmp_path / "put-spread.toml").write_text("not even valid toml =")
    assert strategies.exists(str(tmp_path), "put-spread")
    assert not strategies.exists(str(tmp_path), "iron-condor")
    assert not strategies.exists(str(tmp_path), "desk")
    assert not strategies.exists(str(tmp_path), "../put-spread")
    assert not strategies.exists("", "put-spread")
