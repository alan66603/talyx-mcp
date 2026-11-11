# Metrics

English | [繁體中文](metrics.zh-TW.md)

Talyx exposes Prometheus metrics on `/metrics` (default port `9464`). It
**does not record tool argument contents** or message bodies — only method/tool names,
outcomes, timings, and cycle structure. The one correlation key it needs (the
sealed `requestState` token) is **hashed in memory and never stored** in any
label, log, or trace.

## Core reliability

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `talyx_requests_total` | counter | `method`, `server`, `status` | Requests completed, by method, server, and `ok`/`error`. |
| `talyx_request_duration_seconds` | histogram | `method`, `server` | Round-trip latency, request→terminal response. |
| `talyx_errors_total` | counter | `method`, `server`, `error_code` | Failed requests, by JSON-RPC error code (`app_error` for a tool-level `isError`). |

<p align="center">
  <img src="images/m-requests.svg" width="400" alt="talyx_requests_total: every request counted once, by method, server and status">
  <img src="images/m-errors.svg" width="400" alt="talyx_errors_total: JSON-RPC errors and tool isError results, by error_code">
</p>

<p align="center">
  <img src="images/m-duration.svg" width="400" alt="talyx_request_duration_seconds: a slow request lands in the tail and moves p95">
</p>

## Multi Round-Trip cycles (the flagship)

A `2026-07-28` server can answer a `tools/call` with an `InputRequiredResult`
(`resultType: "input_required"`) instead of a terminal result; the client
re-sends carrying the server's `requestState` token. That elicitation loop is
the differentiator — generic APM sees two unrelated requests; Talyx sees one
cycle.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `talyx_input_required_total` | counter | `method`, `server`, `wait_type` | Cycle legs entered (each `InputRequiredResult`). |
| `talyx_round_trips_per_request` | histogram | `wait_type` | Round-trips one logical request took. |
| `talyx_round_trip_cycle_duration_seconds` | histogram | `wait_type` | Whole-cycle latency, first request → terminal. |
| `talyx_input_wait_duration_seconds` | histogram | `server`, `wait_type` | Time each cycle spent waiting for the follow-up. |
| `talyx_abandoned_cycles_total` | counter | `server`, `wait_type` | Cycles that got `InputRequired` but no follow-up in time. |
| `talyx_request_state_rejected_total` | counter | `server` | Responses that rejected a `requestState`. |

`wait_type` is `input` (the leg carries `inputRequests` — waiting on a
human/client) or `poll` (state-only — the server asked the client to retry).

<p align="center">
  <img src="images/m-input-required.svg" width="400" alt="talyx_input_required_total: the server answers input_required and a cycle begins">
  <img src="images/m-round-trips.svg" width="400" alt="talyx_round_trips_per_request: one request takes several round trips; more than 10 means a runaway loop">
</p>

<p align="center">
  <img src="images/m-input-wait.svg" width="400" alt="cycle_duration minus input_wait: the human wait is cut out, leaving the server-side time to alert on">
  <img src="images/m-abandoned.svg" width="400" alt="talyx_abandoned_cycles_total: the server asks for input and no follow-up ever arrives">
</p>

<p align="center">
  <img src="images/m-state-rejected.svg" width="400" alt="talyx_request_state_rejected_total: the server rejects a requestState with reason invalid_request_state">
  <img src="images/m-wait-type.svg" width="400" alt="wait_type: input waits on a human, poll retries with backoff">
</p>

## Basic liveness & self

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `talyx_server_up` | gauge | `server` | `1` while the wrapped server subprocess is alive; `0` if it exits by itself while the client is still connected (crash). A normal exit after the client closed removes the series (clean, no alert). |
| `talyx_inflight_requests_lost_total` | counter | `server` | In-flight requests dropped when the subprocess went down. |
| `talyx_proxy_overhead_seconds` | histogram | — | Wall-clock time talyx spends observing each forwarded chunk. |

## Label cardinality

`method`, `server`, and `wait_type` are all low-cardinality and bounded.
Talyx never uses session ids, conversation ids, tool arguments, or the raw
`requestState` as labels — those are unbounded and would blow up Prometheus.
(The protocol has no session concept as of `2026-07-28`.)

## Status & error semantics

A request counts as `status="error"` when either the response carries a
JSON-RPC `error`, or its `result` has `isError: true` (a tool-level failure
returned as a successful JSON-RPC response). `error_code` is the JSON-RPC
`error.code`; a tool-level `isError` has no code and lands under `app_error`.

Intermediate `input_required` legs do **not** count toward `talyx_requests_total`
— only the terminal leg does, with that leg's duration — so core latency stays
free of human-wait time.

`talyx_request_state_rejected_total` is discriminated on `error.data.reason ==
"invalid_request_state"`, **not** on the code (which is the generic `-32602`).
There is no `reason` label: the specific cause (expired/tampered/audience) is
server-log-only.

## Example queries

Error rate (fraction of requests failing over 5m):

```promql
sum(rate(talyx_errors_total[5m])) / sum(rate(talyx_requests_total[5m]))
```

p95 request latency by method:

```promql
histogram_quantile(0.95, sum(rate(talyx_request_duration_seconds_bucket[5m])) by (le, method))
```

Abandoned-cycle rate (the flagship "agent silently stuck" signal):

```promql
sum(rate(talyx_abandoned_cycles_total[5m]))
```

Runaway cycles — anything past the `le=10` bucket blew the client's default cap:

```promql
sum(rate(talyx_round_trips_per_request_bucket{le="+Inf"}[5m]))
  - sum(rate(talyx_round_trips_per_request_bucket{le="10.0"}[5m]))
```

Server-side cycle time — **subtract the human wait** so a slow user doesn't page
you; alert on this, not on raw `cycle_duration`:

```promql
(
  sum(rate(talyx_round_trip_cycle_duration_seconds_sum[5m]))
    - sum(rate(talyx_input_wait_duration_seconds_sum[5m]))
) / sum(rate(talyx_round_trip_cycle_duration_seconds_count[5m]))
```

Proxy overhead p95:

```promql
histogram_quantile(0.95, sum(rate(talyx_proxy_overhead_seconds_bucket[5m])) by (le))
```
