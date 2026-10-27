"""Trace the real scalp pipeline without turning model output into a trade.

This module performs no IO, imports no broker client and cannot change risk
limits. Configurable criteria can tighten the installed quality gates. The
current application has no reviewed profitable-scalp promotion protocol, so a
technical candidate is always hypothetical and its recommendation remains WAIT.
"""
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import math

from .model import timestamp


@dataclass(frozen=True)
class ScalpCriteria:
    quantity: float = 1.0
    minimum_success_probability: float = 0.60
    maximum_spread: float = 0.03
    minimum_entered_paths: int = 200
    minimum_net_ev: float = 0.0
    maximum_data_age_seconds: float = 5.0
    maximum_source_skew_seconds: float = 5.0
    maximum_holding_seconds: int = 60

    @classmethod
    def from_mapping(cls, mapping=None):
        if isinstance(mapping, cls):
            mapping = asdict(mapping)
        if mapping is None:
            return cls()
        if not isinstance(mapping, dict):
            raise ValueError("Scalp criteria must be an object")
        unknown = set(mapping) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError("Unknown scalp criteria: " + ", ".join(sorted(unknown)))
        limits = {
            "quantity": (1, 1e6),
            "minimum_success_probability": (0.60, 1),
            "maximum_spread": (0, 0.03),
            "minimum_entered_paths": (200, 10_000_000),
            "minimum_net_ev": (0, 1),
            "maximum_data_age_seconds": (0.05, 5),
            "maximum_source_skew_seconds": (0.05, 5),
            "maximum_holding_seconds": (1, 900),
        }
        integers = {"minimum_entered_paths", "maximum_holding_seconds"}
        for name, value in mapping.items():
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(name + " must be a finite number")
            lo, hi = limits[name]
            if not lo <= value <= hi or (name in integers and type(value) is not int):
                raise ValueError(
                    name + f" must be {'an integer ' if name in integers else ''}in [{lo}, {hi}]"
                )
        return cls(**mapping)

    def to_dict(self):
        return asdict(self)


