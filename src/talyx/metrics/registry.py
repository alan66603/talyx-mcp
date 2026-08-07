from __future__ import annotations

from collections.abc import Iterable

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from prometheus_client import CollectorRegistry

from talyx.proxy.jsonrpc import (
    CycleAbandoned,
    CycleCompleted,
    Event,
    InputRequired,
    InputWaitCompleted,
    RequestCompleted,
    RequestStateRejected,
)


class _ObservableGaugeValue:
    """Backing store for an observable gauge.

    OTel *synchronous* gauges (`create_gauge`) only emit their value on the
    first collect after a `set`; the next scrape drops it, which would make a
    liveness gauge vanish from `/metrics` between Prometheus polls. An
    observable gauge re-reads this holder on every collect, so the last-set
    value persists. Exposes `.set(value, attributes)` to stay drop-in
    compatible with the lifecycle call sites.
    """

    def __init__(self) -> None:
        self._value = 0.0

    def set(self, value: float, attributes: object = None) -> None:
        self._value = float(value)

    def _observe(self, _options: CallbackOptions) -> Iterable[Observation]:
        yield Observation(self._value)

# Seconds-scale buckets for request latency.
_DURATION_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0,
)

# Sub-millisecond buckets: talyx's own per-chunk processing is microseconds,
# so the default seconds-scale buckets would collapse every sample into one.
_OVERHEAD_BUCKETS = (0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05)

# Round-trip counts per logical request. An explicit le=10 boundary matches the
# SDK's default input_required cap, so a runaway cycle (the client raises
# in-place, invisible on the wire) shows up as growth in the >10 tail.
_ROUND_TRIP_BUCKETS = (1.0, 2.0, 3.0, 5.0, 8.0, 10.0)

# Whole-cycle and input-wait latencies span server processing up to a human
# taking minutes to confirm a high-risk action, so buckets run seconds→minutes.
_CYCLE_BUCKETS = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)


def _build_otlp_reader(endpoint: str):
    """Periodic OTLP metrics reader. The OTLP exporter is an optional extra so
    the default install stays lean; a clear error points at the extra."""
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "OTLP export needs the 'otlp' extra: pip install 'talyx-mcp[otlp]'"
        ) from exc
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    return PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))


