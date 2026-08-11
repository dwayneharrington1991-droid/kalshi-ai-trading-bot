"""Regression tests for the bounded shadow-only model-provider boundary."""

import asyncio
import queue
import time

import pytest

from scripts import compare_multi_signal_shadow as comparison


@pytest.mark.asyncio
async def test_isolated_model_prediction_times_out_without_waiting_for_worker(monkeypatch):
    def uncooperative_worker(_result_queue, _market, _market_price):
        # Deliberately never reports during the caller's timeout window.
        time.sleep(0.2)

    monkeypatch.setattr(comparison, "_prediction_worker", uncooperative_worker)
    started = time.monotonic()
    probability, confidence, state = await comparison.isolated_model_prediction(
        object(), 0.5, timeout_seconds=0.01
    )

    assert (probability, confidence, state) == (None, None, "timeout")
    assert time.monotonic() - started < 0.1


@pytest.mark.asyncio
async def test_isolated_model_prediction_returns_sanitized_worker_result(monkeypatch):
    def successful_worker(result_queue: queue.Queue, _market, _market_price):
        result_queue.put(("ok", 0.72, 0.61))

    monkeypatch.setattr(comparison, "_prediction_worker", successful_worker)

    assert await comparison.isolated_model_prediction(
        object(), 0.5, timeout_seconds=0.1
    ) == (0.72, 0.61, "ok")
