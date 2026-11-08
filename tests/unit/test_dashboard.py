from __future__ import annotations

import subprocess
from importlib import resources
from pathlib import Path

from talyx.dashboard import PROJECT, down, up


def _recorder(calls: list[list[str]], fail_first: bool = False):
    def run(args, **kw):
        calls.append(list(args))
        rc = 1 if fail_first and len(calls) == 1 else 0
        return subprocess.CompletedProcess(args, rc)

    return run


def test_up_copies_assets_and_runs_compose(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    spawned: list[int] = []
    assert up(tmp_path, run=_recorder(calls), spawn=lambda: spawned.append(1)) == 0
    assert spawned == [1]
    assert (tmp_path / "dashboard/rules/mcp-rules.yaml").exists()
    assert (tmp_path / "dashboard/dashboards/mcp-overview.json").exists()
    assert (tmp_path / "dashboard/grafana/provisioning/datasources/datasource.yml").exists()
    assert calls[-1][:6] == [
        "docker", "compose", "-p", PROJECT, "-f", str(tmp_path / "dashboard/compose.yaml"),
    ]
    assert calls[-1][-2:] == ["up", "-d"]


def test_up_keeps_other_files_in_home(tmp_path: Path) -> None:
    (tmp_path / "wrap-backup.json").write_text("{}")
    assert up(tmp_path, run=_recorder([]), spawn=lambda: None) == 0
    assert (tmp_path / "wrap-backup.json").read_text() == "{}"


def test_up_without_docker_fails_cleanly(tmp_path: Path, capsys) -> None:
    spawned: list[int] = []
    rc = up(tmp_path, run=_recorder([], fail_first=True), spawn=lambda: spawned.append(1))
    assert rc == 1
    assert "Docker" in capsys.readouterr().err
    assert spawned == []


def test_down_keeps_volumes(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    assert down(tmp_path, run=_recorder(calls)) == 0
    assert calls[-1][-1] == "down"
    assert "-v" not in calls[-1]


def test_stack_config_pins_storage_and_target() -> None:
    assets = resources.files("talyx.dashboard") / "assets"
    compose = (assets / "compose.yaml").read_text()
    assert "talyx-prometheus-data" in compose
    assert "talyx-grafana-data" in compose
    assert "mcp-demo" not in compose
    assert "host.docker.internal:9464" in (assets / "prometheus.yml").read_text()
