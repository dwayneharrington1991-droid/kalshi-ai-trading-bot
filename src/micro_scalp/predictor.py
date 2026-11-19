"""Walk-forward online ridge regression of the short-horizon mid change.

- A sample (features at t) is added to training ONLY after t + horizon has
  passed in simulated time, i.e. when its label is actually known.
- Standardisation statistics come from training samples only.
- Every prediction made before its label was known is scored later, giving a
  genuinely out-of-sample skill estimate. Trading is gated on that skill.
"""
from __future__ import annotations

from collections import deque
import math

import numpy as np


class OnlineRidge:
    def __init__(self, n_features, ridge=1.0, refit_every=200, min_train=1500, skill_window=3000):
        self.k = n_features
        self.ridge = ridge
        self.refit_every = refit_every
        self.min_train = min_train
        self.n = 0
        self.sx = np.zeros(n_features)
        self.sxx = np.zeros((n_features, n_features))
        self.sxy = np.zeros(n_features)
        self.sy = 0.0
        self.syy = 0.0
        self.beta = None
        self.intercept = 0.0
        self.sigma = None
        self.since_fit = 0
        self.oos = deque(maxlen=skill_window)  # (prediction, realized)

    def warm_start_from(self, other: "OnlineRidge"):
        """Copy past training statistics (e.g. historical -> fresh). Extra
        feature columns this model has (e.g. AI inputs) start with no history:
        they enter the fit only once real observations give them variance."""
        k = other.k
        assert k <= self.k
        self.n = other.n
        self.sx[:k] = other.sx
        self.sxx[:k, :k] = other.sxx
        self.sxy[:k] = other.sxy
        self.sy, self.syy = other.sy, other.syy
        self.oos.extend(other.oos)
        if self.n >= self.min_train:
            self._fit()

    def add(self, x, y):
        x = np.asarray(x, dtype=float)
        self.n += 1
        self.sx += x
        self.sxx += np.outer(x, x)
        self.sxy += x * y
        self.sy += y
        self.syy += y * y
        self.since_fit += 1
        if self.n >= self.min_train and (self.beta is None or self.since_fit >= self.refit_every):
            self._fit()

    def _fit(self):
        n = self.n
        mu = self.sx / n
        ybar = self.sy / n
        cov = self.sxx / n - np.outer(mu, mu)
        cxy = self.sxy / n - mu * ybar
        sd = np.sqrt(np.clip(np.diag(cov), 0, None))
        keep = sd > 1e-12
        beta = np.zeros(self.k)
        if keep.any():
            d = sd[keep]
            cs = cov[np.ix_(keep, keep)] / np.outer(d, d)
            # Ridge in correlation units, strength shrinks as evidence grows.
            lam = self.ridge * 50.0 / math.sqrt(n)
            b_std = np.linalg.solve(cs + lam * np.eye(keep.sum()), cxy[keep] / d)
            beta[keep] = b_std / d
        self.beta = beta
        self.intercept = ybar - mu @ beta
        var_y = self.syy / n - ybar * ybar
        resid = var_y - 2 * beta @ cxy + beta @ cov @ beta
        self.sigma = math.sqrt(max(resid, 1e-12))
        self.since_fit = 0

    def ready(self):
        return self.beta is not None

    def predict(self, x):
        if self.beta is None:
            return None
        return float(self.intercept + np.asarray(x, dtype=float) @ self.beta)

    def score(self, prediction, realized):
        if prediction is not None:
            self.oos.append((prediction, realized))

    def skill(self):
        """Out-of-sample R^2 vs a zero-change forecast, and directional hit rate."""
        if len(self.oos) < 200:
            return {"n": len(self.oos), "r2_vs_zero": None, "direction_hit_rate": None}
        p = np.array([a for a, _ in self.oos])
        y = np.array([b for _, b in self.oos])
        sse = float(((y - p) ** 2).sum())
        sst = float((y**2).sum())
        moved = y != 0
        hits = float((np.sign(p[moved]) == np.sign(y[moved])).mean()) if moved.any() else None
        return {"n": len(self.oos), "r2_vs_zero": 1 - sse / sst if sst > 0 else None, "direction_hit_rate": hits}
