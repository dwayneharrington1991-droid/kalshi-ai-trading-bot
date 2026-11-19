"""Isolated research: does a 30-second BTC direction signal beat simple baselines out of sample?

Research only. Nothing here is imported by the live service, places orders, or changes the
production model. A signal is reported as validated only when ALL of these hold on the final
chronological test block, which the fitting and threshold choice never saw:

  * test accuracy beats the best baseline by at least MIN_EDGE
  * the one-sided binomial test is significant at ALPHA
  * log loss beats the always-base-rate baseline
  * test set has at least MIN_TEST decisions
  * calibration error (ECE) stays below MAX_ECE

Otherwise the verdict is NO_VALIDATED_EDGE. The module never claims predictive power on its own.

Input: bars = [{"ts": epoch_seconds, "close": float, "volume": float}, ...] at 30-second spacing,
in time order. Features at index i use bars[:i+1] only. The label is the sign of the next bar's
close minus this bar's close, so every feature is available at prediction time.
"""
import math
import time

ALPHA = 0.05
MIN_EDGE = 0.02
MIN_TEST = 500
MAX_ECE = 0.05
EMA_FAST, EMA_SLOW = 9, 21


def _ema(values, span):
    k = 2.0 / (span + 1)
    out, e = [], values[0]
    for v in values:
        e = e + k * (v - e)
        out.append(e)
    return out


def features(bars):
    """Causal feature rows, one per bar index i (rows[i] uses bars[:i+1] only)."""
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    ema_f, ema_s = _ema(closes, EMA_FAST), _ema(closes, EMA_SLOW)
    rows = []
    for i in range(len(bars)):
        r1 = (closes[i] / closes[i - 1] - 1) if i >= 1 else 0.0
        r3 = (closes[i] / closes[i - 3] - 1) if i >= 3 else 0.0
        trend = (ema_f[i] - ema_s[i]) / closes[i] if closes[i] else 0.0
        window = vols[max(0, i - 19):i + 1]
        mean_v = sum(window) / len(window) if window else 0.0
        relv = (vols[i] / mean_v - 1) if mean_v > 0 else 0.0
        rows.append([r1 * 1e3, r3 * 1e3, trend * 1e3, relv])
    return rows


def labels(bars):
    """1 if the next bar closes higher, 0 if lower or equal, None on the last bar."""
    out = []
    for i in range(len(bars)):
        if i + 1 < len(bars):
            out.append(1 if bars[i + 1]["close"] > bars[i]["close"] else 0)
        else:
            out.append(None)
    return out


def _sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def fit_logistic(X, y, epochs=200, lr=0.1, l2=1e-3):
    """Plain logistic regression by gradient descent (standard library only)."""
    d = len(X[0])
    w, b = [0.0] * d, 0.0
    n = len(X)
    for _ in range(epochs):
        gw, gb = [0.0] * d, 0.0
        for xi, yi in zip(X, y):
            p = _sigmoid(b + sum(wj * xj for wj, xj in zip(w, xi)))
            err = p - yi
            for j in range(d):
                gw[j] += err * xi[j]
            gb += err
        w = [wj - lr * (gj / n + l2 * wj) for wj, gj in zip(w, gw)]
        b -= lr * gb / n
    return w, b


def predict(model, X):
    w, b = model
    return [_sigmoid(b + sum(wj * xj for wj, xj in zip(w, xi))) for xi in X]


def accuracy(p, y):
    return sum(1 for pi, yi in zip(p, y) if (pi >= 0.5) == (yi == 1)) / len(y)


def log_loss(p, y):
    eps = 1e-12
    return -sum(yi * math.log(min(max(pi, eps), 1 - eps)) + (1 - yi) * math.log(min(max(1 - pi, eps), 1 - eps))
                for pi, yi in zip(p, y)) / len(y)


def ece(p, y, bins=10):
    total, err = len(y), 0.0
    for k in range(bins):
        idx = [i for i, pi in enumerate(p) if k / bins <= pi < (k + 1) / bins or (k == bins - 1 and pi == 1.0)]
        if idx:
            conf = sum(p[i] for i in idx) / len(idx)
            acc = sum(y[i] for i in idx) / len(idx)
            err += len(idx) / total * abs(conf - acc)
    return err


def binomial_p_greater(k, n, p0=0.5):
    """One-sided P(X >= k) for X ~ Binomial(n, p0), exact, computed in log space."""
    if n == 0:
        return 1.0
    total = 0.0
    for j in range(k, n + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1)
                          + j * math.log(p0) + (n - j) * math.log(1 - p0))
    return min(1.0, total)


def run(bars, train_frac=0.6, val_frac=0.2):
    """Chronological evaluation. Returns a dict; 'verdict' is the only decision field."""
    t0 = time.perf_counter()
    X_all = features(bars)
    y_all = labels(bars)
    usable = [i for i in range(len(bars)) if y_all[i] is not None]
    n = len(usable)
    a, c = int(n * train_frac), int(n * (train_frac + val_frac))
    tr, va, te = usable[:a], usable[a:c], usable[c:]
    if len(te) < MIN_TEST or len(tr) < 100:
        return {"verdict": "NO_VALIDATED_EDGE", "reason": "insufficient data for a chronological test block",
                "test_n": len(te)}
    X = lambda idx: [X_all[i] for i in idx]
    Y = lambda idx: [y_all[i] for i in idx]
    model = fit_logistic(X(tr), Y(tr))
    p_va = predict(model, X(va))
    p_te = predict(model, X(te))
    base_rate = sum(Y(tr)) / len(tr)
    up_rate = sum(Y(te)) / len(te)
    # persistence: predict the direction of the last 30-second return (no prediction when flat)
    persist = [(1 if X_all[i][0] > 0 else 0) for i in te if X_all[i][0] != 0]
    persist_y = [y_all[i] for i in te if X_all[i][0] != 0]
    persist_acc = (sum(1 for pi, yi in zip(persist, persist_y) if pi == yi) / len(persist)) if persist else 0.0
    baseline_acc = max(up_rate, 1 - up_rate, persist_acc)
    acc = accuracy(p_te, Y(te))
    correct = round(acc * len(te))
    p_value = binomial_p_greater(correct, len(te))
    ll_model = log_loss(p_te, Y(te))
    ll_base = log_loss([base_rate] * len(te), Y(te))
    cal = ece(p_te, Y(te))
    edge = acc - baseline_acc
    validated = (edge >= MIN_EDGE and p_value < ALPHA and ll_model < ll_base and cal <= MAX_ECE
                 and len(te) >= MIN_TEST)
    return {
        "verdict": "VALIDATED_CANDIDATE_NEEDS_LIVE_SHADOW" if validated else "NO_VALIDATED_EDGE",
        "test_n": len(te), "validation_n": len(va), "train_n": len(tr),
        "test_accuracy": round(acc, 4), "best_baseline_accuracy": round(baseline_acc, 4),
        "edge_over_baseline": round(edge, 4), "p_value": round(p_value, 6),
        "log_loss": round(ll_model, 5), "log_loss_base_rate": round(ll_base, 5),
        "ece": round(cal, 5), "validation_log_loss": round(log_loss(p_va, Y(va)), 5),
        "compute_seconds": round(time.perf_counter() - t0, 3),
    }
