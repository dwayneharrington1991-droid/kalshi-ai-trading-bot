"""Research-only speed-ups and resumable checkpoints for opening research.

Nothing here is used by the live bot or by model training. Two pieces:

* ``fit_logistic`` - the same penalised, damped-Newton logistic MLE as
  ``rigorous_evaluation.fit_logistic`` (same objective, intercept left
  unpenalised, same tolerance, iteration cap, 40-step halving line search and
  ``1e-12`` acceptance slack), computed with numpy matrix products instead of a
  per-row Python double loop. Results agree to floating-point rounding; tests
  compare both implementations. If numpy is unavailable the original pure
  Python implementation is used unchanged.

* ``Checkpoints`` - content-addressed fold results. A key hashes the model
  specification, a checkpoint format version, and the exact training,
  validation and test rows (tickers, outcomes, label times, baselines and
  every feature value). A resumed run therefore reuses a fold only when its
  inputs are byte-for-byte the same; any change in data, split or code
  version recomputes it. Files are written atomically and only inside the
  research-output directory.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from . import learning, rigorous_evaluation as rigor

try:  # numpy is a declared project dependency; degrade gracefully regardless.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only on broken installs
    np = None

CHECKPOINT_VERSION = "opening-research-fold-v1"


def numpy_available():
    return np is not None


def _matrix(rows, prep):
    return np.array([learning.transform(r["features"], prep) for r in rows], dtype=float)


def _objective(weights, x, offsets, y, l2):
    z = offsets + x @ weights
    losses = np.maximum(z, 0.0) - y * z + np.log1p(np.exp(-np.abs(z)))
    return float(losses.sum() + 0.5 * l2 * float(weights[:-1] @ weights[:-1]))


def fit_logistic(rows, prep, l2, *, use_offset):
    """Drop-in for ``rigorous_evaluation.fit_logistic`` (same algorithm)."""
    if np is None:
        return rigor.fit_logistic(rows, prep, l2, use_offset=use_offset)
    if not rows or l2 <= 0 or not math.isfinite(l2):
        raise ValueError("Rows and a positive finite L2 strength are required")
    x = _matrix(rows, prep)
    y = np.array([r["outcome"] for r in rows], dtype=float)
    offsets = np.array([rigor._offset(r, use_offset) for r in rows], dtype=float)
    dim = x.shape[1]
    penalty = np.full(dim, float(l2))
    penalty[-1] = 0.0  # intercept unpenalised
    weights = np.zeros(dim)
    converged = False
    gradient_norm = math.inf
    iteration = 0
    for iteration in range(1, rigor.NEWTON_MAX_ITERATIONS + 1):
        z = np.clip(offsets + x @ weights, -35, 35)  # learning.sigmoid clamps at +-35
        p = 1 / (1 + np.exp(-z))
        curvature = np.maximum(p * (1 - p), 1e-12)
        gradient = x.T @ (p - y) + penalty * weights
        hessian = (x * curvature[:, None]).T @ x + np.diag(penalty)
        gradient_norm = float(np.max(np.abs(gradient)))
        if gradient_norm < rigor.NEWTON_TOLERANCE * max(1, len(rows)):
            converged = True
            break
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as exc:
            raise ValueError("Singular Newton system") from exc
        current = _objective(weights, x, offsets, y, l2)
        rate = 1.0
        for _ in range(40):
            proposal = weights - rate * step
            if _objective(proposal, x, offsets, y, l2) <= current + 1e-12:
                break
            rate /= 2
        weights = proposal
        if float(np.max(np.abs(rate * step))) < 1e-12:
            converged = True
            break
    return {
        "kind": "rigorous_offset_logistic" if use_offset else "rigorous_logistic",
        "weights": [float(w) for w in weights],
        "preprocessor": prep,
        "l2": l2,
        "use_baseline_offset": use_offset,
        "converged": converged,
        "newton_iterations": iteration,
        "final_gradient_max_abs": gradient_norm,
        "calibration": {"method": "identity_v1"},
        "role": "SHADOW_ONLY",
    }


# ------------------------------------------------------------- checkpoints


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def row_digest(row):
    """Stable digest of everything a fold can read from one row."""
    payload = [
        row.get("ticker"), row.get("outcome"), row.get("label_available_at"),
        row.get("timestamp"), row.get("opens_at"), _clean(row.get("baseline")),
        sorted((k, _clean(v)) for k, v in (row.get("features") or {}).items()),
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class Checkpoints:
    """Content-addressed fold predictions under one research-output directory."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._digests = {}
        self.hits = 0
        self.misses = 0

    def _digest(self, row):
        key = id(row)
        cached = self._digests.get(key)
        if cached is None or cached[0] is not row:
            cached = (row, row_digest(row))
            self._digests[key] = cached
        return cached[1]

    def key(self, name, spec, train, validation, test):
        h = hashlib.sha256()
        h.update(json.dumps([CHECKPOINT_VERSION, name, sorted(
            (k, list(v) if isinstance(v, tuple) else v) for k, v in spec.items())],
            sort_keys=True, default=str).encode())
        for label, part in (("train", train), ("validation", validation), ("test", test)):
            h.update(label.encode())
            for row in part:
                h.update(self._digest(row).encode())
        return h.hexdigest()

    def _path(self, key):
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key):
        path = self._path(key)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.misses += 1
            return None
        if data.get("version") != CHECKPOINT_VERSION or data.get("key") != key:
            self.misses += 1
            return None
        self.hits += 1
        return data  # {"predictions": {...}} or {"failed": "reason"}

    def put(self, key, name, predictions=None, failed=None):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"version": CHECKPOINT_VERSION, "key": key, "model": name}
        if failed is not None:
            body["failed"] = failed
        else:
            body["predictions"] = predictions
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(body, handle, allow_nan=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
