from __future__ import annotations

import socket
import urllib.request

from talyx.metrics.registry import Metrics
from talyx.metrics.server import start_metrics_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_metrics(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        return resp.read().decode()


def test_endpoint_serves_recorded_values() -> None:
    metrics = Metrics(server="demo")
    metrics.requests.add(1, {"method": "tools/call", "server": "demo", "status": "ok"})

    port = free_port()
    server, _thread = start_metrics_server(metrics, "127.0.0.1", port)
    try:
        body = get_metrics(port)
    finally:
        server.shutdown()

    assert 'talyx_requests_total{method="tools/call",server="demo",status="ok"} 1.0' in body


def test_bind_failure_is_swallowed() -> None:
    # Hold a port so the metrics server cannot bind it; must return None, not raise.
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        result = start_metrics_server(Metrics(), "127.0.0.1", port)
    assert result is None
