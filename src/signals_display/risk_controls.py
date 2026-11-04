"""Advisory-only risk controls for the signals dashboard.

These controls can suppress a displayed signal.  They have no dependency on an
execution client and cannot place, amend, or cancel an order.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

from .model import timestamp


DEFAULT_DAILY_DRAWDOWN_WARNING = 0.03


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def daily_drawdown(account, now=None, *, threshold=DEFAULT_DAILY_DRAWDOWN_WARNING):
    """Estimate today's realized drawdown from confirmed closed account cycles.

    Current cash and confirmed realized P&L do not prove start-of-day equity when
    deposits, withdrawals, or open positions exist.  The result therefore stays
    explicitly estimated and never invents a zero drawdown when evidence is
    incomplete.
    """
    now = now or datetime.now(timezone.utc)
    if not 0 < threshold < 1:
        raise ValueError("daily drawdown threshold must be between zero and one")
    if account.get("status") != "READ_ONLY_CONNECTED":
        return {
            "status": "UNAVAILABLE",
            "warning": False,
            "threshold": threshold,
            "reason": "Verified read-only account data is unavailable",
        }
    cash = account.get("available_balance_dollars")
    if not _finite(cash) or cash < 0:
        return {
            "status": "UNAVAILABLE",
            "warning": False,
            "threshold": threshold,
            "reason": "Current cash balance is unavailable",
        }
    values = []
    for trade in account.get("completed_trades", []):
        try:
            closed = timestamp(trade["closed_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return {
                "status": "UNAVAILABLE",
                "warning": False,
                "threshold": threshold,
                "reason": "Completed-trade timestamps are incomplete",
            }
        if closed.date() != now.astimezone(timezone.utc).date():
            continue
        pnl = trade.get("realized_pnl")
        if not _finite(pnl):
            return {
                "status": "UNAVAILABLE",
                "warning": False,
                "threshold": threshold,
                "reason": "A confirmed same-day trade has unavailable net P&L or fees",
            }
        values.append(float(pnl))
    realized = sum(values)
    estimated_starting_cash = float(cash) - realized
    if estimated_starting_cash <= 0:
        return {
            "status": "UNAVAILABLE",
            "warning": False,
            "threshold": threshold,
            "reason": "Start-of-day cash cannot be estimated safely",
        }
    fraction = max(0.0, -realized / estimated_starting_cash)
    return {
        "status": "ESTIMATED" if values else "NO_CONFIRMED_CLOSED_TRADES_TODAY",
        "warning": fraction >= threshold,
        "threshold": threshold,
        "realized_pnl_today": realized,
        "estimated_starting_cash": estimated_starting_cash,
        "drawdown_fraction": fraction,
        "confirmed_closed_trades_today": len(values),
        "reason": (
            "Estimated from current cash and confirmed realized P&L; deposits, "
            "withdrawals, and open-position value are not reconstructed"
        ),
    }


def signal_kill_switch(state, *, drawdown_threshold=DEFAULT_DAILY_DRAWDOWN_WARNING):
    """Return the complete fail-closed advisory signal suppression state."""
    account = state.get("account") or {}
    drawdown = daily_drawdown(account, threshold=drawdown_threshold)
    workers = ((state.get("feed_health") or {}).get("workers") or {})
    reasons = []
    if state.get("emergency_signal_kill_switch") is True:
        reasons.append("OPERATOR_EMERGENCY_SIGNAL_KILL_SWITCH")
    if state.get("stale", True):
        reasons.append("STALE_OR_INCOMPLETE_DATA")
    if workers and workers.get("healthy") is False:
        reasons.append("BACKGROUND_WORKER_UNHEALTHY")
    if drawdown.get("warning"):
        reasons.append("ESTIMATED_DAILY_DRAWDOWN_AT_OR_ABOVE_3_PERCENT")
    return {
        "active": bool(reasons),
        "reasons": reasons,
        "daily_drawdown": drawdown,
        "automatic_execution_allowed": False,
        "scope": "Display-signal suppression and warning only",
    }
