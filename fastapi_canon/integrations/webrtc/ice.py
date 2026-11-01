"""ICE server configuration, providers and expiring TURN credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from fastapi_canon.integrations.webrtc.declarations import SessionContext


class IceServer(BaseModel):
    """One STUN or TURN server, in the shape of the browser's ``RTCIceServer``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    urls: tuple[str, ...] = Field(min_length=1)
    username: str | None = None
    credential: str | None = None


@dataclass(frozen=True)
class IceServers:
    """ICE servers for both peers of a session.

    ``client`` is sent to the browser. ``engine`` configures the server-side peer
    and defaults to ``client``; use it when the server reaches TURN through an
    internal address that browsers cannot use.
    """

    client: tuple[IceServer, ...] = ()
    engine: tuple[IceServer, ...] | None = None

    @property
    def for_engine(self) -> tuple[IceServer, ...]:
        return self.client if self.engine is None else self.engine


type IceServerSource = (
    IceServers
    | Sequence[IceServer]
    | Callable[[SessionContext], IceServers | Sequence[IceServer]]
    | Callable[[SessionContext], Awaitable[IceServers | Sequence[IceServer]]]
)
"""Static servers, or a per-session provider called after admission."""


async def resolve_ice_servers(
    source: IceServerSource | None, context: SessionContext
) -> IceServers:
    if source is None:
        return IceServers()
    value: Any = source
    if callable(source):
        value = source(context)
        if inspect.isawaitable(value):
            value = await value
    if isinstance(value, IceServers):
        return value
    servers = tuple(value)
    for server in servers:
        if not isinstance(server, IceServer):
            raise TypeError("ICE server providers must return IceServer values")
    return IceServers(client=servers)


@dataclass(frozen=True)
class TurnRestCredentials:
    """Short-lived TURN credentials in the coturn REST scheme.

    The username is ``"<expiry>:<user>"`` and the credential is its
    HMAC-SHA1 digest under ``secret`` (coturn's ``static-auth-secret``).
    ``internal_turn_urls`` are given to the server-side engine instead of
    ``turn_urls`` when both peers reach the TURN server through different routes.
    """

    secret: str = field(repr=False)
    turn_urls: tuple[str, ...]
    stun_urls: tuple[str, ...] = ()
    internal_turn_urls: tuple[str, ...] | None = None
    ttl_seconds: int = 3600
    clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("TURN REST credentials need a non-empty secret")
        if not self.turn_urls:
            raise ValueError("TURN REST credentials need at least one TURN URL")
        if not 60 <= self.ttl_seconds <= 86400:
            raise ValueError("ttl_seconds must be between 60 and 86400")

    def for_user(self, user: object) -> IceServers:
        """Issue credentials bound to ``user`` (for example an account ID)."""
        identity = str(user)
        if ":" in identity:
            raise ValueError("TURN usernames cannot contain ':'")
        expires = int(self.clock()) + self.ttl_seconds
        username = f"{expires}:{identity}"
        digest = hmac.new(
            self.secret.encode(), username.encode(), hashlib.sha1
        ).digest()
        credential = base64.b64encode(digest).decode()
        stun = (IceServer(urls=self.stun_urls),) if self.stun_urls else ()

        def turn(urls: tuple[str, ...]) -> IceServer:
            return IceServer(urls=urls, username=username, credential=credential)

        client = (*stun, turn(self.turn_urls))
        engine = (
            None
            if self.internal_turn_urls is None
            else (*stun, turn(self.internal_turn_urls))
        )
        return IceServers(client=client, engine=engine)
