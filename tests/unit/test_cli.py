from __future__ import annotations

import json

import pytest

from talyx.cli import _Lifecycle, main, parse_command
from talyx.metrics.registry import Metrics
from talyx.proxy.jsonrpc import TrafficObserver


def test_parse_strips_leading_separator() -> None:
    assert parse_command(["--", "node", "server.js"]) == ["node", "server.js"]


def test_parse_without_separator() -> None:
    assert parse_command(["node", "server.js"]) == ["node", "server.js"]


def test_parse_keeps_inner_separator() -> None:
    assert parse_command(["--", "cmd", "--", "flag"]) == ["cmd", "--", "flag"]


def test_parse_empty() -> None:
    assert parse_command([]) == []
    assert parse_command(["--"]) == []


def test_main_without_command_prints_usage(capsys) -> None:
    assert main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # usage must not pollute stdout
    assert "usage" in captured.err


def test_lifecycle_tracks_liveness_and_inflight_loss() -> None:
    metrics = Metrics(server="s")
    tracker = TrafficObserver()
    # One request in flight with no response yet.
    tracker.feed_client(
        (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}) + "\n").encode()
    )
    lifecycle = _Lifecycle(tracker, metrics)

    lifecycle.up()
    assert metrics.registry.get_sample_value("talyx_server_up", {"server": metrics.server}) == 1.0

    lifecycle.down(True)
    assert metrics.registry.get_sample_value("talyx_server_up", {"server": metrics.server}) == 0.0
    # The in-flight request is lost when the child dies.
    assert (
        metrics.registry.get_sample_value("talyx_inflight_requests_lost_total", {"server": "s"})
        == 1.0
    )


def test_lifecycle_clean_shutdown_removes_server_up_series() -> None:
    metrics = Metrics(server="s")
    tracker = TrafficObserver()
    tracker.feed_client(
        (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}) + "\n").encode()
    )
    lifecycle = _Lifecycle(tracker, metrics)

    lifecycle.up()
    lifecycle.down(False)
    assert metrics.registry.get_sample_value("talyx_server_up", {"server": "s"}) is None
    lost = metrics.registry.get_sample_value(
        "talyx_inflight_requests_lost_total", {"server": "s"}
    )
    assert not lost


class _FakeProxy:
    """Stands in for StdioProxy: replays one request and a crash, records its command."""

    commands: list[list[str]] = []

    def __init__(self, command, *, on_client_bytes, on_server_bytes, on_child_up, on_child_down):
        _FakeProxy.commands.append(command)
        self._client = on_client_bytes
        self._up = on_child_up
        self._down = on_child_down

    async def run(self) -> int:
        self._up()
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}
        self._client((json.dumps(req) + "\n").encode())
        self._down(True)
        return 0


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self, host, port, spawn, retry_interval=0.5) -> None:
        self.port = port
        self.sent: list[tuple[str, object]] = []
        self.closed_after = -1
        _FakeClient.instances.append(self)

    def send(self, server, item) -> None:
        self.sent.append((server, item))

    def close(self, timeout: float = 0.5) -> None:
        self.closed_after = len(self.sent)


def _no_metrics_server(*args, **kwargs):
    raise AssertionError("start_metrics_server must not be called in hub mode")


def test_hub_subcommand_dispatches_to_run_hub(monkeypatch) -> None:
    import talyx.cli as cli

    seen = []
    monkeypatch.setattr(cli, "run_hub", lambda config: seen.append(config) or 7)
    assert cli.main(["hub"]) == 7
    assert len(seen) == 1


def test_separator_hub_proxies_a_command_named_hub(monkeypatch) -> None:
    import talyx.cli as cli

    monkeypatch.setattr(cli, "run_hub", lambda config: pytest.fail("must not run hub"))
    monkeypatch.setattr(cli, "StdioProxy", _FakeProxy)
    monkeypatch.setattr(cli, "start_metrics_server", lambda *a, **k: None)
    _FakeProxy.commands = []
    assert cli.main(["--", "hub"]) == 0
    assert _FakeProxy.commands == [["hub"]]


def test_hub_mode_reports_to_client_not_local_endpoint(monkeypatch) -> None:
    import talyx.cli as cli
    from talyx.hub.wire import ChildDown, ChildUp, ProxyOverhead
    from talyx.proxy.jsonrpc import RequestSeen

    monkeypatch.setenv("TALYX_HUB", "on")
    monkeypatch.setenv("TALYX_HUB_PORT", "1")
    monkeypatch.setenv("TALYX_SERVER_NAME", "mem")
    monkeypatch.setattr(cli, "StdioProxy", _FakeProxy)
    monkeypatch.setattr(cli, "HubClient", _FakeClient)
    monkeypatch.setattr(cli, "start_metrics_server", _no_metrics_server)
    _FakeClient.instances = []

    assert cli.main(["--", "srv"]) == 0

    (client,) = _FakeClient.instances
    assert client.port == 1
    servers = {s for s, _ in client.sent}
    assert servers == {"mem"}
    items = [i for _, i in client.sent]
    assert items[0] == ChildUp()
    assert any(isinstance(i, RequestSeen) for i in items)
    assert any(isinstance(i, ProxyOverhead) for i in items)
    # The in-flight request is reported lost on the crash.
    assert items[-1] == ChildDown(crashed=True, inflight_lost=1)
    # close() runs after the final ChildDown was queued.
    assert client.closed_after == len(client.sent)


def test_hub_off_keeps_local_endpoint(monkeypatch) -> None:
    import talyx.cli as cli

    started = []
    monkeypatch.setattr(cli, "StdioProxy", _FakeProxy)
    monkeypatch.setattr(cli, "HubClient", _FakeClient)
    monkeypatch.setattr(cli, "start_metrics_server", lambda *a, **k: started.append(a))
    _FakeClient.instances = []
    assert cli.main(["--", "srv"]) == 0
    assert len(started) == 1
    assert _FakeClient.instances == []
