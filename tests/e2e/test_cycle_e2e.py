"""E2E: drive real Multi Round-Trip cycles through the proxy via the mock
server, then scrape /metrics for the five flagship cycle metrics."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

from talyx.mock.driver import Client, run_cycle

TIMEOUT = 15


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(port: int, abandon_timeout: float) -> subprocess.Popen:
    env = {
        **os.environ,
        "TALYX_METRICS_HOST": "127.0.0.1",
        "TALYX_METRICS_PORT": str(port),
        "TALYX_SERVER_NAME": "mock",
        "TALYX_ABANDONED_TIMEOUT_SECONDS": str(abandon_timeout),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "talyx.cli", "--", sys.executable, "-m", "talyx.mock.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def scrape_until(port: int, needle: str) -> str:
    """Poll /metrics until `needle` appears (covers server-startup and the
    brief window between the proxy forwarding bytes and observing them)."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
                body = r.read().decode()
            if needle in body:
                return body
        except (urllib.error.URLError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"metric never appeared: {needle}")
        time.sleep(0.05)


def value(body: str, prefix: str) -> float:
    for ln in body.splitlines():
        if ln.startswith(prefix):
            return float(ln.rsplit(" ", 1)[1])
    raise AssertionError(f"metric line not found: {prefix}")


def test_completed_input_cycle_populates_cycle_metrics() -> None:
    port = free_port()
    proc = spawn(port, abandon_timeout=60.0)
    client = Client(proc.stdin, proc.stdout)
    try:
        # rounds=2 → two input_required legs, then terminal (three round-trips).
        run_cycle(client, kind="input", rounds=2, delay=0.2)
        needle = 'talyx_round_trip_cycle_duration_seconds_count{wait_type="input"}'
        body = scrape_until(port, needle)
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)

    inp = '{method="tools/call",server="mock",wait_type="input"}'
    assert value(body, f"talyx_input_required_total{inp}") == 2.0
    assert value(body, 'talyx_round_trips_per_request_count{wait_type="input"}') == 1.0
    assert value(body, 'talyx_round_trips_per_request_sum{wait_type="input"}') == 3.0
    assert value(body, 'talyx_round_trip_cycle_duration_seconds_count{wait_type="input"}') == 1.0
    wait = '{server="mock",wait_type="input"}'
    assert value(body, f"talyx_input_wait_duration_seconds_count{wait}") == 2.0
    # Two real ~0.2s waits: the split-out input_wait must be clearly non-zero.
    assert value(body, f"talyx_input_wait_duration_seconds_sum{wait}") > 0.2


def test_abandoned_cycle_populates_metric() -> None:
    port = free_port()
    proc = spawn(port, abandon_timeout=1.0)
    client = Client(proc.stdin, proc.stdout)
    try:
        run_cycle(client, kind="poll", rounds=1, abandon=True)  # no follow-up
        time.sleep(1.3)  # age past the 1s abandon window
        run_cycle(client, kind="input", rounds=1, delay=0.05)  # traffic triggers the sweep
        needle = 'talyx_abandoned_cycles_total{server="mock",wait_type="poll"}'
        body = scrape_until(port, needle)
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)

    assert value(body, 'talyx_abandoned_cycles_total{server="mock",wait_type="poll"}') == 1.0


def test_request_state_rejected_populates_metric() -> None:
    port = free_port()
    proc = spawn(port, abandon_timeout=60.0)
    client = Client(proc.stdin, proc.stdout)
    try:
        # kind="reject": the mock fails the follow-up with invalid_request_state.
        run_cycle(client, kind="reject", rounds=1, delay=0.05)
        needle = 'talyx_request_state_rejected_total{server="mock"}'
        body = scrape_until(port, needle)
    finally:
        proc.stdin.close()
        proc.wait(timeout=TIMEOUT)

    assert value(body, 'talyx_request_state_rejected_total{server="mock"}') == 1.0
