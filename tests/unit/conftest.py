from __future__ import annotations

from collections.abc import Callable

import pytest
from prometheus_client import generate_latest

from talyx.metrics.registry import Metrics


@pytest.fixture
def scrape() -> Callable[[Metrics], str]:
    def _scrape(metrics: Metrics) -> str:
        return generate_latest(metrics.registry).decode()

    return _scrape
