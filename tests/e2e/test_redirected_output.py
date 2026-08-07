"""Regression: output redirected to regular files must not crash the proxy.

asyncio's connect_write_pipe rejects regular files (only pipes/sockets/char
devices), so wrapping stdout/stderr unconditionally raised ValueError under
`talyx ... > out 2> err`. The proxy must fall back to blocking writes for
regular-file outputs, which are always safe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

TIMEOUT = 10

FAKE_SERVER = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    msg = json.loads(line)\n"
    "    resp = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {'ok': True}}\n"
    "    sys.stdout.write(json.dumps(resp) + '\\n')\n"
    "    sys.stdout.flush()\n"
    "    sys.stderr.write('server log line\\n')\n"
    "    sys.stderr.flush()\n"
)


def test_stdout_stderr_redirected_to_files(tmp_path) -> None:
    out_path = tmp_path / "out.log"
    err_path = tmp_path / "err.log"
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo"}}

    with out_path.open("wb") as out_file, err_path.open("wb") as err_file:
        proxy = subprocess.Popen(
            [sys.executable, "-m", "talyx.cli", "--", sys.executable, "-c", FAKE_SERVER],
            stdin=subprocess.PIPE,
            stdout=out_file,  # a regular file, not a pipe
            stderr=err_file,  # a regular file, not a pipe
            env={**os.environ, "TALYX_METRICS_PORT": "0"},
        )
        assert proxy.stdin is not None
        proxy.stdin.write(json.dumps(request).encode() + b"\n")
        proxy.stdin.flush()
        proxy.stdin.close()
        returncode = proxy.wait(timeout=TIMEOUT)

    out = out_path.read_text()
    err = err_path.read_text()
    assert returncode == 0
    assert '"id": 1' in out or '"id":1' in out  # server response reached the file
    assert "BlockingIOError" not in err
    assert "ValueError" not in err
    assert "server log line" in err  # child stderr was forwarded to the file
