from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hub_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep every test on today's local-/metrics path so nothing auto-spawns a
    # real hub; hub tests opt back in explicitly.
    monkeypatch.setenv("TALYX_HUB", "off")
