from __future__ import annotations

import os
import queue
import select
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from talyx.hub.wire import ChildDown, ChildUp, HubItem, encode

_QUEUE_MAX = 10_000
# Bounds every blocking socket call so the worker always notices close().
_SOCKET_TIMEOUT = 1.0
# How often the idle worker re-checks the queue, the stop flag and the peer.
_POLL = 0.05
# Extra time close() grants the worker to exit after its timeout expires.
_EXIT_GRACE = 0.1


class HubClient:
    """Streams wrapper events to the hub over local TCP, from a daemon thread.

    Never blocks or raises into the proxy path: `send` only enqueues, and a full
    queue drops the item (counted in `dropped`). The worker connects, spawns the
    hub at most once per lifetime if nobody is listening, retries every
    `retry_interval`, and reconnects after a write failure (dropping the item it
    was writing) so a restarted hub is picked up automatically. The last
    lifecycle item (`ChildUp` / `ChildDown`) taken off the queue for each server
    is replayed first on every new connection, so a restarted hub still learns
    which servers are up.
    """

    def __init__(
        self,
        host: str,
        port: int,
        spawn: Callable[[], object],
        retry_interval: float = 0.5,
    ) -> None:
        self._addr = (host, port)
        self._spawn = spawn
        self._spawned = False
        self._retry_interval = retry_interval
        # (server, is_lifecycle, encoded line)
        self._queue: queue.Queue[tuple[str, bool, bytes]] = queue.Queue(maxsize=_QUEUE_MAX)
        # server -> encoded last lifecycle item; touched only by the worker thread.
        self._lifecycle: dict[str, bytes] = {}
        self.dropped = 0
        self._closing = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="talyx-hub-client", daemon=True)
        self._thread.start()

    def send(self, server: str, item: HubItem) -> None:
        try:
            lifecycle = isinstance(item, (ChildUp, ChildDown))
            self._queue.put_nowait((server, lifecycle, encode(server, item)))
        except Exception:  # noqa: BLE001 - queue.Full, or an unencodable item
            self.dropped += 1

    def close(self, timeout: float = 0.5) -> None:
        """Flush what is queued for at most `timeout` seconds, then stop."""
        self._closing.set()
        self._thread.join(timeout)
        self._stop.set()
        self._thread.join(_EXIT_GRACE)

    def _run(self) -> None:
        sock: socket.socket | None = None
        try:
            while not self._stop.is_set():
                if self._closing.is_set() and self._queue.empty():
                    return
                if sock is not None and _peer_closed(sock):
                    sock.close()
                    sock = None
                if sock is None:
                    sock = self._connect()
                    if sock is not None and not self._replay(sock):
                        sock.close()
                        sock = None
                    continue
                try:
                    server, lifecycle, data = self._queue.get(timeout=_POLL)
                except queue.Empty:
                    continue
                if lifecycle:
                    # Remembered before writing: if this write fails, the item is
                    # replayed on the next connection instead of being lost.
                    self._lifecycle[server] = data
                try:
                    sock.sendall(data)
                except OSError:
                    sock.close()
                    sock = None
        finally:
            if sock is not None:
                sock.close()

    def _replay(self, sock: socket.socket) -> bool:
        """Send each server's last lifecycle item on a fresh connection."""
        try:
            for data in list(self._lifecycle.values()):
                sock.sendall(data)
        except OSError:
            return False
        return True

    def _connect(self) -> socket.socket | None:
        try:
            return socket.create_connection(self._addr, timeout=_SOCKET_TIMEOUT)
        except OSError:
            pass
        if not self._spawned:
            self._spawned = True
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001 - a failed spawn just means no hub
                # Said once (spawn is attempted once): the proxy itself keeps working.
                try:
                    print(
                        f"talyx: could not start hub: {exc}; metrics will not be reported "
                        "(see ~/.talyx/hub.log, or set TALYX_HUB=off)",
                        file=sys.stderr,
                    )
                except Exception:  # noqa: BLE001 - stderr may be gone
                    pass
        self._stop.wait(self._retry_interval)
        return None


def _peer_closed(sock: socket.socket) -> bool:
    """The hub never writes to us, so readability means EOF or an error."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1) == b""
    except OSError:
        return True


# The only variables the long-lived hub inherits from the wrapper that starts it
# (plus every TALYX_*), so a wrapped server's secrets never reach the hub.
_HUB_ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


def _hub_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k in _HUB_ENV_ALLOWLIST or k.startswith("TALYX_")
    }


def spawn_hub() -> None:
    """Start a detached `python -m talyx hub`, logging to ~/.talyx/hub.log."""
    home = Path.home()
    log_dir = home / ".talyx"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(log_dir / "hub.log", "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "talyx", "hub"],
            # Outlives the wrapper: don't pin its cwd or inherit its environment.
            cwd=home,
            env=_hub_env(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
