"""Integrate bounded advanced descriptors without inventing forecast influence.

New features are journaled before outcomes for later chronological ablation.
They affect a classifier only if an explicitly loaded, approved artifact names
them. Technical readings themselves never change a probability or unlock entry.
"""
from collections import deque
from copy import deepcopy
from time import perf_counter

from .advanced_flow import AdvancedFlow
from .advanced_indicators import technical_snapshot
from .calibration import drift_report
from .model import timestamp


def _family(reason):
    return dict(
        status="UNAVAILABLE",
        reading=None,
        interpretation="UNAVAILABLE",
        as_of=None,
        age_seconds=None,
        contributing=False,
        reason=reason,
    )


def artifact_features(artifact):
    """Only actual serialized inputs count as contributing; not a family vote."""
    if not isinstance(artifact, dict):
        return set()
    model = artifact.get("model", artifact)
    result = set(model.get("preprocessor", {}).get("names", []))
    weights = model.get("mixture_weights")
    for index, child in enumerate(model.get("components", [])):
        if weights is None or weights[index] > 0:
            result.update(artifact_features(child))
    return result


class AdvancedIntelligence:
    def __init__(self):
        self.flow = AdvancedFlow()
        self.technical_key = None
        self.technical = None
        self.technical_cutoff = None
        self.feature_history = deque(maxlen=360)
        self.last_history_minute = None
        self.drift = {
            "status": "WARMING_UP",
            "reason": "Need 120 reference and 60 later minute snapshots; distribution drift is not forecast skill",
        }

    def observe(self, observation):
        return self.flow.observe(observation)

    def summarize(self, now, *, candles=None, primary_source=None, threshold=None):
        started = perf_counter()
        flow = self.flow.summarize(now, threshold=threshold, primary_source=primary_source)
        spot_sources = ("Coinbase BTC/USD stream", "Kraken BTC/USD stream")
        available = flow.get("sources", {})
        primary = (
            primary_source
            if primary_source in spot_sources
            and available.get(primary_source, {}).get("status") == "READY"
            else next(
                (name for name in spot_sources if available.get(name, {}).get("status") == "READY"),
                None,
            )
        )
        if primary is None:
            primary = next((name for name in spot_sources if name in available), None)
        source = flow.get("sources", {}).get(primary, {})
        short_bars = self.flow.closed_bars(primary, now) if primary else None
        # Snapshot cache keys include the requested cutoff. A historical opening
        # must never reuse a live technical calculation from after that opening.
        history = candles or {}
        rows = history.get("candles", [])
        short_rows = (
            (short_bars or {}).get("candles", [])
            if isinstance(short_bars, dict)
            else short_bars or []
        )
        key = (
            int(now.timestamp()) // 30,
            primary,
            history.get("received_at"),
            rows[-1].get("start") if rows else None,
            len(rows),
            short_rows[-1].get("start") if short_rows else None,
            len(short_rows),
        )
        cached_frames = (self.technical or {}).get("timeframes", {})
        expired = any(
            frame.get("last_closed_at")
            and (now - timestamp(frame["last_closed_at"])).total_seconds() > 120
            for name, frame in cached_frames.items()
            if name in {"30s", "1m"}
        )
        if (
            key != self.technical_key
            or self.technical_cutoff is None
            or now < self.technical_cutoff
            or expired
        ):
            self.technical = technical_snapshot(history, now, short_bars=short_bars)
            self.technical_key = key
            self.technical_cutoff = now
        technical = deepcopy(self.technical)
        for frame in technical.get("timeframes", {}).values():
            if frame.get("last_closed_at"):
                age = max(0, (now - timestamp(frame["last_closed_at"])).total_seconds())
                frame["data_age_seconds"] = age
                for name in ("rsi", "bollinger", "ema"):
                    frame[name]["age_seconds"] = age
                for variant in frame["macd"].values():
                    variant["age_seconds"] = age
        families = dict(technical.get("families", {}))
        for family in families.values():
            if family.get("as_of"):
                family["age_seconds"] = max(0, (now - timestamp(family["as_of"])).total_seconds())
        for name in ("vwap", "volume_profile", "order_flow", "order_book", "cvd", "microstructure"):
            raw = source.get(name)
            if isinstance(raw, dict):
                families[name] = {
                    **_family("Actual source data required"),
                    **raw,
                    "source": primary,
                    "contributing": False,
                }
            else:
                families[name] = _family(
                    "No compatible timestamped exchange trades/order book received"
                )
        values = dict(technical.get("candidate_features", {}))
        values.update(source.get("candidate_features", {}))
        receipts = dict(technical.get("candidate_feature_sources", {}))
        receipts.update(source.get("candidate_feature_sources", {}))
        # Defensive filter at the engine boundary, independently of adapters.
        for name in list(values):
            if values[name] is None:
                continue
            proof = receipts.get(name, {})
            try:
                if timestamp(proof["received_at"]) > now or (
                    proof.get("event_at") and timestamp(proof["event_at"]) > now
                ):
                    raise ValueError("Future input")
            except (KeyError, ValueError, TypeError, AttributeError):
                values[name] = None
                receipts.pop(name, None)
        families["ensemble"] = _family(
            "Model configuration will be attached after the forecast calculation"
        )
        families["calibration"] = _family(
            "Open Details to load official-outcome reliability metrics in the background"
        )
        return dict(
            status="EXPERIMENTAL_SHADOW",
            generated_at=now.isoformat(),
            cutoff=now.isoformat(),
            primary_source=primary,
            families=families,
            candidate_features=values,
            candidate_feature_sources=receipts,
            sources=flow.get("sources", {}),
            max_input_received_at=max(
                (p["received_at"] for n, p in receipts.items() if values.get(n) is not None),
                default=None,
            ),
            strongest_validated_factors=[],
            technical_warmup=technical.get("warmup_policy"),
            calculation_ms=round((perf_counter() - started) * 1000, 3),
            validation="New descriptors are shadow inputs. No validated incremental improvement or profitable edge demonstrated.",
        )

    def decorate(self, advanced, model, now, *, artifact=None, probability=None):
        result = {**advanced, "families": deepcopy(advanced["families"])}
        active = artifact_features(artifact)
        names = {
            "rsi": "wilder_rsi",
            "macd": "macd_",
            "bollinger": "bollinger_",
            "ema": "_ema",
            "vwap": "vwap",
            "volume_profile": "profile",
            "order_flow": "flow",
            "order_book": "book",
            "cvd": "cvd",
            "microstructure": "effective_spread",
        }
        for family, part in names.items():
            used = sorted(
                n
                for n in active
                if n.startswith("advanced_")
                and part in n
                and advanced["candidate_features"].get(n) is not None
                and result["families"][family].get("status") not in {"STALE", "UNAVAILABLE"}
            )
            result["families"][family]["contributing"] = bool(used)
            result["families"][family]["active_features"] = used
        minute = int(now.timestamp()) // 60
        if self.last_history_minute is None or minute > self.last_history_minute:
            self.last_history_minute = minute
            self.feature_history.append(
                {**advanced["candidate_features"], "probability": probability}
            )
            if len(self.feature_history) >= 180:
                rows = list(self.feature_history)
                self.drift = drift_report(rows[:120], rows[-60:])
                self.drift[
                    "sampling"
                ] = "Source-specific minute snapshots during this process; reference first 120, latest 60; not independent outcome validation"
        learning = model.get("learning_status", {})
        result["families"]["ensemble"] = dict(
            status="EXPERIMENTAL",
            reading={
                "active": learning.get("active", "experimental statistical fallback"),
                "model_id": learning.get("model_id"),
                "artifact": learning.get("artifact_status"),
                "official_contracts": learning.get("official_contracts"),
                "minimum_contracts": learning.get("minimum_contracts", 170),
                "drift": self.drift,
            },
            interpretation="UNVALIDATED",
            as_of=now.isoformat(),
            age_seconds=0,
            contributing=bool(artifact),
            reason="Existing selected model and statistical fallback are kept separate. New feature families require chronological ablation; no automatic model promotion.",
        )
        return result


