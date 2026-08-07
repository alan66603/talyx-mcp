from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

# MCP stdio framing: one JSON-RPC message per newline-delimited UTF-8 line.
# A line longer than this is forwarded untouched but skipped for observation,
# so a pathological message can never grow our buffer without bound.
_MAX_BUFFERED_LINE = 16 * 1024 * 1024

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_ABANDON_TIMEOUT_SECONDS = 300.0

# Multi Round-Trip wire discriminator (SEP-2322). The value is snake_case even
# though the surrounding field names are camelCase; writing it camelCase makes
# the parser never match, so every cycle metric silently stays 0.
_INPUT_REQUIRED = "input_required"

# wait_type label values: a leg carrying inputRequests is waiting on a
# human/client; a state-only leg is the server asking the client to poll.
WAIT_INPUT = "input"
WAIT_POLL = "poll"

# requestState-rejection discriminator. It rides in error.data.reason, NOT the
# error code — the code is the generic -32602 (SEP-2164), and the SDK
# deliberately keeps the wire message vague, so this constant string is the only
# stable signal (server/request_state.py:_reject).
_INVALID_REQUEST_STATE = "invalid_request_state"


@dataclass(frozen=True)
class RequestSeen:
    """A client-originated request or notification passed through the proxy."""

    method: str
    tool: str | None
    notification: bool


@dataclass(frozen=True)
class RequestCompleted:
    """A response paired back to its request by JSON-RPC id.

    For a Multi Round-Trip cycle this is emitted only on the terminal leg, with
    that leg's duration — the human-wait time lives in the cycle metrics, not
    here, so core latency stays free of it.
    """

    method: str
    tool: str | None
    duration_seconds: float
    ok: bool
    # JSON-RPC `error.code` when the response carried a protocol error; None for
    # a success or a tool-level failure (`result.isError`), which has no code.
    error_code: int | None = None


@dataclass(frozen=True)
class InputRequired:
    """A server returned an InputRequiredResult, i.e. a cycle leg entered a wait."""

    method: str
    wait_type: str


@dataclass(frozen=True)
class InputWaitCompleted:
    """The client's follow-up arrived, closing one input_wait segment of a cycle."""

    wait_type: str
    wait_seconds: float


@dataclass(frozen=True)
class CycleCompleted:
    """A Multi Round-Trip cycle reached a terminal result."""

    wait_type: str
    round_trips: int
    cycle_seconds: float


@dataclass(frozen=True)
class CycleAbandoned:
    """A cycle got an InputRequiredResult but no follow-up within the window."""

    wait_type: str


@dataclass(frozen=True)
class RequestStateRejected:
    """A response rejected a requestState (error.data.reason marks it).

    Carries no fields: the discriminating reason on the wire is a single
    constant, and the specific cause (expired/tampered/audience) is
    server-log-only, so there is deliberately no `reason` label to add.
    """


Event = (
    RequestSeen
    | RequestCompleted
    | InputRequired
    | InputWaitCompleted
    | CycleCompleted
    | CycleAbandoned
    | RequestStateRejected
)


@dataclass
class _Cycle:
    method: str
    tool: str | None
    started_at: float  # first leg's request time = start of the logical request
    rounds: int  # legs seen so far
    wait_type: str  # the most recent leg's wait_type
    wait_started_at: float  # when the outstanding InputRequiredResult was observed


@dataclass
class _Pending:
    method: str
    tool: str | None
    started_at: float
    cycle: _Cycle | None = None  # set when this leg continues a cycle


class _LineSplitter:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._skipping = False

    def feed(self, data: bytes) -> list[bytes]:
        lines: list[bytes] = []
        self._buf += data
        while (nl := self._buf.find(b"\n")) != -1:
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if self._skipping:
                self._skipping = False  # tail of an oversized line: drop it
            else:
                lines.append(line)
        if len(self._buf) > _MAX_BUFFERED_LINE:
            self._buf.clear()
            self._skipping = True
        return lines


