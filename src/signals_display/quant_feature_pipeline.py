"""As-of feature engineering for 15-minute binary prediction markets.

This module performs no network or account operations.  Inputs are timestamped
data frames supplied by an ingestion layer.  All joins are backward/as-of joins;
future candles, books and contract observations are never borrowed.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


OHLCV = ("timestamp", "open", "high", "low", "close", "volume")


def _required(frame: pd.DataFrame, names: Iterable[str]) -> None:
    missing = set(names) - set(frame.columns)
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(sorted(missing)))


def _utc(frame: pd.DataFrame, column: str = "timestamp") -> pd.DataFrame:
    result = frame.copy()
    result[column] = pd.to_datetime(result[column], utc=True, errors="raise")
    if result[column].duplicated().any() or not result[column].is_monotonic_increasing:
        raise ValueError(f"{column} must be unique and chronological")
    return result


def aggregate_ohlcv(
    raw: pd.DataFrame, *, minutes: int = 15, source_minutes: int = 1,
    as_of: pd.Timestamp | str | None = None,
) -> pd.DataFrame:
    """Aggregate closed source candles into closed ``minutes`` OHLCV blocks.

    ``timestamp`` is the source candle's opening time.  A source row is eligible
    only after ``timestamp + source_minutes`` and an aggregate is emitted only
    after the entire aligned target block has closed.  This prevents incomplete
    candles from leaking into historical training rows.
    """
    if not 1 <= source_minutes <= minutes or minutes % source_minutes:
        raise ValueError("minutes must be a positive multiple of source_minutes")
    _required(raw, OHLCV)
    data = _utc(raw)
    for name in OHLCV[1:]:
        data[name] = pd.to_numeric(data[name], errors="raise")
    if (data[["open", "high", "low", "close"]] <= 0).any().any() or (data["volume"] < 0).any():
        raise ValueError("OHLC prices must be positive and volume nonnegative")
    if ((data["low"] > data[["open", "close"]].min(axis=1)) |
            (data["high"] < data[["open", "close"]].max(axis=1))).any():
        raise ValueError("OHLC bounds are incompatible")
    cutoff = pd.Timestamp.now(tz="UTC") if as_of is None else pd.to_datetime(as_of, utc=True)
    data = data[data["timestamp"] + pd.Timedelta(minutes=source_minutes) <= cutoff]
    indexed = data.set_index("timestamp")
    rule = f"{minutes}min"
    grouped = indexed.resample(rule, origin="epoch", label="left", closed="left")
    result = grouped.agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
        source_rows=("close", "count"),
    ).dropna(subset=["open", "high", "low", "close"])
    result = result[result["source_rows"] == minutes // source_minutes].reset_index()
    result["closed_at"] = result["timestamp"] + pd.Timedelta(minutes=minutes)
    return result[result["closed_at"] <= cutoff].reset_index(drop=True)


def candle_features(candles: pd.DataFrame, *, rolling_periods: int = 20) -> pd.DataFrame:
    """Tokenize chronological 15-minute OHLCV candles.

    Body/range = ``abs(close-open)/(high-low)``.  Range position =
    ``(close-low)/(high-low)``.  Volume-weighted move multiplies the candle
    return by current volume divided by the *prior* 20-candle mean volume.
    ATR z-score compares current true range with the prior 20 true ranges.
    """
    if rolling_periods < 5:
        raise ValueError("rolling_periods must be at least five")
    _required(candles, OHLCV)
    result = _utc(candles)
    for name in OHLCV[1:]:
        result[name] = pd.to_numeric(result[name], errors="raise")
    span = result["high"] - result["low"]
    safe_span = span.where(span > 0)
    result["body_to_wick_ratio"] = (result["close"] - result["open"]).abs() / safe_span
    result["position_inside_range"] = (result["close"] - result["low"]) / safe_span
    result["candle_return"] = result["close"] / result["open"] - 1
    prior_volume_mean = result["volume"].shift(1).rolling(rolling_periods).mean()
    result["volume_ratio_20"] = result["volume"] / prior_volume_mean.where(prior_volume_mean > 0)
    result["volume_weighted_move"] = result["candle_return"] * result["volume_ratio_20"]
    previous_close = result["close"].shift(1)
    result["true_range"] = pd.concat(
        [span, (result["high"] - previous_close).abs(), (result["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    prior_tr = result["true_range"].shift(1).rolling(rolling_periods)
    result["atr_20"] = prior_tr.mean()
    tr_std = prior_tr.std(ddof=0)
    result["atr_zscore_20"] = (result["true_range"] - result["atr_20"]) / tr_std.where(tr_std > 0)
    result["unusual_expansion_bar"] = result["atr_zscore_20"] > 1.5
    return result


def order_book_features(books: pd.DataFrame, *, levels: int = 3) -> pd.DataFrame:
    """Calculate top-N OBI and standard micro-price from book snapshots.

    OBI uses the sum of sizes at the top ``levels``.  Micro-price uses best bid
    and ask with those aggregate sizes:
    ``(bid*ask_size + ask*bid_size)/(bid_size+ask_size)``.
    """
    if not 1 <= levels <= 20:
        raise ValueError("levels must be 1..20")
    columns = ["timestamp"] + [
        f"{side}_{field}_{level}"
        for side in ("bid", "ask") for field in ("price", "size")
        for level in range(1, levels + 1)
    ]
    _required(books, columns)
    result = _utc(books)
    numeric = columns[1:]
    result[numeric] = result[numeric].apply(pd.to_numeric, errors="raise")
    if (result[numeric] < 0).any().any():
        raise ValueError("Book prices/sizes cannot be negative")
    bid_size = result[[f"bid_size_{i}" for i in range(1, levels + 1)]].sum(axis=1)
    ask_size = result[[f"ask_size_{i}" for i in range(1, levels + 1)]].sum(axis=1)
    total = bid_size + ask_size
    bid, ask = result["bid_price_1"], result["ask_price_1"]
    if (bid <= 0).any() or (ask <= bid).any():
        raise ValueError("Best prices must be positive and uncrossed")
    result[f"bid_size_top_{levels}"] = bid_size
    result[f"ask_size_top_{levels}"] = ask_size
    result[f"obi_top_{levels}"] = (bid_size - ask_size) / total.where(total > 0)
    result["micro_price"] = (bid * ask_size + ask * bid_size) / total.where(total > 0)
    midpoint = (bid + ask) / 2
    result["micro_price_deviation"] = (result["micro_price"] - midpoint) / midpoint
    result["spread_fraction"] = (ask - bid) / midpoint
    return result


def vig_stripped_probability(yes_mid: pd.Series, no_mid: pd.Series) -> pd.Series:
    """Normalize complementary midpoints; this removes quote overround, not fees."""
    total = yes_mid + no_mid
    return yes_mid / total.where(total > 0)


def _logit(values: pd.Series, epsilon: float = 1e-6) -> pd.Series:
    clipped = values.clip(epsilon, 1 - epsilon)
    return np.log(clipped / (1 - clipped))


def contract_features(
    contracts: pd.DataFrame, *, total_minutes: float = 15,
    exponential_rate: float = 3.0, fee_rate: float = 0.07,
) -> pd.DataFrame:
    """Add binary-contract probability, cost, theta and boundary features.

    Vig stripping normalizes YES/NO midpoints.  Break-even probability uses the
    executable ask plus the one-contract quadratic taker fee and explicit
    slippage; it is a cost threshold, not a claim about the true event chance.
    """
    required = (
        "timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask",
        "minutes_remaining", "fee_multiplier", "slippage",
    )
    _required(contracts, required)
    result = _utc(contracts)
    names = required[1:]
    result[list(names)] = result[list(names)].apply(pd.to_numeric, errors="raise")
    for side in ("yes", "no"):
        if ((result[f"{side}_bid"] < 0) | (result[f"{side}_ask"] > 1) |
                (result[f"{side}_bid"] > result[f"{side}_ask"])).any():
            raise ValueError("Binary quotes must satisfy 0 <= bid <= ask <= 1")
        result[f"{side}_mid"] = (result[f"{side}_bid"] + result[f"{side}_ask"]) / 2
    result["vig_stripped_yes_probability"] = vig_stripped_probability(
        result["yes_mid"], result["no_mid"]
    )
    result["vig_stripped_no_probability"] = 1 - result["vig_stripped_yes_probability"]
    for side in ("yes", "no"):
        price = result[f"{side}_ask"]
        raw_fee = fee_rate * result["fee_multiplier"] * price * (1 - price)
        result[f"{side}_entry_fee"] = np.ceil((raw_fee - 1e-12) * 100) / 100
        result[f"{side}_break_even_probability"] = (
            price + result[f"{side}_entry_fee"] + result["slippage"]
        ).clip(0, 1)
        result[f"{side}_logit"] = _logit(result[f"{side}_mid"])
        result[f"{side}_boundary_sensitivity"] = 1 / (
            result[f"{side}_mid"].clip(1e-6, 1 - 1e-6)
            * (1 - result[f"{side}_mid"].clip(1e-6, 1 - 1e-6))
        )
    fraction = (result["minutes_remaining"] / total_minutes).clip(0, 1)
    result["time_remaining_fraction"] = fraction
    result["theta_decay_linear"] = fraction
    result["theta_decay_exponential"] = np.exp(-exponential_rate * (1 - fraction))
    result["theta_urgency"] = 1 - result["theta_decay_exponential"]
    return result


def build_feature_frame(
    raw_candles: pd.DataFrame, *, order_books: pd.DataFrame | None = None,
    contracts: pd.DataFrame | None = None, as_of: pd.Timestamp | str | None = None,
) -> pd.DataFrame:
    """Build one chronological 15-minute frame with backward-only joins."""
    candles = candle_features(aggregate_ohlcv(raw_candles, as_of=as_of))
    result = candles.copy()
    if order_books is not None:
        books = order_book_features(order_books)
        result = pd.merge_asof(
            result.sort_values("closed_at"), books.sort_values("timestamp"),
            left_on="closed_at", right_on="timestamp", direction="backward",
            suffixes=("", "_book"),
        )
    if contracts is not None:
        contract = contract_features(contracts)
        result = pd.merge_asof(
            result.sort_values("closed_at"), contract.sort_values("timestamp"),
            left_on="closed_at", right_on="timestamp", direction="backward",
            suffixes=("", "_contract"),
        )
    return result
