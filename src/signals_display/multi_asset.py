"""Independent 15-minute market strip with immutable per-asset openings."""
from datetime import timedelta
import math
import re
from statistics import pstdev

from .model import number, timestamp

ASSETS = {
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto", "XRP": "crypto",
    "DOGE": "crypto", "GOLD": "metal", "SILVER": "metal",
}
ALIASES = {
    "BTC": ("BTC", "BITCOIN"), "ETH": ("ETH", "ETHEREUM"),
    "SOL": ("SOL", "SOLANA"), "XRP": ("XRP",), "DOGE": ("DOGE", "DOGECOIN"),
    "GOLD": ("GOLD", "XAU"), "SILVER": ("SILVER", "XAG"),
}


def _time(value):
    return value if hasattr(value, "tzinfo") else timestamp(value)


def asset_of(market):
    explicit = market.get("asset")
    if explicit in ASSETS:
        return explicit
    text = " ".join(str(market.get(k, "")) for k in (
        "ticker", "series", "series_ticker", "title", "subtitle"
    )).upper()
    matches = [
        asset for asset, words in ALIASES.items()
        if any(
            re.search(r"\b" + re.escape(word) + r"\b", text)
            or re.search(r"\bKX" + re.escape(word) + r"15M(?:\b|-)", text)
            for word in words
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _history(history, opened):
    if not isinstance(history, dict):
        raise ValueError("24-hour reference history unavailable")
    if _time(history["received_at"]) > opened:
        raise ValueError("history arrived after opening")
    cutoff = int(opened.timestamp()) // 60 * 60
    rows = {}
    for raw in history.get("candles", []):
        start = int(raw["start"])
        if start + 60 <= cutoff:
            rows[start] = number(float(raw["close"]), 0.000001, 1e15)
    expected = list(range(cutoff - 86400, cutoff, 60))
    if sorted(rows) != expected:
        raise ValueError(f"incomplete opening history: {len(rows)}/1440 closed bars")
    closes = [rows[t] for t in expected]
    sigma = pstdev([math.log(b / a) for a, b in zip(closes, closes[1:])])
    if not sigma > 0:
        raise ValueError("historical variation unavailable")
    return closes, sigma


def opening_forecast(market, reference, history, generated_at):
    opened, closed, generated = map(_time, (
        market["opens_at"], market["closes_at"], generated_at
    ))
    base = {
        "ticker": market.get("ticker"), "asset": asset_of(market),
        "opens_at": opened.isoformat(), "closes_at": closed.isoformat(),
        "generated_at": generated.isoformat(), "data_cutoff": opened.isoformat(),
        "status": "UNAVAILABLE", "direction": "UNAVAILABLE", "up": None, "down": None,
        "experimental": True, "immutable": True,
    }
    try:
        if base["asset"] is None:
            raise ValueError("asset identity is not verified")
        if not 60 <= (closed - opened).total_seconds() <= 900:
            raise ValueError("not a compatible 15-minute contract")
        if market.get("mapping_verified") is not True:
            raise ValueError("YES-to-UP settlement mapping is not verified")
        if not (
            market.get("settlement_rules_verified") is True
            or market.get("averaging_rule_verified") is True
        ):
            raise ValueError("settlement reference and averaging rule are not verified")
        strike = number(market["strike"], 0.000001, 1e15)
        if not isinstance(reference, dict) or not reference.get("observed_at"):
            raise ValueError("independent opening reference unavailable")
        observed = _time(reference["observed_at"])
        if observed > opened or opened - observed > timedelta(seconds=5):
            raise ValueError("fresh independent opening reference unavailable")
        price = number(reference["price"], 0.000001, 1e15)
        closes, sigma = _history(history, opened)
        short_return = closes[-1] / closes[-16] - 1
        hour_return = closes[-1] / closes[-61] - 1
        day_return = closes[-1] / closes[0] - 1
        z = math.log(strike / price) / (sigma * math.sqrt(max(1, (closed - opened).total_seconds() / 60)))
        up = 0.5 * math.erfc(z / math.sqrt(2))
        direction = "UP" if up > 0.5 else "DOWN" if up < 0.5 else "UNCLEAR"
        return {**base, "status": "EXPERIMENTAL", "direction": direction,
                "up": up, "down": 1-up, "threshold": strike,
                "reference_price": price, "reference_source": reference.get("source"),
                "history_samples": len(closes),
                "active_model": "asset-specific-opening-zero-drift-v1",
                "feature_trace": {
                    "volatility_24h": sigma,
                    "return_15m": short_return,
                    "return_60m": hour_return,
                    "return_24h": day_return,
                    "active_probability_inputs": ["opening_reference", "official_threshold", "volatility_24h"],
                    "shadow_only_inputs": ["return_15m", "return_60m", "return_24h"],
                },
                "reason": "Asset-specific opening baseline; unvalidated and edge unverified."}
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        return {**base, "reason": str(exc)}


class MarketStrip:
    def __init__(self):
        self.openings = {}

    def update(self, markets, references, histories, now):
        now, active = _time(now), {}
        for market in markets or []:
            try:
                opened, closed = _time(market["opens_at"]), _time(market["closes_at"])
                asset = asset_of(market)
                if not asset or not opened <= now < closed or not 60 <= (closed-opened).total_seconds() <= 900:
                    continue
                ticker = market["ticker"]
                self.openings.setdefault(ticker, opening_forecast(
                    market, (references or {}).get(asset, {}),
                    (histories or {}).get(asset, {}), now
                ))
                active[asset] = {**self.openings[ticker],
                                 "remaining_seconds": max(0, (closed-now).total_seconds())}
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
        return [active[a] for a in sorted(active)]
