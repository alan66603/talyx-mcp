"""Demo traffic generator: continuously drives every Multi Round-Trip cycle
shape through talyx + the mock server, so the dashboard has live flagship
data with no manual client.

The official everything server can't be used here: it depends on SDK 1.x and
never returns InputRequiredResult, so it produces no cycles (the whole point of
the demo). We drive the bundled mock server instead. talyx exposes /metrics
for Prometheus to scrape; keeping the proxy's stdin open keeps it alive.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import time

from talyx.mock.driver import Client, run_cycle

ABANDON_TIMEOUT_SECONDS = 15  # short enough that abandoned cycles show up live


def main() -> int:
    env = {
        **os.environ,
        "TALYX_METRICS_HOST": "0.0.0.0",
        "TALYX_METRICS_PORT": "9464",
        "TALYX_SERVER_NAME": "mock",
        "TALYX_ABANDONED_TIMEOUT_SECONDS": str(ABANDON_TIMEOUT_SECONDS),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "talyx.cli", "--", sys.executable, "-m", "talyx.mock.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    client = Client(proc.stdin, proc.stdout)
    print("talyx demo driver: generating Multi Round-Trip traffic", file=sys.stderr)

    for iteration in itertools.count(1):
        run_cycle(client, kind="input", rounds=1, delay=0.5)  # single-round human wait
        run_cycle(client, kind="input", rounds=3, delay=0.3)  # multi-round
        run_cycle(client, kind="poll", rounds=2, delay=0.4)  # server-driven polling
        if iteration % 4 == 0:
            run_cycle(client, kind="reject", rounds=1, delay=0.3)  # requestState rejection
        if iteration % 6 == 0:
            run_cycle(client, kind="poll", rounds=1, abandon=True)  # abandoned cycle
        time.sleep(2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
