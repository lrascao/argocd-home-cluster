"""Callers that come through one MCPServer, told apart by the agent calling.

An agent's MCP call goes from its sidecar to the sidecar of the MCPServer's
own App ID, which checks the agent's grant and then calls the adapter. That
second hop carries two things the adapter can rely on:

- a token the region's Sentry signs for the MCPServer's App ID, when the
  MCPServer is created with --spiffe-jwt-header. It proves the call came
  through that MCPServer, and so past its grants. It names the MCPServer,
  not the agent: the MCPServer's sidecar is what calls the adapter.
- the dapr-caller-app-id header, which Dapr sets to the calling agent's App
  ID, from the agent's mTLS identity; the MCPServer's sidecar enforces the
  grants on it and passes it on with the call.

So with a good token from MCPSERVER_APP_ID (broker), the header says who is
calling:

- DESK_APP_ID (options-trader) is the desk;
- STRATEGY_APP_PREFIX + <id> (options-<id>) is strategy <id>, as long as
  strategies/<id>.toml exists: a strategy is added with its file, its agent
  and a grant, and needs no token or MCPServer of its own;
- any other agent is no one.

The header counts only with such a token: anyone who can reach the adapter
can send the header, but only the MCPServer's sidecar has the token. A token
counts only if it is signed by one of the keys the issuer publishes
(CALLER_TOKEN_ISSUER, through its /.well-known/openid-configuration), names
that issuer and CALLER_TOKEN_AUDIENCE (what the MCPServer's
--spiffe-jwt-audience says), has not expired, and names MCPSERVER_APP_ID in
CALLER_TRUST_DOMAIN and CALLER_NAMESPACE. The keys come from the issuer, not
from the cluster's dapr-trust-bundle ConfigMap: in November 2026 its jwks.json
held a key Sentry did not sign with.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt

from .models import DESK

log = logging.getLogger(__name__)

# What Dapr sets to the calling App ID, from its mTLS identity.
CALLER_HEADER = "dapr-caller-app-id"

# A token signed with a key the adapter has not seen sends it to the issuer
# for its keys again, but no more often than this, so made-up tokens cannot
# keep it fetching.
_RETRY_AFTER = 30.0
# The keys are fetched again after this long anyway, so a key Sentry has
# dropped stops counting.
_KEYS_MAX_AGE = 3600.0
# Per request to the issuer: a call that needs new keys still has to answer
# within Catalyst's five seconds.
_FETCH_TIMEOUT = 2.0
# Seconds of clock difference allowed on a token's times.
_LEEWAY = 60

Fetch = Callable[[str], Awaitable[Any]]


async def _get_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()


def looks_like_one(bearer: str) -> bool:
    """A JWT is three parts joined by dots; the static tokens, hex, have none."""
    return bearer.count(".") == 2


class Keys:
    """The issuer's public signing keys, by key id."""

    def __init__(self, issuer: str, fetch: Fetch = _get_json, clock: Callable[[], float] = time.monotonic) -> None:
        self._issuer = issuer.rstrip("/")
        self._fetch = fetch
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched: float | None = None  # when the keys were last fetched
        self._tried: float | None = None  # when a fetch was last started
        self._lock = asyncio.Lock()

    def get(self, kid: str) -> jwt.PyJWK | None:
        return self._keys.get(kid)

    async def ensure(self, kid: str | None = None) -> None:
        """Fetch the keys if kid is not among them or they are an hour old,
        unless a fetch was started in the last 30 seconds. A failed fetch
        keeps the keys there were."""
        async with self._lock:
            now = self._clock()
            fresh = self._fetched is not None and now - self._fetched < _KEYS_MAX_AGE
            if fresh and (kid is None or kid in self._keys):
                return
            if self._tried is not None and now - self._tried < _RETRY_AFTER:
                return
            self._tried = now
            try:
                self._keys = await self._load()
                self._fetched = now
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
                log.warning("could not fetch the caller tokens' keys from %s: %s", self._issuer, e)

    async def _load(self) -> dict[str, jwt.PyJWK]:
        config = await self._fetch(f"{self._issuer}/.well-known/openid-configuration")
        if str(config.get("issuer", "")).rstrip("/") != self._issuer:
            raise ValueError(f"its discovery document names the issuer {config.get('issuer')!r}")
        jwks = await self._fetch(config["jwks_uri"])
        keys: dict[str, jwt.PyJWK] = {}
        for data in jwks["keys"]:
            kid = data.get("kid")
            if not kid or data.get("use", "sig") != "sig":
                continue
            try:
                keys[kid] = jwt.PyJWK(data)
            except jwt.PyJWTError as e:
                log.warning("skipping key %s from %s: %s", kid, self._issuer, e)
        if not keys:
            raise ValueError("it publishes no signing keys")
        log.info("caller tokens: %d signing keys from %s", len(keys), self._issuer)
        return keys


