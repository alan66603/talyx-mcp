from __future__ import annotations

import queue
import select
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from talyx.hub.wire import HubItem, encode

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
    was writing) so a restarted hub is picked up automatically.
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
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_MAX)
        self.dropped = 0
        self._closing = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="talyx-hub-client", daemon=True)
        self._thread.start()

    def send(self, server: str, item: HubItem) -> None:
        try:
            self._queue.put_nowait(encode(server, item))
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
                    continue
                try:
                    data = self._queue.get(timeout=_POLL)
                except queue.Empty:
                    continue
                try:
                    sock.sendall(data)
                except OSError:
                    sock.close()
                    sock = None
        finally:
            if sock is not None:
                sock.close()

    def _connect(self) -> socket.socket | None:
        try:
            return socket.create_connection(self._addr, timeout=_SOCKET_TIMEOUT)
        except OSError:
            pass
        if not self._spawned:
            self._spawned = True
            try:
                self._spawn()
            except Exception:  # noqa: BLE001 - a failed spawn just means no hub
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


def spawn_hub() -> None:
    """Start a detached `python -m talyx hub`, logging to ~/.talyx/hub.log."""
    log_dir = Path.home() / ".talyx"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(log_dir / "hub.log", "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "talyx", "hub"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
