"""Unvalidated, timestamp-conditioned models; no network, credentials, or execution.

Settlement: zero-drift dollar Brownian proxy, opening-window anchored basis, final
60-second arithmetic averaging. Scalp: zero-drift logit quote diffusion with
frozen spreads/fees. These assumptions are not calibrated facts.
"""
from collections import deque
from datetime import timedelta
import hashlib
import math
import random

from .model import Display, fee, number, timestamp
from .candles import candle_context
from .basis import estimate_basis
from .features import extract, DevelopingCandle
from .prediction_engine import context24
from .momentum_confirmation import describe as adaptive_momentum

VERSION = "independent-basis-forecast-v3"
MIN_SAMPLES = 61
MIN_SECONDS = 120
MAX_GAP = 5
PATHS = 2000


def binary_contract_context(market, remaining, slippage):
    """Cost and boundary diagnostics; never an independent event forecast."""
    quotes = market["quotes"]
    multiplier = number(market["fee_multiplier"], 0, 100)
    mids = {}
    result = {}
    for side in ("up", "down"):
        bid = number(quotes[side]["bid"], 0, 1)
        ask = number(quotes[side]["ask"], bid, 1)
        mid = max(1e-6, min(1 - 1e-6, (bid + ask) / 2))
        mids[side] = mid
        result[side] = {
            "midpoint": mid,
            "spread": ask - bid,
            "entry_fee": fee(ask, multiplier),
            "break_even_probability": min(1, ask + fee(ask, multiplier) + slippage),
            "logit": math.log(mid / (1 - mid)),
            "boundary_sensitivity": 1 / (mid * (1 - mid)),
        }
    total = mids["up"] + mids["down"]
    fraction = max(0, min(1, remaining / 900))
    return {
        "status": "EXPERIMENTAL_COST_AND_MARKET_CONTEXT",
        "vig_stripped_up": mids["up"] / total if total else None,
        "vig_stripped_down": mids["down"] / total if total else None,
        "time_remaining_fraction": fraction,
        "theta_decay_linear": fraction,
        "theta_decay_exponential": math.exp(-3 * (1 - fraction)),
        "sides": result,
        "role": "Market-implied/cost context only; excluded from the independent settlement probability features.",
    }


def volatility(points, now, kind):
    points = [(t, p) for t, p in points if 0 <= (now - t).total_seconds() <= 600]
    count = len(points)
    span = (points[-1][0] - points[0][0]).total_seconds() if count > 1 else 0
    detail = {
        "samples": count,
        "span_seconds": round(span, 2),
        "minimum_samples": MIN_SAMPLES,
        "minimum_span_seconds": MIN_SECONDS,
    }
    detail["first_observed_at"] = points[0][0].isoformat() if points else None
    detail["last_observed_at"] = points[-1][0].isoformat() if points else None
    if count < MIN_SAMPLES or span < MIN_SECONDS:
        return (
            None,
            f"INSUFFICIENT_HISTORY: {kind} {count}/{MIN_SAMPLES} samples, {span:.0f}/{MIN_SECONDS}s span",
            detail,
        )
    if not 0 <= (now - points[-1][0]).total_seconds() <= MAX_GAP:
        return None, f"STALE_DATA: latest {kind} sample is older than {MAX_GAP}s", detail
    intervals = [(b[0] - a[0]).total_seconds() for a, b in zip(points, points[1:])]
    if any(not 0 < dt <= MAX_GAP for dt in intervals):
        return (
            None,
            f"DATA_GAP: {kind} history has a gap over {MAX_GAP}s; wait for a contiguous window",
            detail,
        )
    variance = sum((b[1] - a[1]) ** 2 for a, b in zip(points, points[1:])) / sum(intervals)
    if not math.isfinite(variance) or variance <= 0:
        return None, f"INSUFFICIENT_VARIATION: {kind} volatility cannot be estimated", detail
    detail["volatility_per_sqrt_second"] = math.sqrt(variance)
    return math.sqrt(variance), None, detail


def wilson(wins, n):
    p = wins / n
    z = 1.96
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0, centre - half), min(1, centre + half)]


