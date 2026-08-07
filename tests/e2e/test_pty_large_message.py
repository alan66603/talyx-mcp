"""Regression: large writes must not crash when stdin/stdout share one terminal.

When talyx is run directly in a terminal, stdin/stdout/stderr share a single
open file description. asyncio's connect_read_pipe marks stdin non-blocking,
which (via the shared description) also makes stdout non-blocking. A blocking
write of a large MCP message then failed with BlockingIOError (EAGAIN). The fix
routes output through asyncio writers; this test pins that behaviour down.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time

BIG = 200_000
TIMEOUT = 10

# Fake server: reply to any request with a result far larger than a pipe buffer.
FAKE_SERVER = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    msg = json.loads(line)\n"
    f"    resp = {{'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {{'blob': 'x' * {BIG}}}}}\n"
    "    sys.stdout.write(json.dumps(resp) + '\\n')\n"
    "    sys.stdout.flush()\n"
)


def test_large_message_over_shared_pty_does_not_crash() -> None:
    parent_fd, child_fd = pty.openpty()  # one tty used as both stdin and stdout
    proxy = subprocess.Popen(
        [sys.executable, "-m", "talyx.cli", "--", sys.executable, "-c", FAKE_SERVER],
        stdin=child_fd,
        stdout=child_fd,
        stderr=subprocess.PIPE,
        env={**os.environ, "TALYX_METRICS_PORT": "0"},
    )
    os.close(child_fd)
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo"}}
        os.write(parent_fd, (json.dumps(request) + "\n").encode())

        received = b""
        deadline = time.monotonic() + TIMEOUT
        while len(received) < BIG and time.monotonic() < deadline:
            ready, _, _ = select.select([parent_fd], [], [], 0.2)
            if ready:
                chunk = os.read(parent_fd, 65536)
                if not chunk:
                    break
                received += chunk

        assert proxy.poll() is None, "proxy crashed instead of forwarding the large message"
        assert len(received) >= BIG, "large response was not forwarded back through the proxy"
    finally:
        proxy.kill()
        proxy.wait(timeout=TIMEOUT)
        os.close(parent_fd)

    stderr = proxy.stderr.read().decode(errors="replace") if proxy.stderr else ""
    assert "BlockingIOError" not in stderr