def refresh_status(advanced, now):
    """Age cached diagnostics without recalculating indicators inside HTTP."""
    result = {**advanced, "families": {}}
    for name, family in advanced.get("families", {}).items():
        current = dict(family)
        if name != "calibration" and current.get("as_of"):
            age = (now - timestamp(current["as_of"])).total_seconds()
            current["age_seconds"] = age
            limit = 120 if name in {"rsi", "macd", "bollinger", "ema"} else 5
            if not 0 <= age <= limit:
                current.update(
                    status="STALE",
                    contributing=False,
                    reason="Stored reading is stale; fresh compatible inputs required",
                )
        result["families"][name] = current
    return result


def attach_reliability(advanced, cached):
    """Attach already calculated official scores; no full journal read in HTTP."""
    result = {**advanced, "families": deepcopy(advanced.get("families", {}))}
    metrics = (
        (cached.get("value") or {}).get("by_mode", {}).get("PUBLIC MARKET DATA", {}).get("all", {})
    )
    # Engine reports use `metrics` in some older versions; keep both shapes.
    if not metrics:
        report = cached.get("value") or {}
        metrics = report.get("metrics", report).get("PUBLIC MARKET DATA", {}).get("all", {})
    initial, latest = metrics.get("initial", {}), metrics.get("latest", {})
    result["families"]["calibration"] = dict(
        status="EXPERIMENTAL" if latest.get("samples") else cached.get("status", "UNAVAILABLE"),
        reading={
            "opening": {
                k: initial.get(k)
                for k in (
                    "samples",
                    "brier",
                    "log_loss",
                    "expected_calibration_error",
                    "calibration",
                    "by_volatility_regime",
                )
            },
            "live": {
                k: latest.get(k)
                for k in (
                    "samples",
                    "brier",
                    "log_loss",
                    "expected_calibration_error",
                    "calibration",
                    "by_volatility_regime",
                )
            },
            "methods": "Isotonic PAVA / monotone Platt / identity compared on chronological shadow splits; no calibration automatically activated",
        },
        interpretation="UNCALIBRATED",
        as_of=cached.get("generated_at"),
        age_seconds=None,
        contributing=False,
        reason="Official settlement labels only. Reliability bins and intervals are descriptive; insufficient evidence of calibration or profitable edge.",
    )
    return result
