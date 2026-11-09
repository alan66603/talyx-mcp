from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from talyx.hub.client import spawn_hub

PROJECT = "talyx-dashboard"

# In a source checkout the wheel's force-include has not run, so these two
# asset files live in the repo instead.
_REPO_FALLBACKS = {
    "rules/mcp-rules.yaml": "alerts/mcp-rules.yaml",
    "dashboards/mcp-overview.json": "dashboards/mcp-overview.json",
}


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _copy_tree(src: Traversable, dest: Path) -> None:
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            _copy_tree(child, dest / child.name)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())


def _install_assets(target: Path) -> None:
    assets = resources.files("talyx.dashboard") / "assets"
    _copy_tree(assets, target)
    repo_root = Path(__file__).resolve().parents[3]
    for rel, fallback in _REPO_FALLBACKS.items():
        if not (target / rel).exists():
            src = repo_root / fallback
            if not src.is_file():
                raise FileNotFoundError(f"dashboard asset missing: {rel}")
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target / rel)


def _compose(home: Path, *args: str) -> list[str]:
    compose_file = home / "dashboard" / "compose.yaml"
    return ["docker", "compose", "-p", PROJECT, "-f", str(compose_file), *args]


def _docker_ok(run: Callable[..., subprocess.CompletedProcess]) -> bool:
    try:
        result = run(["docker", "compose", "version"], capture_output=True)
    except OSError:
        return False
    return result.returncode == 0


def up(
    home: Path,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    spawn: Callable[[], object] = spawn_hub,
) -> int:
    if not _docker_ok(run):
        _err("talyx: 需要 Docker（docker compose v2）")
        return 1
    _install_assets(home / "dashboard")
    spawn()  # make sure the hub is up so MCPExporterDown does not fire at once
    try:
        result = run(_compose(home, "up", "-d"))
    except OSError as e:
        _err(f"talyx: docker compose failed: {e}")
        return 1
    if result.returncode != 0:
        return result.returncode
    _err("talyx: Grafana http://localhost:3000")
    _err("talyx: Prometheus http://localhost:9090")
    return 0


def down(home: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int:
    try:
        return run(_compose(home, "down")).returncode
    except OSError as e:
        _err(f"talyx: docker compose failed: {e}")
        return 1
