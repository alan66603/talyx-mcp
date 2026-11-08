from __future__ import annotations

import dataclasses
import json

import pytest

from talyx.hub.wire import ChildDown, ChildUp, ProxyOverhead, decode, encode
from talyx.proxy.jsonrpc import (
    CycleAbandoned,
    CycleCompleted,
    InputRequired,
    InputWaitCompleted,
    RequestCompleted,
    RequestSeen,
    RequestStateRejected,
)

ALL_ITEMS = [
    RequestSeen("tools/call", "t", False),
    RequestCompleted("tools/call", "t", 0.5, False, -32602),
    InputRequired("tools/call", "input"),
    InputWaitCompleted("poll", 1.5),
    CycleCompleted("input", 3, 9.0),
    CycleAbandoned("input"),
    RequestStateRejected(),
    ChildUp(),
    ChildDown(crashed=True, inflight_lost=2),
    ProxyOverhead(0.0001),
]


@pytest.mark.parametrize("item", ALL_ITEMS)
def test_roundtrip(item) -> None:
    assert decode(encode("mem", item)) == ("mem", item)


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"not json",
        b"[]",
        b'{"v":2,"server":"a","type":"ChildUp"}',
        b'{"v":1,"server":"a","type":"Popen"}',
        b'{"v":1,"server":"a","type":"ChildDown","bogus":1}',
    ],
)
def test_decode_rejects(line) -> None:
    assert decode(line) is None


def test_wire_carries_only_event_fields() -> None:
    for item in ALL_ITEMS:
        keys = set(json.loads(encode("a", item)))
        assert keys == {"v", "server", "type", *(f.name for f in dataclasses.fields(item))}
