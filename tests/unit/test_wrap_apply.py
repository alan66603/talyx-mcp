from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

from talyx.wrap.apply import ClaudeCli, unwrap_all, wrap_all
from talyx.wrap.plan import ServerEntry


class FakeCli:
    def __init__(self, fail_on: list[tuple[str, int]] | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.specs: list[dict[str, Any]] = []
        self.last_spec: dict[str, Any] | None = None
        self._fail_on = fail_on or []
        self._counts: dict[str, int] = {}

    def _tick(self, op: str, name: str, scope: str) -> bool:
        self.calls.append((op, name, scope))
        self._counts[op] = self._counts.get(op, 0) + 1
        return (op, self._counts[op]) not in self._fail_on

    def remove(self, name: str, scope: str, cwd: Path) -> bool:
        return self._tick("remove", name, scope)

    def add_json(self, name: str, scope: str, spec: dict[str, Any], cwd: Path) -> bool:
        self.last_spec = spec
        self.specs.append(spec)
        return self._tick("add_json", name, scope)


def stdio_entry(name: str, scope: str = "user") -> ServerEntry:
    return ServerEntry(name, scope, {"type": "stdio", "command": "npx", "args": ["-y", "pkg"]})  # type: ignore[arg-type]


def records(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())["records"]


def test_wrap_calls_remove_then_add(tmp_path: Path) -> None:
    cli = FakeCli()
    r = wrap_all([stdio_entry("mem")], "/bin/talyx", cli, tmp_path / "b.json", tmp_path)  # type: ignore[arg-type]
    assert cli.calls == [("remove", "mem", "user"), ("add_json", "mem", "user")]
    assert r.wrapped == ["mem"]
    assert cli.last_spec["command"] == "/bin/talyx"  # type: ignore[index]
    assert len(records(tmp_path / "b.json")) == 1


def test_record_written_before_remove(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    seen: list[int] = []

    class Spy(FakeCli):
        def remove(self, name: str, scope: str, cwd: Path) -> bool:
            seen.append(len(records(backup)))
            return super().remove(name, scope, cwd)

    wrap_all([stdio_entry("mem")], "/bin/talyx", Spy(), backup, tmp_path)  # type: ignore[arg-type]
    assert seen == [1]


def test_add_failure_restores_original(tmp_path: Path) -> None:
    cli = FakeCli(fail_on=[("add_json", 1)])
    backup = tmp_path / "b.json"
    r = wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    assert cli.calls[-1] == ("add_json", "mem", "user")
    assert cli.last_spec == stdio_entry("mem").spec
    assert r.failed and not r.wrapped
    assert records(backup) == []


def test_restore_failure_keeps_record(tmp_path: Path) -> None:
    cli = FakeCli(fail_on=[("add_json", 1), ("add_json", 2)])
    backup = tmp_path / "b.json"
    r = wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    assert r.failed
    assert len(records(backup)) == 1  # original still recoverable


def test_remove_failure_leaves_server_untouched(tmp_path: Path) -> None:
    cli = FakeCli(fail_on=[("remove", 1)])
    backup = tmp_path / "b.json"
    r = wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    assert [c[0] for c in cli.calls] == ["remove"]
    assert r.failed and records(backup) == []


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    cli = FakeCli()
    backup = tmp_path / "b.json"
    r = wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path, dry_run=True)  # type: ignore[arg-type]
    assert cli.calls == []
    assert not backup.exists()
    assert r.wrapped == ["mem"]


def test_dry_run_rejects_corrupt_backup(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    backup.write_text("not json")
    with pytest.raises(BackupError):
        wrap_all([stdio_entry("mem")], "/bin/talyx", FakeCli(), backup, tmp_path, dry_run=True)  # type: ignore[arg-type]


def test_backup_is_private(tmp_path: Path) -> None:
    backup = tmp_path / "sub" / "b.json"
    wrap_all([stdio_entry("mem")], "/bin/talyx", FakeCli(), backup, tmp_path)  # type: ignore[arg-type]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700


def test_skips_are_reported(tmp_path: Path) -> None:
    cli = FakeCli()
    e = ServerEntry("h", "user", {"type": "sse", "url": "http://x"})
    r = wrap_all([e], "/bin/talyx", cli, tmp_path / "b.json", tmp_path)  # type: ignore[arg-type]
    assert cli.calls == [] and r.skipped and r.skipped[0][0] == "h"


def _write_claude_json(path: Path, spec_by_name: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps({"mcpServers": spec_by_name}))


def test_unwrap_restores_original(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    cli = FakeCli()
    wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    wrapped_spec = cli.specs[0]
    cj = tmp_path / "claude.json"
    _write_claude_json(cj, {"mem": wrapped_spec})
    cli2 = FakeCli()
    r = unwrap_all(cj, cli2, backup, tmp_path)  # type: ignore[arg-type]
    assert cli2.calls == [("remove", "mem", "user"), ("add_json", "mem", "user")]
    assert cli2.last_spec == stdio_entry("mem").spec
    assert r.wrapped == ["mem"]
    assert records(backup) == []


def test_unwrap_skips_modified_entry(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    wrap_all([stdio_entry("mem")], "/bin/talyx", FakeCli(), backup, tmp_path)  # type: ignore[arg-type]
    cj = tmp_path / "claude.json"
    _write_claude_json(cj, {"mem": {"type": "stdio", "command": "other"}})
    cli = FakeCli()
    r = unwrap_all(cj, cli, backup, tmp_path)  # type: ignore[arg-type]
    assert cli.calls == []
    assert r.skipped == [("mem", "modified since wrap")]
    assert len(records(backup)) == 1


def test_unwrap_ignores_other_cwd_local_records(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    other = tmp_path / "other"
    other.mkdir()
    cli = FakeCli()
    wrap_all([stdio_entry("loc", "local")], "/bin/talyx", cli, backup, other)  # type: ignore[arg-type]
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"projects": {str(tmp_path): {"mcpServers": {"loc": cli.specs[0]}}}}))
    cli2 = FakeCli()
    r = unwrap_all(cj, cli2, backup, tmp_path)  # type: ignore[arg-type]
    assert cli2.calls == [] and not r.wrapped
    assert len(records(backup)) == 1


def test_unwrap_add_failure_puts_wrapped_back(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    cli = FakeCli()
    wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    wrapped_spec = cli.specs[0]
    cj = tmp_path / "claude.json"
    _write_claude_json(cj, {"mem": wrapped_spec})
    cli2 = FakeCli(fail_on=[("add_json", 1)])
    r = unwrap_all(cj, cli2, backup, tmp_path)  # type: ignore[arg-type]
    assert r.failed and cli2.last_spec == wrapped_spec
    assert len(records(backup)) == 1


def test_wrap_twice_is_idempotent(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    cli = FakeCli()
    wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    again = ServerEntry("mem", "user", cli.specs[0])
    cli2 = FakeCli()
    r = wrap_all([again], "/bin/talyx", cli2, backup, tmp_path)  # type: ignore[arg-type]
    assert cli2.calls == []
    assert r.skipped == [("mem", "already wrapped by talyx")]
    assert len(records(backup)) == 1


def test_claude_cli_argv_and_cwd(tmp_path: Path) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    cli = ClaudeCli(run)
    assert cli.remove("mem", "local", tmp_path)
    assert cli.add_json("mem", "user", {"type": "stdio", "command": "x"}, tmp_path)
    assert seen[0][0] == ["claude", "mcp", "remove", "-s", "local", "mem"]
    assert seen[1][0] == [
        "claude", "mcp", "add-json", "-s", "user", "mem",
        json.dumps({"type": "stdio", "command": "x"}),
    ]  # fmt: skip
    assert all(kw["cwd"] == tmp_path and not kw.get("shell") for _, kw in seen)


def test_claude_cli_failure_and_missing_binary(tmp_path: Path) -> None:
    def bad(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    def missing(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    assert ClaudeCli(bad).remove("m", "user", tmp_path) is False
    assert ClaudeCli(missing).remove("m", "user", tmp_path) is False


def test_existing_dir_is_tightened(tmp_path: Path) -> None:
    d = tmp_path / "dot"
    d.mkdir()
    d.chmod(0o755)
    wrap_all([stdio_entry("mem")], "/bin/talyx", FakeCli(), d / "b.json", tmp_path)  # type: ignore[arg-type]
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "b.json").stat().st_mode) == 0o600


def test_unwrap_putback_failure_reported(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    cli = FakeCli()
    wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    cj = tmp_path / "claude.json"
    _write_claude_json(cj, {"mem": cli.specs[0]})
    cli2 = FakeCli(fail_on=[("add_json", 1), ("add_json", 2)])
    r = unwrap_all(cj, cli2, backup, tmp_path)  # type: ignore[arg-type]
    assert r.failed == [("mem", "restore failed and put-back failed; original kept in backup")]
    assert len(records(backup)) == 1


def test_user_scope_record_ignores_cwd(tmp_path: Path) -> None:
    backup = tmp_path / "b.json"
    a, b = tmp_path / "a", tmp_path / "b"
    wrap_all([stdio_entry("mem")], "/bin/talyx", FakeCli(), backup, a)  # type: ignore[arg-type]
    wrap_all([stdio_entry("mem")], "/bin/talyx", FakeCli(), backup, b)  # type: ignore[arg-type]
    assert len(records(backup)) == 1


import pytest  # noqa: E402

from talyx.wrap.apply import BackupError  # noqa: E402

BAD_BACKUPS = [
    "not json{",
    json.dumps({"version": 1}),
    json.dumps({"version": 1, "records": "x"}),
    json.dumps({"version": 1, "records": [1]}),
    json.dumps({"version": 2, "records": []}),
    json.dumps([]),
]


@pytest.mark.parametrize("content", BAD_BACKUPS)
def test_untrusted_backup_is_never_overwritten(tmp_path: Path, content: str) -> None:
    backup = tmp_path / "b.json"
    backup.write_text(content)
    cli = FakeCli()
    with pytest.raises(BackupError):
        wrap_all([stdio_entry("mem")], "/bin/talyx", cli, backup, tmp_path)  # type: ignore[arg-type]
    cj = tmp_path / "claude.json"
    _write_claude_json(cj, {})
    with pytest.raises(BackupError):
        unwrap_all(cj, cli, backup, tmp_path)  # type: ignore[arg-type]
    assert cli.calls == [] and backup.read_text() == content


def test_claude_cli_timeout_and_stdin(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def ok(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def hang(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 60)

    ClaudeCli(ok).remove("m", "user", tmp_path)
    assert seen[0]["stdin"] == subprocess.DEVNULL and seen[0]["timeout"] == 60
    assert ClaudeCli(hang).remove("m", "user", tmp_path) is False
