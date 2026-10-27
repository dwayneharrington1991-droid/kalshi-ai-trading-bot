"""Plain-language recommendation gate, separate from experimental forecasts."""


def assess(state):
    # Current evaluation establishes no validated profitable edge. Forecast
    # accuracy, MC precision, model EV, and user-supplied labels cannot open this
    # gate. A future validated deployment needs a reviewed promotion protocol.
    stale = state.get("stale", True)
    reason = (
        "Prices are missing or too old. Wait for a fresh update."
        if stale
        else "No profitable opportunity has been validated. Keep watching."
    )
    warning = "No new trade is suggested. Any trades you already hold are shown below."
    account = state.get("account", {})
    if account.get("status") == "READ_ONLY_CONNECTED":
        exits = [g for g in account.get("guidance", []) if g.get("guidance") == "CONSIDER EXIT"]
        if exits:
            warning = "Check your open trades below: an exit warning is active. You must place any exit yourself."
    return dict(
        action="WAIT",
        scalp="NO",
        entry_price=None,
        maximum_risk_dollars=None,
        profit_target=None,
        reason=reason,
        exit_warning=warning,
        updated_at=state.get("now"),
        validation="edge unverified",
        risk_reason="No risk amount is suggested while the opportunity is unverified; a stop cannot guarantee a maximum loss.",
        promotion_status="BLOCKED: no reviewed evidence of profitable execution after costs",
        manual_only=True,
    )
