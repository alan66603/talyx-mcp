"""E2E tests: run the proxy as a real subprocess and verify transparent forwarding."""

from __future__ import annotations

import signal
import subprocess
import sys

TIMEOUT = 10


def run_proxy(*server_command: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "talyx.cli", "--", *server_command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_stdin_stdout_roundtrip() -> None:
    proxy = run_proxy("cat")
    line = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    out, err = proxy.communicate(input=line, timeout=TIMEOUT)
    assert out == line
    assert proxy.returncode == 0


def test_stdout_carries_only_child_bytes() -> None:
    proxy = run_proxy(sys.executable, "-c", "pass")
    out, _ = proxy.communicate(timeout=TIMEOUT)
    assert out == b""


def test_child_exit_code_propagates() -> None:
    proxy = run_proxy(sys.executable, "-c", "raise SystemExit(7)")
    proxy.communicate(timeout=TIMEOUT)
    assert proxy.returncode == 7


def test_stderr_is_forwarded_not_stdout() -> None:
    code = "import sys; print('log line', file=sys.stderr)"
    proxy = run_proxy(sys.executable, "-c", code)
    out, err = proxy.communicate(timeout=TIMEOUT)
    assert out == b""
    assert b"log line" in err


def test_missing_command_exits_127() -> None:
    proxy = run_proxy("definitely-not-a-real-command-xyz")
    out, err = proxy.communicate(timeout=TIMEOUT)
    assert proxy.returncode == 127
    assert out == b""
    assert b"command not found" in err


def test_sigterm_is_forwarded_to_child() -> None:
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proxy = run_proxy(sys.executable, "-c", code)
    assert proxy.stdout is not None
    assert proxy.stdout.readline() == b"ready\n"  # child is up, via proxied stdout
    proxy.send_signal(signal.SIGTERM)
    proxy.communicate(timeout=TIMEOUT)
    assert proxy.returncode == 0  # child's graceful exit code, not a kill
