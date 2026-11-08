"""Read Claude Code's MCP config and decide how to rewrite entries (pure, no side effects)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Scope = Literal["user", "local"]

MCP_REMOTE_PIN = "mcp-remote@0.16.5"


@dataclass(frozen=True)
class ServerEntry:
    name: str
    scope: Scope
    spec: dict[str, Any]


@dataclass(frozen=True)
class Wrap:
    entry: ServerEntry
    new_spec: dict[str, Any]


@dataclass(frozen=True)
class Skip:
    entry: ServerEntry
    reason: str


def _servers(obj: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(obj, dict):
        return {}
    servers = obj.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {k: v for k, v in servers.items() if isinstance(v, dict)}


def read_entries(claude_json: Path, cwd: Path) -> list[ServerEntry]:
    """User-scope (top-level) and local-scope (projects[cwd]) servers; project scope ignored."""
    try:
        data = json.loads(claude_json.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(data, dict):
        return []
    entries = [ServerEntry(n, "user", s) for n, s in _servers(data).items()]
    projects = data.get("projects")
    if isinstance(projects, dict):
        proj = projects.get(str(cwd))
        if proj is None:
            proj = projects.get(str(cwd.resolve()))
        entries += [ServerEntry(n, "local", s) for n, s in _servers(proj).items()]
    return entries


def _wrapped(entry: ServerEntry, talyx_cmd: str, argv: list[str]) -> Wrap:
    env = {"TALYX_SERVER_NAME": entry.name, **(entry.spec.get("env") or {})}
    return Wrap(entry, {"type": "stdio", "command": talyx_cmd, "args": ["--", *argv], "env": env})


def _is_talyx(command: str) -> bool:
    base = command.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base in {"talyx", "talyx.exe"}


def plan_wrap(entry: ServerEntry, talyx_cmd: str) -> Wrap | Skip:
    spec = entry.spec
    kind = spec.get("type", "stdio")
    if kind == "stdio":
        command = spec.get("command")
        if not command:
            return Skip(entry, "stdio server has no command")
        if command == talyx_cmd or _is_talyx(command):
            return Skip(entry, "already wrapped by talyx")
        return _wrapped(entry, talyx_cmd, [command, *(spec.get("args") or [])])
    if kind == "http":
        url = spec.get("url")
        if not url:
            return Skip(entry, "http server has no url")
        if spec.get("headers"):
            return Skip(entry, "http server has headers (credentials must not go into argv)")
        return _wrapped(entry, talyx_cmd, ["npx", "-y", MCP_REMOTE_PIN, url])
    return Skip(entry, f"unsupported transport type: {kind}")
