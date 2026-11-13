"""Who is calling, and what each strategy may do.

Several strategies trade the same account side by side, each run by its own
agent, and the token a call carries says which strategy it is from:

- a token the region's Sentry signs for the calling agent (callers.py), which
  one MCPServer sends for every agent: options-<id> is strategy <id>, and
  options-trader is the desk;
- a strategy's static bearer token (STRATEGY_TOKENS), sent by an MCPServer of
  that strategy's own;
- the admin token (BROKER_TOKEN), the desk, which people and scripts use on
  REST.

The desk sees every strategy's orders and positions, and on REST can act as
any of them.

A strategy is a file, strategies/<id>.toml, mounted from a ConfigMap. It is
read again on every call that needs it, so an edit reaches the adapter within
a minute or two, without restarting the pod.
"""

import hmac
import logging
import os
import re
import tomllib
from pathlib import Path

from pydantic import ValidationError

from . import callers, models
from .models import DESK

log = logging.getLogger(__name__)

_ID = re.compile(models.STRATEGY_ID)


def valid_id(s: str) -> bool:
    return bool(_ID.match(s)) and s != DESK


def exists(directory: str, sid: str) -> bool:
    """Whether strategy sid has a file, loadable or not: the agent of a
    strategy whose file does not load can still manage what it holds."""
    return valid_id(sid) and bool(directory) and (Path(directory) / f"{sid}.toml").is_file()


def parse_tokens(value: str) -> dict[str, str]:
    """STRATEGY_TOKENS, "put-spread=<token>,iron-condor=<token>", as token ->
    strategy id. Raises ValueError, never quoting a token, if it is malformed."""
    out: dict[str, str] = {}
    for n, item in enumerate((i.strip() for i in value.split(",")), 1):
        if not item:
            continue
        sid, sep, token = (s.strip() for s in item.partition("="))
        # Never quote the entry: without its "=", or with the two swapped,
        # what would be quoted is the token.
        if not sep:
            raise ValueError(f"STRATEGY_TOKENS entry {n} has no '=': it should be <strategy id>=<token>")
        if not valid_id(sid):
            raise ValueError(
                f"STRATEGY_TOKENS entry {n} does not start with a strategy id "
                "(lowercase letters, digits and dashes, not 'desk')"
            )
        if not token:
            raise ValueError(f"STRATEGY_TOKENS entry {n}, {sid}, has an empty token")
        if token in out:
            raise ValueError(f"STRATEGY_TOKENS gives {out[token]} and {sid} the same token")
        out[token] = sid
    return out


class Auth:
    def __init__(
        self, admin_token: str, strategy_tokens: dict[str, str], caller_tokens: callers.Callers | None = None
    ) -> None:
        self._admin = admin_token
        self._tokens = [(t.encode(), sid) for t, sid in strategy_tokens.items()]
        if admin_token:
            self._tokens.append((admin_token.encode(), DESK))
        self._callers = caller_tokens

    @classmethod
    def from_env(cls) -> "Auth":
        """The tokens from the environment. A malformed STRATEGY_TOKENS turns
        every strategy's token off, not the adapter: the desk keeps working."""
        try:
            tokens = parse_tokens(os.getenv("STRATEGY_TOKENS", ""))
        except ValueError as e:
            log.error("%s; no strategy's own token works until it is fixed", e)
            tokens = {}
        directory = os.getenv("STRATEGIES_DIR", "")
        return cls(
            os.getenv("BROKER_TOKEN", ""), tokens, callers.Callers.from_env(lambda sid: exists(directory, sid))
        )

    async def warm(self) -> None:
        """Fetch the keys Sentry's tokens are checked with, before the first
        call needs them."""
        if self._callers is not None:
            await self._callers.warm()

    async def prepare(self, authorization: str) -> None:
        """Fetch any keys a Sentry token needs before identify checks it,
        since identify cannot wait on the network. The middleware calls it
        on every request, so the keys are there by the time a tool asks."""
        bearer = _bearer(authorization)
        if self._callers is not None and bearer is not None and callers.looks_like_one(bearer):
            await self._callers.prepare(bearer)

    def identify(self, authorization: str) -> str | None:
        """Who an Authorization header says is calling: a strategy's id, DESK
        for the desk's agent or the admin token, or None for neither. With no
        admin token set (a laptop), anything else is the desk."""
        bearer = _bearer(authorization)
        if bearer is not None:
            if self._callers is not None and callers.looks_like_one(bearer):
                who = self._callers.identify(bearer)
                if who is not None:
                    return who
            given = bearer.encode()
            for token, who in self._tokens:
                if hmac.compare_digest(given, token):
                    return who
        return None if self._admin else DESK


def _bearer(authorization: str) -> str | None:
    return authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None


def load(directory: str) -> tuple[dict[str, models.Strategy], dict[str, str]]:
    """Every strategy in directory, and why any file there did not load."""
    good: dict[str, models.Strategy] = {}
    bad: dict[str, str] = {}
    d = Path(directory) if directory else None
    if d is None or not d.is_dir():
        return good, bad
    for path in sorted(d.glob("*.toml")):
        sid = path.stem
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
            good[sid] = models.Strategy.model_validate(data | {"id": sid})
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as e:
            bad[sid] = f"{path.name}: {e}"
            log.warning("strategy %s did not load, so it opens nothing: %s", sid, e)
    return good, bad
