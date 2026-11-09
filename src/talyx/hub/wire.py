from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

from talyx.proxy.jsonrpc import (
    CycleAbandoned,
    CycleCompleted,
    Event,
    InputRequired,
    InputWaitCompleted,
    RequestCompleted,
    RequestSeen,
    RequestStateRejected,
)

# Wire protocol for wrapper -> hub traffic: one JSON object per line (UTF-8).
# Each line is {"v", "server", "type", **fields}, where "type" is the dataclass
# name and the fields are exactly that dataclass's fields. Nothing else is sent.
WIRE_VERSION = 1

_ENVELOPE = frozenset({"v", "server", "type"})


@dataclass(frozen=True)
class ChildUp:
    """The wrapped child process started (or restarted)."""


@dataclass(frozen=True)
class ChildDown:
    """The wrapped child process exited."""

    crashed: bool
    inflight_lost: int


@dataclass(frozen=True)
class ProxyOverhead:
    """Time the proxy itself spent handling one message, in seconds."""

    seconds: float


HubItem = Event | ChildUp | ChildDown | ProxyOverhead

# Whitelist of decodable types. Anything not listed here is rejected, so a
# malformed or hostile line can never instantiate an arbitrary class.
_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        RequestSeen,
        RequestCompleted,
        InputRequired,
        InputWaitCompleted,
        CycleCompleted,
        CycleAbandoned,
        RequestStateRejected,
        ChildUp,
        ChildDown,
        ProxyOverhead,
    )
}


def encode(server: str, item: HubItem) -> bytes:
    body = {
        "v": WIRE_VERSION,
        "server": server,
        "type": type(item).__name__,
        **dataclasses.asdict(item),
    }
    return (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> tuple[str, HubItem] | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    version = obj.get("v")
    if type(version) is not int or version != WIRE_VERSION:
        return None
    server = obj.get("server")
    type_name = obj.get("type")
    if not isinstance(server, str) or not isinstance(type_name, str):
        return None
    cls = _TYPES.get(type_name)
    if cls is None:
        return None
    fields = {k: v for k, v in obj.items() if k not in _ENVELOPE}
    try:
        item = cls(**fields)
    except TypeError:
        return None
    return server, item
