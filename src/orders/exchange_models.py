"""Normalized, provider-facing representations of Kalshi portfolio data."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional


def _decimal(value: Any, field: str, default: Optional[Decimal] = None) -> Decimal:
    if value in (None, "") and default is not None:
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid {field}: {value!r}")
    return result


def _timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float, Decimal)):
        numeric = float(value)
        if abs(numeric) >= 100_000_000_000:  # millisecond Unix timestamp
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _price(data: Mapping[str, Any], side: str) -> Decimal:
    dollar_key = "yes_price_dollars" if side == "YES" else "no_price_dollars"
    cent_key = "yes_price" if side == "YES" else "no_price"
    if data.get(dollar_key) not in (None, ""):
        return _decimal(data[dollar_key], dollar_key)
    if data.get(cent_key) not in (None, ""):
        return _decimal(data[cent_key], cent_key) / Decimal("100")
    if data.get("price") not in (None, ""):
        return _decimal(data["price"], "price")
    raise ValueError("Exchange response contains no usable price")


def _fee(data: Mapping[str, Any]) -> Decimal:
    if data.get("fee_cost_dollars") not in (None, ""):
        return _decimal(data["fee_cost_dollars"], "fee_cost_dollars")
    if data.get("fee_cost") not in (None, ""):
        return _decimal(data["fee_cost"], "fee_cost") / Decimal("100")
    return Decimal("0")


def _outcome_side(data: Mapping[str, Any]) -> str:
    explicit = str(data.get("outcome_side") or data.get("purchased_side") or "").lower()
    if explicit in {"yes", "no"}:
        return explicit.upper()
    side = str(data.get("side") or "").lower()
    action = str(data.get("action") or "buy").lower()
    if side in {"yes", "no"}:
        return (side if action == "buy" else ("no" if side == "yes" else "yes")).upper()
    if side in {"bid", "ask"}:
        return ("YES" if side == "bid" else "NO")
    raise ValueError(f"Unknown exchange side: {side!r}")


@dataclass(frozen=True)
class ExchangeOrder:
    order_id: str
    client_order_id: str
    market_id: str
    side: str
    action: str
    status: str
    initial_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    price: Optional[Decimal]
    fees: Decimal
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    expiration_at: Optional[datetime]
    raw: Mapping[str, Any]

    @classmethod
    def from_kalshi(cls, data: Mapping[str, Any]) -> "ExchangeOrder":
        order_id = str(data.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("Kalshi order is missing order_id")
        side = _outcome_side(data)
        initial = _decimal(
            data.get("initial_count_fp", data.get("initial_count", data.get("count_fp", data.get("count")))),
            "initial_quantity",
        )
        filled = _decimal(data.get("fill_count_fp", data.get("fill_count")), "filled_quantity", Decimal("0"))
        remaining = _decimal(
            data.get("remaining_count_fp", data.get("remaining_count")),
            "remaining_quantity",
            max(initial - filled, Decimal("0")),
        )
        fee = sum(
            (_decimal(data.get(key), key, Decimal("0")) for key in ("maker_fees_dollars", "taker_fees_dollars")),
            Decimal("0"),
        )
        price_keys = (
            ("yes_price_dollars", "yes_price")
            if side == "YES" else ("no_price_dollars", "no_price")
        ) + ("price",)
        price = (
            _price(data, side)
            if any(data.get(key) not in (None, "") for key in price_keys)
            else None
        )
        return cls(
            order_id=order_id,
            client_order_id=str(data.get("client_order_id") or ""),
            market_id=str(data.get("ticker") or data.get("market_ticker") or ""),
            side=side,
            action=str(data.get("action") or "buy").lower(),
            status=str(data.get("status") or "unknown").lower(),
            initial_quantity=initial,
            filled_quantity=filled,
            remaining_quantity=remaining,
            price=price,
            fees=fee,
            created_at=_timestamp(
                data.get("created_time", data.get("created_ts_ms", data.get("created_ts")))
            ),
            updated_at=_timestamp(
                data.get("last_update_time", data.get("updated_ts_ms", data.get("updated_time")))
            ),
            expiration_at=_timestamp(
                data.get("expiration_time", data.get("expiration_ts_ms", data.get("expiration_ts")))
            ),
            raw=dict(data),
        )


@dataclass(frozen=True)
class ExchangeFill:
    fill_id: str
    trade_id: Optional[str]
    order_id: str
    market_id: str
    side: str
    action: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    is_taker: Optional[bool]
    filled_at: datetime
    raw: Mapping[str, Any]

    @classmethod
    def from_kalshi(cls, data: Mapping[str, Any]) -> "ExchangeFill":
        fill_id = str(data.get("fill_id") or data.get("trade_id") or "").strip()
        order_id = str(data.get("order_id") or "").strip()
        if not fill_id or not order_id:
            raise ValueError("Kalshi fill is missing fill_id/trade_id or order_id")
        side = _outcome_side(data)
        timestamp = _timestamp(data.get("created_time", data.get("ts_ms", data.get("ts"))))
        if timestamp is None:
            raise ValueError("Kalshi fill is missing timestamp")
        return cls(
            fill_id=fill_id,
            trade_id=str(data.get("trade_id")) if data.get("trade_id") else None,
            order_id=order_id,
            market_id=str(data.get("ticker") or data.get("market_ticker") or ""),
            side=side,
            action=str(data.get("action") or "buy").lower(),
            quantity=_decimal(data.get("count_fp", data.get("count")), "fill quantity"),
            price=_price(data, side),
            fee=_fee(data),
            is_taker=data.get("is_taker") if isinstance(data.get("is_taker"), bool) else None,
            filled_at=timestamp,
            raw=dict(data),
        )
