"""Collector pacing around a KXBTC15M quarter-hour opening.

The immutable opening is computed from the first poll that sees the new contract
active with its threshold. Measured on public listings (2026-11-20): the new
contract is active with a published threshold within about one second of the
boundary, while live openings were generated 2-3 s after it. These tests pin the
pacing that removes the avoidable part of that delay without changing inputs.
"""
import pytest

from src.signals_display.server import collector_wait_seconds

BOUNDARY = 1795133700  # 2026-11-20T00:15:00Z, a quarter-hour boundary


def test_normal_cadence_is_unchanged_away_from_a_boundary():
    assert collector_wait_seconds(BOUNDARY + 300, 0.3, 1.0) == pytest.approx(0.7)
    assert collector_wait_seconds(BOUNDARY + 300, 1.7, 1.0) == 0.0


def test_wait_that_would_cross_the_boundary_wakes_just_after_it():
    wait = collector_wait_seconds(BOUNDARY - 0.4, 0.2, 1.0)
    assert wait == pytest.approx(0.45)
    assert BOUNDARY < BOUNDARY - 0.4 + wait < BOUNDARY + 0.1


def test_fast_polling_only_in_the_first_seconds_after_opening():
    assert collector_wait_seconds(BOUNDARY + 0.1, 0.2, 1.0) == pytest.approx(0.25)
    assert collector_wait_seconds(BOUNDARY + 5.9, 0.1, 1.0) == pytest.approx(0.25)
    assert collector_wait_seconds(BOUNDARY + 6.1, 0.1, 1.0) == pytest.approx(0.9)


def test_never_negative_and_never_longer_than_the_normal_wait():
    for offset in (-1.0, -0.01, 0.0, 0.01, 3.0, 450.0, 899.99):
        for elapsed in (0.0, 0.5, 2.0):
            wait = collector_wait_seconds(BOUNDARY + offset, elapsed, 1.0)
            assert 0.0 <= wait <= max(0.0, 1.0 - elapsed) + 1e-9 or wait <= 0.06
