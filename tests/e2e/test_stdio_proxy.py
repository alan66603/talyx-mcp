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


# Drives StdioProxy directly so the injected on_child_down callback can report
# the `crashed` flag it received (the CLI does not expose it).
_DRIVER = (
    "import asyncio, sys\n"
    "from talyx.proxy.stdio import StdioProxy\n"
    "cmd = sys.argv[1:]\n"
    "proxy = StdioProxy(cmd, on_child_down=lambda crashed: "
    "print(f'CRASHED={crashed}', file=sys.stderr))\n"
    "sys.exit(asyncio.run(proxy.run()))\n"
)


def run_driver(*server_command: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", _DRIVER, *server_command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_child_exit_while_client_connected_is_crash() -> None:
    proxy = run_driver(sys.executable, "-c", "import sys; sys.exit(1)")
    # Keep the client's stdin open until the proxy has finished.
    assert proxy.stdin is not None and proxy.stderr is not None
    proxy.wait(timeout=TIMEOUT)
    err = proxy.stderr.read()
    proxy.stdin.close()
    assert b"CRASHED=True" in err


def test_child_exit_after_stdin_eof_is_clean() -> None:
    proxy = run_driver("cat")
    _, err = proxy.communicate(input=b"hello\n", timeout=TIMEOUT)
    assert b"CRASHED=False" in err


def test_sigterm_shutdown_is_not_a_crash() -> None:
    # Older MCP clients stop a stdio server with SIGTERM while stdin stays open.
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proxy = run_driver(sys.executable, "-c", code)
    assert proxy.stdin is not None and proxy.stdout is not None and proxy.stderr is not None
    assert proxy.stdout.readline() == b"ready\n"
    proxy.send_signal(signal.SIGTERM)
    proxy.wait(timeout=TIMEOUT)
    err = proxy.stderr.read()
    proxy.stdin.close()
    proxy.stdout.close()
    proxy.stderr.close()
    assert b"CRASHED=False" in err
