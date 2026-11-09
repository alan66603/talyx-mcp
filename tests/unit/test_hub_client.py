from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from talyx.hub.client import HubClient
from talyx.hub.wire import ChildDown, ChildUp, ProxyOverhead, encode


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
    # The restarted hub first learns the server is up, then gets the new events.
    assert second.wait_lines(2) == [encode("mem", ChildUp()), encode("mem", ProxyOverhead(0.5))]
    assert spawned == [1]


def test_first_connect_does_not_duplicate_lifecycle(hubs: list[FakeHub]) -> None:
    port = free_port()
    c = HubClient("127.0.0.1", port, spawn=lambda: None, retry_interval=0.05)
    c.send("mem", ChildUp())  # queued before any hub exists
    time.sleep(0.1)
    hub = start_fake_hub(port, hubs)
    c.send("mem", ProxyOverhead(0.5))
    c.close(timeout=2.0)
    assert hub.wait_lines(3, timeout=0.3) == [
        encode("mem", ChildUp()),
        encode("mem", ProxyOverhead(0.5)),
    ]


def test_replays_last_lifecycle_per_server(hubs: list[FakeHub]) -> None:
    port = free_port()
    first = start_fake_hub(port, hubs)
    c = HubClient("127.0.0.1", port, spawn=lambda: None, retry_interval=0.05)
    try:
        c.send("a", ChildUp())
        c.send("b", ChildUp())
        c.send("a", ChildDown(False, 0))
        assert len(first.wait_lines(3)) == 3
        first.stop()
        time.sleep(0.2)
        second = start_fake_hub(port, hubs)
        lines = second.wait_lines(2)
    finally:
        c.close(timeout=2.0)
    assert lines == [encode("a", ChildDown(False, 0)), encode("b", ChildUp())]


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


def test_spawn_hub_uses_home_cwd_and_allowlisted_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    import talyx.hub.client as client_mod

    monkeypatch.setenv("HOME", str(tmp_path))  # keep ~/.talyx out of the real home
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
    monkeypatch.setenv("TALYX_METRICS_PORT", "19464")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: list[dict] = []

    def fake_popen(args, **kwargs):
        seen.append({"args": args, **kwargs})

    monkeypatch.setattr(client_mod.subprocess, "Popen", fake_popen)
    client_mod.spawn_hub()

    assert len(seen) == 1
    call = seen[0]
    assert call["cwd"] == Path.home() == tmp_path
    env = call["env"]
    assert "GITHUB_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["TALYX_METRICS_PORT"] == "19464"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == str(tmp_path)
    assert set(env) <= {
        "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT",
    } | {k for k in env if k.startswith("TALYX_")}
