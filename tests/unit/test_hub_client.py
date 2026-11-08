from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from talyx.hub.client import HubClient
from talyx.hub.wire import ChildUp, ProxyOverhead, encode


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHub:
    """Accepts connections on `port` and records every newline-terminated line."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.lines: list[bytes] = []
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen()
        self._accept = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept.start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
            t = threading.Thread(target=self._read_loop, args=(conn,), daemon=True)
            self._threads.append(t)
            t.start()

    def _read_loop(self, conn: socket.socket) -> None:
        with conn.makefile("rb") as f:
            try:
                for line in f:
                    with self._lock:
                        self.lines.append(line)
            except (OSError, ValueError):
                return

    def wait_lines(self, n: int, timeout: float = 2.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while len(self.lines) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return list(self.lines)

    def stop(self) -> None:
        # shutdown() before close() so a thread blocked in accept() wakes up.
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._listener.close()
        self._accept.join(2)
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        for t in self._threads:
            t.join(2)


@pytest.fixture
def hubs() -> Iterator[list[FakeHub]]:
    started: list[FakeHub] = []
    yield started
    for h in started:
        h.stop()


def start_fake_hub(port: int, hubs: list[FakeHub]) -> FakeHub:
    hub = FakeHub(port)
    hubs.append(hub)
    return hub


def test_sent_items_arrive_encoded(hubs: list[FakeHub]) -> None:
    fake_hub = start_fake_hub(free_port(), hubs)
    c = HubClient("127.0.0.1", fake_hub.port, spawn=lambda: None)
    c.send("mem", ChildUp())
    c.close()
    assert fake_hub.wait_lines(1) == [encode("mem", ChildUp())]


def test_events_buffered_until_hub_appears(hubs: list[FakeHub]) -> None:
    port = free_port()
    spawned: list[int] = []
    c = HubClient("127.0.0.1", port, spawn=lambda: spawned.append(1), retry_interval=0.05)
    c.send("mem", ChildUp())  # no hub yet: must neither block nor drop
    time.sleep(0.2)
    hub = start_fake_hub(port, hubs)  # appears later
    c.close(timeout=2.0)
    assert spawned == [1]
    assert hub.wait_lines(1) == [encode("mem", ChildUp())]


def test_reconnects_after_hub_restart(hubs: list[FakeHub]) -> None:
    port = free_port()
    spawned: list[int] = []
    c = HubClient("127.0.0.1", port, spawn=lambda: spawned.append(1), retry_interval=0.05)
    try:
        time.sleep(0.1)  # first connect fails -> spawn
        first = start_fake_hub(port, hubs)
        c.send("mem", ChildUp())
        assert first.wait_lines(1) == [encode("mem", ChildUp())]

        first.stop()
        time.sleep(0.2)  # client notices and fails to reconnect at least once
        second = start_fake_hub(port, hubs)
        c.send("mem", ProxyOverhead(0.5))
    finally:
        c.close(timeout=2.0)
    assert second.wait_lines(1) == [encode("mem", ProxyOverhead(0.5))]
    assert spawned == [1]


def test_send_never_blocks_when_queue_full() -> None:
    c = HubClient("127.0.0.1", free_port(), spawn=lambda: None)
    try:
        t0 = time.monotonic()
        for _ in range(50_000):
            c.send("mem", ProxyOverhead(0.0))
        assert time.monotonic() - t0 < 1.0
        assert c.dropped > 0
    finally:
        c.close(timeout=0.1)


def test_close_is_bounded_without_hub() -> None:
    c = HubClient("127.0.0.1", free_port(), spawn=lambda: None, retry_interval=0.05)
    c.send("mem", ChildUp())
    t0 = time.monotonic()
    c.close(timeout=0.2)
    assert time.monotonic() - t0 < 0.5


def test_spawn_failure_is_swallowed(hubs: list[FakeHub]) -> None:
    def boom() -> None:
        raise OSError("no python")

    port = free_port()
    c = HubClient("127.0.0.1", port, spawn=boom, retry_interval=0.05)
    c.send("mem", ChildUp())
    time.sleep(0.1)
    hub = start_fake_hub(port, hubs)
    c.close(timeout=2.0)
    assert hub.wait_lines(1) == [encode("mem", ChildUp())]


def test_spawn_failure_prints_one_stderr_line(capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> None:
        raise OSError("no python")

    c = HubClient("127.0.0.1", free_port(), spawn=boom, retry_interval=0.02)
    for _ in range(5):
        c.send("mem", ChildUp())  # must never raise
    time.sleep(0.2)  # several failed connect attempts
    c.close(timeout=0.1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "talyx: could not start hub: no python; metrics will not be reported "
        "(see ~/.talyx/hub.log, or set TALYX_HUB=off)"
    ]
