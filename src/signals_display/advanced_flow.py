"""Bounded executed-trade analytics, kept separate from validated forecasts.

Volume is never inferred from quotes. Each source keeps its native quantity unit;
spot BTC and derivative contract quantities are not pooled. Session VWAP means
*the observed, uninterrupted capture session*, not an unobserved UTC-day VWAP.
All summaries use both exchange-event and local-receipt cutoffs.
"""
from collections import deque
from bisect import bisect_left
from datetime import datetime, timezone
import math
import threading

from .model import number, timestamp


FEATURE_NAMES = (
    "advanced_vwap_deviation_fraction",
    "advanced_flow_imbalance",
    "advanced_cvd_slope",
    "advanced_book_imbalance",
    "advanced_effective_spread",
    "advanced_trade_intensity",
    "advanced_profile_threshold_concentration",
)


def _time(value):
    parsed = value if isinstance(value, datetime) else timestamp(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _ratio(a, b):
    return a / b if b else None


def _direction(value, tolerance=1e-12):
    return (
        "BULLISH"
        if value is not None and value > tolerance
        else "BEARISH"
        if value is not None and value < -tolerance
        else "NEUTRAL"
    )


class _Source:
    def __init__(self, limit):
        self.trades = deque(maxlen=limit)
        self.quotes = deque(maxlen=min(limit, 20000))
        self.bars = deque(maxlen=2882)
        self.ids = deque(maxlen=limit)
        self.seen = set()
        self.generation = 0
        self.session = None
        self.last_received = None
        self.last_event = {}
        self.last_sequence = {}
        self.requires_snapshot = False
        self.reset_count = self.rejected = self.truncated = 0
        self.cumulative_volume = self.cumulative_value = 0.0
        self.cumulative_buy = self.cumulative_sell = 0.0
        self.unknown_volume = self.unknown_direction = 0
        self.session_started = None
        self.resets = deque(maxlen=100)
        self.unit = "provider quantity units (not pooled across sources)"

    def reset(self, session, receipt):
        if self.last_received is not None:
            self.reset_count += 1
        self.generation += 1
        self.session = session
        self.session_started = receipt
        self.resets.append((receipt, self.generation))
        self.cumulative_volume = self.cumulative_value = 0.0
        self.cumulative_buy = self.cumulative_sell = 0.0
        self.unknown_volume = self.unknown_direction = 0
        self.last_sequence = {}
        self.last_event = {}
        self.requires_snapshot = False
        # Retain IDs across reconnects: exchanges may replay their last executions.


class AdvancedFlow:
    """Accept normalized stream observations and return auditable diagnostics.

    ``observe(mapping)`` accepts the same source/price/event_at/received_at/kind/
    volume/aggressor/trade_id/session_id fields as ``MarketIntelligence``. Optional
    ``bids`` and ``asks`` are complete [price, quantity] snapshots; aggregate
    ``bid_depth``/``ask_depth`` never stand in for individual levels. ``sequence``
    gaps reset the capture session; incremental messages require a new snapshot.
    """

    def __init__(
        self,
        *,
        max_sources=8,
        max_records=20000,
        window_seconds=900,
        bucket_dollars=10.0,
        large_trade_quantity=1.0,
    ):
        if type(max_sources) is not int or not 1 <= max_sources <= 32:
            raise ValueError("max_sources must be 1..32")
        if type(max_records) is not int or not 100 <= max_records <= 200000:
            raise ValueError("max_records must be 100..200000")
        self.max_sources, self.max_records = max_sources, max_records
        self.window_seconds = number(window_seconds, 30, 86400)
        self.bucket_dollars = number(bucket_dollars, 0.01, 10000)
        self.large_trade_quantity = number(large_trade_quantity, 1e-12, 1e12)
        self.sources = {}
        self.lock = threading.RLock()

    def observe(self, observation):
        if not isinstance(observation, dict):
            raise ValueError("Observation must be a mapping")
        item = observation
        source = item.get("source")
        if not isinstance(source, str) or not source or len(source) > 128:
            raise ValueError("A bounded source name is required")
        receipt = _time(item.get("received_at")).timestamp()
        event = _time(item["event_at"]).timestamp() if item.get("event_at") is not None else None
        if event is not None and not 0 <= receipt - event <= 5:
            raise ValueError("Exchange event is future-dated or stale at receipt")
        price = number(item.get("price"), 1e-6, 1e12)
        kind = item.get("kind", "quote")
        if kind not in ("quote", "trade"):
            raise ValueError("Unknown observation kind")
        side = item.get("aggressor")
        if side not in (None, "buy", "sell"):
            raise ValueError("Unknown aggressor direction")
        volume = number(item["volume"], 0, 1e15) if item.get("volume") is not None else None
        session = item.get("session_id", item.get("session"))
        if session is not None and (not isinstance(session, (str, int)) or len(str(session)) > 128):
            raise ValueError("Invalid session identifier")
        identifier = item.get("trade_id")
        if identifier is not None and (
            not isinstance(identifier, (str, int)) or len(str(identifier)) > 160
        ):
            raise ValueError("Invalid trade identifier")
        sequence = item.get("sequence")
        if sequence is not None and (type(sequence) is not int or sequence < 0):
            raise ValueError("Sequence must be a nonnegative integer")
        quote = self._quote(item, receipt, event, price) if kind == "quote" else None
        with self.lock:
            if source not in self.sources:
                if len(self.sources) >= self.max_sources:
                    raise ValueError("Maximum source count reached")
                self.sources[source] = _Source(self.max_records)
            state = self.sources[source]
            # IDs identify real executions independently of reconnect sessions.
            key = str(identifier) if identifier is not None and kind == "trade" else None
            if key is not None and key in state.seen:
                return False
            if state.last_received is not None and receipt < state.last_received:
                state.rejected += 1
                raise ValueError("Out-of-order receipt timestamp")
            if (
                session != state.session
                or item.get("gap_detected")
                or state.session_started is None
            ):
                state.reset(session, receipt)
            if sequence is not None and state.last_sequence.get(kind) is not None:
                if sequence <= state.last_sequence[kind]:
                    state.rejected += 1
                    return False
                if sequence != state.last_sequence[kind] + 1:
                    state.reset(session, receipt)
                    state.requires_snapshot = bool(item.get("incremental"))
            if state.requires_snapshot and not item.get("snapshot"):
                state.rejected += 1
                return False
            if item.get("snapshot"):
                state.requires_snapshot = False
            if (
                event is not None
                and state.last_event.get(kind) is not None
                and event < state.last_event[kind]
            ):
                state.rejected += 1
                raise ValueError("Out-of-order exchange timestamp within observation kind")
            state.last_received = receipt
            if event is not None:
                state.last_event[kind] = event
            if sequence is not None:
                state.last_sequence[kind] = sequence
            state.unit = str(item.get("quantity_unit", state.unit))[:128]
            if key is not None:
                if len(state.ids) == state.ids.maxlen:
                    state.seen.discard(state.ids[0])
                state.ids.append(key)
                state.seen.add(key)
            if kind == "quote":
                quote["generation"] = state.generation
                state.quotes.append(quote)
            else:
                # Anonymous executions cannot safely be deduplicated; withhold
                # volume-based summaries rather than double-count them.
                if key is None:
                    volume = None
                if len(state.trades) == state.trades.maxlen:
                    state.truncated += 1
                state.unknown_volume += int(volume is None)
                state.unknown_direction += int(side is None or volume is None)
                if volume is not None:
                    state.cumulative_volume += volume
                    state.cumulative_value += price * volume
                    if side == "buy":
                        state.cumulative_buy += volume
                    elif side == "sell":
                        state.cumulative_sell += volume
                trade = dict(
                    received=receipt,
                    event=event,
                    price=price,
                    volume=volume,
                    side=side,
                    generation=state.generation,
                    session_started=state.session_started,
                    cumulative_volume=state.cumulative_volume,
                    cumulative_value=state.cumulative_value,
                    cumulative_buy=state.cumulative_buy,
                    cumulative_sell=state.cumulative_sell,
                    unknown_volume=state.unknown_volume,
                    unknown_direction=state.unknown_direction,
                    direction_basis=item.get(
                        "direction_basis",
                        "exchange supplied aggressor, or documented maker-side inversion",
                    ),
                    inferred=bool(item.get("direction_inferred")),
                    identified=key is not None,
                )
                state.trades.append(trade)
                self._bar(state, trade)
            return True

    @staticmethod
    def _quote(item, receipt, event, price):
        bid = number(item["bid"], 1e-6, 1e12) if item.get("bid") is not None else None
        ask = number(item["ask"], 1e-6, 1e12) if item.get("ask") is not None else None
        if (bid is None) != (ask is None) or (bid is not None and bid > ask):
            raise ValueError("Incomplete or crossed quote")
        depth = []
        for field, descending in (("bids", True), ("asks", False)):
            rows = item.get(field)
            if rows is None:
                depth.append(None)
                continue
            if not isinstance(rows, (list, tuple)) or len(rows) > 100:
                raise ValueError("Depth must be a bounded snapshot")
            levels = [(number(p, 1e-6, 1e12), number(q, 0, 1e15)) for p, q in rows]
            if len({p for p, q in levels}) != len(levels):
                raise ValueError("Duplicate depth price level")
            levels.sort(reverse=descending)
            depth.append(levels[:20])
        bids, asks = depth
        if (bids is None) != (asks is None):
            raise ValueError("Both sides of depth snapshot required")
        if bids and asks:
            if bids[0][0] > asks[0][0] or (
                bid is not None and (bid != bids[0][0] or ask != asks[0][0])
            ):
                raise ValueError("Depth and best prices disagree")
            bid, ask = bids[0][0], asks[0][0]
        bd = number(item["bid_depth"], 0, 1e15) if item.get("bid_depth") is not None else None
        ad = number(item["ask_depth"], 0, 1e15) if item.get("ask_depth") is not None else None
        if (bd is None) != (ad is None):
            raise ValueError("Both aggregate depth quantities required")
        return dict(
            received=receipt,
            event=event,
            price=price,
            bid=bid,
            ask=ask,
            bid_depth=bd,
            ask_depth=ad,
            bids=bids,
            asks=asks,
            depth_levels=item.get("depth_levels"),
            scope=item.get("depth_scope", "supplied aggregate; level count unknown"),
        )

    @staticmethod
    def _bar(state, trade):
        # Without exchange event timestamps these are not exchange-time candles.
        if trade["event"] is None:
            return
        bucket = math.floor(trade["event"] / 30) * 30
        if (
            state.bars
            and state.bars[-1]["timestamp"] == bucket
            and state.bars[-1]["generation"] == state.generation
        ):
            bar = state.bars[-1]
            bar["high"] = max(bar["high"], trade["price"])
            bar["low"] = min(bar["low"], trade["price"])
            bar["close"] = trade["price"]
            bar["received"] = max(bar["received"], trade["received"])
            bar["volume_known"] &= trade["volume"] is not None
            bar["volume"] += trade["volume"] or 0
        else:
            state.bars.append(
                dict(
                    timestamp=bucket,
                    open=trade["price"],
                    high=trade["price"],
                    low=trade["price"],
                    close=trade["price"],
                    volume=trade["volume"] or 0,
                    volume_known=trade["volume"] is not None,
                    received=trade["received"],
                    generation=state.generation,
                )
            )

    def closed_bars(self, source, now):
        """Canonical complete 30s candles with actual completion-receipt proof.

        A clock advancing past a last trade does not prove its bar complete. A
        subsequent same-session trade-channel observation whose exchange event has
        crossed the close is required; its actual receipt is the availability
        timestamp. Disconnections/gaps never supply missing candle continuity.
        """
        cutoff = _time(now).timestamp()
        empty = dict(
            source=source,
            received_at=None,
            candles=[],
            status="WARMING UP",
            reason="No same-session post-close trade-channel observation proves candle completion",
        )
        with self.lock:
            state = self.sources.get(source)
            if state is None:
                return empty
            observations = [
                x
                for x in list(state.trades) + list(state.quotes)
                if x["received"] <= cutoff and x["event"] is not None and x["event"] <= cutoff
            ]
            if not observations:
                return empty
            latest = max(observations, key=lambda x: x["received"])
            generation = latest["generation"]
            resets = [entry for entry in state.resets if entry[0] <= cutoff]
            if resets and resets[-1][1] > generation:
                return empty
            observations = sorted(
                (
                    x
                    for x in state.trades
                    if x["generation"] == generation
                    and x["received"] <= cutoff
                    and x["event"] is not None
                    and x["event"] <= cutoff
                ),
                key=lambda x: x["event"],
            )
            events = [x["event"] for x in observations]
            candles = []
            for bar in state.bars:
                if (
                    bar["generation"] != generation
                    or bar["timestamp"] < cutoff - 86400
                    or bar["timestamp"] + 30 > cutoff
                    or bar["received"] > cutoff
                ):
                    continue
                index = bisect_left(events, bar["timestamp"] + 30)
                if index == len(events):
                    continue
                completion = observations[index]["received"]
                candles.append(
                    dict(
                        start=bar["timestamp"],
                        open=bar["open"],
                        high=bar["high"],
                        low=bar["low"],
                        close=bar["close"],
                        volume=bar["volume"] if bar["volume_known"] else None,
                        first_received_at=_iso(max(completion, bar["received"])),
                        interval_seconds=30,
                    )
                )
            if not candles:
                return empty
            return dict(
                source=source,
                received_at=max(c["first_received_at"] for c in candles),
                candles=candles,
                status="READY",
                basis="Actual executed trades; completion proven by subsequent same-session trade-channel exchange event",
            )

    def summarize(self, now, threshold=None, *, primary_source=None):
        cutoff = _time(now).timestamp()
        if threshold is not None:
            threshold = number(threshold, 1e-6, 1e12)
        with self.lock:
            results = {
                name: self._summary(state, cutoff, threshold, name)
                for name, state in self.sources.items()
            }
        ready = [name for name, value in results.items() if value["status"] == "READY"]
        primary = (
            primary_source
            if primary_source in results
            else ready[0]
            if ready
            else next(iter(results), None)
        )
        return dict(
            sources=results,
            primary_source=primary,
            as_of=_iso(cutoff),
            active_contribution=False,
            probability_adjustment=0,
            validation="EXPERIMENTAL diagnostics; candidate inputs require chronological out-of-sample validation",
            candidate_features=results.get(primary, {}).get("candidate_features", {}),
            candidate_feature_sources=results.get(primary, {}).get("candidate_feature_sources", {}),
            max_input_received_at=results.get(primary, {}).get("received_at"),
            units_policy="Per-source native quantities; no spot/derivative pooling",
        )

    def _summary(self, state, cutoff, threshold, source_name):
        eligible_trades = [
            x
            for x in state.trades
            if x["received"] <= cutoff and (x["event"] is None or x["event"] <= cutoff)
        ]
        eligible_quotes = [
            x
            for x in state.quotes
            if x["received"] <= cutoff and (x["event"] is None or x["event"] <= cutoff)
        ]
        latest = max(
            eligible_trades[-1:] + eligible_quotes[-1:], key=lambda x: x["received"], default=None
        )
        if latest is None:
            return dict(
                status="UNAVAILABLE",
                reason="No observations received by cutoff",
                active_contribution=False,
            )
        generation = latest["generation"]
        resets = [entry for entry in state.resets if entry[0] <= cutoff]
        if resets and resets[-1][1] > generation:
            return dict(
                status="WARMING UP",
                reason="Sequence gap/reconnect requires a fresh valid snapshot",
                active_contribution=False,
            )
        trades = [x for x in eligible_trades if x["generation"] == generation]
        quotes = [x for x in eligible_quotes if x["generation"] == generation]
        receipt_latest = latest
        # Quote and trade channels have independent event clocks. A delayed
        # execution must contribute its actual volume without rewinding a newer
        # quote/reference price. Both inputs remain in availability-time proof.
        latest = max(
            trades[-1:] + quotes[-1:],
            key=lambda x: (
                x["event"] is not None,
                x["event"] if x["event"] is not None else x["received"],
            ),
        )
        recent = [x for x in trades if x["received"] >= cutoff - self.window_seconds]
        recent_quotes = [x for x in quotes if x["received"] >= cutoff - 60]
        age = cutoff - receipt_latest["received"]
        status = "STALE" if age > 5 else "READY"
        base = dict(
            status=status,
            data_age_seconds=age,
            event_at=_iso(receipt_latest["event"]) if receipt_latest["event"] is not None else None,
            received_at=_iso(receipt_latest["received"]),
            latest_price=latest["price"],
            price_event_at=_iso(latest["event"]) if latest["event"] is not None else None,
            price_received_at=_iso(latest["received"]),
            event_latency_seconds=receipt_latest["received"] - receipt_latest["event"]
            if receipt_latest["event"] is not None
            else None,
            active_contribution=False,
            quantity_unit=state.unit,
            reset_count=state.reset_count,
            rejected=state.rejected,
            retained_trade_records=len(state.trades),
            bounded_record_limit=self.max_records,
            truncation_count=state.truncated,
            window_seconds=self.window_seconds,
            observed_window_seconds=min(
                self.window_seconds,
                cutoff - (trades[-1]["session_started"] if trades else quotes[0]["received"]),
            ),
        )
        complete = bool(recent) and all(x["volume"] is not None for x in recent)
        # A full deque can discard records still inside this window. Report the
        # truncation instead of presenting the remaining subset as full-window.
        truncated_window = bool(
            state.truncated
            and recent
            and state.trades
            and state.trades[0]["received"] > cutoff - self.window_seconds
            and state.trades[0]["generation"] == generation
        )
        complete &= not truncated_window
        latest_trade = trades[-1] if trades else None
        vwap = (
            _ratio(sum(x["price"] * x["volume"] for x in recent), sum(x["volume"] for x in recent))
            if complete
            else None
        )
        session_vwap = (
            _ratio(latest_trade["cumulative_value"], latest_trade["cumulative_volume"])
            if latest_trade and not latest_trade["unknown_volume"]
            else None
        )
        vwap_first = [x for x in recent if x["received"] <= cutoff - self.window_seconds / 2]
        first_vwap = (
            _ratio(
                sum(x["price"] * x["volume"] for x in vwap_first),
                sum(x["volume"] for x in vwap_first),
            )
            if complete
            else None
        )
        last_price = latest["price"]
        base["vwap"] = dict(
            status="READY" if vwap is not None else "UNAVAILABLE",
            rolling=vwap,
            session=session_vwap,
            session_basis="Since this uninterrupted capture session; not complete UTC-day history",
            session_started_at=_iso(latest_trade["session_started"]) if latest_trade else None,
            deviation=last_price - vwap if vwap is not None else None,
            interpretation=_direction(last_price - vwap if vwap is not None else None),
            slope_per_second=(vwap - first_vwap) / (self.window_seconds / 2)
            if vwap is not None and first_vwap is not None
            else None,
            crossing=self._crossing(recent) if complete else "UNAVAILABLE",
            volume_weighted_momentum=self._weighted_momentum(recent) if complete else None,
            reason="Actual executed price times quantity"
            if complete
            else "Missing real trade quantities/IDs, empty window, or bounded window truncated",
        )
        base["volume_profile"] = self._profile(recent, complete, threshold)
        base["order_flow"], base["cvd"] = self._flow(recent, complete, latest_trade, cutoff)
        base["order_book"] = self._book(quotes[-1] if quotes else None, cutoff)
        base["microstructure"] = self._micro(
            recent, recent_quotes, base["order_book"], complete, cutoff
        )
        base["candidate_features"] = dict(
            advanced_vwap_deviation_fraction=(last_price - vwap) / vwap if vwap else None,
            advanced_flow_imbalance=base["order_flow"].get("imbalance"),
            advanced_cvd_slope=base["cvd"].get("slope_per_second"),
            advanced_book_imbalance=base["order_book"].get("multi_level_imbalance"),
            advanced_effective_spread=base["microstructure"].get("effective_spread"),
            advanced_trade_intensity=base["order_flow"].get("trades_per_second"),
            advanced_profile_threshold_concentration=base["volume_profile"].get(
                "concentration_near_threshold"
            ),
        )
        base["candidate_feature_sources"] = {}
        readings = {
            "vwap": "rolling",
            "volume_profile": "point_of_control",
            "order_flow": "imbalance",
            "order_book": "multi_level_imbalance",
            "cvd": "window_net_delta",
            "microstructure": "spread",
        }
        for family in readings:
            used = (
                quotes[-1]
                if family == "order_book" and quotes
                else receipt_latest
                if family == "microstructure"
                else latest_trade
            )
            receipt = used["received"] if used else None
            family_age = cutoff - receipt if receipt is not None else None
            base[family].update(
                as_of=_iso(receipt) if receipt is not None else None,
                age_seconds=family_age,
                contributing=False,
                reading=base[family].get(readings[family]),
            )
            base[family].setdefault("interpretation", "NEUTRAL")
            if family_age is not None and family_age > 5:
                base[family]["status"] = "STALE"
        feature_families = {
            "advanced_vwap_deviation_fraction": "vwap",
            "advanced_flow_imbalance": "order_flow",
            "advanced_cvd_slope": "cvd",
            "advanced_book_imbalance": "order_book",
            "advanced_effective_spread": "microstructure",
            "advanced_trade_intensity": "order_flow",
            "advanced_profile_threshold_concentration": "volume_profile",
        }
        for name, family in feature_families.items():
            if base[family]["status"] not in ("READY",):
                base["candidate_features"][name] = None
            if base["candidate_features"][name] is not None:
                proof = (
                    receipt_latest
                    if name == "advanced_vwap_deviation_fraction" or family == "microstructure"
                    else quotes[-1]
                    if family == "order_book"
                    else latest_trade
                )
                base["candidate_feature_sources"][name] = {
                    "received_at": _iso(proof["received"]),
                    "event_at": _iso(proof["event"]) if proof["event"] is not None else None,
                    "source": source_name,
                }
        return base

    @staticmethod
    def _crossing(trades):
        if len(trades) < 2:
            return "UNAVAILABLE"
        numerator = denominator = 0.0
        previous = None
        result = "NONE"
        for trade in trades:
            numerator += trade["price"] * trade["volume"]
            denominator += trade["volume"]
            delta = trade["price"] - numerator / denominator if denominator else None
            if delta is not None and previous is not None:
                if previous <= 0 < delta:
                    result = "ABOVE"
                elif previous >= 0 > delta:
                    result = "BELOW"
            previous = delta
        return result

    @staticmethod
    def _weighted_momentum(trades):
        if len(trades) < 2:
            return None
        weights = sum(t["volume"] for t in trades[1:])
        return _ratio(
            sum((b["price"] - a["price"]) * b["volume"] for a, b in zip(trades, trades[1:])),
            weights,
        )

    def _profile(self, trades, complete, threshold):
        if not complete:
            return dict(
                status="UNAVAILABLE",
                reason="Real, complete trade volumes unavailable",
                point_of_control=None,
            )
        bins = {}
        for trade in trades:
            index = math.floor(trade["price"] / self.bucket_dollars)
            bins[index] = bins.get(index, 0) + trade["volume"]
        total = sum(bins.values())
        if not total:
            return dict(
                status="UNAVAILABLE", reason="No positive executed volume", point_of_control=None
            )
        keys = sorted(bins)
        poc = max(keys, key=lambda k: (bins[k], -k))
        selected = {poc}
        left = right = keys.index(poc)
        selected_volume = bins[poc]
        while selected_volume < total * 0.7 and (left > 0 or right < len(keys) - 1):
            next_left = bins[keys[left - 1]] if left > 0 else -1
            next_right = bins[keys[right + 1]] if right < len(keys) - 1 else -1
            if next_left >= next_right:
                left -= 1
                chosen = keys[left]
            else:
                right += 1
                chosen = keys[right]
            selected.add(chosen)
            selected_volume += bins[chosen]
        volumes = sorted(bins.values())
        high_cut = volumes[math.floor((len(volumes) - 1) * 0.75)]
        low_cut = volumes[math.floor((len(volumes) - 1) * 0.25)]
        center = lambda k: (k + 0.5) * self.bucket_dollars
        return dict(
            status="READY",
            bucket_dollars=self.bucket_dollars,
            point_of_control=center(poc),
            high_volume_nodes=[center(k) for k in keys if bins[k] >= high_cut][:20],
            low_volume_nodes=[center(k) for k in keys if bins[k] <= low_cut][:20],
            value_area_low=min(selected) * self.bucket_dollars,
            value_area_high=(max(selected) + 1) * self.bucket_dollars,
            value_area_fraction=selected_volume / total,
            total_volume=total,
            concentration_near_threshold=sum(
                q
                for k, q in bins.items()
                if threshold is not None and abs(center(k) - threshold) <= self.bucket_dollars
            )
            / total
            if threshold is not None
            else None,
            bucket_count=len(bins),
            definition="70% contiguous observed-price-bin value area; no inferred hidden liquidity",
        )

    def _flow(self, trades, complete, latest, cutoff):
        direction_known = complete and all(t["side"] is not None for t in trades)
        short = [t for t in trades if t["received"] >= cutoff - 60]
        buy = sum(t["volume"] for t in trades if t["side"] == "buy") if direction_known else None
        sell = sum(t["volume"] for t in trades if t["side"] == "sell") if direction_known else None
        delta = buy - sell if direction_known else None
        span = trades[-1]["received"] - trades[0]["received"] if len(trades) > 1 else None
        short_delta = (
            sum((1 if t["side"] == "buy" else -1) * t["volume"] for t in short)
            if direction_known
            else None
        )
        preceding = [t for t in trades if cutoff - 120 <= t["received"] < cutoff - 60]
        preceding_delta = (
            sum((1 if t["side"] == "buy" else -1) * t["volume"] for t in preceding)
            if direction_known
            else None
        )
        momentum = (
            (short_delta - preceding_delta) / 60
            if direction_known and short and preceding
            else None
        )
        inferred = sum(t["inferred"] for t in trades)
        flow = dict(
            status="READY" if direction_known else "UNAVAILABLE",
            aggressive_buy_volume=buy,
            aggressive_sell_volume=sell,
            imbalance=_ratio(delta, buy + sell) if direction_known else None,
            buy_sell_ratio=_ratio(buy, sell) if direction_known else None,
            trades_per_second=len(trades) / span
            if span and all(t["identified"] for t in trades)
            else None,
            observed_span_seconds=span,
            quantity_unit="source native quantity",
            large_trade_quantity_threshold=self.large_trade_quantity,
            large_trade_count=sum(t["volume"] >= self.large_trade_quantity for t in trades)
            if complete
            else None,
            short_term_momentum=momentum,
            inferred_direction_count=inferred,
            direction_reliability="inferred; not validated"
            if inferred
            else "exchange documented aggressor/maker inversion"
            if direction_known
            else "unavailable",
            interpretation=_direction(delta),
            reason="Executed aggressor flow; descriptive, not a calibrated probability",
        )
        cumulative = (
            latest["cumulative_buy"] - latest["cumulative_sell"]
            if latest and not latest["unknown_direction"]
            else None
        )
        cvd = dict(
            status="READY" if direction_known else "UNAVAILABLE",
            cumulative_buy_volume=latest["cumulative_buy"] if cumulative is not None else None,
            cumulative_sell_volume=latest["cumulative_sell"] if cumulative is not None else None,
            session_net_delta=cumulative,
            window_net_delta=delta,
            slope_per_second=_ratio(delta, span) if direction_known and span else None,
            acceleration_per_second_squared=momentum / 60 if momentum is not None else None,
            divergence=self._divergence(trades) if direction_known else "UNAVAILABLE",
            interpretation=_direction(delta),
            basis="Actual executed trades only; session restarts on reconnect/gap",
        )
        return flow, cvd

    @staticmethod
    def _divergence(trades):
        if len(trades) < 6:
            return "UNAVAILABLE"
        midpoint = len(trades) // 2
        first, second = trades[:midpoint], trades[midpoint:]
        running = 0
        values = []
        for t in trades:
            running += t["volume"] * (1 if t["side"] == "buy" else -1)
            values.append(running)
        if max(t["price"] for t in second) > max(t["price"] for t in first) and max(
            values[midpoint:]
        ) < max(values[:midpoint]):
            return "BEARISH PRICE/CVD DIVERGENCE (descriptive, unvalidated)"
        if min(t["price"] for t in second) < min(t["price"] for t in first) and min(
            values[midpoint:]
        ) > min(values[:midpoint]):
            return "BULLISH PRICE/CVD DIVERGENCE (descriptive, unvalidated)"
        return "NONE"

    @staticmethod
    def _book(quote, cutoff):
        if quote is None or quote["bid"] is None:
            return dict(
                status="UNAVAILABLE", reason="No executable source book", multi_level_imbalance=None
            )
        if cutoff - quote["received"] > 5:
            return dict(
                status="STALE", reason="Book older than five seconds", multi_level_imbalance=None
            )
        bids, asks = quote["bids"], quote["asks"]
        total_b = sum(q for p, q in bids) if bids else quote["bid_depth"]
        total_a = sum(q for p, q in asks) if asks else quote["ask_depth"]
        top_b = bids[0][1] if bids else quote["bid_depth"] if quote["depth_levels"] == 1 else None
        top_a = asks[0][1] if asks else quote["ask_depth"] if quote["depth_levels"] == 1 else None
        total = total_b + total_a if total_b is not None else None
        top = top_b + top_a if top_b is not None else None
        imbalance = (total_b - total_a) / total if total else None
        midpoint = (quote["bid"] + quote["ask"]) / 2
        return dict(
            status="READY",
            bid=quote["bid"],
            ask=quote["ask"],
            spread=quote["ask"] - quote["bid"],
            midpoint=midpoint,
            top_of_book_imbalance=(top_b - top_a) / top if top else None,
            multi_level_imbalance=imbalance,
            bid_depth=total_b,
            ask_depth=total_a,
            depth_levels=min(len(bids), len(asks)) if bids and asks else quote["depth_levels"],
            microprice=(quote["ask"] * top_b + quote["bid"] * top_a) / top if top else None,
            depth_weighted_midprice=(sum(p * q for p, q in bids) + sum(p * q for p, q in asks))
            / total
            if bids and asks and total
            else None,
            liquidity_concentration=max([q for p, q in bids] + [q for p, q in asks]) / total
            if bids and asks and total
            else None,
            received_at=_iso(quote["received"]),
            interpretation=_direction(imbalance),
            reason="Snapshot quantities only; no hidden orders or institutional intent inferred",
        )

    @staticmethod
    def _micro(trades, quotes, book, complete, cutoff):
        returns = [math.log(b["price"] / a["price"]) for a, b in zip(trades, trades[1:])]
        span = trades[-1]["received"] - trades[0]["received"] if len(trades) > 1 else None
        effects = []
        quote_index = 0
        for trade in trades:
            while (
                quote_index + 1 < len(quotes)
                and quotes[quote_index + 1]["received"] <= trade["received"]
            ):
                quote_index += 1
            if not quotes:
                break
            quote = quotes[quote_index]
            # Receipt availability AND exchange chronology are required; a
            # quote received just before an older trade is not its pre-trade mid.
            if (
                quote["bid"] is not None
                and quote["received"] <= trade["received"] <= quote["received"] + 1
                and quote["event"] is not None
                and trade["event"] is not None
                and quote["event"] <= trade["event"] <= quote["event"] + 1
                and trade["side"] in ("buy", "sell")
            ):
                effects.append(
                    2
                    * (1 if trade["side"] == "buy" else -1)
                    * (trade["price"] - (quote["bid"] + quote["ask"]) / 2)
                )
        depth_quotes = [q for q in quotes if q["bid_depth"] is not None]
        depth_change = None
        replenishment = deterioration = None
        if len(depth_quotes) >= 2:
            values = [q["bid_depth"] + q["ask_depth"] for q in depth_quotes]
            depth_change = values[-1] - values[0]
            replenishment = sum(max(0, b - a) for a, b in zip(values, values[1:]))
            deterioration = (values[0] - values[-1]) / values[0] if values[0] else None
        net = (
            sum((1 if t["side"] == "buy" else -1) * t["volume"] for t in trades)
            if complete and all(t["side"] for t in trades)
            else None
        )
        impact = (trades[-1]["price"] - trades[0]["price"]) / net if net else None
        qspan = quotes[-1]["received"] - quotes[0]["received"] if len(quotes) > 1 else None
        return dict(
            status="READY" if trades or quotes else "UNAVAILABLE",
            spread=book.get("spread"),
            microprice=book.get("microprice"),
            effective_spread=sum(effects) / len(effects) if effects else None,
            effective_spread_samples=len(effects),
            effective_spread_basis="Twice aggressor-signed trade-minus-contemporaneous-pretrade-mid; <=1s quote age",
            realized_volatility=math.sqrt(sum(r * r for r in returns)) if returns else None,
            trade_arrivals_per_second=len(trades) / span if span else None,
            price_impact_per_signed_quantity=impact,
            bid_ask_depth_change=depth_change,
            replenishment_quantity=replenishment,
            replenishment_basis="Positive changes in observed aggregate depth, not identified order additions",
            liquidity_deterioration_fraction=deterioration,
            quote_updates_per_second=len(quotes) / qspan if qspan else None,
            momentum=trades[-1]["price"] - trades[0]["price"] if len(trades) > 1 else None,
            reversal="UNAVAILABLE"
            if len(trades) < 3
            else "POSSIBLE REVERSAL"
            if (trades[-1]["price"] - trades[-2]["price"])
            * (trades[-2]["price"] - trades[0]["price"])
            < 0
            else "NONE",
            interpretation="DESCRIPTIVE; no validated probability adjustment",
        )
