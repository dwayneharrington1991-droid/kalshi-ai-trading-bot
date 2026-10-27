"""Past-only candidate features, not assertions of predictive power."""
from datetime import datetime, timezone
import math
from statistics import median

from .model import number, timestamp

FEATURES = (
    "distance_scaled",
    "remaining_fraction",
    "return_1m",
    "return_5m",
    "return_15m",
    "acceleration",
    "body_fraction",
    "upper_wick",
    "lower_wick",
    "range_fraction",
    "breakout_distance",
    "volatility_change",
    "relative_volume",
    "cross_venue_divergence",
    "spot_depth_imbalance",
    "derivatives_basis",
    "open_interest_change",
    "public_macro_release_count",
    "spot_cvd_delta_fraction",
    "perpetual_cvd_delta_fraction",
    "spot_book_dominance",
    "spot_absorption_candidate",
    "perpetual_absorption_candidate",
    "spot_delta_efficiency_3m",
    "perpetual_delta_efficiency_3m",
)


def fresh(feed, now, limit):
    try:
        return (
            feed.get("status") == "CONNECTED"
            and 0 <= (now - timestamp(feed["data"]["observed_at"])).total_seconds() <= limit
        )
    except (KeyError, ValueError, TypeError):
        return False


def extract(payload, market, now):
    values = {name: None for name in FEATURES}
    diagnostics = []
    bars = []
    try:
        if not 0 <= (now - timestamp(payload["btc_candles"]["received_at"])).total_seconds() <= 150:
            raise ValueError("stale/future candle response")
        for raw in payload.get("btc_candles", {}).get("candles", []):
            start = int(raw["start"])
            if start + 60 > now.timestamp():
                continue
            row = [number(float(raw[k]), 0.000001, 1e12) for k in ("open", "high", "low", "close")]
            vol = number(float(raw["volume"]), 0, 1e15)
            if not row[2] <= min(row[0], row[3]) <= max(row[0], row[3]) <= row[1]:
                raise ValueError("OHLC bounds")
            bars.append((start, *row, vol))
        bars.sort()
        if len({b[0] for b in bars}) != len(bars):
            raise ValueError("duplicate candles")
        if not bars or now.timestamp() - bars[-1][0] - 60 > 150:
            raise ValueError("missing/stale closed candles")
        if any(b[0] - a[0] != 60 for a, b in zip(bars, bars[1:])):
            raise ValueError("missing candle interval")
        close = [b[4] for b in bars]
        for n, label in ((1, "return_1m"), (5, "return_5m"), (15, "return_15m")):
            if len(close) > n:
                values[label] = math.log(close[-1] / close[-n - 1])
        if len(close) > 2:
            values["acceleration"] = math.log(close[-1] / close[-2]) - math.log(
                close[-2] / close[-3]
            )
        _, o, h, l, c, v = bars[-1]
        scale = max(h - l, 1e-9)
        values.update(
            body_fraction=(c - o) / scale,
            upper_wick=(h - max(c, o)) / scale,
            lower_wick=(min(c, o) - l) / scale,
            range_fraction=(h - l) / o,
        )
        if len(bars) > 20:
            prior = bars[-21:-1]
            values["breakout_distance"] = (c - max(b[2] for b in prior)) / o
            meanv = sum(b[5] for b in prior) / len(prior)
            values["relative_volume"] = v / meanv if meanv else None
            returns = [math.log(b / a) for a, b in zip(close, close[1:])]
            old = sum(r * r for r in returns[-20:-5]) / 15
            values["volatility_change"] = (
                (sum(r * r for r in returns[-5:]) / 5) / old if old > 0 else None
            )
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        diagnostics.append("CANDLE_FEATURES_UNAVAILABLE: " + str(exc))
        bars = []
        for name in FEATURES[2:13]:
            values[name] = None
    ref = payload.get("reference", {})
    if market:
        try:
            remaining = (timestamp(market["closes_at"]) - now).total_seconds()
            price = number(ref["price"], 0.000001, 1e12)
            if not 0 <= (now - timestamp(ref["observed_at"])).total_seconds() <= 5:
                raise ValueError("stale reference")
            values["distance_scaled"] = (price - number(market["strike"], 0.000001, 1e12)) / price
            values["remaining_fraction"] = max(0, remaining) / 900
            feed = payload.get("coverage", {}).get("Kraken spot", {})
            if fresh(feed, now, 15):
                data = feed["data"]
                other = number(data["price"], 1, 1e12)
                if abs(other / price - 1) > 0.02:
                    diagnostics.append("CROSS_VENUE_OUTLIER: >2% divergence; excluded")
                else:
                    values["cross_venue_divergence"] = other / price - 1
                    values["spot_depth_imbalance"] = data.get("depth_imbalance")
        except (KeyError, ValueError, TypeError) as exc:
            diagnostics.append("REFERENCE_FEATURES_UNAVAILABLE: " + str(exc))
    derivative = payload.get("coverage", {}).get("Kraken derivatives", {})
    if fresh(derivative, now, 15):
        values["derivatives_basis"] = derivative["data"].get("basis_fraction")
        values["open_interest_change"] = derivative["data"].get("open_interest_change_fraction")
    news = payload.get("coverage", {}).get("Federal Reserve public releases", {})
    if fresh(news, now, 180):
        values["public_macro_release_count"] = sum(
            0 <= (now - timestamp(item["published_at"])).total_seconds() <= 3600
            for item in news["data"].get("items", [])
        )
    flow = payload.get("coverage", {}).get("Kraken public order flow", {}).get("data", {})
    for venue, feature in (
        ("spot", "spot_cvd_delta_fraction"),
        ("perpetual", "perpetual_cvd_delta_fraction"),
    ):
        item = flow.get(venue, {})
        try:
            if (
                item.get("status") == "LIVE"
                and 0 <= (now - timestamp(item["observed_at"])).total_seconds() <= 5
            ):
                values[feature] = number(item["delta_fraction_60s"], -1, 1)
                absorption = item.get("passive_sell_absorption_candidate")
                if type(absorption) is bool:
                    values[venue + "_absorption_candidate"] = int(absorption)
                dem = item.get("delta_efficiency_3m", {})
                if dem.get("status") == "LIVE" and dem.get("delta_efficiency") is not None:
                    values[venue + "_delta_efficiency_3m"] = number(
                        dem["delta_efficiency"], -1e15, 1e15
                    )
        except (KeyError, ValueError, TypeError):
            pass
    item = flow.get("spot_book", {})
    try:
        if (
            item.get("status") == "LIVE"
            and 0 <= (now - timestamp(item["observed_at"])).total_seconds() <= 5
        ):
            ratio = number(item["bid_ask_ratio"], 0, 1e12)
            values["spot_book_dominance"] = (ratio - 1) / (ratio + 1)
    except (KeyError, ValueError, TypeError):
        pass
    return {
        "values": values,
        "diagnostics": diagnostics,
        "independence": "No Kalshi price/odds in settlement features. Coinbase is primary; Kraken divergence is context, not a second independent vote.",
        "unavailable_features": {
            "trade_flow_imbalance": "Available only from fresh per-venue WebSocket trade messages after warm-up; never inferred from OHLCV",
            "cross_market_lead_lag": "No validated lead/lag estimator; receipt-time cross-venue divergence is context only",
            "derivatives_predictive_effect": "Funding/basis/OI are context until validated",
        },
        "closed_candles": [
            dict(start=b[0], open=b[1], high=b[2], low=b[3], close=b[4], volume=b[5])
            for b in bars[-60:]
        ],
    }


class DevelopingCandle:
    def __init__(self):
        self.bar = None

    def update(self, reference, now):
        try:
            at = timestamp(reference["observed_at"])
            price = number(reference["price"], 1, 1e12)
            if not 0 <= (now - at).total_seconds() <= 5:
                return None
            minute = int(at.timestamp()) // 60 * 60
            if self.bar is None or self.bar["start"] != minute:
                self.bar = {
                    "start": minute,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": None,
                    "status": "DEVELOPING",
                    "source": "Receipt-time proxy samples; volume unknown",
                }
            self.bar.update(
                high=max(self.bar["high"], price), low=min(self.bar["low"], price), close=price
            )
            return dict(self.bar)
        except (KeyError, ValueError, TypeError):
            return None
