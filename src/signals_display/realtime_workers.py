"""Bounded latest-value workers for optional signals-only analytics."""
from __future__ import annotations

from queue import Empty, Full, Queue
import threading
from time import monotonic


class LatestValueWorker:
    """Run a pure calculation away from ingestion, dropping superseded work."""

    def __init__(self, calculate, *, name="signals-analysis", max_queue=2):
        if not 1 <= max_queue <= 32:
            raise ValueError("max_queue must be between 1 and 32")
        self.calculate = calculate
        self.queue = Queue(maxsize=max_queue)
        self.name = name
        self.result = None
        self.metrics = {
            "submitted": 0, "completed": 0, "dropped_superseded": 0,
            "errors": 0, "last_latency_ms": None, "maximum_queue": max_queue,
        }
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self.thread.start()

    def submit(self, sequence, value):
        self.metrics["submitted"] += 1
        item = (sequence, monotonic(), value)
        try:
            self.queue.put_nowait(item)
        except Full:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                self.metrics["dropped_superseded"] += 1
            except Empty:  # pragma: no cover - another consumer won the race
                pass
            self.queue.put_nowait(item)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                sequence, started, value = self.queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                calculated = self.calculate(value)
                current = self.result
                if current is None or sequence > current[0]:
                    self.result = (sequence, calculated)
                self.metrics["completed"] += 1
                self.metrics["last_latency_ms"] = round((monotonic() - started) * 1000, 3)
            except Exception:
                self.metrics["errors"] += 1
            finally:
                self.queue.task_done()

    def snapshot(self):
        return {
            "status": "RUNNING" if self.thread and self.thread.is_alive() else "STOPPED",
            "queue_depth": self.queue.qsize(), "metrics": dict(self.metrics),
            "latest_sequence": self.result[0] if self.result else None,
            "latest": self.result[1] if self.result else None,
        }

    def close(self, timeout=1):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

