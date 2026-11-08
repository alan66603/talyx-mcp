from __future__ import annotations

import asyncio
import sys
from collections import Counter

from prometheus_client import start_http_server

from talyx.config import Config
from talyx.hub.wire import ChildDown, ChildUp, ProxyOverhead, decode
from talyx.metrics.registry import Metrics, apply_event

_INGEST_HOST = "127.0.0.1"
# Generous per-line cap; an overlong line drops that one connection only.
_LINE_LIMIT = 1024 * 1024


class Hub:
    """Aggregates wrapper events (newline-JSON over local TCP) into one Metrics."""

    def __init__(self, metrics: Metrics) -> None:
        self.metrics = metrics
        # server -> number of live children (across all connections)
        self._live: Counter[str] = Counter()

    async def serve(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._handle, host, port, limit=_LINE_LIMIT)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Children this connection has announced and not yet retired.
        mine: Counter[str] = Counter()
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, ConnectionError):
                    break
                if not line:
                    break
                try:
                    self._process(line, mine)
                except Exception:  # noqa: BLE001 - one bad line must never kill the hub
                    continue
        finally:
            for server, n in list(mine.items()):
                for _ in range(n):
                    self._down(server, crashed=False, lost=0)
            mine.clear()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def _process(self, line: bytes, mine: Counter[str]) -> None:
        decoded = decode(line)
        if decoded is None:
            return
        server, item = decoded
        if isinstance(item, ChildUp):
            mine[server] += 1
            self._live[server] += 1
            self.metrics.server_up.set(1, {"server": server})
        elif isinstance(item, ChildDown):
            if mine[server] <= 0:
                return
            mine[server] -= 1
            if mine[server] == 0:
                del mine[server]
            self._down(server, item.crashed, item.inflight_lost)
        elif isinstance(item, ProxyOverhead):
            self.metrics.proxy_overhead.record(item.seconds, {})
        else:
            apply_event(self.metrics, item, server=server)

    def _down(self, server: str, crashed: bool, lost: int) -> None:
        self._live[server] -= 1
        if crashed and lost:
            self.metrics.inflight_lost.add(lost, {"server": server})
        if self._live[server] <= 0:
            del self._live[server]
            if crashed:
                self.metrics.server_up.set(0, {"server": server})
            else:
                self.metrics.server_up.remove({"server": server})


async def _run(config: Config) -> int:
    metrics = Metrics()
    try:
        ingest = await Hub(metrics).serve(_INGEST_HOST, config.hub_port)
    except OSError:
        print(
            f"talyx: hub already running on {_INGEST_HOST}:{config.hub_port}", file=sys.stderr
        )
        return 0
    try:
        start_http_server(config.metrics_port, addr=config.metrics_host, registry=metrics.registry)
    except OSError as exc:
        print(
            f"talyx: hub cannot bind metrics {config.metrics_host}:{config.metrics_port}: {exc}",
            file=sys.stderr,
        )
        ingest.close()
        await ingest.wait_closed()
        return 1
    try:
        await ingest.serve_forever()
    finally:
        ingest.close()
    return 0


def run_hub(config: Config) -> int:
    try:
        return asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 0
