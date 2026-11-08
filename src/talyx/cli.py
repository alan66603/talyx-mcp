from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from time import perf_counter

from talyx.config import Config
from talyx.hub.client import HubClient, spawn_hub
from talyx.hub.server import run_hub
from talyx.hub.wire import ChildDown, ChildUp, ProxyOverhead
from talyx.metrics.registry import Metrics, apply_event
from talyx.metrics.server import start_metrics_server
from talyx.proxy.jsonrpc import Event, TrafficObserver
from talyx.proxy.stdio import StdioProxy

USAGE = "usage: talyx -- <server command> [args...]"

_HUB_HOST = "127.0.0.1"


def parse_command(argv: list[str]) -> list[str]:
    """Extract the wrapped server command: everything after an optional `--`."""
    if argv and argv[0] == "--":
        argv = argv[1:]
    return argv


def server_name(config: Config, command: list[str]) -> str:
    """The `server` metric label: explicit config, else the command's basename."""
    if config.server_name:
        return config.server_name
    return os.path.basename(command[0]) if command else "default"


class _Recorder:
    """Bridges observed traffic to metrics, timing its own overhead per chunk."""

    def __init__(self, tracker: TrafficObserver, metrics: Metrics) -> None:
        self._tracker = tracker
        self._metrics = metrics

    def on_client_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_client, chunk)

    def on_server_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_server, chunk)

    def _record(self, feed: Callable[[bytes], list[Event]], chunk: bytes) -> None:
        start = perf_counter()
        for event in feed(chunk):
            apply_event(self._metrics, event)
        self._metrics.proxy_overhead.record(perf_counter() - start, {})


class _Lifecycle:
    """Basic liveness: child up/down, and in-flight loss when the child dies."""

    def __init__(self, tracker: TrafficObserver, metrics: Metrics) -> None:
        self._tracker = tracker
        self._metrics = metrics

    def up(self) -> None:
        self._metrics.server_up.set(1, {"server": self._metrics.server})

    def down(self, crashed: bool) -> None:
        labels = {"server": self._metrics.server}
        if not crashed:
            # Client closed first and the server followed: a clean shutdown, not
            # an outage, so drop the series instead of reporting 0.
            self._metrics.server_up.remove(labels)
            return
        self._metrics.server_up.set(0, labels)
        # Requests still awaiting a response when the child dies are lost — the
        # server that would have answered them is gone.
        lost = self._tracker.pending_count
        if lost:
            self._metrics.inflight_lost.add(lost, {"server": self._metrics.server})


class _HubRecorder:
    """Hub mode: ships observed events (and per-chunk overhead) to the hub."""

    def __init__(self, tracker: TrafficObserver, client: HubClient, server: str) -> None:
        self._tracker = tracker
        self._client = client
        self._server = server

    def on_client_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_client, chunk)

    def on_server_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_server, chunk)

    def _record(self, feed: Callable[[bytes], list[Event]], chunk: bytes) -> None:
        start = perf_counter()
        for event in feed(chunk):
            self._client.send(self._server, event)
        self._client.send(self._server, ProxyOverhead(perf_counter() - start))


class _HubLifecycle:
    """Hub mode: child up/down; the hub decides what that means for server_up."""

    def __init__(self, tracker: TrafficObserver, client: HubClient, server: str) -> None:
        self._tracker = tracker
        self._client = client
        self._server = server

    def up(self) -> None:
        self._client.send(self._server, ChildUp())

    def down(self, crashed: bool) -> None:
        lost = self._tracker.pending_count if crashed else 0
        self._client.send(self._server, ChildDown(crashed=crashed, inflight_lost=lost))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["hub"]:
        return run_hub(Config.from_env())
    command = parse_command(args)
    if not command:
        print(USAGE, file=sys.stderr)
        return 2

    config = Config.from_env()
    server = server_name(config, command)
    tracker = TrafficObserver(abandon_timeout_seconds=config.abandoned_timeout_seconds)
    client: HubClient | None = None
    recorder: _Recorder | _HubRecorder
    lifecycle: _Lifecycle | _HubLifecycle
    if config.hub_enabled:
        client = HubClient(_HUB_HOST, config.hub_port, spawn=spawn_hub)
        recorder = _HubRecorder(tracker, client, server)
        lifecycle = _HubLifecycle(tracker, client, server)
    else:
        metrics = Metrics(server=server, otlp_endpoint=config.otlp_endpoint)
        start_metrics_server(metrics, config.metrics_host, config.metrics_port)
        recorder = _Recorder(tracker, metrics)
        lifecycle = _Lifecycle(tracker, metrics)

    proxy = StdioProxy(
        command,
        on_client_bytes=recorder.on_client_bytes,
        on_server_bytes=recorder.on_server_bytes,
        on_child_up=lifecycle.up,
        on_child_down=lifecycle.down,
    )
    try:
        return asyncio.run(proxy.run())
    finally:
        # on_child_down has queued the final ChildDown by now; flush it (bounded).
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
