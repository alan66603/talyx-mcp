from __future__ import annotations

import json

from talyx.proxy.jsonrpc import RequestCompleted, RequestSeen, SessionTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_tracker(ttl: float = 300.0) -> tuple[SessionTracker, FakeClock]:
    clock = FakeClock()
    return SessionTracker(ttl_seconds=ttl, clock=clock), clock


def line(msg: dict) -> bytes:
    return json.dumps(msg).encode() + b"\n"


def request(id_, method: str, params: dict | None = None) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return line(msg)


def response(id_, result: dict | None = None, error: dict | None = None) -> bytes:
    msg: dict = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    return line(msg)


def test_request_response_pairing_measures_duration() -> None:
    tracker, clock = make_tracker()
    [seen] = tracker.feed_client(request(1, "tools/list"))
    assert seen == RequestSeen(method="tools/list", tool=None, notification=False)

    clock.advance(0.25)
    [done] = tracker.feed_server(response(1))
    assert isinstance(done, RequestCompleted)
    assert done.method == "tools/list"
    assert done.duration_seconds == 0.25
    assert done.ok is True
    assert tracker.pending_count == 0


def test_notification_has_no_pending_entry() -> None:
    tracker, _ = make_tracker()
    [seen] = tracker.feed_client(line({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert seen.notification is True
    assert tracker.pending_count == 0


def test_out_of_order_responses_pair_correctly() -> None:
    tracker, clock = make_tracker()
    tracker.feed_client(request(1, "tools/call", {"name": "slow"}))
    clock.advance(1.0)
    tracker.feed_client(request(2, "tools/call", {"name": "fast"}))
    clock.advance(1.0)

    [done2] = tracker.feed_server(response(2))
    [done1] = tracker.feed_server(response(1))
    assert done2.tool == "fast"
    assert done2.duration_seconds == 1.0
    assert done1.tool == "slow"
    assert done1.duration_seconds == 2.0


def test_non_json_lines_are_ignored() -> None:
    tracker, _ = make_tracker()
    assert tracker.feed_client(b"not json at all\n") == []
    assert tracker.feed_server(b"\x80\xff garbage bytes\n") == []
    assert tracker.feed_server(b"[1, 2, 3]\n") == []  # JSON but not an object
    assert tracker.pending_count == 0


def test_line_split_across_chunks_reassembles() -> None:
    tracker, _ = make_tracker()
    data = request(1, "tools/list")
    assert tracker.feed_client(data[:10]) == []
    [seen] = tracker.feed_client(data[10:])
    assert seen.method == "tools/list"


def test_multiple_lines_in_one_chunk() -> None:
    tracker, _ = make_tracker()
    chunk = request(1, "tools/list") + request(2, "resources/list")
    events = tracker.feed_client(chunk)
    assert [e.method for e in events] == ["tools/list", "resources/list"]


def test_tools_call_extracts_tool_name() -> None:
    tracker, _ = make_tracker()
    [seen] = tracker.feed_client(request(1, "tools/call", {"name": "echo", "arguments": {}}))
    assert seen.tool == "echo"


def test_tool_level_error_flag() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(request(1, "tools/call", {"name": "echo"}))
    [done] = tracker.feed_server(response(1, result={"isError": True, "content": []}))
    assert done.ok is False
    # A tool-level failure carries no JSON-RPC code.
    assert done.error_code is None


def test_jsonrpc_error_response_captures_code() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(request(1, "tools/call", {"name": "echo"}))
    [done] = tracker.feed_server(response(1, error={"code": -32602, "message": "boom"}))
    assert done.ok is False
    assert done.error_code == -32602


def test_string_ids_work() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(request("abc-1", "ping"))
    [done] = tracker.feed_server(response("abc-1"))
    assert done.method == "ping"


def test_unknown_response_id_is_ignored() -> None:
    tracker, _ = make_tracker()
    assert tracker.feed_server(response(99)) == []


def test_pending_entry_expires_after_ttl() -> None:
    tracker, clock = make_tracker(ttl=300.0)
    tracker.feed_client(request(1, "tools/call", {"name": "hung"}))
    clock.advance(301.0)
    # Next insert sweeps the table; a very late response no longer pairs.
    tracker.feed_client(request(2, "ping"))
    assert tracker.pending_count == 1
    assert tracker.feed_server(response(1)) == []


def test_late_response_past_ttl_emits_nothing_even_without_sweep() -> None:
    tracker, clock = make_tracker(ttl=300.0)
    tracker.feed_client(request(1, "ping"))
    clock.advance(301.0)
    assert tracker.feed_server(response(1)) == []
    assert tracker.pending_count == 0


def test_server_initiated_request_does_not_touch_pending_table() -> None:
    tracker, _ = make_tracker()
    tracker.feed_client(request(1, "tools/list"))
    # Server sends its own request whose id collides with the client's.
    assert tracker.feed_server(request(1, "sampling/createMessage")) == []
    assert tracker.pending_count == 1  # client's request still pending
    [done] = tracker.feed_server(response(1))
    assert done.method == "tools/list"


def test_client_answer_to_server_request_is_not_a_request() -> None:
    tracker, _ = make_tracker()
    assert tracker.feed_client(response(7, result={"role": "user"})) == []
    assert tracker.pending_count == 0
