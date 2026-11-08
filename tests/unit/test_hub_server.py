from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable

import pytest

from talyx.config import Config
from talyx.hub.server import Hub, run_hub
from talyx.hub.wire import ChildDown, ChildUp, encode
from talyx.metrics.registry import Metrics
from talyx.proxy.jsonrpc import RequestCompleted

Scrape = Callable[[Metrics], str]


class Running:
    def __init__(self, metrics: Metrics, server: asyncio.Server) -> None:
        self.metrics = metrics
        self.server = server
        self.writers: list[asyncio.StreamWriter] = []

    async def connect(self) -> asyncio.StreamWriter:
        port = self.server.sockets[0].getsockname()[1]
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        return writer


@pytest.fixture
async def hub() -> AsyncIterator[Running]:
    metrics = Metrics()
    server = await Hub(metrics).serve("127.0.0.1", 0)
    running = Running(metrics, server)
    yield running
    for w in running.writers:
        w.close()
    server.close()
    await server.wait_closed()


async def send(w: asyncio.StreamWriter, data: bytes) -> None:
    w.write(data)
    await w.drain()
    await asyncio.sleep(0.05)


async def test_events_reach_metrics(hub: Running, scrape: Scrape) -> None:
    w = await hub.connect()
    await send(w, encode("mem", RequestCompleted("tools/call", None, 0.1, True)))
    assert 'server="mem",status="ok"} 1.0' in scrape(hub.metrics)


async def test_clean_exit_removes_series(hub: Running, scrape: Scrape) -> None:
    w = await hub.connect()
    await send(w, encode("mem", ChildUp()))
    assert 'talyx_server_up{server="mem"} 1.0' in scrape(hub.metrics)
    await send(w, encode("mem", ChildDown(False, 0)))
    assert 'talyx_server_up{server="mem"}' not in scrape(hub.metrics)


async def test_crash_sets_zero_and_counts_lost(hub: Running, scrape: Scrape) -> None:
    w = await hub.connect()
    await send(w, encode("mem", ChildUp()))
    await send(w, encode("mem", ChildDown(True, 2)))
    out = scrape(hub.metrics)
    assert 'talyx_server_up{server="mem"} 0.0' in out
    assert 'talyx_inflight_requests_lost_total{server="mem"} 2.0' in out


async def test_two_connections_one_clean_exit_stays_up(hub: Running, scrape: Scrape) -> None:
    a = await hub.connect()
    b = await hub.connect()
    await send(a, encode("mem", ChildUp()))
    await send(b, encode("mem", ChildUp()))
    await send(a, encode("mem", ChildDown(False, 0)))
    assert 'talyx_server_up{server="mem"} 1.0' in scrape(hub.metrics)


async def test_disconnect_without_childdown_is_clean(hub: Running, scrape: Scrape) -> None:
    w = await hub.connect()
    await send(w, encode("mem", ChildUp()))
    w.close()
    await asyncio.sleep(0.1)
    assert 'talyx_server_up{server="mem"}' not in scrape(hub.metrics)


async def test_garbage_line_is_ignored(hub: Running, scrape: Scrape) -> None:
    w = await hub.connect()
    await send(w, b"garbage\n")
    await send(w, encode("mem", RequestCompleted("tools/call", None, 0.1, True)))
    assert 'server="mem",status="ok"} 1.0' in scrape(hub.metrics)


async def test_wrong_value_type_line_does_not_kill_connection(
    hub: Running, scrape: Scrape
) -> None:
    w = await hub.connect()
    bad = b'{"v":1,"server":"mem","type":"ProxyOverhead","seconds":"oops"}\n'
    await send(w, bad)
    await send(w, encode("mem", RequestCompleted("tools/call", None, 0.1, True)))
    assert 'server="mem",status="ok"} 1.0' in scrape(hub.metrics)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_metrics_port_taken_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        cfg = Config(
            metrics_port=blocker.getsockname()[1], metrics_host="127.0.0.1", hub_port=_free_port()
        )
        assert run_hub(cfg) == 1
    assert "cannot bind metrics" in capsys.readouterr().err


def test_hub_port_taken_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        cfg = Config(metrics_port=_free_port(), metrics_host="127.0.0.1", hub_port=port)
        assert run_hub(cfg) == 0
    assert f"hub already running on 127.0.0.1:{port}" in capsys.readouterr().err


def test_run_hub_passes_otlp_endpoint_to_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    import talyx.hub.server as hub_server

    seen: list[dict] = []

    def fake_metrics(*args: object, **kwargs: object) -> Metrics:
        seen.append(kwargs)
        return Metrics()  # no OTLP exporter in tests

    monkeypatch.setattr(hub_server, "Metrics", fake_metrics)
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        cfg = Config(
            metrics_port=blocker.getsockname()[1],
            metrics_host="127.0.0.1",
            hub_port=_free_port(),
            otlp_endpoint="http://collector:4318/v1/metrics",
        )
        assert run_hub(cfg) == 1  # metrics port taken: exits right after building Metrics
    assert seen == [{"otlp_endpoint": "http://collector:4318/v1/metrics"}]
