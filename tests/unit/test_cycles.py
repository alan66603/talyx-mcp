"""Multi Round-Trip cycle tracking: the parser and the five cycle metrics."""

from __future__ import annotations

import json

import pytest

from talyx.metrics.registry import Metrics, apply_event
from talyx.proxy.jsonrpc import (
    CycleAbandoned,
    CycleCompleted,
    InputRequired,
    InputWaitCompleted,
    RequestCompleted,
    RequestStateRejected,
    SessionTracker,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_tracker(abandon: float = 300.0) -> tuple[SessionTracker, FakeClock]:
    clock = FakeClock()
    return SessionTracker(abandon_timeout_seconds=abandon, clock=clock), clock


def line(msg: dict) -> bytes:
    return json.dumps(msg).encode() + b"\n"


def call(id_: int, *, state: str | None = None) -> bytes:
    params: dict = {"name": "transfer"}
    if state is not None:
        params["requestState"] = state
    return line({"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params})


def input_required(id_: int, *, state: str, poll: bool = False) -> bytes:
    result: dict = {"resultType": "input_required", "requestState": state}
    if not poll:
        result["inputRequests"] = [{"prompt": "confirm?"}]
    return line({"jsonrpc": "2.0", "id": id_, "result": result})


def terminal(id_: int) -> bytes:
    return line({"jsonrpc": "2.0", "id": id_, "result": {"content": [{"text": "done"}]}})


def reject(id_: int, *, reason: str = "invalid_request_state") -> bytes:
    error = {"code": -32602, "message": "Invalid params", "data": {"reason": reason}}
    return line({"jsonrpc": "2.0", "id": id_, "error": error})


def only(events: list, kind: type) -> list:
    return [e for e in events if isinstance(e, kind)]


def test_single_round_input_cycle() -> None:
    tracker, clock = make_tracker()

    tracker.feed_client(call(1))
    legs = tracker.feed_server(input_required(1, state="S1"))
    assert legs == [InputRequired(method="tools/call", wait_type="input")]
    # An InputRequiredResult is not a terminal completion.
    assert only(legs, RequestCompleted) == []
    assert tracker.waiting_cycle_count == 1

    clock.advance(4.0)  # human takes 4s to confirm
    followup = tracker.feed_client(call(2, state="S1"))
    [waited] = only(followup, InputWaitCompleted)
    assert waited.wait_type == "input"
    assert waited.wait_seconds == 4.0
    assert tracker.waiting_cycle_count == 0

    clock.advance(0.2)  # server processes the final leg
    done = tracker.feed_server(terminal(2))
    [cycle] = only(done, CycleCompleted)
    assert cycle.wait_type == "input"
    assert cycle.round_trips == 2
    assert cycle.cycle_seconds == pytest.approx(4.2)  # first request → terminal
    # Core completion uses the final leg's duration, not the human-inflated cycle.
    [completed] = only(done, RequestCompleted)
    assert completed.duration_seconds == pytest.approx(0.2)
    assert completed.ok is True


def test_multi_round_cycle_counts_every_leg() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(call(1))
    tracker.feed_server(input_required(1, state="S1"))
    tracker.feed_client(call(2, state="S1"))
    tracker.feed_server(input_required(2, state="S2"))
    tracker.feed_client(call(3, state="S2"))
    done = tracker.feed_server(terminal(3))
    [cycle] = only(done, CycleCompleted)
    assert cycle.round_trips == 3


def test_poll_cycle_has_poll_wait_type() -> None:
    tracker, clock = make_tracker()
    tracker.feed_client(call(1))
    legs = tracker.feed_server(input_required(1, state="P1", poll=True))
    assert legs == [InputRequired(method="tools/call", wait_type="poll")]

    clock.advance(0.25)
    followup = tracker.feed_client(call(2, state="P1"))
    [waited] = only(followup, InputWaitCompleted)
    assert waited.wait_type == "poll"

    done = tracker.feed_server(terminal(2))
    [cycle] = only(done, CycleCompleted)
    assert cycle.wait_type == "poll"


def test_abandoned_cycle_is_swept_after_timeout() -> None:
    tracker, clock = make_tracker(abandon=30.0)
    tracker.feed_client(call(1))
    tracker.feed_server(input_required(1, state="S1"))
    assert tracker.waiting_cycle_count == 1

    clock.advance(31.0)  # follow-up never comes
    swept = tracker.feed_client(line({"jsonrpc": "2.0", "method": "notifications/ping"}))
    assert only(swept, CycleAbandoned) == [CycleAbandoned(wait_type="input")]
    assert tracker.waiting_cycle_count == 0


def test_cycle_within_timeout_is_not_abandoned() -> None:
    tracker, clock = make_tracker(abandon=30.0)
    tracker.feed_client(call(1))
    tracker.feed_server(input_required(1, state="S1"))
    clock.advance(29.0)
    followup = tracker.feed_client(call(2, state="S1"))
    assert only(followup, CycleAbandoned) == []
    assert len(only(followup, InputWaitCompleted)) == 1


def test_wrong_request_state_does_not_correlate() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(call(1))
    tracker.feed_server(input_required(1, state="S1"))
    # Follow-up echoes a different token: not this cycle.
    followup = tracker.feed_client(call(2, state="OTHER"))
    assert only(followup, InputWaitCompleted) == []
    assert tracker.waiting_cycle_count == 1  # original cycle still waiting


def test_followup_without_request_state_is_a_plain_request() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(call(1))
    tracker.feed_server(input_required(1, state="S1"))
    followup = tracker.feed_client(call(2))  # no requestState
    assert only(followup, InputWaitCompleted) == []


def drive_all(metrics: Metrics, tracker: SessionTracker, chunks: list) -> None:
    for feed, data in chunks:
        for event in feed(data):
            apply_event(metrics, event)


def test_metrics_populate_end_to_end() -> None:
    metrics = Metrics(server="bank")
    tracker, clock = make_tracker()

    drive_all(
        metrics,
        tracker,
        [
            (tracker.feed_client, call(1)),
            (tracker.feed_server, input_required(1, state="S1")),
        ],
    )
    clock.advance(3.0)
    drive_all(
        metrics,
        tracker,
        [
            (tracker.feed_client, call(2, state="S1")),
            (tracker.feed_server, terminal(2)),
        ],
    )

    reg = metrics.registry
    assert (
        reg.get_sample_value(
            "talyx_input_required_total",
            {"method": "tools/call", "server": "bank", "wait_type": "input"},
        )
        == 1.0
    )
    assert (
        reg.get_sample_value(
            "talyx_input_wait_duration_seconds_count", {"server": "bank", "wait_type": "input"}
        )
        == 1.0
    )
    assert (
        reg.get_sample_value(
            "talyx_input_wait_duration_seconds_sum", {"server": "bank", "wait_type": "input"}
        )
        == 3.0
    )
    assert (
        reg.get_sample_value("talyx_round_trips_per_request_count", {"wait_type": "input"}) == 1.0
    )
    assert (
        reg.get_sample_value(
            "talyx_round_trip_cycle_duration_seconds_count", {"wait_type": "input"}
        )
        == 1.0
    )


def test_request_state_rejection_is_not_a_completion() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(call(1))
    tracker.feed_server(input_required(1, state="S1"))
    tracker.feed_client(call(2, state="S1"))  # follow-up closes the input_wait
    done = tracker.feed_server(reject(2))

    assert only(done, RequestStateRejected) == [RequestStateRejected()]
    # A rejection ends the cycle abnormally: it must not land in the completion
    # histograms.
    assert only(done, CycleCompleted) == []
    [rc] = only(done, RequestCompleted)
    assert rc.ok is False
    assert rc.error_code == -32602


def test_generic_invalid_params_is_not_a_state_rejection() -> None:
    # Discriminate on data.reason, not the code: a bare -32602 is not a rejection.
    tracker, _ = make_tracker()
    tracker.feed_client(call(1))
    done = tracker.feed_server(
        line({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}})
    )
    assert only(done, RequestStateRejected) == []


def test_rejected_metric_populates() -> None:
    metrics = Metrics(server="bank")
    tracker, _ = make_tracker()
    drive_all(
        metrics,
        tracker,
        [
            (tracker.feed_client, call(1)),
            (tracker.feed_server, input_required(1, state="S1")),
            (tracker.feed_client, call(2, state="S1")),
            (tracker.feed_server, reject(2)),
        ],
    )
    assert (
        metrics.registry.get_sample_value("talyx_request_state_rejected_total", {"server": "bank"})
        == 1.0
    )


def test_abandoned_metric_populates() -> None:
    metrics = Metrics(server="bank")
    tracker, clock = make_tracker(abandon=10.0)
    drive_all(
        metrics,
        tracker,
        [
            (tracker.feed_client, call(1)),
            (tracker.feed_server, input_required(1, state="S1", poll=True)),
        ],
    )
    clock.advance(11.0)
    drive_all(metrics, tracker, [(tracker.feed_client, b"")])
    assert (
        metrics.registry.get_sample_value(
            "talyx_abandoned_cycles_total", {"server": "bank", "wait_type": "poll"}
        )
        == 1.0
    )
