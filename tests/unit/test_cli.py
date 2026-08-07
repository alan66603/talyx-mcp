from __future__ import annotations

import json

from talyx.cli import _Lifecycle, main, parse_command
from talyx.metrics.registry import Metrics
from talyx.proxy.jsonrpc import SessionTracker


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
    tracker = SessionTracker()
    # One request in flight with no response yet.
    tracker.feed_client(
        (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}) + "\n").encode()
    )
    lifecycle = _Lifecycle(tracker, metrics)

    lifecycle.up()
    assert metrics.registry.get_sample_value("talyx_server_up") == 1.0

    lifecycle.down()
    assert metrics.registry.get_sample_value("talyx_server_up") == 0.0
    # The in-flight request is lost when the child dies.
    assert (
        metrics.registry.get_sample_value("talyx_inflight_requests_lost_total", {"server": "s"})
        == 1.0
    )
