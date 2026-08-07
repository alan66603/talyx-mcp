from __future__ import annotations

from opentelemetry import trace

from talyx.telemetry import extract_trace_context, trace_context_carrier

# A well-formed W3C traceparent (version 00) and its embedded ids.
_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


def msg_with_meta(meta: dict | None) -> dict:
    params: dict = {"name": "transfer"}
    if meta is not None:
        params["_meta"] = meta
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}


def test_extract_links_to_the_caller_trace() -> None:
    ctx = extract_trace_context(msg_with_meta({"traceparent": _TRACEPARENT}))
    assert ctx is not None
    span_ctx = trace.get_current_span(ctx).get_span_context()
    assert format(span_ctx.trace_id, "032x") == _TRACE_ID
    assert span_ctx.is_remote  # it came from the wire, not from us


def test_carrier_collects_all_three_keys() -> None:
    meta = {"traceparent": _TRACEPARENT, "tracestate": "vendor=x", "baggage": "k=v", "other": 1}
    carrier = trace_context_carrier(msg_with_meta(meta))
    assert carrier == {"traceparent": _TRACEPARENT, "tracestate": "vendor=x", "baggage": "k=v"}


def test_absent_meta_yields_no_context() -> None:
    assert extract_trace_context(msg_with_meta(None)) is None
    assert extract_trace_context({"jsonrpc": "2.0", "id": 1, "method": "ping"}) is None


def test_meta_without_trace_keys_yields_no_context() -> None:
    assert extract_trace_context(msg_with_meta({"io.modelcontextprotocol/clientInfo": {}})) is None


def test_extraction_does_not_mutate_the_message() -> None:
    message = msg_with_meta({"traceparent": _TRACEPARENT})
    before = repr(message)
    extract_trace_context(message)
    assert repr(message) == before  # observation must be read-only
