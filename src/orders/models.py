"""Database-facing order reconciliation models."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Order:
    client_order_id: str
    market_id: str
    side: str
    action: str
    order_type: str
    requested_quantity: float
    submission_fingerprint: str
    environment: str = "paper"
    strategy: Optional[str] = None
    limit_price: Optional[float] = None
    position_id: Optional[int] = None
    parent_order_id: Optional[int] = None
    id: Optional[int] = None
    exchange_order_id: Optional[str] = None
    state: str = "locally_created"
    filled_quantity: float = 0.0
    remaining_quantity: Optional[float] = None
    vwap_fill_price: Optional[float] = None
    fees: float = 0.0
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class OrderFill:
    exchange_fill_id: str
    exchange_order_id: str
    quantity: float
    price: float
    filled_at: datetime
    exchange_trade_id: Optional[str] = None
    fee: float = 0.0
    is_taker: Optional[bool] = None
    raw_response: Optional[str] = None
    id: Optional[int] = None
    order_id: Optional[int] = None
