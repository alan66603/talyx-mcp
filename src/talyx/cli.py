from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from time import perf_counter

from talyx.config import Config
from talyx.metrics.registry import Metrics, apply_event
from talyx.metrics.server import start_metrics_server
from talyx.proxy.jsonrpc import Event, SessionTracker
from talyx.proxy.stdio import StdioProxy

USAGE = "usage: talyx -- <server command> [args...]"


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

    def __init__(self, tracker: SessionTracker, metrics: Metrics) -> None:
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

    def __init__(self, tracker: SessionTracker, metrics: Metrics) -> None:
        self._tracker = tracker
        self._metrics = metrics

    def up(self) -> None:
        self._metrics.server_up.set(1)

    def down(self) -> None:
        self._metrics.server_up.set(0)
        # Requests still awaiting a response when the child dies are lost — the
        # server that would have answered them is gone.
        lost = self._tracker.pending_count
        if lost:
            self._metrics.inflight_lost.add(lost, {"server": self._metrics.server})


def main(argv: list[str] | None = None) -> int:
    command = parse_command(sys.argv[1:] if argv is None else list(argv))
    if not command:
        print(USAGE, file=sys.stderr)
        return 2

    config = Config.from_env()
    metrics = Metrics(
        server=server_name(config, command), otlp_endpoint=config.otlp_endpoint
    )
    start_metrics_server(metrics, config.metrics_host, config.metrics_port)

    tracker = SessionTracker(abandon_timeout_seconds=config.abandoned_timeout_seconds)
    recorder = _Recorder(tracker, metrics)
    lifecycle = _Lifecycle(tracker, metrics)
    proxy = StdioProxy(
        command,
        on_client_bytes=recorder.on_client_bytes,
        on_server_bytes=recorder.on_server_bytes,
        on_child_up=lifecycle.up,
        on_child_down=lifecycle.down,
    )
    return asyncio.run(proxy.run())


if __name__ == "__main__":
    sys.exit(main())
