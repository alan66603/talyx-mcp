"""E2E: drive the CLI with metrics enabled, then scrape /metrics for real numbers."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 15

# A minimal MCP-ish server: echo tools/call requests back as proper responses.
FAKE_SERVER = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if msg.get("method") == "tools/call":
        resp = {
            "jsonrpc": "2.0",
            "id": msg["id"],
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scrape(port: int) -> str:
    deadline = time.monotonic() + TIMEOUT
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
                return r.read().decode()
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_tool_call_shows_up_in_metrics() -> None:
    port = free_port()
    env = {
        **os.environ,
        "TALYX_METRICS_HOST": "127.0.0.1",
        "TALYX_METRICS_PORT": str(port),
        "TALYX_SERVER_NAME": "echo-server",
    }
    proxy = subprocess.Popen(
        [sys.executable, "-m", "talyx.cli", "--", sys.executable, "-c", FAKE_SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proxy.stdin is not None
    assert proxy.stdout is not None
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo"}}
        proxy.stdin.write(json.dumps(request).encode() + b"\n")
        proxy.stdin.flush()

        response = json.loads(proxy.stdout.readline())
        assert response["id"] == 1  # traffic really flowed through the proxy

        body = scrape(port)
    finally:
        proxy.stdin.close()
        proxy.wait(timeout=TIMEOUT)

    assert 'talyx_requests_total{method="tools/call",server="echo-server",status="ok"} 1.0' in body
    assert (
        'talyx_request_duration_seconds_count{method="tools/call",server="echo-server"} 1.0' in body
    )
    assert "talyx_proxy_overhead_seconds_count" in body
