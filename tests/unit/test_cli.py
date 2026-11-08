from __future__ import annotations

import json

import pytest

from talyx import cli
from talyx.cli import _Lifecycle, main, parse_command, resolve_talyx_cmd
from talyx.metrics.registry import Metrics
from talyx.proxy.jsonrpc import TrafficObserver
from talyx.wrap.apply import Report


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


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every real-path helper at tmp_path so no test touches ~ or `claude`."""
    monkeypatch.setattr(cli, "_claude_json_path", lambda: tmp_path / "claude.json")
    monkeypatch.setattr(cli, "_backup_path", lambda: tmp_path / "backup.json")
    monkeypatch.setattr(cli, "_cwd", lambda: tmp_path)
    monkeypatch.setattr(cli, "read_entries", lambda claude_json, cwd: [])
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/opt/bin/talyx")
    return tmp_path


def test_separator_forces_proxy_mode(monkeypatch) -> None:
    called = {}
    monkeypatch.setattr(cli, "_run_proxy", lambda cmd: called.setdefault("cmd", cmd) and 0)
    cli.main(["--", "wrap", "x"])
    assert called["cmd"] == ["wrap", "x"]


def test_wrap_subcommand_dispatches(sandbox, monkeypatch) -> None:
    seen = {}

    def fake_wrap_all(entries, talyx_cmd, cli_, backup, cwd, dry_run=False):
        seen.update(talyx_cmd=talyx_cmd, dry_run=dry_run, backup=backup, cwd=cwd)
        return Report(wrapped=["a"], skipped=[("b", "remote")], failed=[])

    monkeypatch.setattr(cli, "wrap_all", fake_wrap_all)
    assert main(["wrap", "--dry-run"]) == 0
    assert seen["dry_run"] is True
    assert seen["talyx_cmd"] == "/opt/bin/talyx"
    assert seen["cwd"] == sandbox


def test_wrap_failure_exits_1(sandbox, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "wrap_all", lambda *a, **k: Report(failed=[("x", "boom")]))
    assert main(["wrap"]) == 1
    assert "boom" in capsys.readouterr().err


def test_wrap_notes_project_scope(sandbox, monkeypatch, capsys) -> None:
    (sandbox / ".mcp.json").write_text("{}")
    monkeypatch.setattr(cli, "wrap_all", lambda *a, **k: Report())
    assert main(["wrap"]) == 0
    assert ".mcp.json" in capsys.readouterr().err


def test_unwrap_prints_restored(sandbox, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "unwrap_all", lambda *a, **k: Report(wrapped=["mem"]))
    assert main(["unwrap"]) == 0
    err = capsys.readouterr().err
    assert "restored" in err and "mem" in err


def test_backup_error_is_one_line(sandbox, monkeypatch, capsys) -> None:
    def boom(*a, **k):
        raise cli.BackupError("backup is corrupt")

    monkeypatch.setattr(cli, "unwrap_all", boom)
    assert main(["unwrap"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "talyx: backup is corrupt"


def test_invalid_claude_json_is_one_line(sandbox, monkeypatch, capsys) -> None:
    def bad(claude_json, cwd):
        raise json.JSONDecodeError("bad", "x", 0)

    monkeypatch.setattr(cli, "read_entries", bad)
    assert main(["wrap"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("talyx: ") and "Traceback" not in err


def test_hub_subcommand_runs_hub(monkeypatch) -> None:
    monkeypatch.setattr(cli, "run_hub", lambda config: 7)
    assert main(["hub"]) == 7


def test_dashboard_placeholder(capsys) -> None:
    assert main(["dashboard"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dashboard not available yet" in captured.err


def test_venv_talyx_warns(tmp_path, monkeypatch) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("")
    exe = tmp_path / ".venv" / "bin" / "talyx"
    exe.write_text("")
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(exe))
    path, warning = resolve_talyx_cmd()
    assert path is not None and warning is not None and "virtualenv" in warning


def test_talyx_missing_errors(sandbox, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert main(["wrap"]) == 1
    assert "uv tool install" in capsys.readouterr().err


def test_subcommand_output_never_on_stdout(sandbox, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli, "wrap_all", lambda *a, **k: Report(wrapped=["a"], skipped=[("b", "r")])
    )
    monkeypatch.setattr(cli, "unwrap_all", lambda *a, **k: Report(wrapped=["a"]))
    main(["wrap"])
    main(["unwrap"])
    main(["dashboard"])
    assert capsys.readouterr().out == ""


def test_help_lists_subcommands(capsys) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in ("wrap", "unwrap", "hub", "dashboard"):
        assert name in out
