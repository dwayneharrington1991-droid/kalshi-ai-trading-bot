from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionQuote:
    side: str
    requested_quantity: float
    fillable_quantity: float
    best_price: float | None
    average_price: float | None
    slippage_dollars: float | None


def executable_quote(orderbook_response: Any, side: str, quantity: float,
                     *, maximum_price: float = 0.99) -> ExecutionQuote:
    """Walk the opposite-side bids that are executable for a YES/NO buy."""
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
        raise ValueError("quantity must be positive")
    side = side.upper()
    if side not in {"YES", "NO"} or not 0 < maximum_price < 1:
        raise ValueError("side and maximum price must be valid")
    if not isinstance(orderbook_response, dict):
        return ExecutionQuote(side, quantity, 0, None, None, None)
    book = orderbook_response.get("orderbook_fp", orderbook_response.get("orderbook"))
    if not isinstance(book, dict):
        return ExecutionQuote(side, quantity, 0, None, None, None)
    levels = book.get("no_dollars" if side == "YES" else "yes_dollars")
    if levels is None:
        levels = book.get("no" if side == "YES" else "yes")
    if not isinstance(levels, list):
        return ExecutionQuote(side, quantity, 0, None, None, None)
    parsed = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return ExecutionQuote(side, quantity, 0, None, None, None)
        try:
            opposing_bid, available = float(level[0]), float(level[1])
        except (TypeError, ValueError):
            return ExecutionQuote(side, quantity, 0, None, None, None)
        if opposing_bid > 1:
            opposing_bid /= 100
        price = 1 - opposing_bid
        if not 0 < price < 1 or available < 0:
            return ExecutionQuote(side, quantity, 0, None, None, None)
        if price <= maximum_price:
            parsed.append((price, available))
    parsed.sort()
    remaining = float(quantity)
    cost = 0.0
    filled = 0.0
    for price, available in parsed:
        take = min(remaining, available)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if not parsed or filled <= 0:
        return ExecutionQuote(side, quantity, 0, None, None, None)
    best = parsed[0][0]
    average = cost / filled
    slippage = max(0.0, average - best) * filled
    return ExecutionQuote(side, quantity, filled, best, average, slippage)
