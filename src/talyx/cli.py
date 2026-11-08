from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from talyx.config import Config
from talyx.hub.client import HubClient, spawn_hub
from talyx.hub.server import run_hub
from talyx.hub.wire import ChildDown, ChildUp, ProxyOverhead
from talyx.metrics.registry import Metrics, apply_event
from talyx.metrics.server import start_metrics_server
from talyx.proxy.jsonrpc import Event, TrafficObserver
from talyx.proxy.stdio import StdioProxy
from talyx.wrap.apply import BackupError, ClaudeCli, Report, unwrap_all, wrap_all
from talyx.wrap.plan import read_entries

SUBCOMMANDS = ("wrap", "unwrap", "hub", "dashboard")

USAGE = "usage: talyx -- <server command> [args...]"

_HUB_HOST = "127.0.0.1"


def parse_command(argv: list[str]) -> list[str]:
    """Extract the wrapped server command: everything after an optional `--`."""
    if argv and argv[0] == "--":
        argv = argv[1:]
    return argv


def server_name(config: Config, command: list[str]) -> str:
    """The `server` metric label: explicit config, else the command's basename."""
    if config.server_name:
        return config.server_name
    return os.path.basename(command[0]) if command else "default"


class _Recorder:
    """Bridges observed traffic to metrics, timing its own overhead per chunk."""

    def __init__(self, tracker: TrafficObserver, metrics: Metrics) -> None:
        self._tracker = tracker
        self._metrics = metrics

    def on_client_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_client, chunk)

    def on_server_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_server, chunk)

    def _record(self, feed: Callable[[bytes], list[Event]], chunk: bytes) -> None:
        start = perf_counter()
        for event in feed(chunk):
            apply_event(self._metrics, event)
        self._metrics.proxy_overhead.record(perf_counter() - start, {})


class _Lifecycle:
    """Basic liveness: child up/down, and in-flight loss when the child dies."""

    def __init__(self, tracker: TrafficObserver, metrics: Metrics) -> None:
        self._tracker = tracker
        self._metrics = metrics

    def up(self) -> None:
        self._metrics.server_up.set(1, {"server": self._metrics.server})

    def down(self, crashed: bool) -> None:
        labels = {"server": self._metrics.server}
        if not crashed:
            # Client closed first and the server followed: a clean shutdown, not
            # an outage, so drop the series instead of reporting 0.
            self._metrics.server_up.remove(labels)
            return
        self._metrics.server_up.set(0, labels)
        # Requests still awaiting a response when the child dies are lost — the
        # server that would have answered them is gone.
        lost = self._tracker.pending_count
        if lost:
            self._metrics.inflight_lost.add(lost, {"server": self._metrics.server})


class _HubRecorder:
    """Hub mode: ships observed events (and per-chunk overhead) to the hub."""

    def __init__(self, tracker: TrafficObserver, client: HubClient, server: str) -> None:
        self._tracker = tracker
        self._client = client
        self._server = server

    def on_client_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_client, chunk)

    def on_server_bytes(self, chunk: bytes) -> None:
        self._record(self._tracker.feed_server, chunk)

    def _record(self, feed: Callable[[bytes], list[Event]], chunk: bytes) -> None:
        start = perf_counter()
        for event in feed(chunk):
            self._client.send(self._server, event)
        self._client.send(self._server, ProxyOverhead(perf_counter() - start))


class _HubLifecycle:
    """Hub mode: child up/down; the hub decides what that means for server_up."""

    def __init__(self, tracker: TrafficObserver, client: HubClient, server: str) -> None:
        self._tracker = tracker
        self._client = client
        self._server = server

    def up(self) -> None:
        self._client.send(self._server, ChildUp())

    def down(self, crashed: bool) -> None:
        lost = self._tracker.pending_count if crashed else 0
        self._client.send(self._server, ChildDown(crashed=crashed, inflight_lost=lost))


def resolve_talyx_cmd() -> tuple[str | None, str | None]:
    """Absolute path of the `talyx` on PATH, plus an optional warning."""
    found = shutil.which("talyx")
    if found is None:
        return None, "talyx is not on PATH; install it with: uv tool install talyx-mcp"
    path = Path(found).absolute()
    for parent in path.parents:
        if (parent / "pyvenv.cfg").is_file():
            return str(path), (
                f"talyx resolves inside a virtualenv ({path}); wrapped servers break if it "
                "moves. Prefer: uv tool install talyx-mcp"
            )
    return str(path), None