class Callers:
    def __init__(
        self,
        keys: Keys,
        *,
        issuer: str,
        audience: str,
        trust_domain: str,
        namespace: str,
        mcpserver_app_id: str,
        desk_app_id: str,
        strategy_app_prefix: str,
        is_strategy: Callable[[str], bool],
    ) -> None:
        self._keys = keys
        self._issuer = issuer
        self._audience = audience
        self._mcpserver = f"spiffe://{trust_domain}/ns/{namespace}/{mcpserver_app_id}"
        self._desk_app_id = desk_app_id
        self._strategy_app_prefix = strategy_app_prefix
        self._is_strategy = is_strategy
        self._seen: set[str] = set()

    @classmethod
    def from_env(cls, is_strategy: Callable[[str], bool]) -> "Callers | None":
        """None, so only the static tokens count, unless CALLER_TOKEN_ISSUER is
        set; and then also if the rest is missing, which is logged."""
        issuer = os.getenv("CALLER_TOKEN_ISSUER", "")
        if not issuer:
            return None
        settings = {n: os.getenv(n, "") for n in ("CALLER_TOKEN_AUDIENCE", "CALLER_TRUST_DOMAIN", "CALLER_NAMESPACE")}
        missing = [n for n, v in settings.items() if not v]
        if missing or not issuer.startswith("https://"):
            problem = f"{', '.join(missing)} not set" if missing else "CALLER_TOKEN_ISSUER is not https"
            log.error("%s; only the bearer tokens count until it is fixed", problem)
            return None
        return cls(
            Keys(issuer),
            issuer=issuer,
            audience=settings["CALLER_TOKEN_AUDIENCE"],
            trust_domain=settings["CALLER_TRUST_DOMAIN"],
            namespace=settings["CALLER_NAMESPACE"],
            mcpserver_app_id=os.getenv("MCPSERVER_APP_ID", "broker"),
            desk_app_id=os.getenv("DESK_APP_ID", "options-trader"),
            strategy_app_prefix=os.getenv("STRATEGY_APP_PREFIX", "options-"),
            is_strategy=is_strategy,
        )

    async def warm(self) -> None:
        await self._keys.ensure()

    async def prepare(self, bearer: str) -> None:
        """Fetch the keys again if the token names one not seen yet:
        identify cannot wait on the network."""
        kid = _kid(bearer)
        if kid is not None:
            await self._keys.ensure(kid)

    def identify(self, bearer: str, caller: str) -> str | None:
        """DESK or a strategy's id for a good token from the MCPServer and the
        App ID it says is calling, None for anything else."""
        try:
            kid = _kid(bearer)
            key = self._keys.get(kid) if kid is not None else None
            if key is None:
                raise jwt.InvalidTokenError(f"it is signed with key {kid!r}, which the issuer does not publish")
            claims = jwt.decode(
                bearer,
                key.key,
                algorithms=[key.algorithm_name],
                audience=self._audience,
                issuer=self._issuer,
                leeway=_LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            log.warning("a caller token was refused: %s", e)
            return None
        subject = str(claims["sub"])
        if subject != self._mcpserver:
            log.warning("a caller token for %s was refused: only %s's count", subject, self._mcpserver)
            return None
        who = self._who(caller)
        if who is None:
            log.warning(
                "a call through %s was refused: its caller %r is not the desk's agent or a strategy's", subject, caller
            )
        elif caller not in self._seen:
            self._seen.add(caller)
            log.info("%s calls as %s, through %s", caller, who, subject)
        return who

    def _who(self, app_id: str) -> str | None:
        if app_id == self._desk_app_id:
            return DESK
        if not app_id.startswith(self._strategy_app_prefix):
            return None
        sid = app_id.removeprefix(self._strategy_app_prefix)
        return sid if self._is_strategy(sid) else None


def _kid(bearer: str) -> str | None:
    try:
        kid = jwt.get_unverified_header(bearer).get("kid")
    except jwt.PyJWTError:
        return None
    return kid if isinstance(kid, str) else None
