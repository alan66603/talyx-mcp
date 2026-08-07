from __future__ import annotations

import os
from dataclasses import dataclass

# 9464 is the conventional Prometheus-format exporter port; it deliberately
# avoids 9090, which Prometheus itself uses (the demo stack runs both).
DEFAULT_METRICS_PORT = 9464

# How long a cycle may sit waiting for its follow-up before we call it
# abandoned. Deliberately conservative: input_required waits are often a human
# confirming a high-risk action, so a too-eager window would flag live users as
# abandonment. Tune against the observed input_wait distribution.
DEFAULT_ABANDONED_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class Config:
    metrics_port: int = DEFAULT_METRICS_PORT
    metrics_host: str = "0.0.0.0"
    # Value of the `server` metric label. None → derive from the wrapped command.
    server_name: str | None = None
    abandoned_timeout_seconds: float = DEFAULT_ABANDONED_TIMEOUT_SECONDS
    # Optional OTLP metrics endpoint. None → Prometheus /metrics only (default).
    otlp_endpoint: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            metrics_port=int(os.environ.get("TALYX_METRICS_PORT", str(DEFAULT_METRICS_PORT))),
            metrics_host=os.environ.get("TALYX_METRICS_HOST", "0.0.0.0"),
            server_name=os.environ.get("TALYX_SERVER_NAME") or None,
            abandoned_timeout_seconds=float(
                os.environ.get(
                    "TALYX_ABANDONED_TIMEOUT_SECONDS", str(DEFAULT_ABANDONED_TIMEOUT_SECONDS)
                )
            ),
            otlp_endpoint=os.environ.get("TALYX_OTLP_ENDPOINT") or None,
        )