def _claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def _backup_path() -> Path:
    return Path.home() / ".talyx" / "wrap-backup.json"


def _cwd() -> Path:
    return Path.cwd()


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _print_report(report: Report, done: str) -> None:
    for name in report.wrapped:
        _err(f"  {done}: {name}")
    for name, reason in report.skipped:
        _err(f"  skipped: {name} ({reason})")
    for name, reason in report.failed:
        _err(f"  failed: {name} ({reason})")


def _cmd_wrap(dry_run: bool) -> int:
    talyx_cmd, warning = resolve_talyx_cmd()
    if talyx_cmd is None:
        _err(f"talyx: {warning}")
        return 1
    if warning:
        _err(f"talyx: warning: {warning}")
    cwd = _cwd()
    entries = read_entries(_claude_json_path(), cwd)
    report = wrap_all(entries, talyx_cmd, ClaudeCli(), _backup_path(), cwd, dry_run=dry_run)
    _print_report(report, "would wrap" if dry_run else "wrapped")
    if (cwd / ".mcp.json").exists():
        _err("talyx: project scope (.mcp.json) is not handled")
    if dry_run:
        _err("talyx: dry run, nothing changed")
    else:
        _err("talyx: restart your Claude Code session for changes to take effect")
    return 1 if report.failed else 0


def _cmd_unwrap() -> int:
    report = unwrap_all(_claude_json_path(), ClaudeCli(), _backup_path(), _cwd())
    _print_report(report, "restored")
    _err("talyx: restart your Claude Code session for changes to take effect")
    return 1 if report.failed else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="talyx",
        description="MCP observability proxy. Proxy a server with: talyx -- <server command>",
    )
    sub = parser.add_subparsers(dest="subcommand", metavar="{wrap,unwrap,hub,dashboard}")
    wrap = sub.add_parser("wrap", help="route Claude Code's MCP servers through talyx")
    wrap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    sub.add_parser("unwrap", help="restore the MCP servers that `talyx wrap` rewrote")
    sub.add_parser("hub", help="run the shared metrics hub")
    sub.add_parser("dashboard", help="start/stop the bundled dashboard (not available yet)")
    return parser


def _run_subcommand(args: list[str]) -> int:
    try:
        ns = _build_parser().parse_args(args)
    except SystemExit as e:  # --help (0) or a usage error (2)
        return e.code if isinstance(e.code, int) else 1
    try:
        if ns.subcommand == "wrap":
            return _cmd_wrap(ns.dry_run)
        if ns.subcommand == "unwrap":
            return _cmd_unwrap()
        if ns.subcommand == "hub":
            return run_hub(Config.from_env())
        _err("talyx: dashboard not available yet")
        return 2
    except (BackupError, json.JSONDecodeError, OSError) as e:
        _err(f"talyx: {e}")
        return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args and (args[0] in SUBCOMMANDS or args[0] in ("-h", "--help")):
        return _run_subcommand(args)
    command = parse_command(args)
    if not command:
        print(USAGE, file=sys.stderr)
        return 2
    return _run_proxy(command)


def _run_proxy(command: list[str]) -> int:
    if not command:
        print(USAGE, file=sys.stderr)
        return 2

    config = Config.from_env()
    server = server_name(config, command)
    tracker = TrafficObserver(abandon_timeout_seconds=config.abandoned_timeout_seconds)
    client: HubClient | None = None
    recorder: _Recorder | _HubRecorder
    lifecycle: _Lifecycle | _HubLifecycle
    if config.hub_enabled:
        client = HubClient(_HUB_HOST, config.hub_port, spawn=spawn_hub)
        recorder = _HubRecorder(tracker, client, server)
        lifecycle = _HubLifecycle(tracker, client, server)
    else:
        metrics = Metrics(server=server, otlp_endpoint=config.otlp_endpoint)
        start_metrics_server(metrics, config.metrics_host, config.metrics_port)
        recorder = _Recorder(tracker, metrics)
        lifecycle = _Lifecycle(tracker, metrics)

    proxy = StdioProxy(
        command,
        on_client_bytes=recorder.on_client_bytes,
        on_server_bytes=recorder.on_server_bytes,
        on_child_up=lifecycle.up,
        on_child_down=lifecycle.down,
    )
    try:
        return asyncio.run(proxy.run())
    finally:
        # on_child_down has queued the final ChildDown by now; flush it (bounded).
        if client is not None:
            client.close()



if __name__ == "__main__":
    sys.exit(main())