class SessionTracker:
    """Observes proxied JSON-RPC traffic and pairs requests with responses.

    Purely observational: it never modifies traffic, and a line it cannot
    parse is silently ignored — forwarding correctness must never depend on
    what this class understands. Pending requests expire after `ttl_seconds`
    so a request that never gets a response cannot leak memory.

    It also tracks Multi Round-Trip cycles (SEP-2322): an InputRequiredResult
    opens a cycle, keyed by the sha256 of its sealed `requestState` token (the
    raw token never leaves this object), and the client's follow-up request —
    which echoes the same token in `params.requestState` — continues it. A
    cycle whose follow-up never arrives within `abandon_timeout_seconds` is
    swept and reported abandoned.
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        abandon_timeout_seconds: float = DEFAULT_ABANDON_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._abandon_timeout = abandon_timeout_seconds
        self._clock = clock
        self._pending: dict[str | int, _Pending] = {}
        # Cycles awaiting their follow-up, keyed by the hashed requestState.
        self._waiting: dict[str, _Cycle] = {}
        self._client_lines = _LineSplitter()
        self._server_lines = _LineSplitter()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def waiting_cycle_count(self) -> int:
        return len(self._waiting)

    def feed_client(self, data: bytes) -> list[Event]:
        """Observe bytes flowing client -> server."""
        events: list[Event] = self._sweep_abandoned()
        for line in self._client_lines.feed(data):
            events.extend(self._observe_client_line(line))
        return events

    def feed_server(self, data: bytes) -> list[Event]:
        """Observe bytes flowing server -> client."""
        events: list[Event] = self._sweep_abandoned()
        for line in self._server_lines.feed(data):
            events.extend(self._observe_server_line(line))
        return events

    def _observe_client_line(self, line: bytes) -> list[Event]:
        msg = _parse_message(line)
        if msg is None:  # Not JSON / Bad data
            return []
        method = msg.get("method")
        if not isinstance(method, str):
            # No method: this is the client answering a server-initiated
            # request (e.g. sampling). Not ours to track in the MVP.
            return []

        tool = _tool_name(method, msg.get("params"))
        events: list[Event] = []

        # A follow-up leg echoes the sealed requestState it was handed; matching
        # its hash to a waiting cycle links the leg and closes the input_wait.
        cycle = self._continue_cycle(msg, method, tool)
        if cycle is not None:
            events.append(
                InputWaitCompleted(cycle.wait_type, self._clock() - cycle.wait_started_at)
            )

        msg_id = msg.get("id")
        notification = msg_id is None
        if not notification and isinstance(msg_id, str | int):
            self._evict_expired()
            self._pending[msg_id] = _Pending(method, tool, self._clock(), cycle=cycle)
        events.append(RequestSeen(method=method, tool=tool, notification=notification))
        return events

    def _continue_cycle(self, msg: dict, method: str, tool: str | None) -> _Cycle | None:
        params = msg.get("params")
        state = params.get("requestState") if isinstance(params, dict) else None
        if not isinstance(state, str):
            return None
        cycle = self._waiting.pop(_hash_state(state), None)
        if cycle is not None:
            cycle.rounds += 1  # this leg is another round-trip of the cycle
        return cycle

    def _observe_server_line(self, line: bytes) -> list[Event]:
        msg = _parse_message(line)
        if msg is None:
            return []
        if "method" in msg:
            # Server-initiated request/notification, not a response: its id
            # lives in a separate id space and must not touch our table.
            return []
        if "result" not in msg and "error" not in msg:
            return []
        msg_id = msg.get("id")
        if not isinstance(msg_id, str | int):
            return []

        pending = self._pending.pop(msg_id, None)
        if pending is None:
            return []
        now = self._clock()
        elapsed = now - pending.started_at
        if elapsed > self._ttl:
            return []  # response arrived after we gave up on this request

        result = msg.get("result")
        if isinstance(result, dict) and result.get("resultType") == _INPUT_REQUIRED:
            return self._open_or_extend_cycle(pending, result, now)
        return self._complete_request(pending, msg, result, elapsed, now)

    def _open_or_extend_cycle(self, pending: _Pending, result: dict, now: float) -> list[Event]:
        wait_type = WAIT_INPUT if result.get("inputRequests") is not None else WAIT_POLL
        cycle = pending.cycle
        if cycle is None:
            # First InputRequiredResult of this logical request: open the cycle,
            # counting this leg as round 1.
            cycle = _Cycle(
                method=pending.method,
                tool=pending.tool,
                started_at=pending.started_at,
                rounds=1,
                wait_type=wait_type,
                wait_started_at=now,
            )
        else:
            cycle.wait_type = wait_type
            cycle.wait_started_at = now

        # Correlation needs the requestState value; the type invariant allows a
        # leg to carry inputRequests without one, which we can't link — emit the
        # InputRequired but leave the cycle un-tracked (no wait/abandon signal).
        state = result.get("requestState")
        if isinstance(state, str):
            self._waiting[_hash_state(state)] = cycle
        return [InputRequired(method=cycle.method, wait_type=wait_type)]

    def _complete_request(
        self, pending: _Pending, msg: dict, result: object, elapsed: float, now: float
    ) -> list[Event]:
        events: list[Event] = []
        error = msg.get("error")
        rejected = _is_state_rejection(error)
        if rejected:
            events.append(RequestStateRejected())

        # A rejected requestState is an abnormal end, not a completion, so it
        # stays out of the round_trips / cycle_duration histograms.
        if pending.cycle is not None and not rejected:
            cycle = pending.cycle
            events.append(
                CycleCompleted(
                    wait_type=cycle.wait_type,
                    round_trips=cycle.rounds,
                    cycle_seconds=now - cycle.started_at,
                )
            )

        error_code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(error_code, int):
            error_code = None
        tool_errored = isinstance(result, dict) and result.get("isError") is True
        ok = error is None and not tool_errored
        events.append(
            RequestCompleted(
                method=pending.method,
                tool=pending.tool,
                duration_seconds=elapsed,
                ok=ok,
                error_code=error_code,
            )
        )
        return events

    def _sweep_abandoned(self) -> list[Event]:
        now = self._clock()
        stale = [
            (key, cycle)
            for key, cycle in self._waiting.items()
            if now - cycle.wait_started_at > self._abandon_timeout
        ]
        for key, _ in stale:
            del self._waiting[key]
        return [CycleAbandoned(wait_type=cycle.wait_type) for _, cycle in stale]

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [key for key, p in self._pending.items() if now - p.started_at > self._ttl]
        for key in expired:
            del self._pending[key]


def _is_state_rejection(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    data = error.get("data")
    return isinstance(data, dict) and data.get("reason") == _INVALID_REQUEST_STATE


def _tool_name(method: str, params: object) -> str | None:
    if method != "tools/call" or not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) else None


def _hash_state(state: str) -> str:
    """Correlation key for a cycle: the sealed requestState is opaque and must
    never be persisted, so we key on a truncated sha256 of it and drop the raw
    value. The hash is used only as an in-memory dict key, never as a label."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()[:16]


def _parse_message(line: bytes) -> dict | None:
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None
