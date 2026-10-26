"""Manually launched offline display; reads only uploaded JSON, never credentials."""
from datetime import datetime, timezone
import json

import streamlit as st
from src.signals_display.model import evaluate


def probability(value):
    return "unavailable" if value is None else f"{value:.1%}"


@st.fragment(run_every="1s")
def main():
    st.title("Bitcoin 15-minute signals — offline")
    st.warning("UNVALIDATED estimates. Scenario frequencies are not calibrated live probabilities.")
    st.caption("Signals only. Local uploaded data; no account connection or order execution.")
    target = st.number_input("Net profit target ($ per contract)", 0.001, 1.0, 0.05, 0.001)
    stop = st.number_input("Net stop-loss ($ per contract)", 0.001, 1.0, 0.03, 0.001)
    horizon = st.number_input("Scalp time limit (seconds)", 1, 900, 60)
    upload = st.file_uploader("Contract snapshot and conditional scenarios (JSON)", type=["json"])
    result = None
    if upload:
        try:
            result = evaluate(
                json.load(upload),
                now=datetime.now(timezone.utc),
                target=target,
                stop=stop,
                horizon=horizon,
            )
        except (ValueError, TypeError):
            st.error("Invalid JSON snapshot — estimates unavailable")
    st.subheader(result.status if result else "WAIT")
    st.write(result.reason if result else "Fresh contract snapshot and scenario evidence required.")
    if result:
        st.write(f"Contract: {result.contract or 'unavailable'} · Scenarios: {result.samples}")
    st.subheader("Overall settlement estimate — UP / DOWN")
    up, down = st.columns(2)
    up.metric(
        "UP at settlement (unvalidated)", probability(result.settlement_up if result else None)
    )
    down.metric(
        "DOWN at settlement (unvalidated)", probability(result.settlement_down if result else None)
    )
    st.subheader("Scalp — target before stop-loss or time limit")
    up, down = st.columns(2)
    up.metric("UP scalp (unvalidated)", probability(result.scalp_up if result else None))
    down.metric("DOWN scalp (unvalidated)", probability(result.scalp_down if result else None))
    st.caption(
        "One-contract taker entry at ask, exit at scenario bid, rounded fees on both legs. "
        "Timeouts are non-successes. Scalp probabilities need not sum to 100%. "
        "Discrete scenario observations can miss intrastep barriers; fills and future liquidity are unvalidated. "
        "Display rechecks freshness every second; snapshots expire after 5 seconds."
    )


if __name__ == "__main__":
    st.set_page_config(page_title="Bitcoin 15-minute signals", layout="wide")
    main()
