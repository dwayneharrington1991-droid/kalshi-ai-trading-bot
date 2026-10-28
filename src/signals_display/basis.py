"""Explicit opening-reference anchoring; never silently equate a proxy to CF."""
from datetime import timedelta

from .model import number, timestamp


def estimate_basis(payload, market, history, now):
    ref = payload.get("reference", {})
    if ref.get("official") is True and ref.get("source") == market.get("settlement_reference"):
        return {
            "method": "official matching index",
            "point": 0.0,
            "low": 0.0,
            "high": 0.0,
            "future_basis_uncertainty": "matching official index, model error still unvalidated",
        }, None
    opened = timestamp(market["opens_at"])
    start = opened - timedelta(seconds=60)
    strike = number(market["strike"], 0.000001, 1e12)
    # Prefer actual observed proxy prices spanning the official opening-average window.
    points = [(t, p) for t, p in history if start - timedelta(seconds=5) <= t <= opened]
    if points and points[0][0] <= start and (opened - points[-1][0]).total_seconds() <= 5:
        points = points + [(opened, points[-1][1])]
        if all(0 <= (b[0] - a[0]).total_seconds() <= 5 for a, b in zip(points, points[1:])):
            area = 0
            for (a, pa), (b, pb) in zip(points, points[1:]):
                left = max(a, start)
                if b <= left:
                    continue
                pleft = pa + (pb - pa) * (left - a).total_seconds() / (b - a).total_seconds()
                area += (pleft + pb) / 2 * (b - left).total_seconds()
            anchor = area / 60
            lo = min(p for _, p in points)
            hi = max(p for _, p in points)
            return {
                "method": "observed opening-window proxy average, trapezoidal interpolation",
                "point": strike - anchor,
                "low": strike - hi,
                "high": strike - lo,
                "proxy_opening_average": anchor,
                "opening_window_end": opened.isoformat(),
                "future_basis_uncertainty": "Unmeasured: assumes opening proxy/index basis persists; range is sensitivity, not a confidence interval",
            }, None
    # A restart/reconnect can leave fewer than sixty seconds of receipt-proven
    # proxy ticks even though a genuinely fresh pre-opening receipt and the
    # verified official threshold are available. Anchor the observed
    # proxy-to-official difference at that instant; never assume zero basis and
    # never describe this as an official-index observation.
    if points and (opened - points[-1][0]).total_seconds() <= 5:
        proxy = points[-1][1]
        strike = number(market["strike"], 0.000001, 1e12)
        point = strike - proxy
        return {
            "method": "single fresh pre-opening proxy receipt anchored to official threshold",
            "point": point,
            "low": point,
            "high": point,
            "proxy_opening_average": proxy,
            "opening_window_end": opened.isoformat(),
            "coverage_status": "REDUCED: full 60-second proxy window unavailable",
            "future_basis_uncertainty": "Unmeasured and potentially material: the official threshold is known, but one proxy receipt cannot measure the official 60-second path or future basis drift. Low/high are the same point estimate, not uncertainty bounds.",
        }, None
    data = payload.get("btc_candles", {})
    try:
        if not 0 <= (now - timestamp(data["received_at"])).total_seconds() <= 150:
            raise ValueError("opening candle response stale/future-dated")
        for bar in data["candles"]:
            if int(bar["start"]) != int(start.timestamp()):
                continue
            if int(bar["start"]) + 60 > now.timestamp():
                continue
            o, h, l, c = [
                number(float(bar[k]), 0.000001, 1e12) for k in ("open", "high", "low", "close")
            ]
            if not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError("invalid opening candle")
            anchor = (o + h + l + c) / 4
            return {
                "method": "opening-minute OHLC4 proxy approximation, NOT a measured time average",
                "point": strike - anchor,
                "low": strike - h,
                "high": strike - l,
                "proxy_opening_average": anchor,
                "opening_window_end": opened.isoformat(),
                "future_basis_uncertainty": "Unmeasured: assumes constant basis. Low/high sensitivity uses opening-minute trade range, not a bound on future official-index basis.",
            }, None
    except (KeyError, ValueError, TypeError) as exc:
        return {}, "MISSING_BASIS_ANCHOR: " + str(exc)
    return (
        {},
        "MISSING_BASIS_ANCHOR: no matching official index or opening-minute proxy observations/candle; zero basis is not assumed",
    )
