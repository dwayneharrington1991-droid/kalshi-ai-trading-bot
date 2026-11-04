"""Deterministic, optional news metadata for the signals-only monitor.

Headlines are untrusted data.  This module never follows instructions in text,
never turns a headline into a probability, and fails closed when provenance or
timestamps cannot be verified.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .model import timestamp


HIGH_URGENCY = ("fomc", "federal funds", "interest rate", "emergency", "cpi")
BTC_RELEVANT = ("interest rate", "inflation", "financial stability", "liquidity", "fomc")
RISK_OFF = ("rate increase", "rate hike", "inflation", "financial instability")
RISK_ON = ("rate reduction", "rate cut", "disinflation", "liquidity facility")


def classify(feed, now: datetime | None = None, *, max_age_seconds: int = 3600):
    """Classify verified public-release metadata without making a market call."""
    now = now or datetime.now(timezone.utc)
    data = feed.get("data", {}) if isinstance(feed, dict) else {}
    source = data.get("name")
    output = []
    rejected = {"invalid_or_unverified_timestamp": 0, "stale": 0, "future": 0}
    for raw in data.get("items", []) if isinstance(data, dict) else []:
        try:
            published = timestamp(raw["published_at"])
            age = (now - published).total_seconds()
        except (KeyError, TypeError, ValueError, AttributeError):
            rejected["invalid_or_unverified_timestamp"] += 1
            continue
        if age < 0:
            rejected["future"] += 1
            continue
        if age > max_age_seconds:
            rejected["stale"] += 1
            continue
        headline = str(raw.get("headline_untrusted", ""))[:240]
        lowered = headline.casefold()
        relevance = "HIGH" if any(k in lowered for k in BTC_RELEVANT) else "LOW"
        urgency = "HIGH" if age <= 900 and any(k in lowered for k in HIGH_URGENCY) else "LOW"
        sentiment = "RISK_OFF" if any(k in lowered for k in RISK_OFF) else (
            "RISK_ON" if any(k in lowered for k in RISK_ON) else "NEUTRAL"
        )
        output.append({
            "source": source,
            "published_at": published.isoformat(),
            "observed_at": data.get("observed_at"),
            "age_seconds": round(age, 3),
            "headline_untrusted": headline,
            "relevance": relevance,
            "urgency": urgency,
            "sentiment_context": sentiment,
            "timestamp_verified": True,
            "actionable": False,
            "trusted_as_instructions": False,
        })
    return {
        "status": "AVAILABLE" if output else "UNAVAILABLE",
        "source": source,
        "items": output,
        "rejected": rejected,
        "nonblocking": True,
        "model_role": "context/shadow only; no probability or trading action",
        "limitations": (
            "Optional Federal Reserve public releases only; not universal news coverage. "
            "Headline sentiment is a deterministic context label and has no validated predictive effect."
        ),
    }
