"""
Unified Advanced Trading System - The Beast Mode 🚀

This system orchestrates all cutting-edge strategies:
1. Market Making Strategy (limit orders for spreads)
2. Advanced Portfolio Optimization (Kelly Criterion Extension)
3. Risk Parity Allocation (equal risk, not equal capital)
4. Dynamic Exit Strategies (time-based, confidence-based, volatility-based)
5. Cross-Market Arbitrage Detection
6. Multi-Model AI Ensemble

The goal: Use ALL available capital optimally across the BEST opportunities
with sophisticated risk management and dynamic rebalancing.

Key innovations:
- No time restrictions (trade any deadline with smart exits)
- Market making for spread profits without directional risk
- Portfolio optimization using latest Kelly Criterion research
- Real-time rebalancing based on market conditions
- Maximum capital utilization through diverse strategies
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
import numpy as np

from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.utils.database import DatabaseManager, Market, Position
from src.config.settings import settings
from src.utils.logging_setup import get_trading_logger
from src.orders.readiness import (
    CheckStatus, ReadinessCheck, ReadinessContext, ReadinessReport,
    SubmissionReadinessEvaluator,
)
from src.orders.repository import OrderRepository
from src.orders.decision_log import log_order_decision
from src.utils.market_prices import get_market_prices

from src.strategies.market_making import (
    AdvancedMarketMaker, 
    MarketMakingOpportunity,
    run_market_making_strategy
)
from src.strategies.portfolio_optimization import (
    AdvancedPortfolioOptimizer, 
    MarketOpportunity, 
    PortfolioAllocation,
    run_portfolio_optimization,
    create_market_opportunities_from_markets
)
from src.strategies.directional_policy import (
    evaluate_directional_candidate,
    classify_market_phase,
    log_directional_evaluation,
)
@dataclass
class TradingSystemConfig:
    """Configuration for the unified trading system."""
    # Capital allocation across strategies
    # Quick flip strategy REMOVED — 0% win rate, -$208 in 12 trades.
    # Reallocated to directional (ensemble-driven) trading.
    market_making_allocation: float = 0.30  # 30% for market making
    directional_trading_allocation: float = 0.70  # 70% for directional positions (AI ensemble)
    arbitrage_allocation: float = 0.00      # 0% for arbitrage opportunities
    
    # Risk management
    max_portfolio_volatility: float = 0.20  # 20% max portfolio vol
    max_correlation_exposure: float = 0.70  # Max 70% in correlated positions
    max_single_position: float = 0.15  # Max 15% in any single position
    
    # Performance targets
    target_sharpe_ratio: float = 2.0
    target_annual_return: float = 0.30  # 30% annual target
    max_drawdown_limit: float = 0.15  # 15% max drawdown
    
    # Rebalancing
    rebalance_frequency_hours: int = 6  # Rebalance every 6 hours
    profit_taking_threshold: float = 0.25  # Take profits at 25%
    loss_cutting_threshold: float = 0.10  # Cut losses at 10%


@dataclass
class TradingSystemResults:
    """Results from unified trading system execution."""
    # Market making results
    market_making_orders: int = 0
    market_making_exposure: float = 0.0
    market_making_expected_profit: float = 0.0
    
    # Directional trading results
    directional_positions: int = 0
    directional_exposure: float = 0.0
    directional_expected_return: float = 0.0
    
    # Portfolio metrics
    total_capital_used: float = 0.0
    portfolio_expected_return: float = 0.0
    portfolio_sharpe_ratio: float = 0.0
    portfolio_volatility: float = 0.0
    
    # Risk metrics
    max_portfolio_drawdown: float = 0.0
    correlation_score: float = 0.0
    diversification_ratio: float = 0.0
    
    # Performance
    total_positions: int = 0
    capital_efficiency: float = 0.0  # % of capital used
    expected_annual_return: float = 0.0


class UnifiedAdvancedTradingSystem:
    """
    The Beast Mode Trading System 🚀
    
    This orchestrates all advanced strategies to maximize returns while
    optimally using ALL available capital with sophisticated risk management.
    
    Strategy allocation:
    1. Market Making (40%): Profit from spreads without directional risk
    2. Directional Trading (50%): Take positions based on AI edge
    3. Arbitrage (10%): Cross-market and temporal arbitrage
    
    Features:
    - No time restrictions (trade any deadline)
    - Dynamic position sizing (Kelly Criterion Extension)
    - Risk parity allocation (equal risk contribution)
    - Real-time rebalancing
    - Multi-strategy diversification
    """
    
    def __init__(
        self,
        db_manager: DatabaseManager,
        kalshi_client: KalshiClient,
        xai_client: XAIClient,
        config: Optional[TradingSystemConfig] = None
    ):
        self.db_manager = db_manager
        self.kalshi_client = kalshi_client
        self.xai_client = xai_client
        self.config = config or TradingSystemConfig()
        self.logger = get_trading_logger("unified_trading_system")
        
        # 🚨 DYNAMIC CAPITAL: Will be set by async_initialize() from actual Kalshi balance
        self.total_capital = 100  # Temporary default, will be updated by async_initialize()
        
        # OLD HARDCODED WAY (REMOVED):
        # self.total_capital = getattr(settings.trading, 'total_capital', 10000)
        self.last_rebalance = datetime.now()
        self.system_metrics = {}

        self.market_maker = None
        self.portfolio_optimizer = None
        self.multi_agent_shadow_service = None
        
        # Capital allocation will be set by async_initialize() after getting actual balance

    async def async_initialize(self):
        """
        Asynchronously initialize the trading system by fetching the current balance
        from Kalshi and setting the total capital.
        """
        try:
            # Get total portfolio value (cash + current positions)
            balance_response = await self.kalshi_client.get_balance()
            available_cash = balance_response.get('balance', 0) / 100  # Convert cents to dollars

            # Get current positions to calculate total portfolio value
            # Kalshi API v2 returns portfolio_value in balance response (in cents)
            total_position_value = balance_response.get('portfolio_value', 0) / 100  # Convert cents to dollars

            # Also log active positions for visibility
            positions_response = await self.kalshi_client.get_positions()
            event_positions = positions_response.get('event_positions', []) if isinstance(positions_response, dict) else []
            active_positions = [p for p in event_positions if float(p.get('event_exposure_dollars', '0')) > 0]
            if active_positions:
                self.logger.info(f"📊 Active positions: {len(active_positions)}")
                for pos in active_positions:
                    ticker = pos.get('event_ticker', '?')
                    exposure = float(pos.get('event_exposure_dollars', '0'))
                    pnl = float(pos.get('realized_pnl_dollars', '0'))
                    self.logger.info(f"  📌 {ticker}: exposure=${exposure:.2f}, realized_pnl=${pnl:.2f}")

            # Total portfolio value is the basis for all allocations
            total_portfolio_value = available_cash + total_position_value
            self.total_capital = total_portfolio_value

            self.logger.info(f"💰 PORTFOLIO VALUE: Cash=${available_cash:.2f} + Positions=${total_position_value:.2f} = Total=${self.total_capital:.2f}")

            if self.total_capital < 10:  # Minimum $10 to trade
                self.logger.warning(f"⚠️ Total capital too low: ${self.total_capital:.2f} - may limit trading")

        except Exception as e:
            self.logger.error(f"Failed to get portfolio value, using default: {e}")
            self.total_capital = 100  # Conservative fallback

        # Update capital allocation based on actual balance
        self.market_making_capital = self.total_capital * self.config.market_making_allocation
        self.directional_capital = self.total_capital * self.config.directional_trading_allocation
        self.arbitrage_capital = self.total_capital * self.config.arbitrage_allocation

        # Initialize strategy modules with actual capital
        self.market_maker = AdvancedMarketMaker(self.db_manager, self.kalshi_client, self.xai_client)
        self.portfolio_optimizer = AdvancedPortfolioOptimizer(self.db_manager, self.kalshi_client, self.xai_client)

        if settings.multi_agent_shadow.enabled:
            try:
                from src.agents.orchestrator import MultiAgentOrchestrator
                from src.agents.repository import AgentRepository
                from src.agents.shadow_service import MultiAgentShadowService, build_existing_agent_executors

                shadow = settings.multi_agent_shadow
                MultiAgentOrchestrator.assert_shadow_configuration(shadow.enabled, shadow.can_affect_trading)
                orchestrator = MultiAgentOrchestrator(
                    build_existing_agent_executors(self.xai_client.get_completion),
                    max_concurrency=shadow.max_concurrency,
                    timeout_seconds=shadow.timeout_seconds,
                    max_retries=shadow.max_retries,
                    min_successful_agents=shadow.min_successful_agents,
                    require_forecaster=shadow.require_forecaster,
                    require_risk=shadow.require_risk_agent,
                )
                self.multi_agent_shadow_service = MultiAgentShadowService(
                    orchestrator, AgentRepository(self.db_manager.db_path),
                    settings.api.kalshi_environment,
                )
            except Exception:
                self.multi_agent_shadow_service = None
                self.logger.exception("Multi-agent shadow initialization failed; trading path unchanged")

        self.logger.info(f"🎯 CAPITAL ALLOCATION: Market Making=${self.market_making_capital:.2f}, Directional=${self.directional_capital:.2f}, Arbitrage=${self.arbitrage_capital:.2f}")

    async def execute_unified_trading_strategy(self) -> TradingSystemResults:
        """
        Execute the unified trading strategy across all approaches.
        
        Process:
        1. Analyze all available markets (no time restrictions!)
        2. Identify market making opportunities
        3. Identify directional trading opportunities  
        4. Optimize portfolio allocation using advanced Kelly Criterion
        5. Execute trades across all strategies
        6. Monitor and rebalance as needed
        """
        self.logger.info("🚀 Executing Unified Advanced Trading Strategy")
        self._readiness_logged = False
        
        try:
            # Step 0: Check and enforce position limits AND cash reserves
            from src.utils.position_limits import PositionLimitsManager
            from src.utils.cash_reserves import CashReservesManager, is_cash_emergency
            
            limits_manager = PositionLimitsManager(self.db_manager, self.kalshi_client)
            cash_manager = CashReservesManager(self.db_manager, self.kalshi_client)
            
            # Check position limits
            limits_status = await limits_manager.get_position_limits_status()
            self._readiness_limits_status = limits_status
            self.logger.info(f"📊 POSITION LIMITS STATUS: {limits_status['status']} ({limits_status['position_utilization']})")
            
            # Check cash reserves
            cash_status = await cash_manager.get_cash_status()
            self._readiness_cash_status = cash_status
            self.logger.info(f"💰 CASH RESERVES STATUS: {cash_status['status']} ({cash_status['reserve_percentage']:.1f}%)")
            
            # Handle cash emergency first (higher priority)
            if cash_status['emergency_status']:
                self.logger.warning(f"🚨 CASH EMERGENCY: {cash_status['recommendations']}")
                emergency_action = await cash_manager.handle_cash_emergency()
                if emergency_action.action_type == 'halt_trading':
                    self.logger.critical(f"🛑 TRADING HALTED DUE TO CASH EMERGENCY: {emergency_action.reason}")
                    await self._log_cycle_readiness(None, None, None)
                    return TradingSystemResults()  # Return empty results
                elif emergency_action.action_type == 'close_positions':
                    self.logger.warning(f"⚠️ Need to close {emergency_action.positions_to_close} positions for cash reserves")
            
            # Enforce position limits if needed (after cash check)
            if limits_status['status'] in ['OVER_LIMIT', 'WARNING']:
                self.logger.info(f"⚠️  Position limits enforcement needed: {limits_status['recommendations']}")
                enforcement_result = await limits_manager.enforce_position_limits()
                if enforcement_result['action'] == 'positions_closed':
                    self.logger.info(f"✅ CLOSED {enforcement_result['positions_closed']} positions to meet limits")
            
            # Step 1: Get ALL available markets (no time restrictions) - MORE PERMISSIVE VOLUME
            markets = await self.db_manager.get_eligible_markets(
            volume_min=0,  # DECREASED: Much lower volume requirement (was 50,000, now 200) for more opportunities
            max_days_to_expiry=3  # Accept any timeline with dynamic exits
        )
            if not markets:
                self.logger.warning("No markets available for trading")
                await self._log_cycle_readiness([], None, None)
                return TradingSystemResults()
            self._readiness_market_count = len(markets)
            
            self.logger.info(f"Analyzing {len(markets)} markets across all strategies")
            
            # Step 2: Parallel strategy analysis (ensemble-driven only)
            market_making_results, portfolio_allocation = await asyncio.gather(
                self._execute_market_making_strategy(markets),
                self._execute_directional_trading_strategy(markets),
            )
            
            # Step 3: Execute arbitrage opportunities
            arbitrage_results = await self._execute_arbitrage_strategy(markets)
            
            # Step 4: Compile results
            results = self._compile_unified_results(
                market_making_results, portfolio_allocation, arbitrage_results
            )
            
            # Step 4.5: Log if no positions were created (removed emergency fallback)
            if results.total_positions == 0:
                self.logger.warning("No positions created by main strategies - investigating why")
            
            # Step 5: Risk management and rebalancing
            await self._manage_risk_and_rebalance(results)
            
            self.logger.info(
                f"🎯 Unified Strategy Complete: "
                f"Capital Used: ${results.total_capital_used:.0f} ({results.capital_efficiency:.1%}), "
                f"Expected Return: {results.expected_annual_return:.1%}, "
                f"Sharpe Ratio: {results.portfolio_sharpe_ratio:.2f}, "
                f"Positions: {results.total_positions}"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"Error in unified trading strategy: {e}")
            await self._log_cycle_readiness(None, None, None)
            return TradingSystemResults()

    async def _execute_market_making_strategy(self, markets: List[Market]) -> Dict:
        """
        Execute market making strategy for spread profits.
        """
        try:
            self.logger.info(f"🎯 Executing Market Making Strategy on {len(markets)} markets")
            
            # Analyze market making opportunities
            opportunities = await self.market_maker.analyze_market_making_opportunities(markets)
            
            if not opportunities:
                self.logger.warning("No market making opportunities found")
                return {'orders_placed': 0, 'expected_profit': 0.0}
            
            # Filter to top opportunities within capital allocation
            max_opportunities = int(self.market_making_capital / 100)  # $100 per opportunity
            top_opportunities = opportunities[:max_opportunities]
            
            # Execute market making
            results = await self.market_maker.execute_market_making_strategy(top_opportunities)
            
            self.logger.info(
                f"✅ Market Making: {results['orders_placed']} orders, "
                f"${results['expected_profit']:.2f} expected profit"
            )
            
            return results
            
        except Exception as e:
            self.logger.error(f"Error in market making strategy: {e}")
            return {'orders_placed': 0, 'expected_profit': 0.0}

    async def _execute_directional_trading_strategy(self, markets: List[Market]) -> PortfolioAllocation:
        """
        Execute directional trading with advanced portfolio optimization.
        """
        try:
            self.logger.info(f"🎯 Executing Directional Trading Strategy")
            
            # Convert markets to opportunities (with immediate trading capability)
            opportunities = await create_market_opportunities_from_markets(
                markets, self.xai_client, self.kalshi_client, 
                self.db_manager, self.directional_capital
            )
            
            if not opportunities:
                self.logger.warning("No directional trading opportunities found")
                await self._log_cycle_readiness(markets, [], self.portfolio_optimizer._empty_allocation())
                return self.portfolio_optimizer._empty_allocation()

            # Observation-only: results are persisted and logged, but never
            # returned to or consumed by the optimizer/execution path.
            await self._run_multi_agent_shadow(markets, opportunities)
            
            # Filter opportunities based on available capital
            # Adjust portfolio optimizer capital
            original_capital = self.portfolio_optimizer.total_capital
            self.portfolio_optimizer.total_capital = self.directional_capital
            
            # Optimize portfolio
            allocation = await self.portfolio_optimizer.optimize_portfolio(opportunities)
            
            # Restore original capital setting
            self.portfolio_optimizer.total_capital = original_capital
            
            # DEBUG: Log allocation details before execution attempt
            self.logger.info(f"Portfolio allocation result: {len(allocation.allocations) if allocation else 0} allocations, ${allocation.total_capital_used if allocation else 0:.0f} capital used")
            await self._log_cycle_readiness(markets, opportunities, allocation)
            
            # Actually execute the trades from the allocation
            if allocation and allocation.allocations:
                self.logger.info(f"Attempting to execute {len(allocation.allocations)} allocations: {list(allocation.allocations.keys())}")
                execution_results = await self._execute_portfolio_allocations(allocation, opportunities)
                self._last_directional_execution_results = execution_results
                self.logger.info(
                    "Directional execution summary",
                    authoritative_positions_created=execution_results['positions_created'],
                    orders_pending=execution_results['orders_pending'],
                    failed_executions=execution_results['failed_executions'],
                )
            else:
                self._last_directional_execution_results = {
                    'positions_created': 0, 'orders_pending': 0,
                    'failed_executions': 0, 'total_capital_used': 0.0,
                }
                self.logger.warning(f"No allocations to execute. Allocation exists: {allocation is not None}, Has allocations: {bool(allocation and allocation.allocations)}")
            
            execution = self._last_directional_execution_results
            self.logger.info(
                "Directional trading complete",
                proposed_allocations=len(allocation.allocations),
                authoritative_positions_created=execution['positions_created'],
                orders_pending=execution['orders_pending'],
                capital_actually_used=execution['total_capital_used'],
                sharpe=allocation.portfolio_sharpe,
            )
            
            return allocation
            
        except Exception as e:
            self.logger.error(f"Error in directional trading strategy: {e}")
            await self._log_cycle_readiness(markets, None, None)
            return self.portfolio_optimizer._empty_allocation()

    async def _run_multi_agent_shadow(self, markets, opportunities) -> None:
        """Run a bounded shadow comparison; failures cannot escape into trading."""
        service = self.multi_agent_shadow_service
        if service is None:
            return
        analyzed = failed = 0
        try:
            from src.agents.models import MarketAnalysisRequest

            market_by_id = {market.market_id: market for market in markets}
            limit = settings.multi_agent_shadow.max_markets_per_cycle
            cycle_id = datetime.now(timezone.utc).isoformat()
            for opportunity in list(opportunities)[:limit]:
                market = market_by_id.get(opportunity.market_id)
                if market is None:
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                expiry = datetime.fromtimestamp(market.expiration_ts, timezone.utc)
                request = MarketAnalysisRequest(
                    request_id=MarketAnalysisRequest.deterministic_id(
                        market.market_id, timestamp,
                        settings.multi_agent_shadow.configuration_version,
                    ),
                    market_id=market.market_id, title=market.title, rules="",
                    category=market.category, yes_price=float(market.yes_price),
                    no_price=float(market.no_price), bid_ask_spread=abs(float(market.no_price) - float(market.yes_price)),
                    volume=float(market.volume), expiration_timestamp=expiry.isoformat(),
                    time_remaining_seconds=max(0, int((expiry - datetime.now(timezone.utc)).total_seconds())),
                    analysis_timestamp=timestamp,
                    configuration_version=settings.multi_agent_shadow.configuration_version,
                    single_model_analysis={
                        "probability_yes": opportunity.predicted_probability,
                        "confidence": opportunity.confidence,
                    },
                )
                try:
                    await service.analyze(request, cycle_id=cycle_id)
                    analyzed += 1
                except BaseException as exc:
                    if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                        raise
                    failed += 1
                    self.logger.error(
                        "MULTI_AGENT_SHADOW market=%s status=FAILED trading_impact=NONE",
                        market.market_id,
                    )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            self.logger.error("Multi-agent shadow cycle unavailable; trading path unchanged")
        finally:
            self.logger.info(
                "MULTI_AGENT_SHADOW cycle analyzed=%s failed=%s trading_impact=NONE",
                analyzed, failed,
            )

    async def _log_cycle_readiness(self, markets, opportunities, allocation) -> None:
        """Emit one human and one structured readiness summary for this cycle."""
        if getattr(self, "_readiness_logged", False):
            return
        self._readiness_logged = True
        try:
            await self._evaluate_and_log_cycle_readiness(markets, opportunities, allocation)
        except Exception:
            live = settings.trading.live_trading_enabled
            fallback = ReadinessReport(
                mode="LIVE" if live else "PAPER",
                overall="BLOCKED" if live else "PAPER",
                scope="cycle",
                checks=[ReadinessCheck(
                    "Diagnostic snapshot", CheckStatus.BLOCKED,
                    "cycle readiness data unavailable", required=live,
                )],
            )
            SubmissionReadinessEvaluator.log(fallback, self.logger)

    async def _evaluate_and_log_cycle_readiness(self, markets, opportunities, allocation) -> None:
        """Build the cycle snapshot; caller prevents diagnostic failures escaping."""
        limits = getattr(self, "_readiness_limits_status", {})
        cash = getattr(self, "_readiness_cash_status", {})
        allocations = allocation.allocations if allocation is not None else None
        duplicate_count = None
        if allocations is not None:
            open_position_keys = {
                (position.market_id, str(position.side).upper())
                for position in await self.db_manager.get_open_positions()
            }
            intended_keys = set()
            for market_id in allocations:
                opportunity = next((item for item in (opportunities or [])
                                    if item.market_id == market_id), None)
                intended_keys.add((market_id, opportunity.recommended_side if opportunity else "UNKNOWN"))
            duplicate_count = len(open_position_keys & intended_keys)
        evaluator = SubmissionReadinessEvaluator(OrderRepository(self.db_manager.db_path))
        report = await evaluator.evaluate(ReadinessContext(
            live_mode=settings.trading.live_trading_enabled,
            authoritative_execution_enabled=settings.trading.authoritative_live_execution_enabled,
            reconciliation_enabled=settings.trading.order_reconciliation_enabled,
            reconciliation_shadow_mode=settings.trading.reconciliation_shadow_mode,
            kill_switch=settings.trading.live_order_submission_kill_switch,
            configured_environment=settings.api.kalshi_environment,
            client_environment=getattr(self.kalshi_client, "environment", None),
            production_acknowledgement=settings.trading.production_execution_acknowledgement,
            reconciliation_max_age_seconds=settings.trading.reconciliation_health_max_age_seconds,
            total_markets=await self.db_manager.get_market_count(),
            eligible_markets=len(markets) if markets is not None else 0,
            opportunity_count=len(opportunities) if opportunities is not None else None,
            allocation_count=len(allocations) if allocations is not None else None,
            allocated_dollars=(allocation.total_capital_used if allocation is not None else None),
            cash_reserve_ok=(None if not cash else cash.get("trading_permitted", False)),
            cash_reserve_pct=cash.get("reserve_percentage"),
            position_limit_ok=(None if not limits else limits.get("status") == "HEALTHY"),
            current_positions=limits.get("current_positions"),
            max_positions=limits.get("max_positions"),
            existing_position_blocked=(None if duplicate_count is None else duplicate_count > 0),
        ), scope="cycle")
        evaluator.log(report, self.logger)

    # Quick flip strategy REMOVED — 0% win rate, -$208 across 12 trades.
    # All capital now routed to AI ensemble directional trading.

    async def _execute_portfolio_allocations(
        self, 
        allocation: PortfolioAllocation, 
        opportunities: List[MarketOpportunity]
    ) -> Dict:
        """
        Convert portfolio allocations to actual Position objects and execute them.
        """
        results = {
            'positions_created': 0,
            'orders_pending': 0,
            'total_capital_used': 0.0,
            'successful_executions': 0,
            'failed_executions': 0
        }
        
        try:
            from src.jobs.execute import execute_position
            
            for market_id, allocation_fraction in allocation.allocations.items():
                intended_side = "UNKNOWN"
                price = None
                quantity = None
                try:
                    # Find the corresponding opportunity first to determine intended side
                    opportunity = next((opp for opp in opportunities if opp.market_id == market_id), None)
                    if not opportunity:
                        self.logger.warning(f"Could not find opportunity for {market_id}")
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side="UNKNOWN", action="buy", price=None, quantity=None,
                            outcome="BLOCKED", reason="allocation has no matching opportunity",
                            risk_limit="SIGNAL_INTEGRITY", api_attempted=False,
                        )
                        continue
                    
                    # Determine the intended side based on edge direction
                    intended_side = opportunity.recommended_side
                    if intended_side not in {"YES", "NO"}:
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side="UNKNOWN", action="buy", price=None, quantity=None,
                            outcome="BLOCKED", reason="directional side was not verified",
                            risk_limit="SIGNAL_INTEGRITY", api_attempted=False,
                        )
                        continue
                    
                    # 🚨 ONLY SKIP if we already have a position on the EXACT same market_id AND side
                    existing_position = await self.db_manager.get_position_by_market_and_side(market_id, intended_side)
                    
                    if existing_position:
                        self.logger.info(f"⏭️ SKIPPING {market_id} {intended_side} - exact position already exists (likely from immediate trade)")
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side=intended_side, action="buy", price=opportunity.market_probability,
                            quantity=None, outcome="BLOCKED",
                            reason="existing local position or pending intent",
                            risk_limit="DUPLICATE_POSITION", api_attempted=False,
                        )
                        continue
                    else:
                        # Check if we have the opposite side (just for logging)
                        opposite_side = "NO" if intended_side == "YES" else "YES"
                        opposite_position = await self.db_manager.get_position_by_market_and_side(market_id, opposite_side)
                        if opposite_position:
                            self.logger.info(f"📊 {market_id} - Adding {intended_side} position (already have {opposite_side})")
                        else:
                            self.logger.info(f"📊 {market_id} - New {intended_side} position")
                    
                    # Calculate initial position size
                    initial_position_value = allocation_fraction * self.directional_capital
                    
                    # Check position limits and adjust if needed
                    from src.utils.position_limits import check_can_add_position
                    
                    can_add_position, limit_reason = await check_can_add_position(
                        initial_position_value, self.db_manager, self.kalshi_client
                    )
                    
                    if not can_add_position:
                        # Instead of blocking, try to find a smaller position size that fits
                        self.logger.info(f"⚠️ Position size ${initial_position_value:.2f} exceeds limits, attempting to reduce...")
                        
                        # Try progressively smaller position sizes
                        for reduction_factor in [0.8, 0.6, 0.4, 0.2, 0.1]:
                            reduced_position_value = initial_position_value * reduction_factor
                            can_add_reduced, reduced_reason = await check_can_add_position(
                                reduced_position_value, self.db_manager, self.kalshi_client
                            )
                            
                            if can_add_reduced:
                                initial_position_value = reduced_position_value
                                self.logger.info(f"✅ Position size reduced to ${initial_position_value:.2f} to fit limits")
                                break
                        else:
                            # If even the smallest size doesn't fit, check if it's due to position count
                            from src.utils.position_limits import PositionLimitsManager
                            limits_manager = PositionLimitsManager(self.db_manager, self.kalshi_client)
                            current_positions = await limits_manager._get_position_count()
                            
                            if current_positions >= limits_manager.max_positions:
                                self.logger.info(f"❌ POSITION COUNT LIMIT: {current_positions}/{limits_manager.max_positions} positions - cannot add new position")
                                results['failed_executions'] += 1
                                log_order_decision(
                                    self.logger, market_id=market_id, strategy="portfolio_optimization",
                                    side=intended_side, action="buy", price=opportunity.market_probability,
                                    quantity=None, outcome="BLOCKED", reason=limit_reason,
                                    risk_limit=f"MAX_POSITIONS={limits_manager.max_positions}",
                                    api_attempted=False,
                                )
                                continue
                            else:
                                self.logger.info(f"❌ POSITION SIZE LIMIT: Even minimum size ${initial_position_value * 0.1:.2f} exceeds limits")
                                results['failed_executions'] += 1
                                log_order_decision(
                                    self.logger, market_id=market_id, strategy="portfolio_optimization",
                                    side=intended_side, action="buy", price=opportunity.market_probability,
                                    quantity=None, outcome="BLOCKED", reason=limit_reason,
                                    risk_limit="POSITION_EXPOSURE", api_attempted=False,
                                )
                                continue
                    
                    position_value = initial_position_value
                    self.logger.info(f"✅ POSITION LIMITS OK FOR ALLOCATION: ${position_value:.2f}")
                    
                    # Check cash reserves for this allocation
                    from src.utils.cash_reserves import check_can_trade_with_cash_reserves
                    
                    can_trade_reserves, reserves_reason = await check_can_trade_with_cash_reserves(
                        position_value, self.db_manager, self.kalshi_client
                    )
                    
                    if not can_trade_reserves:
                        self.logger.info(f"❌ CASH RESERVES BLOCK ALLOCATION: {market_id} - {reserves_reason}")
                        results['failed_executions'] += 1
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side=intended_side, action="buy", price=opportunity.market_probability,
                            quantity=None, outcome="BLOCKED", reason=reserves_reason,
                            risk_limit="CASH_RESERVE", api_attempted=False,
                        )
                        continue
                    
                    self.logger.info(f"✅ CASH RESERVES OK FOR ALLOCATION: {market_id}")
                    
                    # Get current market data
                    market_data = await self.kalshi_client.get_market(market_id)
                    if not market_data:
                        self.logger.warning(f"Could not get market data for {market_id}")
                        results['failed_executions'] += 1
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side=intended_side, action="buy", price=None, quantity=None,
                            outcome="BLOCKED", reason="market data unavailable",
                            risk_limit="MARKET_DATA", api_attempted=False,
                        )
                        continue
                    
                    # FIXED: Extract from nested 'market' object
                    market_info = market_data.get('market', {})
                    
                    # Get price for the intended side (already determined above)
                    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market_info)
                    live_evaluation = evaluate_directional_candidate(
                        market_id=market_id,
                        predicted_yes_probability=opportunity.predicted_probability,
                        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
                        min_probability=settings.trading.min_preferred_probability,
                        max_preferred_probability=settings.trading.max_preferred_probability,
                        min_edge=settings.trading.min_directional_edge,
                        fee_estimate=settings.trading.directional_fee_estimate,
                        slippage_estimate=settings.trading.directional_slippage_estimate,
                        sports_phase=classify_market_phase(
                            market_info, opportunity.category,
                            expiration_ts=(datetime.now(timezone.utc).timestamp()
                                           + opportunity.time_to_expiry * 86400),
                        ),
                    )
                    log_directional_evaluation(self.logger, live_evaluation)
                    if not live_evaluation.accepted or live_evaluation.side != intended_side:
                        results['failed_executions'] += 1
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side=intended_side, action="buy", price=None, quantity=None,
                            outcome="BLOCKED",
                            reason=(live_evaluation.reason if not live_evaluation.accepted
                                    else "live quote changed the preferred directional side"),
                            risk_limit="DIRECTIONAL_QUALITY", api_attempted=False,
                        )
                        continue
                    price = yes_ask if intended_side == "YES" else no_ask
                    if not 0.01 <= price <= 0.99:
                        results['failed_executions'] += 1
                        log_order_decision(
                            self.logger, market_id=market_id, strategy="portfolio_optimization",
                            side=intended_side, action="buy", price=price, quantity=None,
                            outcome="BLOCKED", reason="valid executable ask unavailable",
                            risk_limit="VALID_QUOTE", api_attempted=False,
                        )
                        continue
                    
                    # Calculate quantity
                    quantity = max(1, int(position_value / price))
                    
                    # Calculate proper stop-loss levels using Grok4 recommendations
                    from src.utils.stop_loss_calculator import StopLossCalculator
                    
                    # Calculate time to expiry for the market
                    time_to_expiry_days = 30  # Default fallback
                    try:
                        market_obj = next((m for m in opportunities if m.market_id == market_id), None)
                        if market_obj:
                            time_to_expiry_days = getattr(market_obj, 'time_to_expiry', 30)
                    except Exception:
                        pass  # keep 30-day default
                    
                    exit_levels = StopLossCalculator.calculate_stop_loss_levels(
                        entry_price=price,
                        side=intended_side,
                        confidence=opportunity.confidence,
                        market_volatility=0.2,  # Default volatility estimate
                        time_to_expiry_days=time_to_expiry_days
                    )
                    
                    # Create Position object
                    position = Position(
                        market_id=market_id,
                        side=intended_side,
                        entry_price=price,
                        quantity=quantity,
                        timestamp=datetime.now(),
                        rationale=f"Portfolio optimization allocation: {allocation_fraction:.1%} of capital. Edge: {opportunity.edge:.3f}, Confidence: {opportunity.confidence:.3f}, Stop: {exit_levels['stop_loss_pct']}%",
                        confidence=opportunity.confidence,
                        live=False,  # Will be set to True after execution
                        status="pending",
                        strategy="portfolio_optimization",
                        
                        # Enhanced exit strategy using Grok4 recommendations
                        stop_loss_price=exit_levels['stop_loss_price'],
                        take_profit_price=exit_levels['take_profit_price'],
                        max_hold_hours=exit_levels['max_hold_hours'],
                        target_confidence_change=exit_levels['target_confidence_change']
                    )
                    
                    # Add position to database
                    position_id = await self.db_manager.add_position(position)
                    if position_id is None:
                        # This shouldn't happen now that we check above, but safety net
                        self.logger.warning(f"Position already exists for {market_id}, skipping execution")
                        continue
                    
                    position.id = position_id
                    
                    # Execute the position
                    live_mode = getattr(settings.trading, 'live_trading_enabled', False)
                    self.logger.info(f"🎛️ Trading mode check: live_mode={live_mode} for market {opportunity.market_id}")
                    
                    success = await execute_position(
                        position=position,
                        live_mode=live_mode,
                        db_manager=self.db_manager,
                        kalshi_client=self.kalshi_client
                    )
                    
                    if success:
                        results['successful_executions'] += 1
                        results['positions_created'] += 1
                        results['total_capital_used'] += position_value
                        self.logger.info(f"✅ Executed position: {market_id} {intended_side} {quantity} at {price:.3f}")
                    else:
                        active_orders = await OrderRepository(
                            self.db_manager.db_path
                        ).get_active_orders_for_position(position.id)
                        if active_orders:
                            results['orders_pending'] += 1
                            self.logger.info(
                                "Order accepted or unresolved; position not counted until authoritative fill",
                                market_id=market_id, position_id=position.id,
                            )
                        else:
                            results['failed_executions'] += 1
                            await self.db_manager.quarantine_position(
                                position.id, "entry execution refused before authoritative fill"
                            )
                            self.logger.error(
                                "Failed to execute position", market_id=market_id,
                                position_id=position.id,
                            )
                
                except Exception as e:
                    self.logger.error(f"Error executing allocation for {market_id}: {e}")
                    log_order_decision(
                        self.logger, market_id=market_id, strategy="portfolio_optimization",
                        side=intended_side, action="buy", price=price, quantity=quantity,
                        outcome="BLOCKED", reason="allocation execution error",
                        api_attempted=False, api_error_category=type(e).__name__,
                    )
                    # Transient decision context used for immediate fail-closed
                    # quote/order-book revalidation. It is intentionally not
                    # fabricated during restart recovery.
                    position.model_probability = opportunity.predicted_probability
                    position.model_generated_at = datetime.now(timezone.utc).timestamp()
                    position.market_phase = opportunity.sports_phase
                    position.market_category = opportunity.category
                    results['failed_executions'] += 1
                    continue
            
            return results
            
        except Exception as e:
            self.logger.error(f"Error in portfolio allocation execution: {e}")
            return results

    async def _execute_arbitrage_strategy(self, markets: List[Market]) -> Dict:
        """
        Execute arbitrage opportunities (placeholder for future implementation).
        """
        try:
            # TODO: Implement cross-market arbitrage detection
            # This could include:
            # - Kalshi vs Polymarket price differences
            # - Related market arbitrage (correlated events)
            # - Temporal arbitrage (same event, different expiries)
            
            self.logger.info("🎯 Arbitrage opportunities analysis (future feature)")
            return {
                'arbitrage_trades': 0,
                'arbitrage_profit': 0.0,
                'arbitrage_exposure': 0.0
            }
            
        except Exception as e:
            self.logger.error(f"Error in arbitrage strategy: {e}")
            return {'arbitrage_trades': 0, 'arbitrage_profit': 0.0}

    def _compile_unified_results(
        self, 
        market_making_results: Dict, 
        portfolio_allocation: PortfolioAllocation,
        arbitrage_results: Dict
    ) -> TradingSystemResults:
        """
        Compile results from all strategies into unified metrics.
        Quick flip strategy removed — 0% win rate across 12 trades.
        """
        try:
            # Calculate total metrics
            directional_execution = getattr(self, "_last_directional_execution_results", {})
            directional_capital_used = float(directional_execution.get('total_capital_used', 0.0))
            directional_positions_created = int(directional_execution.get('positions_created', 0))
            total_capital_used = (
                market_making_results.get('total_exposure', 0) +
                directional_capital_used +
                arbitrage_results.get('arbitrage_exposure', 0)
            )
            
            # Weight expected returns by capital allocation
            mm_weight = market_making_results.get('total_exposure', 0) / (total_capital_used + 1e-8)
            dir_weight = directional_capital_used / (total_capital_used + 1e-8)
            arb_weight = arbitrage_results.get('arbitrage_exposure', 0) / (total_capital_used + 1e-8)
            
            # Portfolio expected return (weighted average)
            portfolio_expected_return = (
                mm_weight * market_making_results.get('expected_profit', 0) +
                dir_weight * portfolio_allocation.expected_portfolio_return +
                arb_weight * arbitrage_results.get('arbitrage_profit', 0)
            )
            
            # Annualize expected return (assume positions held for 30 days average)
            expected_annual_return = portfolio_expected_return * (365 / 30)
            
            # Capital efficiency
            capital_efficiency = total_capital_used / self.total_capital
            
            # Total positions
            total_positions = (
                market_making_results.get('orders_placed', 0) // 2 +  # 2 orders per position
                directional_positions_created +
                arbitrage_results.get('arbitrage_trades', 0)
            )
            
            return TradingSystemResults(
                # Market making
                market_making_orders=market_making_results.get('orders_placed', 0),
                market_making_exposure=market_making_results.get('total_exposure', 0),
                market_making_expected_profit=market_making_results.get('expected_profit', 0),
                
                # Directional trading
                directional_positions=directional_positions_created,
                directional_exposure=directional_capital_used,
                directional_expected_return=portfolio_allocation.expected_portfolio_return,
                
                # Portfolio metrics
                total_capital_used=total_capital_used,
                portfolio_expected_return=portfolio_expected_return,
                portfolio_sharpe_ratio=portfolio_allocation.portfolio_sharpe,
                portfolio_volatility=portfolio_allocation.portfolio_volatility,
                
                # Risk metrics
                max_portfolio_drawdown=portfolio_allocation.max_portfolio_drawdown,
                correlation_score=1.0 - portfolio_allocation.diversification_ratio,
                diversification_ratio=portfolio_allocation.diversification_ratio,
                
                # Performance
                total_positions=total_positions,
                capital_efficiency=capital_efficiency,
                expected_annual_return=expected_annual_return
            )
            
        except Exception as e:
            self.logger.error(f"Error compiling results: {e}")
            return TradingSystemResults()

    async def _manage_risk_and_rebalance(self, results: TradingSystemResults):
        """
        Manage risk and rebalance portfolio if needed.
        """
        try:
            # Check risk constraints
            risk_violations = []
            
            if results.portfolio_volatility > self.config.max_portfolio_volatility:
                risk_violations.append(f"Portfolio vol {results.portfolio_volatility:.1%} > limit {self.config.max_portfolio_volatility:.1%}")
            
            if results.max_portfolio_drawdown > self.config.max_drawdown_limit:
                risk_violations.append(f"Max drawdown {results.max_portfolio_drawdown:.1%} > limit {self.config.max_drawdown_limit:.1%}")
            
            if results.correlation_score > self.config.max_correlation_exposure:
                risk_violations.append(f"Correlation {results.correlation_score:.1%} > limit {self.config.max_correlation_exposure:.1%}")
            
            if risk_violations:
                self.logger.warning(f"⚠️  Risk violations detected: {risk_violations}")
                # TODO: Implement automatic position sizing reduction
            
            # Check if rebalancing is needed
            time_since_rebalance = datetime.now() - self.last_rebalance
            if time_since_rebalance.total_seconds() > (self.config.rebalance_frequency_hours * 3600):
                self.logger.info("🔄 Portfolio rebalancing triggered")
                # TODO: Implement rebalancing logic
                self.last_rebalance = datetime.now()
            
            # Performance monitoring
            if results.portfolio_sharpe_ratio < self.config.target_sharpe_ratio * 0.5:
                self.logger.warning(f"⚠️  Low Sharpe ratio: {results.portfolio_sharpe_ratio:.2f}")
            
            if results.capital_efficiency < 0.8:
                self.logger.warning(f"⚠️  Low capital efficiency: {results.capital_efficiency:.1%}")
            
        except Exception as e:
            self.logger.error(f"Error in risk management: {e}")


    def get_system_performance_summary(self) -> Dict:
        """
        Get comprehensive system performance summary.
        """
        try:
            # Get individual strategy performance

            # 🚨 FIX: Check if market_maker is initialized (not None)
            if self.market_maker:
                mm_performance = self.market_maker.get_performance_summary()
            else:
                # Provide a safe, empty default if not yet initialized
                mm_performance = {'status': 'Uninitialized', 'net_profit': 0.0, 'orders_count': 0}

            market_making_allocation = getattr(self.config, 'market_making_allocation', 0.0)
            directional_trading_allocation = getattr(self.config, 'directional_trading_allocation', 0.0)
            arbitrage_allocation = getattr(self.config, 'arbitrage_allocation', 0.0)

            return {
                'system_status': 'active' if self.market_maker else 'pending_init',
                'total_capital': self.total_capital,
                'capital_allocation': {
                    # Use the allocation from the config, not the dynamic capital attributes
                    'market_making': market_making_allocation,
                    'directional': directional_trading_allocation,
                    'arbitrage': arbitrage_allocation
                },
                'market_making_performance': mm_performance,
                'last_rebalance': self.last_rebalance.isoformat(),
                'risk_limits': {
                    'max_volatility': self.config.max_portfolio_volatility,
                    'max_drawdown': self.config.max_drawdown_limit,
                    'max_correlation': self.config.max_correlation_exposure
                }
            }

        except Exception as e:
            self.logger.error(f"Error getting performance summary: {e}")
            return {}


async def run_unified_trading_system(
    db_manager: DatabaseManager,
    kalshi_client: KalshiClient,
    xai_client: XAIClient,
    config: Optional[TradingSystemConfig] = None
) -> TradingSystemResults:
    """
    Main entry point for the unified advanced trading system.
    
    This is the "Beast Mode" that orchestrates all strategies to maximize
    returns while optimally using all available capital.
    """
    logger = get_trading_logger("unified_trading_main")
    
    try:
        logger.info("🚀 Starting Unified Advanced Trading System")
        
        # Initialize system
        trading_system = UnifiedAdvancedTradingSystem(
            db_manager, kalshi_client, xai_client, config
        )
        
        # 🚨 CRITICAL: Initialize with dynamic balance from Kalshi
        await trading_system.async_initialize()
        
        # Execute unified strategy
        results = await trading_system.execute_unified_trading_strategy()
        
        # Log final summary
        logger.info(
            f"🎯 UNIFIED SYSTEM COMPLETE 🎯\n"
            f"📊 PERFORMANCE SUMMARY:\n"
            f"  • Total Positions: {results.total_positions}\n" 
            f"  • Capital Used: ${results.total_capital_used:.0f} ({results.capital_efficiency:.1%})\n"
            f"  • Expected Annual Return: {results.expected_annual_return:.1%}\n"
            f"  • Portfolio Sharpe Ratio: {results.portfolio_sharpe_ratio:.2f}\n"
            f"  • Portfolio Volatility: {results.portfolio_volatility:.1%}\n"
            f"  • Max Drawdown: {results.max_portfolio_drawdown:.1%}\n"
            f"  • Diversification Ratio: {results.diversification_ratio:.2f}\n"
            f"\n"
            f"💰 STRATEGY BREAKDOWN:\n"
            f"  • Market Making: {results.market_making_orders} orders, ${results.market_making_expected_profit:.2f} profit\n"
            f"  • Directional: {results.directional_positions} positions, ${results.directional_expected_return:.2f} return\n"
            f"\n"
            f"🚀 SYSTEM STATUS: BEAST MODE ACTIVATED! 🚀"
        )
        
        return results
        
    except Exception as e:
        logger.error(f"Error in unified trading system: {e}")
        return TradingSystemResults() 
