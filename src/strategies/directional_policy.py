"""Conservative, side-aware admission policy for directional entries."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
from typing import Any, Optional


@dataclass(frozen=True)
class DirectionalEvaluation:
    market_id: str
    side: str
    market_implied_probability: float
    estimated_probability: float
    gross_edge: float
    spread: float
    estimated_fees: float
    estimated_slippage: float
    estimated_net_return: float
    preferred_band: bool
    accepted: bool
    reason: str
    ranking_score: float
    sports_phase: str = "NOT_APPLICABLE"
    event_title: str = ""
    market_type: str = "OTHER"
    liquidity: float = 0.0
    existing_correlated_exposure: float = 0.0

    def safe_metadata(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_directional_candidate(
    *,
    market_id: str,
    predicted_yes_probability: float,
    yes_bid: float,
    yes_ask: float,
    no_bid: float,
    no_ask: float,
    min_probability: float,
    max_preferred_probability: float,
    min_edge: float,
    fee_estimate: float,
    slippage_estimate: float,
    sports_phase: str = "NOT_APPLICABLE",
    event_title: str = "",
    market_type: str = "OTHER",
) -> DirectionalEvaluation:
    """Choose the better side and fail closed unless it clears every quality gate."""
    values = (predicted_yes_probability, yes_bid, yes_ask, no_bid, no_ask)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return _blocked(market_id, "UNKNOWN", "malformed probability or quote", sports_phase)
    if not 0 <= predicted_yes_probability <= 1:
        return _blocked(market_id, "UNKNOWN", "model probability outside 0%-100%", sports_phase)
    if not (0 < yes_ask < 1 and 0 < no_ask < 1 and 0 <= yes_bid <= yes_ask and 0 <= no_bid <= no_ask):
        return _blocked(market_id, "UNKNOWN", "valid live bid/ask unavailable", sports_phase)

    choices = []
    for side, estimated, bid, ask in (
        ("YES", predicted_yes_probability, yes_bid, yes_ask),
        ("NO", 1 - predicted_yes_probability, no_bid, no_ask),
    ):
        spread = ask - bid
        gross_edge = estimated - ask
        net_return = gross_edge - spread - fee_estimate - slippage_estimate
        choices.append((net_return, side, estimated, bid, ask, spread, gross_edge))
    net_return, side, estimated, _, ask, spread, gross_edge = max(choices, key=lambda item: item[0])
    preferred = min_probability <= estimated <= max_preferred_probability and min_probability <= ask <= max_preferred_probability

    if estimated < min_probability:
        accepted, reason = False, f"model side probability {estimated:.1%} below preferred minimum {min_probability:.1%}"
    elif ask < min_probability:
        accepted, reason = False, f"market-implied side probability {ask:.1%} below preferred minimum {min_probability:.1%}"
    elif gross_edge < min_edge:
        accepted, reason = False, f"gross edge {gross_edge:.1%} below minimum {min_edge:.1%}"
    elif net_return <= 0:
        accepted, reason = False, "estimated return is not positive after spread, fees, and slippage"
    else:
        accepted = True
        reason = "preferred probability band with positive net edge" if preferred else "above preferred band but independently justified by positive net edge"

    # A band bonus makes 65%-90% candidates rank ahead of otherwise comparable
    # extreme favorites, while net return remains the primary economic signal.
    ranking_score = net_return + (0.05 if preferred else 0.0) if accepted else float("-inf")
    return DirectionalEvaluation(
        market_id, side, ask, estimated, gross_edge, spread,
        fee_estimate, slippage_estimate, net_return, preferred,
        accepted, reason, ranking_score, sports_phase, event_title, market_type,
    )


def log_directional_evaluation(logger: logging.Logger, result: DirectionalEvaluation) -> None:
    logger.info(
        "DIRECTIONAL_CANDIDATE market=%s side=%s implied=%.4f estimated=%.4f "
        "edge=%.4f price=%.4f estimated_profit=%.4f net_return=%.4f net_ev=%.4f sports_phase=%s phase=%s "
        "event=%s market_type=%s liquidity=%.2f correlated_exposure=%.2f outcome=%s reason=%s metadata=%s",
        result.market_id, result.side, result.market_implied_probability,
        result.estimated_probability, result.gross_edge,
        result.market_implied_probability, result.estimated_net_return,
        result.estimated_net_return, result.estimated_net_return, result.sports_phase,
        result.sports_phase, result.event_title,
        result.market_type, result.liquidity, result.existing_correlated_exposure,
        "ACCEPTED" if result.accepted else "REJECTED",
        result.reason, result.safe_metadata(),
    )


def _blocked(market_id: str, side: str, reason: str, sports_phase: str = "NOT_APPLICABLE") -> DirectionalEvaluation:
    return DirectionalEvaluation(
        market_id, side, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        0.0, False, False, reason, float("-inf"), sports_phase,
    )


def classify_market_phase(
    market_info: dict[str, Any], category: str = "", *,
    expiration_ts: Optional[float] = None, now: Optional[datetime] = None,
    fast_live_minutes: float = 60.0,
) -> str:
    """Classify every open market; short-duration markets are FAST_LIVE."""
    now = now or datetime.now(timezone.utc)
    if expiration_ts is None:
        for key in ("expected_expiration_time", "expiration_time", "close_time"):
            value = market_info.get(key)
            if not isinstance(value, str) or not value:
                continue
            try:
                expiration_ts = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                break
            except ValueError:
                continue
    if expiration_ts is not None:
        remaining = expiration_ts - now.timestamp()
        if 0 <= remaining <= fast_live_minutes * 60:
            return "FAST_LIVE"
    live_markers = (
        market_info.get("in_play"), market_info.get("is_live"),
        str(market_info.get("game_status", "")).casefold() in {"live", "in_progress"},
    )
    return "LIVE" if any(value is True for value in live_markers) else "PRE_GAME"


def classify_sports_phase(market_info: dict[str, Any], category: str = "") -> str:
    """Backward-compatible sports classifier used by existing callers/tests."""
    is_sports = category.casefold() in {"sports", "esports"} or bool(market_info.get("_is_sports_market"))
    return classify_market_phase(market_info, category) if is_sports else "NOT_APPLICABLE"


def executable_liquidity(orderbook_response: Any, side: str, max_price: float) -> float:
    """Return contracts available at or better than a buy limit, failing closed."""
    if not isinstance(orderbook_response, dict):
        return 0.0
    book = orderbook_response.get("orderbook_fp", orderbook_response.get("orderbook"))
    if not isinstance(book, dict):
        return 0.0
    source = book.get("no_dollars" if side.upper() == "YES" else "yes_dollars")
    if source is None:
        source = book.get("no" if side.upper() == "YES" else "yes")
    if not isinstance(source, list):
        return 0.0
    available = 0.0
    for level in source:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return 0.0
        try:
            opposing_bid, quantity = float(level[0]), float(level[1])
        except (TypeError, ValueError):
            return 0.0
        if opposing_bid > 1:
            opposing_bid /= 100
        if quantity < 0:
            return 0.0
        if 1 - opposing_bid <= max_price + 1e-9:
            available += quantity
    return available
