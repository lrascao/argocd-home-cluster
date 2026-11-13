"""Who is calling, and what each strategy may do.

Several strategies trade the same account side by side, each run by its own
agent. Who a call is from:

- through MCPServer broker, which every agent uses, the calling agent's App
  ID, which counts with the token the region's Sentry signs for that
  MCPServer (callers.py): options-<id> is strategy <id>, and options-trader
  is the desk;
- with the admin token (BROKER_TOKEN), the desk, which people and scripts
  use on REST.

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


class Auth:
    def __init__(self, admin_token: str, caller_tokens: callers.Callers | None = None) -> None:
        self._admin = admin_token.encode()
        self._callers = caller_tokens

    @classmethod
    def from_env(cls) -> "Auth":
        directory = os.getenv("STRATEGIES_DIR", "")
        return cls(os.getenv("BROKER_TOKEN", ""), callers.Callers.from_env(lambda sid: exists(directory, sid)))

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

    def identify(self, authorization: str, caller: str = "") -> str | None:
        """Who is calling, by the Authorization header and, with a Sentry
        token, the dapr-caller-app-id header: a strategy's id, DESK for the
        desk's agent or the admin token, MCPSERVER_CHECK for the MCPServer's
        own check, or None for none of them. With no admin token set (a
        laptop), anything else is the desk."""
        bearer = _bearer(authorization)
        if bearer is not None:
            if self._callers is not None and callers.looks_like_one(bearer):
                who = self._callers.identify(bearer, caller)
                if who is not None:
                    return who
            if self._admin and hmac.compare_digest(bearer.encode(), self._admin):
                return DESK
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
