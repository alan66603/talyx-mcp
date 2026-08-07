"""SEP-414 trace-context reading.

The proxy forwards bytes verbatim, so it already *propagates* a client's
`traceparent` downstream untouched (the pass-through half of SEP-414). This
module adds the *read* half: pulling the W3C trace context out of a JSON-RPC
message's `_meta` so talyx can, in a later PR, parent spans/exemplars to the
caller's trace. It deliberately does not emit spans yet.

Wire location (SEP-414): trace context lives in `_meta`, under the request's
`params`. The keys used here are the W3C standard names (`traceparent` /
`tracestate` / `baggage`). MCP also uses a reverse-DNS key convention for its
own `_meta` entries (e.g. `io.modelcontextprotocol/clientInfo`); if the final
spec namespaces the trace keys that way, only `_CARRIER_KEYS` needs to change.
This is the same "re-verify against the SDK" caveat the cycle parser carries.
"""

from __future__ import annotations

from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_PROPAGATOR = TraceContextTextMapPropagator()
_CARRIER_KEYS = ("traceparent", "tracestate", "baggage")


def trace_context_carrier(message: dict) -> dict[str, str]:
    """Extract the W3C carrier (traceparent/tracestate/baggage) from `_meta`."""
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict):
        return {}
    return {key: meta[key] for key in _CARRIER_KEYS if isinstance(meta.get(key), str)}


def extract_trace_context(message: dict) -> Context | None:
    """Return an OTel Context for the caller's trace, or None if absent.

    Read-only: the message is never mutated. The returned Context can be passed
    as the parent when starting a span, so talyx's observations link to the
    trace the client already started.
    """
    carrier = trace_context_carrier(message)
    if not carrier:
        return None
    return _PROPAGATOR.extract(carrier)
