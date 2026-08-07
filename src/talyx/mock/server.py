"""A stateless MCP-ish server that returns InputRequiredResult on demand.

The cycle metrics need a server that emits `InputRequiredResult` (SEP-2322).
The official everything server still depends on SDK 1.x and only does the old
standalone `elicitation/create`, so it never produces one — hence this mock,
which is a first-class test asset.

Wire contract (this is what the e2e test and the driver pin down):

  - The client's initial `tools/call` carries
        params.arguments = {"kind": "input"|"poll"|"reject", "rounds": <int>}
    where `rounds` is how many InputRequiredResult legs precede the terminal
    result, and `kind` selects the wait_type ("reject" behaves like "input" but
    fails the final follow-up with an invalid_request_state error).
  - The server replies with an InputRequiredResult:
        result.resultType  == "input_required"   (snake_case — the load-bearing
                                                   discriminator)
        result.requestState == "<kind>:<remaining>:<nonce>" (opaque to the proxy;
                                                   the nonce makes each token
                                                   unique, like a real one)
        result.inputRequests present only for kind == "input"; a poll leg is
        state-only.
  - The client re-sends `tools/call` echoing the token at
        params.requestState
    (the mock and driver define this wire location together).
  - When `remaining` hits 0 the server replies with a terminal result.

All cycle state rides in the requestState token, so the server keeps nothing
between requests — the 2026-07-28 stateless model in miniature. To trigger an
*abandoned* cycle the driver simply never sends the follow-up; the server holds
no state, so there is nothing to clean up on its side.
"""

from __future__ import annotations

import json
import secrets
import sys

_INPUT_REQUEST = {"prompt": "Confirm the high-risk action?"}


def _remaining(state: str) -> tuple[str, int]:
    """Parse a `<kind>:<remaining>:<nonce>` token; anything unparseable terminates."""
    parts = state.split(":")
    kind = parts[0]
    try:
        return kind, int(parts[1])
    except (IndexError, ValueError):
        return kind, 0


def _mint(kind: str, remaining: int) -> str:
    """A fresh requestState token. The nonce makes every leg's token unique, as
    a real sealed token (with iat/nonce) is — without it, two cycles that reach
    the same `<kind>:<remaining>` would collide on the proxy's hash key and one
    would clobber the other's waiting entry."""
    return f"{kind}:{remaining}:{secrets.token_hex(6)}"


def handle(msg: dict) -> dict | None:
    """Map one JSON-RPC request to its response, or None to stay silent."""
    if msg.get("method") != "tools/call":
        return None  # notifications and other methods are not part of the mock
    params = msg.get("params") or {}
    state = params.get("requestState")
    if isinstance(state, str) and ":" in state:
        kind, remaining = _remaining(state)
    else:
        args = params.get("arguments") or {}
        kind = args.get("kind", "input")
        remaining = int(args.get("rounds", 1))

    if remaining > 0:
        result: dict = {
            "resultType": "input_required",
            "requestState": _mint(kind, remaining - 1),
        }
        if kind != "poll":  # input and reject are human-wait legs; poll is state-only
            result["inputRequests"] = [_INPUT_REQUEST]
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}

    if kind == "reject":
        # Simulate a stale/tampered token: the discriminator is data.reason, not
        # the (generic) code (SEP-2164).
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {"reason": "invalid_request_state"},
            },
        }
    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"content": [], "isError": False}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        response = handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
