"""A client that drives the mock server's Multi Round-Trip cycles through the
proxy. Shared by the cycle e2e test and by a one-command manual demo
(`python -m talyx.mock.driver`).

It plays the MCP *client*: it writes JSON-RPC to the proxy's stdin and reads
the proxy's stdout. The proxy sits in the middle and spawns the mock server, so
a run exercises the real observation path end to end.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from typing import BinaryIO

_INPUT_REQUIRED = "input_required"


class Client:
    """Minimal newline-delimited JSON-RPC client over a pair of byte streams."""

    def __init__(self, to_proxy: BinaryIO, from_proxy: BinaryIO) -> None:
        self._out = to_proxy
        self._in = from_proxy
        self._id = 0

    def _send(self, params: dict) -> None:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": "tools/call", "params": params}
        self._out.write(json.dumps(msg).encode() + b"\n")
        self._out.flush()

    def initial(self, kind: str, rounds: int) -> None:
        self._send({"name": "cycle", "arguments": {"kind": kind, "rounds": rounds}})

    def followup(self, request_state: str) -> None:
        # Echo the sealed token back at params.requestState, where the proxy
        # looks for it — the mock and driver define that wire location together.
        self._send(
            {"name": "cycle", "requestState": request_state, "inputResponses": [{"value": "yes"}]}
        )

    def recv(self) -> dict | None:
        line = self._in.readline()
        if not line:
            return None
        return json.loads(line)


def run_cycle(
    client: Client,
    *,
    kind: str = "input",
    rounds: int = 1,
    delay: float = 0.0,
    abandon: bool = False,
) -> None:
    """Run one logical request to completion (or abandon it after the first leg)."""
    client.initial(kind, rounds)
    while True:
        resp = client.recv()
        if resp is None:
            return
        result = resp.get("result") or {}
        if result.get("resultType") == _INPUT_REQUIRED:
            if abandon:
                return  # never follow up: this is how an abandoned cycle is made
            if delay:
                time.sleep(delay)
            client.followup(result.get("requestState"))
        else:
            return  # terminal result: the cycle is done


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _scrape(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        return resp.read().decode()


def _demo() -> int:
    """Spawn proxy+mock, run every cycle shape, then print the cycle metrics."""
    port = _free_port()
    abandon_timeout = 2.0
    env = {
        **os.environ,
        "TALYX_METRICS_HOST": "127.0.0.1",
        "TALYX_METRICS_PORT": str(port),
        "TALYX_SERVER_NAME": "mock",
        "TALYX_ABANDONED_TIMEOUT_SECONDS": str(abandon_timeout),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "talyx.cli", "--", sys.executable, "-m", "talyx.mock.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    client = Client(proc.stdin, proc.stdout)
    try:
        run_cycle(client, kind="input", rounds=1, delay=0.5)  # single-round input
        run_cycle(client, kind="input", rounds=3, delay=0.2)  # multi-round
        run_cycle(client, kind="poll", rounds=2, delay=0.3)  # poll-only
        run_cycle(client, kind="poll", rounds=1, abandon=True)  # abandoned (no follow-up)
        time.sleep(abandon_timeout + 0.5)  # let the abandoned cycle age out
        run_cycle(client, kind="input", rounds=1, delay=0.1)  # traffic to trigger the sweep
        body = _scrape(port)
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)

    cycle_lines = [
        ln
        for ln in body.splitlines()
        if ln.startswith("talyx_")
        and "_bucket" not in ln
        and any(k in ln for k in ("input_required", "round_trip", "input_wait", "abandoned"))
    ]
    print("\n".join(cycle_lines))
    return 0


if __name__ == "__main__":
    sys.exit(_demo())
