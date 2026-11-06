"""Temporary test that must fail CI. Revert the commit that adds this file."""

from __future__ import annotations


def test_ops_002_ci_canary() -> None:
    """Proves the test job goes red. This file is not meant to stay."""
    actual = 0
    expected = 1
    assert actual == expected, "OPS-002 deliberate CI failure"
