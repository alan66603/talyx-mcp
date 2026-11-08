"""E2E: two wrappers report to one auto-spawned hub, which serves a single /metrics."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from talyx.mock.driver import Client, run_cycle

TIMEOUT = 15


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scrape_until(port: int, needles: list[str]) -> str:
    deadline = time.monotonic() + TIMEOUT
    body = ""
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
                body = r.read().decode()
            if all(n in body for n in needles):
                return body
        except (urllib.error.URLError, ConnectionError):
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"metrics never contained {needles}:\n{body}")
        time.sleep(0.05)


def hub_children(wrapper_pid: int) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-P", str(wrapper_pid), "-f", "talyx hub"], capture_output=True, text=True
    ).stdout
    return [int(p) for p in out.split()]


def kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)


def spawn_wrapper(env: dict[str, str], name: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "talyx", "--", sys.executable, "-m", "talyx.mock.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**env, "TALYX_SERVER_NAME": name},
    )


def test_two_wrappers_share_one_metrics_endpoint(tmp_path: Path) -> None:
    metrics_port = free_port()
    env = {
        **os.environ,
        "TALYX_HUB": "on",
        "TALYX_HUB_PORT": str(free_port()),
        "TALYX_METRICS_HOST": "127.0.0.1",
        "TALYX_METRICS_PORT": str(metrics_port),
        "HOME": str(tmp_path),  # keep ~/.talyx/hub.log out of the real home
    }
    hubs: set[int] = set()
    wrappers: list[subprocess.Popen] = []
    try:
        a = spawn_wrapper(env, "alpha")
        wrappers.append(a)
        run_cycle(Client(a.stdin, a.stdout), rounds=0)
        # The first wrapper auto-spawned the hub; find it while it is still our child.
        scrape_until(metrics_port, ['server="alpha"'])
        hubs.update(hub_children(a.pid))

        b = spawn_wrapper(env, "beta")
        wrappers.append(b)
        run_cycle(Client(b.stdin, b.stdout), rounds=0)

        body = scrape_until(metrics_port, ['server="alpha"', 'server="beta"'])
        assert 'talyx_server_up{server="alpha"} 1.0' in body
        assert 'talyx_server_up{server="beta"} 1.0' in body
        assert (tmp_path / ".talyx" / "hub.log").exists()
    finally:
        # Look the hub up again before any wrapper exits (it is only findable
        # as a wrapper's child), so it is killed even if the test failed early.
        for w in wrappers:
            hubs.update(hub_children(w.pid))
        for w in wrappers:
            try:
                w.stdin.close()
                w.wait(timeout=TIMEOUT)
            except (OSError, subprocess.TimeoutExpired):
                w.kill()
                w.wait()
            finally:
                w.stdout.close()
        for pid in hubs:
            kill(pid)
    assert hubs, "no auto-spawned hub found"
