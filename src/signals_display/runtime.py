"""Bounded background work for a responsive, read-only dashboard."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import threading
from time import monotonic, perf_counter
from time import process_time
import logging
import json
import resource


class CachedReport:
    """One calculation at a time; HTTP requests only read the last result."""

    def __init__(self, calculate, *, interval=30, name="dashboard-report"):
        self.calculate = calculate
        self.interval = interval
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self.lock = threading.Lock()
        self.future = None
        self.started = None
        self.value = None
        self.generated_at = None
        self.duration_ms = None
        self.failed = False

    def _calculate(self):
        started = perf_counter()
        value = self.calculate()
        return value, datetime.now(timezone.utc).isoformat(), (perf_counter() - started) * 1000

    def get(self):
        with self.lock:
            if self.future is not None and self.future.done():
                try:
                    self.value, self.generated_at, self.duration_ms = self.future.result()
                    self.failed = False
                except Exception:
                    # Do not forward private database contents or exception bodies.
                    self.failed = True
                self.future = None
            if self.future is None and (
                self.started is None or monotonic() - self.started >= self.interval
            ):
                self.started = monotonic()
                self.future = self.pool.submit(self._calculate)
            return {
                "status": "ERROR"
                if self.failed
                else "READY"
                if self.value is not None
                else "CALCULATING",
                "generated_at": self.generated_at,
                "calculation_ms": round(self.duration_ms, 2)
                if self.duration_ms is not None
                else None,
                "refresh_in_progress": self.future is not None,
                "value": deepcopy(self.value),
            }

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class LatestPayload:
    """A size-one mailbox: slow calculations cannot queue obsolete market frames."""

    def __init__(self):
        self.condition = threading.Condition()
        self.sequence = 0
        self.item = None

    def put(self, item):
        with self.condition:
            self.sequence += 1
            self.item = item
            self.condition.notify_all()

    def get(self, after, *, timeout=0.25):
        with self.condition:
            self.condition.wait_for(lambda: self.sequence > after, timeout)
            return self.sequence, self.item if self.sequence > after else None


class RuntimeMeter:
    def __init__(self):
        self.wall = monotonic()
        self.cpu = process_time()
        self.logged = {}

    def sample(self):
        wall, cpu = monotonic(), process_time()
        elapsed = wall - self.wall
        result = {
            "process_cpu_percent_one_core": round(100 * (cpu - self.cpu) / elapsed, 2)
            if elapsed > 0
            else None,
            "peak_resident_mib": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
            ),
            "memory_note": "Linux process high-water RSS, not current usage; CPU is process time / monotonic elapsed time",
        }
        self.wall, self.cpu = wall, cpu
        return result

    def error(self, component, error_type):
        # Callers pass exception TYPE only; never exception messages, account
        # payloads, authentication headers, file contents or arbitrary URLs.
        if component not in {"public_rest", "account_sync", "settlement", "prediction"}:
            return
        if not isinstance(error_type, str) or not error_type.isidentifier():
            error_type = "RedactedError"
        key, now = (component, error_type), monotonic()
        if now - self.logged.get(key, -1e9) < 30:
            return
        self.logged[key] = now
        logging.getLogger("signals_display").warning(
            json.dumps(
                {
                    "event": "read_only_monitor_error",
                    "component": component,
                    "error_type": error_type,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