def _number(value, lo=None, hi=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if lo is not None and value < lo or hi is not None and value > hi:
        return None
    return value


def _instant(value):
    try:
        return timestamp(value)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def _fees(quantity, price, multiplier):
    return math.ceil(0.07 * multiplier * quantity * price * (1 - price) * 100 - 1e-12) / 100


def _exposure(account, ticker, now):
    """Unfilled orders are reservations, never invested quantity or cost."""
    observed = _instant(account.get("observed_at"))
    fresh = (
        account.get("status") == "READ_ONLY_CONNECTED"
        and observed is not None
        and 0 <= (now - observed).total_seconds() <= 20
    )
    raw_holdings = account.get("holdings")
    holdings = (
        [h for h in raw_holdings if isinstance(h, dict) and h.get("status") == "OPEN"]
        if isinstance(raw_holdings, list)
        else []
    )
    valid = (
        fresh
        and isinstance(raw_holdings, list)
        and all(_number(h.get("quantity"), 0, 1e15) is not None for h in holdings)
    )
    side = {
        name: sum(
            h["quantity"] for h in holdings if h.get("ticker") == ticker and h.get("side") == name
        )
        if valid
        else None
        for name in ("up", "down")
    }
    pending = account.get("pending_orders")
    quantities = [] if isinstance(pending, list) else None
    for order in pending if isinstance(pending, list) else []:
        try:
            raw = order.get("remaining_quantity")
            if isinstance(raw, bool) or raw is None:
                raise ValueError("Unknown reservation")
            q = float(raw)
            if not math.isfinite(q) or q < 0:
                raise ValueError("Unknown reservation")
            quantities.append((order.get("ticker"), q))
        except (ValueError, TypeError):
            quantities = None
            break
    reserved = sum(q for _, q in quantities) if fresh and quantities is not None else None
    contract_reserved = (
        sum(q for t, q in quantities if t == ticker) if fresh and quantities is not None else None
    )
    positions = []
    for card in account.get("live_trade_cards", []):
        pnl, cost = card.get("estimated_exit_net_pnl"), card.get("total_entry_cost")
        card_fresh = fresh and not card.get("values_stale", False)
        value = (
            pnl + cost
            if card_fresh and _number(pnl) is not None and _number(cost, 0) is not None
            else None
        )
        positions.append(
            {
                "holding_id": card.get("holding_id"),
                "ticker": card.get("contract"),
                "side": card.get("side"),
                "quantity": card.get("filled_quantity"),
                "current_executable_exit_price": card.get("current_executable_exit_price")
                if card_fresh
                else None,
                "estimated_exit_net_pnl": pnl if card_fresh else None,
                "estimated_liquidation_value_after_costs": value,
                "remaining_cost_at_risk": card.get("remaining_cost_at_risk"),
                "guidance": card.get("guidance", "WAIT") if card_fresh else "WAIT",
                "reason": card.get("reason")
                if card_fresh
                else "Account reconciliation is stale; check manually",
                "timestamp": card.get("timestamp"),
                "model_probability": card.get("current_model_probability") if card_fresh else None,
            }
        )
    return {
        "account_status": account.get("status", "PUBLIC_ONLY"),
        "account_fresh": fresh,
        "last_successful_sync": account.get("observed_at"),
        "confirmed_quantity_by_side": side,
        "confirmed_contract_quantity": sum(side.values()) if valid else None,
        "confirmed_account_quantity": sum(h["quantity"] for h in holdings) if valid else None,
        "unfilled_reserved_quantity": reserved,
        "unfilled_contract_reserved_quantity": contract_reserved,
        "available_balance_dollars": account.get("available_balance_dollars") if fresh else None,
        "limits": dict(account["limits"]) if isinstance(account.get("limits"), dict) else None,
        "limits_configured": bool(account.get("limits")),
        "positions": positions,
        "notice": "Only confirmed holdings are exposure; unfilled orders are separate reservations. No account connection means unknown exposure, not zero.",
    }


def diagnose(state, criteria=None, account=None, now=None):
    criteria = ScalpCriteria.from_mapping(criteria)
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        now = timestamp(now)
    if now.tzinfo is None:
        raise ValueError("Scalping diagnostics require a UTC-aware clock")
    now = now.astimezone(timezone.utc)
    market = state.get("market") or {}
    model = state.get("model") or {}
    estimate = state.get("estimate") or {}
    execution = model.get("execution_assumptions") or {}
    scan = model.get("scanner") or {}
    edge_details = model.get("scalp_edges") or {}
    account = account if account is not None else state.get("account") or {}
    checks, blockers = [], []

    def check(code, stage, passed, reason, *, side=None, essential=False):
        item = {
            "code": code,
            "stage": stage,
            "status": "PASS" if passed is True else "UNKNOWN" if passed is None else "FAIL",
            "reason": reason,
        }
        if side:
            item["side"] = side
        checks.append(item)
        if passed is not True:
            blockers.append(
                {**item, "severity": "DATA_OR_SAFETY" if essential else "QUALIFICATION"}
            )
        return passed is True

    enabled = model.get("enabled") is True and bool(model.get("version"))
    check(
        "ENGINE_ENABLED",
        "MODEL_COMPONENT",
        enabled,
        "LiveEstimator scalp component is running"
        if enabled
        else model.get("scalp_reason")
        or "No enabled LiveEstimator scalp component in this snapshot",
        essential=True,
    )
    ticker = market.get("ticker")
    check(
        "ACTIVE_CONTRACT",
        "CONTRACT",
        bool(ticker and str(ticker).startswith("KXBTC15M-")),
        "Active KXBTC15M contract identified" if ticker else "No active KXBTC15M contract",
        essential=True,
    )
    opened, closed = _instant(market.get("opens_at")), _instant(market.get("closes_at"))
    active = opened is not None and closed is not None and opened <= now < closed
    remaining = max(0, (closed - now).total_seconds()) if closed else None
    check(
        "CONTRACT_LIFETIME",
        "TIMING",
        active,
        "Contract is within its exact opening/closing times"
        if active
        else "Contract is expired, not open, or missing its times",
        essential=True,
    )
    check(
        "VERIFIED_UP_DOWN_MAPPING",
        "CONTRACT",
        market.get("mapping_verified") is True,
        "Published YES/UP mapping verified"
        if market.get("mapping_verified") is True
        else "YES/UP settlement mapping is unverified",
        essential=True,
    )

    ages, times = {}, {}
    for key, value in (
        ("quote", market.get("observed_at")),
        ("reference", (state.get("reference") or {}).get("observed_at")),
    ):
        at = _instant(value)
        age = (now - at).total_seconds() if at else None
        ages[key + "_seconds"] = age
        times[key] = at
        valid = age is not None and 0 <= age <= criteria.maximum_data_age_seconds
        check(
            key.upper() + "_FRESHNESS",
            "DATA",
            valid,
            f"{key.capitalize()} receipt age {age:.3f}s (maximum {criteria.maximum_data_age_seconds}s)"
            if age is not None
            else key.capitalize() + " receipt timestamp unavailable",
            essential=True,
        )
    check(
        "SNAPSHOT_FRESHNESS",
        "DATA",
        not state.get("stale", True),
        "Published snapshot marked fresh"
        if not state.get("stale", True)
        else "Published snapshot is stale; no entry guidance",
        essential=True,
    )
    check(
        "REFERENCE_PRICE",
        "DATA",
        _number((state.get("reference") or {}).get("price"), 0.000001, 1e12) is not None,
        "Timestamped BTC reference price required",
        essential=True,
    )
    skew = (
        abs((times["quote"] - times["reference"]).total_seconds()) if all(times.values()) else None
    )
    ages["quote_reference_skew_seconds"] = skew
    check(
        "SOURCE_SYNCHRONIZATION",
        "DATA",
        skew is not None and skew <= criteria.maximum_source_skew_seconds,
        f"Quote/reference receipt skew {skew:.3f}s (maximum {criteria.maximum_source_skew_seconds}s)"
        if skew is not None
        else "Source synchronization unavailable",
        essential=True,
    )
    event_times = {}
    for key, value in (
        ("quote", market.get("quote_event_time")),
        ("reference", (state.get("reference") or {}).get("event_time")),
    ):
        at = _instant(value)
        event_times[key] = at.isoformat() if at else None
        if value is not None:
            age = (now - at).total_seconds() if at else None
            ages[key + "_event_seconds"] = age
            check(
                key.upper() + "_EVENT_FRESHNESS",
                "DATA",
                age is not None and 0 <= age <= criteria.maximum_data_age_seconds,
                key.capitalize() + " exchange event time must be valid, fresh and not future-dated",
                essential=True,
            )
    data_fresh = active and not any(b["stage"] == "DATA" for b in blockers)

    fee_known = (
        market.get("fee_type") in {"quadratic", "quadratic_with_maker_fees"}
        and _number(market.get("fee_multiplier"), 0, 100) is not None
    )
    check(
        "SUPPORTED_FEE_SCHEDULE",
        "COSTS",
        fee_known,
        "Published quadratic fee schedule available"
        if fee_known
        else "Applicable entry and exit fees are unverified",
        essential=True,
    )
    slip = _number(execution.get("slippage_per_leg"), 0, 0.10)
    delay = _number(execution.get("manual_delay_seconds"), 0, 30)
    check(
        "EXECUTION_ASSUMPTIONS",
        "COSTS",
        slip is not None and delay is not None,
        "Manual entry delay and per-leg slippage must be explicit; neither is a guaranteed execution price",
        essential=True,
    )
    check(
        "BOOK_SOURCE",
        "QUOTES",
        not market.get("book_diagnostic"),
        market.get("book_diagnostic") or "Executable book schema available",
        essential=True,
    )
    if scan.get("macro_blackout"):
        check(
            "MACRO_BLACKOUT",
            "RISK_FILTER",
            False,
            "Recent public Federal Reserve release activates the existing 15-minute post-release pause",
            essential=True,
        )

    exposure = _exposure(account, ticker, now)
    limits = exposure["limits"]
    needed_limits = {
        "max_add_quantity",
        "max_per_contract_quantity",
        "max_account_quantity",
        "max_add_cost_dollars",
        "minimum_cash_reserve",
    }
    limits_valid = bool(
        limits
        and needed_limits <= set(limits)
        and all(_number(limits[n], 0, 1e12) is not None for n in needed_limits)
    )
    check(
        "CONFIGURED_RISK_LIMITS",
        "EXPOSURE",
        limits_valid,
        "Existing explicit exposure/cash limits retained"
        if limits_valid
        else "Explicit position/exposure/cash limits are unavailable; no real entry or add recommendation",
    )
    account_known = (
        exposure["confirmed_account_quantity"] is not None
        and exposure["unfilled_reserved_quantity"] is not None
    )
    check(
        "CONFIRMED_EXPOSURE",
        "EXPOSURE",
        account_known,
        "Confirmed holdings and separate pending-order reservations available"
        if account_known
        else "Exposure is unknown or stale; displayed opportunities do not imply a fill",
    )

    quotes = market.get("quotes") or {}
    up, down = quotes.get("up") or {}, quotes.get("down") or {}
    binary_valid = None
    if all(_number(q.get(key), 0, 1) is not None for q in (up, down) for key in ("bid", "ask")):
        binary_valid = up["bid"] + down["bid"] <= 1 + 1e-9 and up["ask"] + down["ask"] >= 1 - 1e-9
    check(
        "BINARY_BOOK_COMPATIBILITY",
        "QUOTES",
        binary_valid,
        "YES/NO executable prices must describe a compatible binary book",
        essential=True,
    )
    global_safe = not any(b["severity"] == "DATA_OR_SAFETY" for b in blockers)
    sides, candidates = {}, []
    for side in ("up", "down"):
        start = len(blockers)
        q, detail, plan = (
            quotes.get(side) or {},
            edge_details.get(side) or {},
            (state.get("plans") or {}).get(side) or {},
        )
        bid, ask = _number(q.get("bid"), 0, 1), _number(q.get("ask"), 0, 1)
        bid_size, ask_size = _number(q.get("bid_size"), 0, 1e15), _number(
            q.get("ask_size"), 0, 1e15
        )
        valid_quote = bid is not None and ask is not None and 0 < ask < 1 and bid <= ask
        check(
            "EXECUTABLE_QUOTE",
            "QUOTES",
            valid_quote,
            "Executable bid and ask must be finite, ordered and below payout cap",
            side=side,
            essential=True,
        )
        spread = ask - bid if valid_quote else None
        liquidity = (
            bid_size is not None
            and ask_size is not None
            and min(bid_size, ask_size) >= criteria.quantity
        )
        check(
            "CURRENT_LIQUIDITY",
            "LIQUIDITY",
            liquidity,
            f"Need at least {criteria.quantity:g} contracts on both bid and ask; unknown depth is unavailable, not zero",
            side=side,
            essential=True,
        )
        spread_ok = spread is not None and spread <= criteria.maximum_spread + 1e-9
        check(
            "SPREAD_LIMIT",
            "COSTS",
            spread_ok,
            f"Spread {spread:.4f}; maximum {criteria.maximum_spread:.4f}"
            if spread is not None
            else "Spread unavailable",
            side=side,
            essential=True,
        )
        holding = plan.get("holding_seconds")
        holding = holding if type(holding) is int and 1 <= holding <= 900 else None
        time_ok = (
            holding is not None
            and holding <= criteria.maximum_holding_seconds
            and delay is not None
            and remaining is not None
            and remaining >= holding + delay
        )
        check(
            "HOLDING_LIFETIME",
            "TIMING",
            time_ok,
            f"Modeled hold {holding}s + manual delay {delay}s must fit remaining {remaining}s and configured maximum {criteria.maximum_holding_seconds}s",
            side=side,
            essential=True,
        )
        quantity_supported = criteria.quantity == 1
        check(
            "MODELED_QUANTITY",
            "MODEL_COMPONENT",
            quantity_supported,
            "Installed diffusion model estimates one-contract all-or-no entry; larger size requires separate execution evidence",
            side=side,
            essential=True,
        )
        cap = ask + slip if valid_quote and slip is not None and ask + slip < 1 else None
        entry_fee = (
            _fees(criteria.quantity, cap, market["fee_multiplier"])
            if fee_known and cap is not None
            else None
        )
        exit_fee = (
            _fees(criteria.quantity, max(0, bid - slip), market["fee_multiplier"])
            if fee_known and bid is not None and slip is not None
            else None
        )
        check(
            "ENTRY_CAP_FEASIBLE",
            "COSTS",
            cap is not None,
            "Ask plus assumed entry slippage must remain below $1 payout",
            side=side,
            essential=True,
        )
        p = _number(detail.get("target_probability_given_entry"), 0, 1)
        if p is None:
            p = _number(estimate.get("scalp_" + side), 0, 1)
        ev = _number(detail.get("net_expected_profit_per_opportunity"))
        interval = detail.get("net_ev_sampling_interval")
        interval = (
            list(interval)
            if isinstance(interval, (tuple, list))
            and len(interval) == 2
            and all(_number(x) is not None for x in interval)
            and interval[0] <= interval[1]
            else None
        )
        entered = detail.get("entered_paths")
        entered = entered if type(entered) is int and entered >= 0 else None
        probability_ok = p is not None and p >= criteria.minimum_success_probability
        check(
            "TARGET_PROBABILITY",
            "MODEL_ESTIMATE",
            probability_ok,
            f"Experimental target-before-stop/time probability must reach {criteria.minimum_success_probability:.1%}; {model.get('scalp_reason') or state.get('scalp_explanation') or 'No model estimate'}",
            side=side,
        )
        ev_ok = (
            ev is not None
            and ev > criteria.minimum_net_ev
            and interval is not None
            and interval[0] > criteria.minimum_net_ev
        )
        check(
            "NET_EXPECTED_VALUE",
            "MODEL_ESTIMATE",
            ev_ok,
            "Require positive net opportunity EV and Monte Carlo lower EV bound after entry/exit fees, spread, latency, slippage and missed entries; this excludes model error",
            side=side,
        )
        sample_ok = entered is not None and entered >= criteria.minimum_entered_paths
        check(
            "ENTERED_SIMULATION_PATHS",
            "MODEL_ESTIMATE",
            sample_ok,
            f"Entered Monte Carlo paths {entered}; minimum {criteria.minimum_entered_paths}. Simulation paths are not real fills or independent historical trades",
            side=side,
        )
        changes = scan.get("timeframe_returns") or {}
        sign = 1 if side == "up" else -1
        confluence = (
            all(
                _number(changes.get(t)) is not None and sign * changes[t] > 0
                for t in ("1m", "3m", "5m")
            )
            and (scan.get("structural_candidate") or {}).get(side) is True
        )
        check(
            "EXISTING_CONFLUENCE_FILTER",
            "RISK_FILTER",
            confluence,
            "Existing closed 1m/3m/5m momentum and structural sweep/reclaim filter must align; it is experimental, not proof of an edge",
            side=side,
        )
        risk_ok = None
        if limits_valid and account_known and cap is not None and entry_fee is not None:
            cost = criteria.quantity * cap + entry_fee
            cash = _number(exposure["available_balance_dollars"], 0, 1e15)
            risk_ok = (
                cash is not None
                and criteria.quantity <= limits["max_add_quantity"]
                and exposure["confirmed_contract_quantity"]
                + exposure["unfilled_contract_reserved_quantity"]
                + criteria.quantity
                <= limits["max_per_contract_quantity"]
                and exposure["confirmed_account_quantity"]
                + exposure["unfilled_reserved_quantity"]
                + criteria.quantity
                <= limits["max_account_quantity"]
                and cost <= limits["max_add_cost_dollars"]
                and cash - cost >= limits["minimum_cash_reserve"]
            )
        check(
            "EXISTING_EXPOSURE_LIMITS",
            "EXPOSURE",
            risk_ok,
            "A hypothetical entry must fit existing quantity, cost and cash limits, including pending-order reservations; no limits are changed",
            side=side,
        )
        side_blockers = blockers[start:]
        technical = global_safe and all(
            (
                valid_quote,
                liquidity,
                spread_ok,
                time_ok,
                quantity_supported,
                cap is not None,
                probability_ok,
                ev_ok,
                sample_ok,
                confluence,
            )
        )
        if technical:
            candidates.append((side, ev))
        sides[side] = {
            "bid": bid,
            "ask": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "spread": spread,
            "entry_fee": entry_fee,
            "estimated_exit_fee_now": exit_fee,
            "slippage": slip,
            "manual_delay_seconds": delay,
            "target_probability": p if quantity_supported and data_fresh else None,
            "model_probability_up": _number(estimate.get("settlement_up"), 0, 1)
            if data_fresh
            else None,
            "model_probability_down": _number(estimate.get("settlement_down"), 0, 1)
            if data_fresh
            else None,
            "net_expected_profit": ev if quantity_supported and data_fresh else None,
            "net_ev_interval": interval if quantity_supported and data_fresh else None,
            "entered_paths": entered,
            "entry_cap": cap,
            "target_bid": _number(plan.get("target_bid"), 0, 1),
            "stop_bid": _number(plan.get("stop_bid"), 0, 1),
            "holding_seconds": holding,
            "hypothetical_candidate": technical,
            "risk_qualified": risk_ok is True,
            "entry_qualified": False,
            "reasons": [b["reason"] for b in side_blockers],
            "blocker_codes": [b["code"] for b in side_blockers],
            "quantity": criteria.quantity,
            "simulated_unit_quantity": 1,
            "values_stale": not data_fresh,
            "notice": "Hypothetical analysis only; displayed entry/exit conditions are not validated recommendations or fills",
        }

    quote_history = model.get("quote_history") or {}
    if enabled and (
        not edge_details or any(s["target_probability"] is None for s in sides.values())
    ):
        check(
            "SCALP_ESTIMATOR_READY",
            "MODEL_ESTIMATE",
            False,
            model.get("scalp_reason")
            or state.get("scalp_explanation")
            or "No conditional scalp estimates; inspect quote-history warm-up, data and model assumptions",
        )
    check(
        "REVIEWED_SCALP_PROMOTION",
        "PROMOTION",
        False,
        "Entry recommendations are disabled: no reviewed statistically supported profitable scalp-execution promotion protocol is installed. A settlement classifier, high model probability or positive Monte Carlo EV cannot open this gate",
    )
    essential = [b for b in blockers if b["severity"] == "DATA_OR_SAFETY"]
    signal = "CANDIDATE" if candidates else "BLOCKED" if essential else "WAIT"
    ranked = sorted(candidates, key=lambda item: item[1], reverse=True)
    selected = (
        ranked[0][0]
        if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1] + 1e-12)
        else None
    )
    if candidates:
        reason = "Hypothetical model-only candidate; no actionable entry because scalp calibration/profitability is unverified"
        if any(not sides[side]["risk_qualified"] for side, _ in candidates):
            reason += "; exposure or configured risk limits also do not qualify"
    elif essential:
        reason = essential[0]["reason"]
    else:
        reason = (
            model.get("scalp_reason")
            or state.get("scalp_explanation")
            or "No side passes the installed numerical and confluence criteria"
        )
        if edge_details:
            failed = [b for b in blockers if b["stage"] in {"MODEL_ESTIMATE", "RISK_FILTER"}]
            if failed:
                reason = failed[0]["reason"]
        reason += "; actionable entries remain disabled because edge is unverified"
    fresh = data_fresh
    liquidity_status = (
        "SUFFICIENT"
        if all(
            s["bid_size"] is not None
            and s["ask_size"] is not None
            and min(s["bid_size"], s["ask_size"]) >= criteria.quantity
            for s in sides.values()
        )
        else "INSUFFICIENT"
    )
    return {
        "engine_status": "ACTIVE" if enabled else "INACTIVE",
        "data_status": "FRESH" if fresh else "STALE",
        "model_status": "EXPERIMENTAL",
        "model_version": model.get("version"),
        "model_validation_reason": "No reviewed scalp execution promotion exists; a model.validated flag or settlement-model holdout report alone is not sufficient",
        "liquidity_status": liquidity_status,
        "spread": {s: d["spread"] for s, d in sides.items()},
        "estimated_edge": {s: d["net_expected_profit"] for s, d in sides.items()},
        "edge_status": "EDGE UNVERIFIED",
        "signal_status": signal,
        "exact_reason": reason,
        "blockers": blockers,
        "checks": checks,
        "ticker": ticker,
        "timestamp": now.isoformat(),
        "prediction_timestamp": (state.get("prediction_engine") or {}).get("updated_at")
        or state.get("now"),
        "data_ages": ages,
        "exchange_event_times": event_times,
        "timestamp_note": "Receipt and exchange event clocks are separate; unknown event time is not zero latency",
        "remaining_seconds": remaining,
        "criteria": criteria.to_dict(),
        "sides": sides,
        "exposure": exposure,
        "candidate_side": selected,
        "hypothetical_candidates": [s for s, _ in ranked],
        "quote_history": quote_history,
        "uncertainty": {
            "status": "UNCALIBRATED",
            "model": model.get("scalp_assumptions"),
            "monte_carlo_scope": (model.get("monte_carlo") or {}).get("interval_scope")
            or "Monte Carlo intervals exclude model error, future liquidity and real manual fills",
            "partial_fill_probability": None,
            "macro_calendar": scan.get(
                "macro_calendar_status", "Forward macro-event coverage unavailable"
            ),
        },
        "recommendation": "WAIT",
        "actionable": False,
        "pipeline": [
            "LiveEstimator._scalp",
            "edges.assess_edges",
            "scanner.qualify",
            "decision.assess",
        ],
        "promotion_status": (state.get("trade_decision") or {}).get("promotion_status")
        or "BLOCKED: no reviewed evidence of profitable execution after costs",
        "notice": "Independent settlement outlook is not a scalp success probability; hypothetical candidates, actual positions and simulated fills remain separate. All execution is manual.",
    }
