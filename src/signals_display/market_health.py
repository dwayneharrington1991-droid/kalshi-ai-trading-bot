"""Contract/sample freshness is independent from HTTP success status."""
from .model import timestamp


def selection_summary(markets, now):
    records = []
    for market in markets:
        try:
            opened, closed = timestamp(market["opens_at"]), timestamp(market["closes_at"])
            records.append(
                (
                    abs((closed - now).total_seconds()),
                    dict(
                        ticker=market["ticker"],
                        opens_at=opened.isoformat(),
                        closes_at=closed.isoformat(),
                        status=market.get("status"),
                    ),
                )
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return [record for _, record in sorted(records, key=lambda row: row[0])[:8]]


def diagnose(state, now):
    market = state.get("market")
    reference = state.get("reference") or {}
    reasons = []
    detail = dict(
        clock_utc=now.isoformat(),
        timestamp_basis="Receipt timestamps; exchange event latency is not established",
        contract=market.get("ticker") if market else None,
        quote_age_seconds=None,
        reference_age_seconds=None,
        seconds_remaining=None,
    )
    if not market:
        candidates = state.get("discovered_contracts", [])
        active = []
        next_open = []
        previous_close = []
        for candidate in candidates:
            try:
                opened, closed = timestamp(candidate["opens_at"]), timestamp(candidate["closes_at"])
                if opened <= now < closed and candidate.get("status") == "open":
                    active.append(candidate["ticker"])
                if opened > now:
                    next_open.append(opened)
                if closed <= now:
                    previous_close.append(closed)
            except (KeyError, ValueError, TypeError, AttributeError):
                pass
        reasons.append(
            "MULTIPLE_ACTIVE_CONTRACTS"
            if len(active) > 1
            else "ACTIVE_CONTRACT_SELECTION_PENDING"
            if active
            else "NO_ACTIVE_CONTRACT"
        )
        detail.update(
            active_candidates=active,
            next_known_open=min(next_open).isoformat() if next_open else None,
            last_known_close=max(previous_close).isoformat() if previous_close else None,
        )
    else:
        try:
            opened, closed = timestamp(market["opens_at"]), timestamp(market["closes_at"])
            detail["seconds_remaining"] = (closed - now).total_seconds()
            if now < opened:
                reasons.append("CONTRACT_NOT_OPEN")
            if now >= closed:
                reasons.append("CONTRACT_EXPIRED")
            age = (now - timestamp(market["observed_at"])).total_seconds()
            detail["quote_age_seconds"] = round(age, 3)
            if age < 0:
                reasons.append("QUOTE_TIMESTAMP_IN_FUTURE")
            elif age > 5:
                reasons.append("QUOTE_OLDER_THAN_5_SECONDS")
        except (KeyError, ValueError, TypeError, AttributeError):
            reasons.append("INVALID_CONTRACT_OR_QUOTE_TIMESTAMP")
    if state.get("connection_error"):
        reasons.append("LATEST_PUBLIC_POLL_FAILED")
    ref_reasons = []
    try:
        age = (now - timestamp(reference["observed_at"])).total_seconds()
        detail["reference_age_seconds"] = round(age, 3)
        if age < 0:
            ref_reasons.append("REFERENCE_TIMESTAMP_IN_FUTURE")
        elif age > 5:
            ref_reasons.append("REFERENCE_OLDER_THAN_5_SECONDS")
        if reference.get("price") is None:
            ref_reasons.append("REFERENCE_PRICE_MISSING")
    except (KeyError, ValueError, TypeError, AttributeError):
        ref_reasons.append("REFERENCE_TIMESTAMP_MISSING_OR_INVALID")
    detail.update(
        contract_quote_reasons=reasons,
        reference_reasons=ref_reasons,
        contract_quotes_fresh=not reasons,
        reference_fresh=not ref_reasons,
        http_note="HTTP 200 proves request success, not active-contract selection, unexpired settlement time or fresh samples. Five-second freshness checks are unchanged.",
    )
    return detail
