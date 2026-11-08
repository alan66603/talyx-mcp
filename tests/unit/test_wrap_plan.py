from __future__ import annotations

import json
from pathlib import Path

import pytest

from talyx.wrap.plan import (
    MCP_REMOTE_PIN,
    ServerEntry,
    Skip,
    Wrap,
    plan_wrap,
    read_entries,
)


def test_stdio_is_wrapped_and_env_kept() -> None:
    e = ServerEntry(
        "mem", "user", {"type": "stdio", "command": "npx", "args": ["-y", "m"], "env": {"K": "v"}}
    )
    assert plan_wrap(e, "/bin/talyx") == Wrap(
        e,
        {
            "type": "stdio",
            "command": "/bin/talyx",
            "args": ["--", "npx", "-y", "m"],
            "env": {"TALYX_SERVER_NAME": "mem", "K": "v"},
        },
    )


def test_user_server_name_wins() -> None:
    e = ServerEntry("mem", "user", {"command": "x", "env": {"TALYX_SERVER_NAME": "mine"}})
    r = plan_wrap(e, "/bin/talyx")
    assert isinstance(r, Wrap)
    assert r.new_spec["env"]["TALYX_SERVER_NAME"] == "mine"


def test_already_wrapped_is_skipped() -> None:
    e = ServerEntry("mem", "user", {"type": "stdio", "command": "/x/talyx", "args": ["--", "a"]})
    r = plan_wrap(e, "/bin/talyx")
    assert isinstance(r, Skip)
    assert "already wrapped" in r.reason


@pytest.mark.parametrize("cmd", ["/x/talyx.exe", "C:\\x\\talyx.exe", "C:\\x\\TALYX.EXE"])
def test_already_wrapped_windows_exe_is_skipped(cmd: str) -> None:
    e = ServerEntry("mem", "user", {"type": "stdio", "command": cmd, "args": ["--", "a"]})
    r = plan_wrap(e, "/bin/talyx")
    assert isinstance(r, Skip)
    assert "already wrapped" in r.reason


def test_http_without_headers_uses_mcp_remote() -> None:
    e = ServerEntry("h", "user", {"type": "http", "url": "https://e.com/mcp"})
    r = plan_wrap(e, "/bin/talyx")
    assert isinstance(r, Wrap)
    assert r.new_spec["args"] == ["--", "npx", "-y", MCP_REMOTE_PIN, "https://e.com/mcp"]
    assert r.new_spec["env"] == {"TALYX_SERVER_NAME": "h"}
    assert r.new_spec["type"] == "stdio"


def test_http_with_headers_is_skipped() -> None:
    e = ServerEntry("h", "user", {"type": "http", "url": "https://e.com", "headers": {"X": "1"}})
    r = plan_wrap(e, "/bin/talyx")
    assert isinstance(r, Skip)
    assert "headers" in r.reason


def test_sse_is_skipped() -> None:
    e = ServerEntry("s", "user", {"type": "sse", "url": "https://e.com/sse"})
    assert isinstance(plan_wrap(e, "/bin/talyx"), Skip)


def test_missing_type_defaults_to_stdio() -> None:
    e = ServerEntry("old", "user", {"command": "node", "args": ["a.js"]})
    r = plan_wrap(e, "/bin/talyx")
    assert isinstance(r, Wrap)
    assert r.new_spec["args"] == ["--", "node", "a.js"]
    assert r.new_spec["env"] == {"TALYX_SERVER_NAME": "old"}


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "claude.json"
    p.write_text(json.dumps(data))
    return p


def test_read_entries_user_and_local(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    p = _write(
        tmp_path,
        {
            "mcpServers": {"u": {"command": "a"}},
            "projects": {str(cwd): {"mcpServers": {"l": {"command": "b"}}, "other": 1}},
        },
    )
    assert read_entries(p, cwd) == [
        ServerEntry("u", "user", {"command": "a"}),
        ServerEntry("l", "local", {"command": "b"}),
    ]


def test_read_entries_resolved_cwd_fallback(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    p = _write(tmp_path, {"projects": {str(real.resolve()): {"mcpServers": {"l": {}}}}})
    assert [e.name for e in read_entries(p, link)] == ["l"]


def test_read_entries_ignores_other_projects(tmp_path: Path) -> None:
    p = _write(tmp_path, {"projects": {"/elsewhere": {"mcpServers": {"x": {"command": "a"}}}}})
    assert read_entries(p, tmp_path) == []


def test_read_entries_missing_file(tmp_path: Path) -> None:
    assert read_entries(tmp_path / "nope.json", tmp_path) == []
