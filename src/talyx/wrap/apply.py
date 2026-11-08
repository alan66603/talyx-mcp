"""Apply a wrap plan through the Claude Code CLI, with a durable backup and safe rollback."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from talyx.wrap.plan import Scope, ServerEntry, Skip, plan_wrap, read_entries

BACKUP_VERSION = 1
CLI_TIMEOUT_S = 60


class BackupError(Exception):
    """The backup file exists but cannot be trusted; it is never overwritten."""


class ClaudeCli:
    """Thin wrapper over `claude mcp`; never goes through a shell."""

    def __init__(
        self, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    ) -> None:
        self._run = run

    def _exec(self, argv: list[str], cwd: Path) -> bool:
        try:
            proc = self._run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=CLI_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def remove(self, name: str, scope: Scope, cwd: Path) -> bool:
        return self._exec(["claude", "mcp", "remove", "-s", scope, name], cwd)

    def add_json(self, name: str, scope: Scope, spec: dict[str, Any], cwd: Path) -> bool:
        argv = ["claude", "mcp", "add-json", "-s", scope, name, json.dumps(spec)]
        return self._exec(argv, cwd)


@dataclass
class Report:
    wrapped: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def _load(backup: Path) -> list[dict[str, Any]]:
    try:
        text = backup.read_text()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise BackupError(f"cannot read backup {backup}: {e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise BackupError(f"backup {backup} is not valid JSON: {e}") from e
    records = data.get("records") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("version") != BACKUP_VERSION
        or not isinstance(records, list)
        or not all(isinstance(r, dict) for r in records)
    ):
        raise BackupError(f"backup {backup} has an unrecognised shape or version")
    return records


def _save(backup: Path, records: list[dict[str, Any]]) -> None:
    """Durable atomic write: tmp file (0600) + fsync + os.replace."""
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    tmp = backup.with_name(backup.name + ".tmp")
    payload = json.dumps({"version": BACKUP_VERSION, "records": records}, indent=2)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, backup)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _same(rec: dict[str, Any], name: str, scope: str, cwd: str) -> bool:
    if rec.get("name") != name or rec.get("scope") != scope:
        return False
    return scope == "user" or rec.get("cwd") == cwd


def _drop(backup: Path, name: str, scope: str, cwd: str) -> None:
    _save(backup, [r for r in _load(backup) if not _same(r, name, scope, cwd)])


def wrap_all(
    entries: list[ServerEntry],
    talyx_cmd: str,
    cli: ClaudeCli,
    backup: Path,
    cwd: Path,
    dry_run: bool = False,
) -> Report:
    report = Report()
    cwd_s = str(cwd)
    _load(backup)  # read-only; fail fast on an untrusted backup, even for a dry run
    for entry in entries:
        plan = plan_wrap(entry, talyx_cmd)
        if isinstance(plan, Skip):
            report.skipped.append((entry.name, plan.reason))
            continue
        if dry_run:
            report.wrapped.append(entry.name)
            continue
        record = {
            "name": entry.name,
            "scope": entry.scope,
            "cwd": cwd_s,
            "original": entry.spec,
            "wrapped": plan.new_spec,
        }
        # Record must be durable BEFORE the server is removed.
        others = [r for r in _load(backup) if not _same(r, entry.name, entry.scope, cwd_s)]
        _save(backup, [*others, record])
        if not cli.remove(entry.name, entry.scope, cwd):
            report.failed.append((entry.name, "claude mcp remove failed"))
            _drop(backup, entry.name, entry.scope, cwd_s)
            continue
        if cli.add_json(entry.name, entry.scope, plan.new_spec, cwd):
            report.wrapped.append(entry.name)
            continue
        if cli.add_json(entry.name, entry.scope, entry.spec, cwd):
            report.failed.append((entry.name, "add of wrapped spec failed; original restored"))
            _drop(backup, entry.name, entry.scope, cwd_s)
        else:
            # Server is gone; keep the record so `unwrap` / manual recovery has the original.
            report.failed.append(
                (entry.name, "add failed and restore failed; original kept in backup")
            )
    return report


def _cwd_matches(record_cwd: Any, cwd: Path) -> bool:
    if not isinstance(record_cwd, str):
        return False
    if record_cwd == str(cwd) or record_cwd == str(cwd.resolve()):
        return True
    try:
        return Path(record_cwd).resolve() == cwd.resolve()
    except OSError:
        return False


def unwrap_all(claude_json: Path, cli: ClaudeCli, backup: Path, cwd: Path) -> Report:
    report = Report()
    saved = _load(backup)
    current = {(e.name, e.scope): e.spec for e in read_entries(claude_json, cwd)}
    for rec in saved:
        name, scope = rec.get("name"), rec.get("scope")
        if not isinstance(name, str) or scope not in ("user", "local"):
            continue
        if scope == "local" and not _cwd_matches(rec.get("cwd"), cwd):
            continue
        spec = current.get((name, scope))
        if spec is None:
            report.skipped.append((name, "not found in config"))
            continue
        if spec != rec.get("wrapped"):
            report.skipped.append((name, "modified since wrap"))
            continue
        original = rec.get("original")
        if not isinstance(original, dict):
            report.failed.append((name, "backup record has no original"))
            continue
        rec_cwd = rec.get("cwd", "")
        if not cli.remove(name, scope, cwd):
            report.failed.append((name, "claude mcp remove failed"))
            continue
        if cli.add_json(name, scope, original, cwd):
            report.wrapped.append(name)
            _drop(backup, name, scope, rec_cwd)
        else:
            if cli.add_json(name, scope, rec["wrapped"], cwd):
                report.failed.append((name, "restoring original failed; wrapped spec put back"))
            else:
                report.failed.append(
                    (name, "restore failed and put-back failed; original kept in backup")
                )
    return report