class LiveEstimator:
    def __init__(self, manual_delay=3, slippage=0.01):
        self.manual_delay = manual_delay
        self.slippage = slippage
        self.reference = deque(maxlen=1800)
        # Receipt audit used only for as-of-opening selection. Unlike the live
        # volatility buffer, a reconnect/gap must not erase observations that
        # were genuinely received before the contract opened.
        self.opening_references = {}
        self.quotes = {}
        self.reference_source = None
        self.developing = DevelopingCandle()

    def restore(self, states):
        """Recover only observed inputs, never probabilities or fabricated paths."""
        for state in states:
            if state.get("mode") != "PUBLIC MARKET DATA":
                continue
            market = state.get("market")
            if market:
                self.observe(
                    {"reference": state.get("reference", {}), "markets": [market]},
                    timestamp(state["now"]),
                )

    def observe(self, payload, now):
        reference = payload.get("reference", {})
        try:
            source = self.reference_identity(reference["source"])
            when = timestamp(reference["observed_at"])
            price = number(reference["price"], 0.000001, 1e12)
            if 0 <= (now - when).total_seconds() <= MAX_GAP:
                self._audit_reference(source, when, price)
                if source != self.reference_source:
                    self.reference.clear()
                    self.reference_source = source
                if self.reference and (
                    when < self.reference[-1][0]
                    or (when - self.reference[-1][0]).total_seconds() > MAX_GAP
                ):
                    self.reference.clear()
                if not self.reference or when > self.reference[-1][0]:
                    self.reference.append((when, price))
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        for market in payload.get("markets", []):
            try:
                if not timestamp(market["opens_at"]) <= now < timestamp(market["closes_at"]):
                    continue
                when = timestamp(market["observed_at"])
                if not 0 <= (now - when).total_seconds() <= MAX_GAP:
                    continue
                q = market["quotes"]["up"]
                bid, ask = number(q["bid"], 0, 1), number(q["ask"], 0, 1)
                if bid > ask:
                    continue
                mid = number((bid + ask) / 2, 0.000001, 0.999999)
                ticker = market["ticker"]
                # A rollover starts a new contract-quote history; reference history can persist.
                history = self.quotes.setdefault(ticker, deque(maxlen=1800))
                if history and (
                    when < history[-1][0] or (when - history[-1][0]).total_seconds() > MAX_GAP
                ):
                    history.clear()
                if not history or when > history[-1][0]:
                    history.append((when, math.log(mid / (1 - mid))))
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
        active = {m.get("ticker") for m in payload.get("markets", [])}
        self.quotes = {t: h for t, h in self.quotes.items() if t in active}

    @staticmethod
    def reference_identity(source):
        """Keep REST/stream labels for the same venue from clearing valid history."""
        source = str(source)
        if "Coinbase" in source and "BTC" in source:
            return "Coinbase BTC/USD proxy"
        if "Kraken" in source and "BTC" in source and "futures" not in source.lower():
            return "Kraken BTC/USD proxy"
        return source

    def _audit_reference(self, source, received, price):
        """Keep bounded source-specific receipts across live continuity resets."""
        history = self.opening_references.setdefault(source, deque(maxlen=20000))
        if history and received == history[-1][0]:
            if price != history[-1][1]:
                return False
            return False
        if history and received < history[-1][0]:
            return False
        history.append((received, price))
        return True

    def opening_points(self, opened):
        """Select one proxy source using receipts at or before opening only."""
        start = opened - timedelta(seconds=65)
        candidates = []
        for source, history in self.opening_references.items():
            points = [(when, price) for when, price in history if start <= when <= opened]
            if points:
                candidates.append((points[-1][0], len(points), source, points))
        if not candidates:
            return [], None
        _, _, source, points = max(candidates, key=lambda item: (item[0], item[1]))
        return points, source

    def observe_reference(self, observation, now):
        """Retain every normalized spot sample drained between model updates."""
        if observation.get("kind") not in {"quote", "trade"}:
            return False
        source = self.reference_identity(observation.get("source", ""))
        if source not in {"Coinbase BTC/USD proxy", "Kraken BTC/USD proxy"}:
            return False
        try:
            received = timestamp(observation["received_at"])
            price = number(observation["price"], 0.000001, 1e12)
            if not 0 <= (now - received).total_seconds() <= 65:
                return False
            self._audit_reference(source, received, price)
            if source != self.reference_source:
                self.reference.clear()
                self.reference_source = source
            if self.reference and received < self.reference[-1][0]:
                return False
            if self.reference and (received - self.reference[-1][0]).total_seconds() > MAX_GAP:
                self.reference.clear()
            if not self.reference or received > self.reference[-1][0]:
                self.reference.append((received, price))
                return True
        except (KeyError, TypeError, ValueError, AttributeError):
            return False
        return False

    def estimate(self, payload, market, now, target, stop, horizon):
        self.observe(payload, now)
        info = {
            "version": VERSION,
            "validated": False,
            "enabled": True,
            "settlement_assumptions": "Zero-drift dollar Brownian motion fitted to up to 10m of Coinbase receipt-time samples; opening-window anchored proxy/index basis, assumed constant thereafter; OHLC4 is only an approximation when tick coverage is absent. Not an official-index forecast.",
            "scalp_assumptions": "Zero-drift logit midpoint diffusion fitted to this contract; frozen current spread and fee schedule; 1s Monte Carlo observations; future one-contract liquidity assumed. Intrapoll barriers/fills are unknown.",
            "timestamp_basis": "HTTP receipt timestamps, not exchange event timestamps; quote and reference freshness are checked separately.",
        }
        info["btc_context"], info["candle_diagnostic"] = candle_context(
            payload.get("btc_candles", {}), now
        )
        info["analysis_24h"] = context24(payload.get("btc_candles", {}), now)
        info["candidate_features"] = extract(payload, market, now)
        info["candidate_features"]["values"].update(info["analysis_24h"].get("features", {}))
        info["adaptive_momentum"] = adaptive_momentum(
            info["candidate_features"]["values"], market
        )
        info["candidate_features"]["values"].update(info["adaptive_momentum"]["features"])
        info["developing_candle"] = self.developing.update(payload.get("reference", {}), now)
        info["multi_timeframe_context"] = {
            "closed_candle_trends": info["analysis_24h"].get("timeframes", {}),
            "developing_1m": info["developing_candle"],
            "as_of": now.isoformat(),
            "probability_role": (
                "Recorded shadow context. The current statistical fallback uses measured "
                "volatility and threshold distance; directional candle trends require "
                "chronological out-of-sample validation before receiving probability weight."
            ),
            "source_limitation": "Coinbase BTC-USD is a proxy, not the official settlement index.",
        }
        info["coverage"] = payload.get("coverage", {})
        info["learning_status"] = {
            "status": "EXPERIMENTAL_FALLBACK",
            "reason": "No validation-selected learned artifact loaded; official-label sufficiency checked by journal learner. No trained accuracy is claimed.",
        }
        up = down = su = sd = None
        overall_reason = scalp_reason = "MISSING_DATA: no unique active contract"
        if market is not None:
            remaining = None
            try:
                if not market.get("mapping_verified", False):
                    raise ValueError("UNSUPPORTED_RULES: YES-to-UP mapping not verified")
                if not 0 <= (now - timestamp(market["observed_at"])).total_seconds() <= MAX_GAP:
                    raise ValueError("STALE_DATA: contract snapshot older than 5s or future-dated")
                remaining = (timestamp(market["closes_at"]) - now).total_seconds()
                if remaining <= 0:
                    raise ValueError("EXPIRED_CONTRACT: await rollover")
                overall_reason, up = self._settlement(payload, market, now, remaining, info)
                down = None if up is None else 1 - up
                scalp_reason, su, sd = self._scalp(
                    market, now, remaining, target, stop, horizon, info
                )
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                overall_reason = scalp_reason = str(exc)
            if remaining is not None and remaining > 0:
                try:
                    info["binary_contract_context"] = binary_contract_context(
                        market, remaining, self.slippage
                    )
                except (KeyError, ValueError, TypeError, AttributeError):
                    info["binary_contract_context"] = {
                        "status": "UNAVAILABLE",
                        "reason": "Fresh compatible executable quotes and fee metadata are required",
                    }
        info["execution_assumptions"] = {
            "manual_delay_seconds": self.manual_delay,
            "slippage_per_leg": self.slippage,
            "entry_rule": "At delay, assume one contract only if observed/simulated ask plus slippage does not exceed the displayed entry cap; otherwise no entry. Not a real fill.",
        }
        info["edge_status"] = "edge unverified"
        info["overall_reason"] = overall_reason
        info["scalp_reason"] = scalp_reason
        return (
            Display(
                "ESTIMATE" if su is not None and sd is not None else "WAIT",
                overall_reason + " | " + scalp_reason,
                contract=market.get("ticker") if market else None,
                settlement_up=up,
                settlement_down=down,
                scalp_up=su,
                scalp_down=sd,
                samples=PATHS if su is not None else 0,
                label="UNVALIDATED — explicit proxy/diffusion assumptions; not calibrated probabilities",
            ),
            info,
        )

    def _settlement(self, payload, market, now, remaining, info):
        try:
            if not market.get("averaging_rule_verified", False):
                raise ValueError(
                    "UNSUPPORTED_RULES: final 60s CF Benchmarks averaging and >= strike must be verified"
                )
            ref = payload.get("reference", {})
            if ref.get("price") is None or not ref.get("observed_at"):
                raise ValueError("MISSING_REFERENCE: timestamped Bitcoin proxy price required")
            current = number(ref["price"], 0.000001, 1e12)
            age = (now - timestamp(ref["observed_at"])).total_seconds()
            if not 0 <= age <= MAX_GAP:
                raise ValueError(
                    "STALE_DATA: Bitcoin reference sample older than 5s or future-dated"
                )
            strike = number(market["strike"], 0.000001, 1e12)
            sigma, reason, stats = volatility(list(self.reference), now, "reference")
            info["reference_history"] = stats
            context, candle_error = candle_context(payload.get("btc_candles", {}), now)
            info["btc_context"] = context
            info["candle_diagnostic"] = candle_error
            if reason and context:
                sigma = context["volatility_dollars_per_sqrt_second"]
                info[
                    "volatility_method"
                ] = "closed one-minute proxy candles; all close times <= prediction time"
            elif reason:
                return reason + "; " + str(candle_error), None
            else:
                info["volatility_method"] = "contiguous observed proxy tick history"
            day = info.get("analysis_24h", {})
            if day.get("status") == "READY":
                sigma = max(sigma, day["sigma_dollars_per_sqrt_second"])
                info["volatility_method"] += "; maximum of recent and measured 24h RMS volatility"
                info["version"] += "/btc24h-v1"
                info[
                    "settlement_assumptions"
                ] += " With complete 24h data, volatility is the maximum of recent and 24h RMS; no indicator votes or arbitrary directional drift."
            if remaining >= 60:
                mean = current
                variance = sigma * sigma * (remaining - 40)
            else:
                # Integrate the observed part of the closing window; never replace
                # unavailable past samples with the current value.
                start = timestamp(market["closes_at"])
                from datetime import timedelta

                start -= timedelta(seconds=60)
                history = list(self.reference)
                before = [(t, p) for t, p in history if t <= start]
                if not before or (start - before[-1][0]).total_seconds() > MAX_GAP:
                    return "MISSING_AVERAGING_HISTORY: final averaging window not covered", None
                points = [before[-1]] + [(t, p) for t, p in history if start < t <= now]
                if (now - points[-1][0]).total_seconds() > MAX_GAP:
                    return "MISSING_AVERAGING_HISTORY: latest averaging observation missing", None
                points.append((now, current))
                area = 0
                for (a, pa), (b, pb) in zip(points, points[1:]):
                    left = max(a, start)
                    if b <= left:
                        continue
                    leftp = pa + (pb - pa) * (left - a).total_seconds() / (b - a).total_seconds()
                    area += (leftp + pb) / 2 * (b - left).total_seconds()
                mean = (area + remaining * current) / 60
                variance = sigma * sigma * remaining**3 / (3 * 60**2)
            basis, basis_error = estimate_basis(payload, market, list(self.reference), now)
            info["basis"] = basis
            if basis_error:
                return basis_error, None
            deviation = math.sqrt(variance)

            def event_probability(offset):
                return 0.5 * (1 + math.erf((mean + offset - strike) / (math.sqrt(2) * deviation)))

            probability = event_probability(basis["point"])
            info["settlement_probability_sensitivity"] = [
                event_probability(basis["low"]),
                event_probability(basis["high"]),
            ]
            info["price_forecast"] = {
                "terminal_proxy_spot_mean": current,
                "terminal_proxy_spot_interval": [
                    current - 1.96 * sigma * math.sqrt(remaining),
                    current + 1.96 * sigma * math.sqrt(remaining),
                ],
                "settlement_average_mean": mean + basis["point"],
                "settlement_average_conditional_interval": [
                    mean + basis["low"] - 1.96 * deviation,
                    mean + basis["high"] + 1.96 * deviation,
                ],
                "interval_label": "Experimental Gaussian 95% conditional interval plus opening-anchor sensitivity; NOT calibrated coverage; future basis drift unmeasured",
                "comparison_operator": market.get("comparison_operator", ">="),
                "averaging_seconds": 60,
            }
            info["candidate_features"]["values"]["distance_scaled"] = (
                current + basis["point"] - strike
            ) / current
            info["baseline_probability_up"] = probability
            info["factors"] = [
                {
                    "factor": "Basis-adjusted projected settlement average versus actual target",
                    "value": mean + basis["point"] - strike,
                    "role": "supports UP if positive, DOWN if negative",
                },
                {
                    "factor": "Opening-anchor probability sensitivity",
                    "value": info["settlement_probability_sensitivity"],
                    "role": "opposes a strong lean when the range straddles neutral",
                },
                {
                    "factor": "Momentum/volume/cross-venue features",
                    "role": "candidate context only until a trained candidate beats baselines chronologically",
                },
            ]

            info["settlement_inputs"] = {
                "proxy_price": current,
                "strike": strike,
                "remaining_seconds": remaining,
                "mean_average": mean + basis["point"],
                "proxy_mean_average": mean,
                "standard_deviation_average": math.sqrt(variance),
                "reference_observed_at": ref["observed_at"],
            }
            return (
                "MODEL_ESTIMATE: proxy final-average probability under stated assumptions; UNVALIDATED",
                probability,
            )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            return f"MISSING_OR_UNSUPPORTED_SETTLEMENT_DATA: {exc}", None

    def _scalp(self, market, now, remaining, target, stop, horizon, info):
        try:
            if horizon + self.manual_delay > remaining:
                raise ValueError("INSUFFICIENT_TIME: holding limit exceeds contract lifetime")
            if market.get("book_diagnostic"):
                raise ValueError(market["book_diagnostic"])
            if market["fee_type"] not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError("UNKNOWN_FEES: no supported series fee schedule")
            multiplier = number(market["fee_multiplier"], 0, 100)
            qs = market["quotes"]
            for side in ("up", "down"):
                q = qs[side]
                bid = number(q["bid"], 0, 1)
                number(q["ask"], max(0.000001, bid), 0.999999)
                if q.get("bid_size", 0) < 1 or q.get("ask_size", 0) < 1:
                    raise ValueError(
                        f"INSUFFICIENT_LIQUIDITY: {side} needs at least one contract on bid and ask"
                    )
                number(q["bid_size"], 1, 1e12)
                number(q["ask_size"], 1, 1e12)
            if qs["up"]["bid"] + qs["down"]["bid"] > 1 or qs["up"]["ask"] + qs["down"]["ask"] < 1:
                raise ValueError("INVALID_QUOTES: crossed binary book")
            sigma, reason, stats = volatility(
                list(self.quotes.get(market["ticker"], [])), now, "contract quotes"
            )
            history = list(self.quotes.get(market["ticker"], []))
            earlier = [(t, p) for t, p in history if (now - t).total_seconds() >= 60]
            if history:
                stats["midpoint_now"] = 1 / (1 + math.exp(-history[-1][1]))
            if earlier:
                stats["midpoint_change_60s"] = stats["midpoint_now"] - 1 / (
                    1 + math.exp(-earlier[-1][1])
                )
            info["quote_history"] = stats
            if reason:
                return reason, None, None
            mid = (qs["up"]["bid"] + qs["up"]["ask"]) / 2
            initial = math.log(mid / (1 - mid))
            spreads = {s: (qs[s]["ask"] - qs[s]["bid"]) / 2 for s in ("up", "down")}
            caps = {s: qs[s]["ask"] + self.slippage for s in ("up", "down")}
            if any(cap >= 1 for cap in caps.values()):
                raise ValueError("NO_EXECUTABLE_ENTRY: ask plus slippage reaches payout cap")
            seed = int.from_bytes(
                hashlib.sha256(
                    (
                        market["ticker"]
                        + market["observed_at"]
                        + str((target, stop, horizon, self.manual_delay, self.slippage, VERSION))
                    ).encode()
                ).digest()[:8],
                "big",
            )
            rng = random.Random(seed)
            totals = {
                s: {
                    "target": 0,
                    "stop": 0,
                    "timeout": 0,
                    "no_entry": 0,
                    "pnl": [],
                    "outcome_pnl": {"target": [], "stop": [], "timeout": []},
                }
                for s in caps
            }
            for _ in range(PATHS):
                entries = {s: None for s in caps}
                resolved = {s: False for s in caps}
                value = initial
                for second in range(self.manual_delay + horizon + 1):
                    if second:
                        value += rng.gauss(0, sigma)
                    p = 1 / (1 + math.exp(-max(-30, min(30, value))))
                    for side in caps:
                        if resolved[side]:
                            continue
                        fair = p if side == "up" else 1 - p
                        if second < self.manual_delay:
                            continue
                        if second == self.manual_delay:
                            ask = qs[side]["ask"] if second == 0 else min(1, fair + spreads[side])
                            buy = ask + self.slippage
                            if buy > caps[side] or buy >= 1:
                                totals[side]["no_entry"] += 1
                                totals[side]["pnl"].append(0.0)
                                resolved[side] = True
                                continue
                            entries[side] = buy
                        raw_bid = qs[side]["bid"] if second == 0 else max(0, fair - spreads[side])
                        bid = max(0, raw_bid - self.slippage)
                        entry = entries[side]
                        net = bid - entry - fee(entry, multiplier) - fee(bid, multiplier)
                        elapsed = second - self.manual_delay
                        outcome = None
                        if elapsed >= horizon:
                            outcome = "timeout"
                        elif net <= -stop:
                            outcome = "stop"
                        elif net >= target:
                            outcome = "target"
                        if outcome:
                            totals[side][outcome] += 1
                            totals[side]["pnl"].append(net)
                            totals[side]["outcome_pnl"][outcome].append(net)
                            resolved[side] = True
                    if all(resolved.values()):
                        break
            details = {}
            probabilities = {}
            for side, total in totals.items():
                entered = PATHS - total["no_entry"]
                probabilities[side] = total["target"] / entered if entered else None
                values = total["pnl"]
                mean = sum(values) / PATHS
                variance = sum((p - mean) ** 2 for p in values) / (PATHS - 1)
                half = 1.96 * math.sqrt(variance / PATHS)
                details[side] = {
                    "entered_paths": entered,
                    "paths": PATHS,
                    "entry_probability": entered / PATHS,
                    "unconditional_probabilities": {
                        o: total[o] / PATHS for o in ("target", "stop", "timeout", "no_entry")
                    },
                    "conditional_on_entry_probabilities": {
                        o: total[o] / entered if entered else None
                        for o in ("target", "stop", "timeout")
                    },
                    "net_expected_profit_given_entry": sum(values) / entered if entered else None,
                    "partial_fill_probability": None,
                    "liquidity_uncertainty": "Current one-contract depth verified; future depth/partial-fill/queue probability unmeasured. Model assumes all-or-no entry, not observed fills.",
                    "target_probability_given_entry": probabilities[side],
                    "target_count": total["target"],
                    "stop_count": total["stop"],
                    "timeout_count": total["timeout"],
                    "no_entry_count": total["no_entry"],
                    "net_expected_profit_per_opportunity": mean,
                    "net_ev_sampling_interval": [mean - half, mean + half],
                    "target_sampling_interval": wilson(total["target"], entered)
                    if entered
                    else None,
                    "mean_net_pnl_by_outcome": {
                        o: sum(v) / len(v) if v else None for o, v in total["outcome_pnl"].items()
                    },
                    "entry_cap": caps[side],
                    "manual_delay_seconds": self.manual_delay,
                    "slippage_per_leg": self.slippage,
                }
            info["monte_carlo"] = {
                "paths": PATHS,
                "step_seconds": 1,
                "seed": seed,
                "up_sampling_interval": details["up"]["target_sampling_interval"],
                "down_sampling_interval": details["down"]["target_sampling_interval"],
                "interval_scope": "Monte Carlo sampling error only; excludes model/parameter error, basis risk and real fills",
            }
            info["scalp_edges"] = details
            return (
                "MODEL_ESTIMATE: delay-conditioned target/stop/timeout outcomes with entry/exit fees and slippage; edge unverified",
                probabilities["up"],
                probabilities["down"],
            )
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            return f"MISSING_OR_UNSUPPORTED_SCALP_DATA: {exc}", None, None
