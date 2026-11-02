"""Bounded advisory events only. Alert preferences are local to the browser."""
from collections import deque
import hashlib


def risk_alerts(account, now):
    """Warn against existing explicit limits; never set limits or suggest adds."""
    if account.get("status") != "READ_ONLY_CONNECTED" or not account.get("limits"):
        return []
    limits = account["limits"]
    holdings = [h for h in account.get("holdings", []) if h.get("status") == "OPEN"]
    total = sum(h.get("quantity", 0) for h in holdings)
    per_contract = {}
    for holding in holdings:
        per_contract[holding["ticker"]] = (
            per_contract.get(holding["ticker"], 0) + holding["quantity"]
        )
    breaches = []
    if total > limits["max_account_quantity"]:
        breaches.append(("account_quantity", None, total, limits["max_account_quantity"]))
    for ticker, quantity in per_contract.items():
        if quantity > limits["max_per_contract_quantity"]:
            breaches.append(
                ("contract_quantity", ticker, quantity, limits["max_per_contract_quantity"])
            )
    cash = account.get("available_balance_dollars")
    if cash is not None and cash < limits["minimum_cash_reserve"]:
        breaches.append(("cash_reserve", None, cash, limits["minimum_cash_reserve"]))
    return [
        {
            "id": "risk-" + hashlib.sha256(repr(breach).encode()).hexdigest()[:20],
            "type": "RISK_LIMIT_BREACH",
            "ticker": breach[1],
            "title": "Configured risk limit exceeded",
            "reason": f"{breach[0]}: observed {breach[2]}, configured limit {breach[3]}. Check manually; no position was changed.",
            "timestamp": now.isoformat(),
        }
        for breach in breaches
    ]


class MonitoringAlerts:
    def __init__(self):
        self.events = deque(maxlen=40)
        self.previous = None
        self.keys = set()

    def update(self, state):
        market = state.get("market") or {}
        engine = state.get("prediction_engine") or {}
        prior = self.previous or {}
        ticker = market.get("ticker")
        now = state.get("now")

        def add(kind, title, reason, discriminator, **values):
            key = (kind, ticker, discriminator)
            if key in self.keys:
                return
            if len(self.keys) > 1000:
                self.keys = {
                    (e["type"], e.get("ticker"), e.get("discriminator")) for e in self.events
                }
            self.keys.add(key)
            identifier = hashlib.sha256(repr(key).encode()).hexdigest()[:20]
            self.events.append(
                {
                    "id": identifier,
                    "type": kind,
                    "title": title,
                    "reason": reason,
                    "ticker": ticker,
                    "timestamp": now,
                    "discriminator": discriminator,
                    **values,
                }
            )

        stale = bool(state.get("stale", True))
        if stale and not prior.get("stale", True):
            add(
                "DATA_OUTAGE",
                "Market data unavailable",
                "Fresh data required; check held positions manually.",
                now,
            )
        if not stale and ticker:
            remaining = state.get("remaining_seconds")
            if remaining is not None:
                for threshold in (120, 60, 30, 10):
                    if 0 < remaining <= threshold and (
                        prior.get("ticker") != ticker
                        or prior.get("remaining_seconds", 901) > threshold
                    ):
                        add(
                            "SETTLEMENT",
                            "Settlement approaching",
                            "Contract closes soon; this is a timing alert, not an exit fill.",
                            threshold,
                            remaining_seconds=remaining,
                        )
                        break
            p = engine.get("up") if engine.get("status") == "EXPERIMENTAL" else None
            if (
                p is not None
                and prior.get("ticker") == ticker
                and prior.get("probability") is not None
            ):
                delta = p - prior["probability"]
                if abs(delta) >= 0.001:
                    add(
                        "PROBABILITY",
                        "Experimental forecast changed",
                        "Model probability changed; estimates remain unvalidated.",
                        now,
                        change_probability=delta,
                        up_probability=p,
                    )
            ratio = state.get("model", {}).get("analysis_24h", {}).get("volatility_ratio")
            if ratio is not None and ratio >= 1.5 and not prior.get("high_volatility"):
                add(
                    "VOLATILITY",
                    "Higher observed volatility",
                    "Recent volatility exceeds its 24-hour comparison; no direction implied.",
                    "high",
                    volatility_ratio=ratio,
                )
        self.previous = {
            "ticker": ticker,
            "stale": stale,
            "probability": engine.get("up"),
            "remaining_seconds": state.get("remaining_seconds", 901),
            "high_volatility": (
                state.get("model", {}).get("analysis_24h", {}).get("volatility_ratio") or 0
            )
            >= 1.5,
        }
        return list(self.events)
