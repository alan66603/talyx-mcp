from __future__ import annotations

import importlib.util

import pytest

from talyx.metrics.registry import Metrics, apply_event
from talyx.proxy.jsonrpc import RequestCompleted, RequestSeen


def _otlp_installed() -> bool:
    # find_spec raises (not returns None) when a parent package is missing.
    try:
        return (
            importlib.util.find_spec("opentelemetry.exporter.otlp.proto.http.metric_exporter")
            is not None
        )
    except ModuleNotFoundError:
        return False


_OTLP_INSTALLED = _otlp_installed()


def sample(metrics: Metrics, name: str, labels: dict[str, str] | None = None) -> float | None:
    return metrics.registry.get_sample_value(name, labels)


def req(method: str, server: str, status: str) -> dict[str, str]:
    return {"method": method, "server": server, "status": status}


def dur(method: str, server: str) -> dict[str, str]:
    return {"method": method, "server": server}


def err(method: str, server: str, code: str) -> dict[str, str]:
    return {"method": method, "server": server, "error_code": code}


def completed(
    metrics: Metrics,
    method: str = "tools/call",
    *,
    ok: bool = True,
    error_code: int | None = None,
    tool: str | None = "echo",
    duration: float = 0.1,
) -> None:
    apply_event(
        metrics,
        RequestCompleted(
            method=method, tool=tool, duration_seconds=duration, ok=ok, error_code=error_code
        ),
    )


def test_core_metrics_expose_expected_names() -> None:
    metrics = Metrics(server="demo")
    completed(metrics, method="tools/call", ok=True, duration=0.1)
    completed(metrics, method="tools/list", ok=False, error_code=-32602, tool=None)

    assert sample(metrics, "talyx_requests_total", req("tools/call", "demo", "ok")) == 1.0
    assert sample(metrics, "talyx_request_duration_seconds_count", dur("tools/call", "demo")) == 1.0
    assert sample(metrics, "talyx_request_duration_seconds_sum", dur("tools/call", "demo")) == 0.1

    assert sample(metrics, "talyx_requests_total", req("tools/list", "demo", "error")) == 1.0
    assert sample(metrics, "talyx_errors_total", err("tools/list", "demo", "-32602")) == 1.0
    # The doubled-suffix mistake must not be present.
    assert sample(metrics, "talyx_requests_total_total", req("tools/call", "demo", "ok")) is None


def test_server_label_comes_from_metrics_instance() -> None:
    metrics = Metrics(server="billing")
    completed(metrics, method="tools/call", ok=True)
    assert sample(metrics, "talyx_requests_total", req("tools/call", "billing", "ok")) == 1.0


def test_request_seen_alone_records_no_metric() -> None:
    # Counting happens at completion, where status is known; a bare RequestSeen
    # (including notifications) drives nothing.
    metrics = Metrics()
    apply_event(metrics, RequestSeen(method="resources/read", tool=None, notification=False))
    apply_event(
        metrics, RequestSeen(method="notifications/initialized", tool=None, notification=True)
    )
    assert sample(metrics, "talyx_requests_total", req("resources/read", "default", "ok")) is None


def test_completed_ok_has_no_error_sample() -> None:
    metrics = Metrics()
    completed(metrics, method="tools/call", ok=True)
    assert sample(metrics, "talyx_requests_total", req("tools/call", "default", "ok")) == 1.0
    assert sample(metrics, "talyx_errors_total", err("tools/call", "default", "app_error")) is None


def test_tool_level_error_uses_app_error_code() -> None:
    # A tool-level failure (result.isError) has no JSON-RPC code, so it lands
    # under the app_error sentinel, not a numeric code.
    metrics = Metrics()
    completed(metrics, method="tools/call", ok=False, error_code=None)
    assert sample(metrics, "talyx_errors_total", err("tools/call", "default", "app_error")) == 1.0
    assert sample(metrics, "talyx_requests_total", req("tools/call", "default", "error")) == 1.0


_OTLP_ENDPOINT = "http://127.0.0.1:4318/v1/metrics"


@pytest.mark.skipif(_OTLP_INSTALLED, reason="requires the otlp extra to be absent")
def test_otlp_endpoint_without_extra_raises_a_clear_error() -> None:
    with pytest.raises(RuntimeError, match="otlp"):
        Metrics(server="s", otlp_endpoint=_OTLP_ENDPOINT)


@pytest.mark.skipif(not _OTLP_INSTALLED, reason="requires the otlp extra installed")
def test_otlp_endpoint_keeps_prometheus_output() -> None:
    metrics = Metrics(server="s", otlp_endpoint=_OTLP_ENDPOINT)
    completed(metrics, method="tools/call", ok=True)
    # Prometheus /metrics stays intact when OTLP is also enabled.
    assert sample(metrics, "talyx_requests_total", req("tools/call", "s", "ok")) == 1.0