class Metrics:
    """The Talyx metrics, instrumented via the OpenTelemetry SDK.

    Internal instrumentation goes through OTel (metrics API); the default output
    is a native Prometheus `/metrics` endpoint via OTel's Prometheus exporter,
    which bridges into an isolated prometheus_client registry (`self.registry`)
    so instances don't share global state and tests stay isolated.

    Counter instruments are named without a `_total` suffix; the Prometheus
    exporter appends it (e.g. `talyx_requests_total`).
    """

    def __init__(
        self,
        server: str = "default",
        registry: CollectorRegistry | None = None,
        otlp_endpoint: str | None = None,
    ) -> None:
        self.server = server
        self.registry = registry if registry is not None else CollectorRegistry()
        # Prometheus /metrics is always on; OTLP is an optional extra reader on
        # the same MeterProvider, so both outputs see identical instruments.
        readers: list = [
            PrometheusMetricReader(
                disable_target_info=True,
                scope_info_enabled=False,
                registry=self.registry,
            )
        ]
        if otlp_endpoint:
            readers.append(_build_otlp_reader(otlp_endpoint))
        views = [
            View(
                instrument_name="talyx_request_duration_seconds",
                aggregation=ExplicitBucketHistogramAggregation(_DURATION_BUCKETS),
            ),
            View(
                instrument_name="talyx_proxy_overhead_seconds",
                aggregation=ExplicitBucketHistogramAggregation(_OVERHEAD_BUCKETS),
            ),
            View(
                instrument_name="talyx_round_trips_per_request",
                aggregation=ExplicitBucketHistogramAggregation(_ROUND_TRIP_BUCKETS),
            ),
            View(
                instrument_name="talyx_round_trip_cycle_duration_seconds",
                aggregation=ExplicitBucketHistogramAggregation(_CYCLE_BUCKETS),
            ),
            View(
                instrument_name="talyx_input_wait_duration_seconds",
                aggregation=ExplicitBucketHistogramAggregation(_CYCLE_BUCKETS),
            ),
        ]
        meter = MeterProvider(metric_readers=readers, views=views).get_meter("talyx")

        self.requests = meter.create_counter(
            "talyx_requests",
            description="Total MCP requests completed, by method, server and outcome.",
        )
        self.request_duration = meter.create_histogram(
            "talyx_request_duration_seconds",
            unit="s",
            description="MCP request round-trip latency, by method and server.",
        )
        self.errors = meter.create_counter(
            "talyx_errors",
            description="Total failed MCP requests, by method, server and error code.",
        )
        self.proxy_overhead = meter.create_histogram(
            "talyx_proxy_overhead_seconds",
            unit="s",
            description="Wall-clock time talyx spends observing each forwarded chunk.",
        )

        # Multi Round-Trip cycle metrics.
        self.input_required = meter.create_counter(
            "talyx_input_required",
            description="Times a server returned an InputRequiredResult (a cycle leg).",
        )
        self.round_trips = meter.create_histogram(
            "talyx_round_trips_per_request",
            description="Round-trips a single logical request took, by wait_type.",
        )
        self.cycle_duration = meter.create_histogram(
            "talyx_round_trip_cycle_duration_seconds",
            unit="s",
            description="Full cycle latency, first request to terminal result, by wait_type.",
        )
        self.input_wait = meter.create_histogram(
            "talyx_input_wait_duration_seconds",
            unit="s",
            description="Time a cycle spent waiting for the follow-up, by server and wait_type.",
        )
        self.abandoned_cycles = meter.create_counter(
            "talyx_abandoned_cycles",
            description="Cycles that got an InputRequiredResult but no follow-up in time.",
        )
        self.request_state_rejected = meter.create_counter(
            "talyx_request_state_rejected",
            description="Responses that rejected a requestState (invalid/expired/tampered).",
        )

        # Basic liveness: child subprocess up/down and in-flight loss.
        self.server_up = _ObservableGaugeValue()
        meter.create_observable_gauge(
            "talyx_server_up",
            callbacks=[self.server_up._observe],
            description="1 while the wrapped MCP server subprocess is alive, else 0.",
        )
        self.inflight_lost = meter.create_counter(
            "talyx_inflight_requests_lost",
            description="In-flight requests dropped when the server subprocess went down.",
        )


# Sentinel error_code label for a tool-level failure (`result.isError == true`):
# these carry no JSON-RPC numeric code, the error is in the application payload.
_APP_ERROR_CODE = "app_error"


def apply_event(metrics: Metrics, event: Event) -> None:
    """Translate one observed JSON-RPC event into metric updates.

    Counts happen at completion so `status`/`error_code` are known; a
    `RequestSeen` on its own carries no outcome and drives no metric.
    """
    server = metrics.server
    if isinstance(event, RequestCompleted):
        status = "ok" if event.ok else "error"
        metrics.requests.add(1, {"method": event.method, "server": server, "status": status})
        metrics.request_duration.record(
            event.duration_seconds, {"method": event.method, "server": server}
        )
        if not event.ok:
            code = str(event.error_code) if event.error_code is not None else _APP_ERROR_CODE
            metrics.errors.add(
                1, {"method": event.method, "server": server, "error_code": code}
            )
    elif isinstance(event, InputRequired):
        metrics.input_required.add(
            1, {"method": event.method, "server": server, "wait_type": event.wait_type}
        )
    elif isinstance(event, InputWaitCompleted):
        metrics.input_wait.record(
            event.wait_seconds, {"server": server, "wait_type": event.wait_type}
        )
    elif isinstance(event, CycleCompleted):
        metrics.round_trips.record(event.round_trips, {"wait_type": event.wait_type})
        metrics.cycle_duration.record(event.cycle_seconds, {"wait_type": event.wait_type})
    elif isinstance(event, CycleAbandoned):
        metrics.abandoned_cycles.add(1, {"server": server, "wait_type": event.wait_type})
    elif isinstance(event, RequestStateRejected):
        metrics.request_state_rejected.add(1, {"server": server})
