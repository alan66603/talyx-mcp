"""The mock server's wire contract and — crucially — unique requestState tokens.

If two cycles minted the same token they would collide on the proxy's hash key
and one would clobber the other's waiting entry, so an abandoned cycle could be
silently overwritten before the sweep. Real sealed tokens are unique; the mock
must be too.
"""

from __future__ import annotations

from talyx.mock.server import handle


def initial(kind: str, rounds: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "cycle", "arguments": {"kind": kind, "rounds": rounds}},
    }


def followup(state: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "cycle", "requestState": state},
    }


def test_two_identical_calls_mint_distinct_tokens() -> None:
    a = handle(initial("poll", 1))["result"]["requestState"]
    b = handle(initial("poll", 1))["result"]["requestState"]
    assert a != b  # unique per issuance, or the proxy hash keys collide
    assert a.startswith("poll:0:") and b.startswith("poll:0:")


def test_input_leg_carries_input_requests_poll_leg_does_not() -> None:
    inp = handle(initial("input", 1))["result"]
    assert inp["resultType"] == "input_required"
    assert "inputRequests" in inp

    poll = handle(initial("poll", 1))["result"]
    assert poll["resultType"] == "input_required"
    assert "inputRequests" not in poll


def test_followup_when_remaining_zero_terminates() -> None:
    state = handle(initial("input", 1))["result"]["requestState"]
    resp = handle(followup(state))
    assert resp["result"].get("resultType") != "input_required"


def test_multi_round_decrements_until_terminal() -> None:
    resp = handle(initial("input", 2))["result"]
    assert resp["resultType"] == "input_required"
    resp = handle(followup(resp["requestState"]))["result"]
    assert resp["resultType"] == "input_required"  # one more leg
    resp = handle(followup(resp["requestState"]))["result"]
    assert resp.get("resultType") != "input_required"  # terminal


def test_reject_kind_fails_the_final_followup() -> None:
    state = handle(initial("reject", 1))["result"]["requestState"]
    resp = handle(followup(state))
    assert resp["error"]["data"]["reason"] == "invalid_request_state"
    assert resp["error"]["code"] == -32602


def test_non_tool_call_is_ignored() -> None:
    assert handle({"jsonrpc": "2.0", "method": "notifications/ping"}) is None


def request(method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}


def test_initialize_echoes_protocol_version_and_offers_tools() -> None:
    resp = handle(request("initialize", {"protocolVersion": "2025-06-18"}))
    assert resp["id"] == 7
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "talyx-mock"


def test_tools_list_describes_the_cycle_tool() -> None:
    (tool,) = handle(request("tools/list"))["result"]["tools"]
    assert tool["name"] == "cycle"
    props = tool["inputSchema"]["properties"]
    assert props["kind"]["enum"] == ["input", "poll", "reject"]
    assert props["rounds"]["type"] == "integer"


def test_unknown_request_gets_method_not_found() -> None:
    # Clients probe (e.g. server/discover) and wait for an answer; silence would hang them.
    resp = handle(request("server/discover"))
    assert resp["id"] == 7
    assert resp["error"]["code"] == -32601


def test_notifications_get_no_reply() -> None:
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
