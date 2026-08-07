from __future__ import annotations

import talyx


def test_package_importable() -> None:
    assert isinstance(talyx.__version__, str)
