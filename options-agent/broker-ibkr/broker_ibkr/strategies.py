"""Who is calling, and what each strategy may do.

Several strategies trade the same account side by side, each run by its own
agent. Each agent reaches the adapter through its own MCPServer, which sends
that strategy's bearer token, so the token is what says which strategy a call
is from. The admin token (BROKER_TOKEN) is the desk: it sees every strategy's
orders and positions, and can act as any of them.

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

from . import models
from .models import DESK

log = logging.getLogger(__name__)

_ID = re.compile(models.STRATEGY_ID)


def valid_id(s: str) -> bool:
    return bool(_ID.match(s)) and s != DESK


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
    def __init__(self, admin_token: str, strategy_tokens: dict[str, str]) -> None:
        self._admin = admin_token
        self._tokens = [(t.encode(), sid) for t, sid in strategy_tokens.items()]
        if admin_token:
            self._tokens.append((admin_token.encode(), DESK))

    @classmethod
    def from_env(cls) -> "Auth":
        """The tokens from the environment. A malformed STRATEGY_TOKENS turns
        every strategy's token off, not the adapter: the desk keeps working."""
        try:
            tokens = parse_tokens(os.getenv("STRATEGY_TOKENS", ""))
        except ValueError as e:
            log.error("%s; no strategy can call the broker until it is fixed", e)
            tokens = {}
        return cls(os.getenv("BROKER_TOKEN", ""), tokens)

    def identify(self, authorization: str) -> str | None:
        """Who an Authorization header says is calling: a strategy's id, DESK
        for the admin token, or None for neither. With no admin token set (a
        laptop), anything that is not a strategy's token is the desk."""
        if authorization.startswith("Bearer "):
            given = authorization.removeprefix("Bearer ").encode()
            for token, who in self._tokens:
                if hmac.compare_digest(given, token):
                    return who
        return None if self._admin else DESK


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
