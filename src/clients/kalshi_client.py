"""
Kalshi API client for trading operations.
Handles authentication, market data, and trade execution.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Any, Union
from urllib.parse import urlencode
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.config.settings import settings
from src.orders.exchange_models import ExchangeFill, ExchangeOrder
from src.utils.logging_setup import TradingLoggerMixin


class KalshiAPIError(Exception):
    """Custom exception for Kalshi API errors."""
    pass


class KalshiClient(TradingLoggerMixin):
    """
    Kalshi API client for automated trading.
    Handles authentication, market data retrieval, and trade execution.
    """
    
    ENVIRONMENT_URLS = {
        "production": "https://external-api.kalshi.com",
        "demo": "https://external-api.demo.kalshi.co",
    }

    def __init__(
        self, 
        api_key: Optional[str] = None, 
        private_key_path: str = None,
        max_retries: int = 5,
        backoff_factor: float = 0.5,
        environment: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """
        Initialize Kalshi client.
        
        Args:
            api_key: Kalshi API key (Key ID from the API key generation)
            private_key_path: Path to private key file
            max_retries: Maximum number of retries for failed requests
            backoff_factor: Factor for exponential backoff
        """
        self.api_key = api_key or settings.api.kalshi_api_key
        self.environment = (environment or getattr(settings.api, "kalshi_environment", "production")).lower()
        if self.environment not in self.ENVIRONMENT_URLS:
            raise ValueError("Kalshi environment must be 'demo' or 'production'")
        configured_url = getattr(settings.api, "kalshi_base_url", None)
        self.base_url = (base_url or (
            self.ENVIRONMENT_URLS[self.environment]
            if environment is not None or self.environment == "demo"
            else configured_url or self.ENVIRONMENT_URLS["production"]
        )).rstrip("/")
        selected_host = urlparse(self.base_url).hostname
        production_host = urlparse(self.ENVIRONMENT_URLS["production"]).hostname
        demo_host = urlparse(self.ENVIRONMENT_URLS["demo"]).hostname
        if self.environment == "demo" and selected_host == production_host:
            raise ValueError("Demo environment cannot use the production Kalshi host")
        if self.environment == "production" and selected_host == demo_host:
            raise ValueError("Production environment cannot use the demo Kalshi host")
        self.private_key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
        self.private_key = None
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        
        # Load private key
        self._load_private_key()
        
        # HTTP client with timeouts
        self.client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20)
        )
        
        self.logger.info(
            "Kalshi client initialized",
            api_key_length=len(self.api_key) if self.api_key else 0,
            environment=self.environment,
            base_url=self.base_url,
        )
    
    def _load_private_key(self) -> None:
        """Load private key from file."""
        try:
            private_key_path = Path(self.private_key_path)
            if not private_key_path.exists():
                raise KalshiAPIError(f"Private key file not found: {self.private_key_path}")
            
            with open(private_key_path, 'rb') as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None
                )
            self.logger.info("Private key loaded successfully")
        except Exception as e:
            self.logger.error("Failed to load private key", error=str(e))
            raise KalshiAPIError(f"Failed to load private key: {e}")
    
    def _sign_request(self, timestamp: str, method: str, path: str) -> str:
        """
        Sign request using RSA PSS signing method as per Kalshi API docs.
        
        Args:
            timestamp: Request timestamp in milliseconds
            method: HTTP method
            path: Request path
        
        Returns:
            Base64 encoded signature
        """
        # Create message to sign: timestamp + method + path
        message = timestamp + method.upper() + path
        message_bytes = message.encode('utf-8')
        
        try:
            # Sign using RSA PSS as per Kalshi documentation
            signature = self.private_key.sign(
                message_bytes,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH
                ),
                hashes.SHA256()
            )
            
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            self.logger.error("Failed to sign request", error=str(e))
            raise KalshiAPIError(f"Failed to sign request: {e}")
    
    async def _make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """
        Make authenticated request to Kalshi API with retry logic.
        
        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters
            json_data: JSON request body
            require_auth: Whether authentication is required
        
        Returns:
            API response data
        """
        # Prepare request
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Add authentication headers if required
        if require_auth:
            # Get current timestamp in milliseconds
            timestamp = str(int(time.time() * 1000))
            
            # Create signature
            signature = self._sign_request(timestamp, method, endpoint)
            
            headers.update({
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": signature
            })
        
        # Prepare body
        body = None
        if json_data:
            body = json.dumps(json_data, separators=(',', ':'))
        
        # Add query parameters to URL if present
        if params:
            query_string = urlencode(params)
            url = f"{url}?{query_string}"
        
        last_exception = None
        for attempt in range(self.max_retries):
            try:
                self.logger.debug(
                    "Making API request",
                    method=method,
                    endpoint=endpoint,
                    has_auth=require_auth,
                    attempt=attempt + 1
                )
                
                # Rate limit delay to prevent 429s (200ms = 5 req/s)
                await asyncio.sleep(0.2)
                
                response = await self.client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body if body else None
                )
                
                response.raise_for_status()
                return response.json()
                
            except httpx.HTTPStatusError as e:
                last_exception = e
                # Rate limit (429) or server errors (5xx) are worth retrying
                if e.response.status_code == 429 or e.response.status_code >= 500:
                    sleep_time = self.backoff_factor * (2 ** attempt)
                    self.logger.warning(
                        f"API request failed with status {e.response.status_code}. Retrying in {sleep_time:.2f}s...",
                        endpoint=endpoint,
                        attempt=attempt + 1
                    )
                    await asyncio.sleep(sleep_time)
                else:
                    # Don't retry on other client errors (e.g., 400, 401, 404)
                    error_msg = f"HTTP {e.response.status_code}: {e.response.text}"
                    self.logger.error("API request failed without retry", error=error_msg, endpoint=endpoint)
                    raise KalshiAPIError(error_msg)
            except Exception as e:
                last_exception = e
                self.logger.warning(f"Request failed with general exception. Retrying...", error=str(e), endpoint=endpoint)
                sleep_time = self.backoff_factor * (2 ** attempt)
                await asyncio.sleep(sleep_time)
        
        raise KalshiAPIError(f"API request failed after {self.max_retries} retries: {last_exception}")
    
    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/balance")
    
    async def get_positions(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        """Get portfolio positions."""
        params = {}
        if ticker:
            params["ticker"] = ticker
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/positions", params=params)
    
    async def get_fills(
        self, ticker: Optional[str] = None, limit: int = 100,
        cursor: Optional[str] = None, order_id: Optional[str] = None,
        min_ts: Optional[int] = None, max_ts: Optional[int] = None,
        subaccount: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get one page of fills with authoritative portfolio filters."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if order_id:
            params["order_id"] = order_id
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        if subaccount is not None:
            params["subaccount"] = subaccount
        return await self._make_authenticated_request("GET", "/trade-api/v2/portfolio/fills", params=params)
    
    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get one authoritative order by exchange order ID."""
        if not order_id or not order_id.strip():
            raise ValueError("order_id is required")
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/portfolio/orders/{order_id}"
        )

    async def get_orders(
        self, ticker: Optional[str] = None, status: Optional[str] = None,
        limit: int = 100, cursor: Optional[str] = None,
        min_ts: Optional[int] = None, max_ts: Optional[int] = None,
        subaccount: Optional[int] = None, client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get one page of orders; client ID filtering is applied locally."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        if subaccount is not None:
            params["subaccount"] = subaccount
        response = await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/orders", params=params
        )
        if client_order_id:
            response = dict(response)
            response["orders"] = [
                order for order in response.get("orders", [])
                if order.get("client_order_id") == client_order_id
            ]
        return response

    async def iter_orders(self, **filters):
        """Yield normalized orders across every cursor page."""
        cursor = filters.pop("cursor", None)
        seen_cursors = set()
        seen_orders: Dict[str, ExchangeOrder] = {}
        while True:
            if cursor is not None:
                if cursor in seen_cursors:
                    raise KalshiAPIError(f"Repeated orders cursor: {cursor}")
                seen_cursors.add(cursor)
            response = await self.get_orders(cursor=cursor, **filters)
            if not isinstance(response, dict) or not isinstance(response.get("orders", []), list):
                raise KalshiAPIError("Malformed orders page")
            if "cursor" not in response:
                raise KalshiAPIError("Orders page is missing its pagination cursor")
            for raw_order in response.get("orders", []):
                order = ExchangeOrder.from_kalshi(raw_order)
                previous = seen_orders.get(order.order_id)
                if previous is not None:
                    if previous != order:
                        raise KalshiAPIError(f"Conflicting duplicate order: {order.order_id}")
                    continue
                seen_orders[order.order_id] = order
                yield order
            cursor = response["cursor"]
            if cursor is not None and not isinstance(cursor, str):
                raise KalshiAPIError("Orders page has an invalid pagination cursor")
            if not cursor:
                break

    async def get_all_orders(self, **filters) -> List[ExchangeOrder]:
        return [order async for order in self.iter_orders(**filters)]

    async def iter_fills(self, **filters):
        """Yield normalized fills across every cursor page."""
        cursor = filters.pop("cursor", None)
        seen_cursors = set()
        seen_fills: Dict[str, ExchangeFill] = {}
        while True:
            if cursor is not None:
                if cursor in seen_cursors:
                    raise KalshiAPIError(f"Repeated fills cursor: {cursor}")
                seen_cursors.add(cursor)
            response = await self.get_fills(cursor=cursor, **filters)
            if not isinstance(response, dict) or not isinstance(response.get("fills", []), list):
                raise KalshiAPIError("Malformed fills page")
            if "cursor" not in response:
                raise KalshiAPIError("Fills page is missing its pagination cursor")
            for raw_fill in response.get("fills", []):
                fill = ExchangeFill.from_kalshi(raw_fill)
                previous = seen_fills.get(fill.fill_id)
                if previous is not None:
                    if previous != fill:
                        raise KalshiAPIError(f"Conflicting duplicate fill: {fill.fill_id}")
                    continue
                seen_fills[fill.fill_id] = fill
                yield fill
            cursor = response["cursor"]
            if cursor is not None and not isinstance(cursor, str):
                raise KalshiAPIError("Fills page has an invalid pagination cursor")
            if not cursor:
                break

    async def get_all_fills(self, **filters) -> List[ExchangeFill]:
        return [fill async for fill in self.iter_fills(**filters)]
    
    async def get_markets(
        self,
        limit: int = 100,
        cursor: Optional[str] = None,
        event_ticker: Optional[str] = None,
        series_ticker: Optional[str] = None,
        status: Optional[str] = None,
        tickers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get markets data.
        
        Args:
            limit: Maximum number of markets to return
            cursor: Pagination cursor
            event_ticker: Filter by event ticker
            series_ticker: Filter by series ticker
            status: Filter by market status
            tickers: List of specific tickers to fetch
        
        Returns:
            Markets data
        """
        params = {"limit": limit}
        
        if cursor:
            params["cursor"] = cursor
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = ",".join(tickers)
        
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/markets", params=params, require_auth=True
        )
    
    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Get specific market data."""
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}", require_auth=False
        )
    
    async def get_orderbook(self, ticker: str, depth: int = 100) -> Dict[str, Any]:
        """
        Get market orderbook.
        
        Args:
            ticker: Market ticker
            depth: Orderbook depth
        
        Returns:
            Orderbook data
        """
        params = {"depth": depth}
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}/orderbook", params=params, require_auth=False
        )
    
    async def get_market_history(
        self,
        ticker: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get market price history.
        
        Args:
            ticker: Market ticker
            start_ts: Start timestamp
            end_ts: End timestamp
            limit: Number of records to return
        
        Returns:
            Price history data
        """
        params = {"limit": limit}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        
        return await self._make_authenticated_request(
            "GET", f"/trade-api/v2/markets/{ticker}/history", params=params, require_auth=False
        )
    
    async def place_order(
        self,
        ticker: str,
        client_order_id: str,
        side: str,
        action: str,
        count: int,
        type_: str = "market",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        expiration_ts: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Place a trading order through Kalshi's Create Order V2 endpoint.

        Existing callers may continue passing outcome-side orders such as
        ``side="yes", action="buy"`` or ``side="no", action="buy"``.
        They are converted to Kalshi's single YES-side bid/ask order book.
        """
        side_normalized = side.lower()
        action_normalized = action.lower()

        if side_normalized not in {"yes", "no"}:
            raise ValueError("side must be 'yes' or 'no'")
        if action_normalized not in {"buy", "sell"}:
            raise ValueError("action must be 'buy' or 'sell'")

        # Callers in this project sometimes pass count as text. Normalize it
        # before any numeric comparison or fixed-point formatting.
        try:
            count_value = Decimal(str(count))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"count must be numeric; got {count!r}") from exc

        if not count_value.is_finite() or count_value <= 0:
            raise ValueError("count must be a finite number greater than zero")

        # Create Order V2 quotes every order from the YES side:
        # bid = buy YES; ask = sell YES.
        if side_normalized == "yes":
            if yes_price is None:
                raise ValueError("yes_price is required for YES orders")
            yes_price_cents = int(yes_price)
            book_side = "bid" if action_normalized == "buy" else "ask"
        else:
            if no_price is None:
                raise ValueError("no_price is required for NO orders")
            no_price_cents = int(no_price)
            yes_price_cents = 100 - no_price_cents
            book_side = "ask" if action_normalized == "buy" else "bid"

        if not 0 < yes_price_cents < 100:
            raise ValueError(
                "Converted YES price must be between 1 and 99 cents; "
                f"got {yes_price_cents}"
            )

        order_data = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": book_side,
            "count": f"{count_value:.2f}",
            "price": f"{yes_price_cents / 100:.4f}",
            "time_in_force": (
                "immediate_or_cancel"
                if type_.lower() == "market"
                else "good_till_canceled"
            ),
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": action_normalized == "sell",
            "subaccount": 0,
            "exchange_index": 0,
        }

        if expiration_ts:
            order_data["expiration_time"] = int(expiration_ts)

        response = await self._make_authenticated_request(
            "POST",
            "/trade-api/v2/portfolio/events/orders",
            json_data=order_data,
        )

        # Preserve the response shape expected by the rest of this project.
        return response if "order" in response else {"order": response}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order through Kalshi's Cancel Order V2 endpoint."""
        response = await self._make_authenticated_request(
            "DELETE",
            f"/trade-api/v2/portfolio/events/orders/{order_id}",
        )
        return response if "order" in response else {"order": response}

    async def get_trades(
        self,
        ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get trade history.
        
        Args:
            ticker: Filter by ticker
            limit: Maximum number of trades to return
            cursor: Pagination cursor
        
        Returns:
            Trades data
        """
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        
        return await self._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/trades", params=params
        )
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
        self.logger.info("Kalshi client closed")
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
