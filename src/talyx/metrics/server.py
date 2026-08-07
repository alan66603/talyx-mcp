from __future__ import annotations

import sys
from threading import Thread
from wsgiref.simple_server import WSGIServer

from prometheus_client import start_http_server

from talyx.metrics.registry import Metrics


def start_metrics_server(
    metrics: Metrics, host: str, port: int
) -> tuple[WSGIServer, Thread] | None:
    """Serve /metrics on a daemon thread. Best-effort: never break the proxy.

    Returns the (server, thread) pair, or None if the endpoint could not be
    bound. Monitoring must never take down the wrapped MCP server, so a bind
    failure is logged to stderr and swallowed rather than raised.
    """
    try:
        return start_http_server(port, addr=host, registry=metrics.registry)
    except OSError as exc:
        print(
            f"talyx: metrics endpoint disabled, could not bind {host}:{port}: {exc}",
            file=sys.stderr,
        )
        return None
