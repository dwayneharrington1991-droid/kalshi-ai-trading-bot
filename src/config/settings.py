"""
Configuration settings for the Kalshi trading system.
Manages trading parameters, API configurations, and risk management settings.
"""

import os
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class APIConfig:
    """API configuration settings."""
    kalshi_api_key: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    kalshi_base_url: str = "https://external-api.kalshi.com"  # Updated to new API endpoint
    kalshi_environment: str = field(
        default_factory=lambda: os.getenv("KALSHI_ENVIRONMENT", "production").lower()
    )
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openai_base_url: str = "https://api.openai.com/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # xai_api_key removed — all models now route through OpenRouter


@dataclass
class EnsembleConfig:
    """Multi-model ensemble configuration."""
    enabled: bool = True
    # Model roster for ensemble decisions — all via OpenRouter (April 2026)
    models: Dict[str, Dict] = field(default_factory=lambda: {
        "anthropic/claude-sonnet-4.5": {"provider": "openrouter", "role": "lead_analyst", "weight": 0.30},
        "google/gemini-3.1-pro": {"provider": "openrouter", "role": "forecaster", "weight": 0.30},
        "openai/gpt-5.4": {"provider": "openrouter", "role": "risk_manager", "weight": 0.20},
        "deepseek/deepseek-v3.2": {"provider": "openrouter", "role": "bull_researcher", "weight": 0.10},
        "x-ai/grok-4.1-fast": {"provider": "openrouter", "role": "bear_researcher", "weight": 0.10},
    })
    min_models_for_consensus: int = 3
    disagreement_threshold: float = 0.25  # Std dev above this = low confidence
    parallel_requests: bool = True
    debate_enabled: bool = True
    calibration_tracking: bool = True
    max_ensemble_cost: float = 0.50  # Max cost per ensemble decision


@dataclass
class SentimentConfig:
    """News and sentiment analysis configuration."""
    enabled: bool = True
    rss_feeds: List[str] = field(default_factory=lambda: [
        "https://feeds.reuters.com/reuters/topNews",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    ])
    sentiment_model: str = "google/gemini-3.1-flash-lite-preview"  # Fast/cheap for sentiment ($0.25/M)
    cache_ttl_minutes: int = 30
    max_articles_per_source: int = 10
    relevance_threshold: float = 0.3


@dataclass
class MultiAgentShadowConfig:
    """Disabled-by-default intelligence that is forbidden from affecting trades."""
    enabled: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_SHADOW_ENABLED", "false").lower() == "true")
    can_affect_trading: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_CAN_AFFECT_TRADING", "false").lower() == "true")
    max_concurrency: int = field(default_factory=lambda: int(os.getenv("MULTI_AGENT_MAX_CONCURRENCY", "3")))
    timeout_seconds: int = field(default_factory=lambda: int(os.getenv("MULTI_AGENT_TIMEOUT_SECONDS", "30")))
    max_retries: int = field(default_factory=lambda: int(os.getenv("MULTI_AGENT_MAX_RETRIES", "1")))
    min_successful_agents: int = field(default_factory=lambda: int(os.getenv("MULTI_AGENT_MIN_SUCCESSFUL_AGENTS", "3")))
    require_risk_agent: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_REQUIRE_RISK_AGENT", "true").lower() == "true")
    require_forecaster: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_REQUIRE_FORECASTER", "true").lower() == "true")
    abstain_on_weak_evidence: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_ABSTAIN_ON_WEAK_EVIDENCE", "true").lower() == "true")
    log_raw_responses: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_LOG_RAW_RESPONSES", "false").lower() == "true")
    store_sanitized_responses: bool = field(default_factory=lambda: os.getenv("MULTI_AGENT_STORE_SANITIZED_RESPONSES", "true").lower() == "true")
    max_markets_per_cycle: int = field(default_factory=lambda: int(os.getenv("MULTI_AGENT_MAX_MARKETS_PER_CYCLE", "5")))
    configuration_version: str = field(default_factory=lambda: os.getenv("MULTI_AGENT_CONFIGURATION_VERSION", "phase4-v1"))
    weight_learning_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_WEIGHT_LEARNING_ENABLED", "false").lower() == "true")
    weight_auto_apply: bool = field(default_factory=lambda: os.getenv("AGENT_WEIGHT_AUTO_APPLY", "false").lower() == "true")
    weight_min_samples: int = field(default_factory=lambda: int(os.getenv("AGENT_WEIGHT_MIN_SAMPLES", "100")))
    weight_max_change: float = field(default_factory=lambda: float(os.getenv("AGENT_WEIGHT_MAX_CHANGE", "0.05")))
    weight_min: float = field(default_factory=lambda: float(os.getenv("AGENT_WEIGHT_MIN", "0.05")))
    weight_max: float = field(default_factory=lambda: float(os.getenv("AGENT_WEIGHT_MAX", "0.40")))
    weight_rolling_days: int = field(default_factory=lambda: int(os.getenv("AGENT_WEIGHT_ROLLING_DAYS", "90")))

    def __post_init__(self) -> None:
        if self.can_affect_trading:
            raise RuntimeError("MULTI_AGENT_CAN_AFFECT_TRADING=true is forbidden in shadow phase")
        if self.max_concurrency < 1 or self.max_markets_per_cycle < 1:
            raise ValueError("multi-agent bounds must be positive")


# Trading strategy configuration — DISCIPLINED DEFAULTS (sane risk management)
# Beast mode is still available via --beast flag, but NOT the default.
# Discipline defaults based on live prediction market trading experience.
# NCAAB NO-side: 74% WR, +10% ROI — ONLY profitable category.
# Economic trades: -70% ROI, 78% of all losses.
@dataclass
class TradingConfig:
    """Trading strategy configuration."""
    # Position sizing and risk management — DISCIPLINED DEFAULTS
    max_position_size_pct: float = 3.0  # SANE: 3% per position (was 5% "beast mode")
    max_daily_loss_pct: float = 10.0    # SANE: 10% daily loss limit (was 15%)
    max_positions: int = 10              # SANE: 10 concurrent positions (was 15)
    min_balance: float = 100.0          # SANE: $100 minimum balance (was $50)
    
    # Market filtering criteria — DISCIPLINED
    min_volume: float = 500.0           # SANE: Higher volume requirement (was 200 beast mode)
    max_time_to_expiry_days: int = 90   # SANE: Shorter timeframes (was 30)
    
    # AI decision making — DATA-DRIVEN THRESHOLDS  
    min_confidence_to_trade: float = 0.55   # LOOSENED: 45% confidence minimum (was 60%, approved 2026-03-29)
                                           # Based on analysis: 65% was too conservative, bot finding 0 eligible markets
                                           # NCAAB NO-side showed 74% WR at +10% ROI, suggesting value at lower thresholds
    
    # Category-specific confidence adjustments (applied as multipliers to base threshold)
    category_confidence_adjustments: Dict[str, float] = field(default_factory=lambda: {
        "sports": 0.90,      # Sports showed best performance (NCAAB 74% WR), lower threshold
        "economics": 1.15,   # Economics showed -70% ROI, higher threshold required  
        "politics": 1.05,    # Slight increase for political volatility
        "default": 1.0       # Base multiplier for other categories
    })
    
    scan_interval_seconds: int = 60      # SANE: 60-second scan interval (was 30)
    
    # AI model configuration
    primary_model: str = "anthropic/claude-sonnet-4.5"  # Primary model via OpenRouter
    fallback_model: str = "deepseek/deepseek-v3.2"  # Fallback model via OpenRouter
    ai_temperature: float = 0  # Lower temperature for more consistent JSON output
    ai_max_tokens: int = 8000    # Reasonable limit for reasoning models (grok-4 works better with 8000)
    
    # Position sizing (LEGACY - now using Kelly-primary approach)
    default_position_size: float = 3.0  # REDUCED: Now using Kelly Criterion as primary method (was 5%, now 3%)
    position_size_multiplier: float = 1.0  # Multiplier for AI confidence
    
    # Kelly Criterion settings (PRIMARY position sizing method) — DISCIPLINED
    use_kelly_criterion: bool = True        # Use Kelly Criterion for position sizing (PRIMARY METHOD)
    kelly_fraction: float = 0.25            # SANE: Quarter-Kelly (was 0.75 beast mode — gambling)
    max_single_position: float = 0.10       # SANE: 3% max position cap (was 0.05 beast mode)
    min_position_size: float = 1.0  # Minimum live position size
    
    # Live trading mode control
    live_trading_enabled: bool = field(default_factory=lambda: os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true")
    paper_trading_mode: bool = field(default_factory=lambda: os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true")

    # Exchange reconciliation is disabled and shadow-only by default. Phase 2
    # never routes order submission or position mutation through this system.
    order_reconciliation_enabled: bool = field(
        default_factory=lambda: os.getenv("ORDER_RECONCILIATION_ENABLED", "false").lower() == "true"
    )
    reconciliation_shadow_mode: bool = field(
        default_factory=lambda: os.getenv("RECONCILIATION_SHADOW_MODE", "true").lower() == "true"
    )
    reconciliation_startup_required: bool = field(
        default_factory=lambda: os.getenv("RECONCILIATION_STARTUP_REQUIRED", "true").lower() == "true"
    )
    reconciliation_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("RECONCILIATION_INTERVAL_SECONDS", "10"))
    )
    full_reconciliation_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("FULL_RECONCILIATION_INTERVAL_SECONDS", "60"))
    )
    order_verification_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("ORDER_VERIFICATION_TIMEOUT_SECONDS", "30"))
    )
    reconciliation_max_staleness_seconds: int = field(
        default_factory=lambda: int(os.getenv("RECONCILIATION_MAX_STALENESS_SECONDS", "30"))
    )
    authoritative_live_execution_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTHORITATIVE_LIVE_EXECUTION_ENABLED", "false").lower() == "true"
    )
    live_order_submission_kill_switch: bool = field(
        default_factory=lambda: os.getenv("LIVE_ORDER_SUBMISSION_KILL_SWITCH", "true").lower() == "true"
    )
    production_execution_acknowledgement: str = field(
        default_factory=lambda: os.getenv("PRODUCTION_EXECUTION_ACKNOWLEDGEMENT", "")
    )
    reconciliation_health_max_age_seconds: int = field(
        default_factory=lambda: int(os.getenv("RECONCILIATION_HEALTH_MAX_AGE_SECONDS", "30"))
    )
    allow_risk_reducing_live_exits: bool = field(
        default_factory=lambda: os.getenv("ALLOW_RISK_REDUCING_LIVE_EXITS", "false").lower() == "true"
    )
    allow_live_order_cancellations: bool = field(
        default_factory=lambda: os.getenv("ALLOW_LIVE_ORDER_CANCELLATIONS", "false").lower() == "true"
    )
    overnight_canary_enabled: bool = field(
        default_factory=lambda: os.getenv("OVERNIGHT_CANARY_ENABLED", "false").lower() == "true"
    )
    overnight_canary_max_total_risk: float = field(
        default_factory=lambda: float(os.getenv("OVERNIGHT_CANARY_MAX_TOTAL_RISK", "5"))
    )
    overnight_canary_max_market_risk: float = field(
        default_factory=lambda: float(os.getenv("OVERNIGHT_CANARY_MAX_MARKET_RISK", "1"))
    )
    overnight_canary_max_positions: int = field(
        default_factory=lambda: int(os.getenv("OVERNIGHT_CANARY_MAX_POSITIONS", "5"))
    )
    overnight_canary_max_rejections: int = field(
        default_factory=lambda: int(os.getenv("OVERNIGHT_CANARY_MAX_REJECTIONS", "3"))
    )
    overnight_canary_max_daily_loss: float = field(
        default_factory=lambda: float(os.getenv("OVERNIGHT_CANARY_MAX_DAILY_LOSS", "2"))
    )
    overnight_canary_market_data_max_age_seconds: int = field(
        default_factory=lambda: int(os.getenv("OVERNIGHT_CANARY_MARKET_DATA_MAX_AGE_SECONDS", "120"))
    )
    
    # Trading frequency - MORE FREQUENT
    market_scan_interval: int = 30          # DECREASED: Scan every 30 seconds (was 60)
    position_check_interval: int = 15       # DECREASED: Check positions every 15 seconds (was 30)
    max_trades_per_hour: int = 20           # INCREASED: Allow more trades per hour (was 10, now 20)
    run_interval_minutes: int = 10          # DECREASED: Run more frequently (was 15, now 10)
    num_processor_workers: int = 5      # Number of concurrent market processor workers
    
    # Market selection preferences
    preferred_categories: List[str] = field(default_factory=lambda: [])
    excluded_categories: List[str] = field(default_factory=lambda: [])
    
    # High-confidence, near-expiry strategy
    enable_high_confidence_strategy: bool = True
    high_confidence_threshold: float = 0.95  # LLM confidence needed
    high_confidence_market_odds: float = 0.90 # Market price to look for
    high_confidence_expiry_hours: int = 24   # Max hours until expiry

    # AI trading criteria - MORE PERMISSIVE
    max_analysis_cost_per_decision: float = 0.15  # INCREASED: Allow higher cost per decision (was 0.10, now 0.15)
    min_confidence_threshold: float = 0.45  # DECREASED: Lower confidence threshold (was 0.55, now 0.45)

    # Cost control and market analysis frequency - MORE PERMISSIVE
    daily_ai_budget: float = 15.0
    max_ai_cost_per_decision: float = 0.08
    analysis_cooldown_hours: int = 3
    max_analyses_per_market_per_day: int = 4
    skip_news_for_low_volume: bool = True
    news_search_volume_threshold: float = 1000.0

    # Daily AI spending limits - SAFETY CONTROLS
    daily_ai_cost_limit: float = field(
        default_factory=lambda: float(os.getenv("DAILY_AI_COST_LIMIT", "10.0"))
    )
    enable_daily_cost_limiting: bool = True
    sleep_when_limit_reached: bool = True

    # Enhanced market filtering to reduce analyses - MORE PERMISSIVE
    min_volume_for_ai_analysis: float = 200.0
    exclude_low_liquidity_categories: List[str] = field(default_factory=list)


@dataclass
class LoggingConfig:
    """Logging configuration."""
    log_level: str = "DEBUG"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_file: str = "logs/trading_system.log"
    enable_file_logging: bool = True
    enable_console_logging: bool = True
    max_log_file_size: int = 10 * 1024 * 1024
    backup_count: int = 5


# BEAST MODE UNIFIED TRADING SYSTEM CONFIGURATION 🚀
# These settings control the advanced multi-strategy trading system

# === CAPITAL ALLOCATION ACROSS STRATEGIES ===
# Allocate capital across different trading approaches
market_making_allocation: float = 0.40  # 40% for market making (spread profits)
directional_allocation: float = 0.50    # 50% for directional trading (AI predictions) 
arbitrage_allocation: float = 0.10      # 10% for arbitrage opportunities

# === PORTFOLIO OPTIMIZATION SETTINGS ===
# Kelly Criterion is now the PRIMARY position sizing method (moved to TradingConfig)
# total_capital: DYNAMICALLY FETCHED from Kalshi balance - never hardcoded!
use_risk_parity: bool = True            # Equal risk allocation vs equal capital
rebalance_hours: int = 6                # Rebalance portfolio every 6 hours
min_position_size: float = 1.0          # Minimum position size ($5 vs $10)
max_opportunities_per_batch: int = 50   # Limit opportunities to prevent optimization issues

# === RISK MANAGEMENT LIMITS ===
# Portfolio-level risk constraints — DISCIPLINED DEFAULTS
# Conservative defaults based on live trading experience. Beast mode available via CLI flag.
max_volatility: float = 0.40            # SANE: 40% volatility max (was 80%)
max_correlation: float = 0.70           # SANE: 70% correlation max (was 95%)
max_drawdown: float = 0.15              # SANE: 15% drawdown limit (was 50% — suicidal)
max_sector_exposure: float = 0.30       # SANE: 30% sector concentration (was 90%)

# === PERFORMANCE TARGETS ===
# System performance objectives - MORE AGGRESSIVE FOR MORE TRADES
target_sharpe: float = 0.3              # DECREASED: Lower Sharpe requirement (was 0.5, now 0.3)
target_return: float = 0.15             # INCREASED: Higher return target (was 0.10, now 0.15)
min_trade_edge: float = 0.08           # DECREASED: Lower edge requirement (was 0.15, now 8%)
min_confidence_for_large_size: float = 0.50  # DECREASED: Lower confidence requirement (was 0.65, now 50%)

# === DYNAMIC EXIT STRATEGIES ===
# Enhanced exit strategy settings - MORE AGGRESSIVE
use_dynamic_exits: bool = True
profit_threshold: float = 0.20          # DECREASED: Take profits sooner (was 0.25, now 0.20)
loss_threshold: float = 0.15            # INCREASED: Allow larger losses (was 0.10, now 0.15)
confidence_decay_threshold: float = 0.25  # INCREASED: Allow more confidence decay (was 0.20, now 0.25)
max_hold_time_hours: int = 240          # INCREASED: Hold longer (was 168, now 240 hours = 10 days)
volatility_adjustment: bool = True      # Adjust exits based on volatility

# === MARKET MAKING STRATEGY ===
# Settings for limit order market making - MORE AGGRESSIVE
enable_market_making: bool = True       # Enable market making strategy
min_spread_for_making: float = 0.01     # DECREASED: Accept smaller spreads (was 0.02, now 1¢)
max_inventory_risk: float = 0.15        # INCREASED: Allow higher inventory risk (was 0.10, now 15%)
order_refresh_minutes: int = 15         # Refresh orders every 15 minutes
max_orders_per_market: int = 4          # Maximum orders per market (2 each side)

# === MARKET SELECTION (ENHANCED FOR MORE OPPORTUNITIES) ===
# Removed time restrictions - trade ANY deadline with dynamic exits!
# max_time_to_expiry_days: REMOVED      # No longer used - trade any timeline!
min_volume_for_analysis: float = 200.0  # DECREASED: Much lower minimum volume (was 1000, now 200)
min_volume_for_market_making: float = 500.0  # DECREASED: Lower volume for market making (was 2000, now 500)
min_price_movement: float = 0.02        # DECREASED: Lower minimum range (was 0.05, now 2¢)
max_bid_ask_spread: float = 0.15        # INCREASED: Allow wider spreads (was 0.10, now 15¢)
min_confidence_long_term: float = 0.45  # DECREASED: Lower confidence for distant expiries (was 0.65, now 45%)

# === COST OPTIMIZATION (MORE GENEROUS) ===
# Enhanced cost controls for the beast mode system
daily_ai_budget: float = 15.0           # INCREASED: Higher budget for more opportunities (was 10.0, now 15.0)
max_ai_cost_per_decision: float = 0.12  # INCREASED: Higher per-decision limit (was 0.08, now 0.12)
analysis_cooldown_hours: int = 2        # DECREASED: Much shorter cooldown (was 4, now 2)
max_analyses_per_market_per_day: int = 6  # INCREASED: More analyses per day (was 3, now 6)
skip_news_for_low_volume: bool = True   # Skip expensive searches for low volume
news_search_volume_threshold: float = 1000.0  # News threshold

# === SYSTEM BEHAVIOR ===
# Overall system behavior settings
beast_mode_enabled: bool = True         # Enable the unified advanced system
fallback_to_legacy: bool = True         # Fallback to legacy system if needed
log_level: str = "INFO"                 # Logging level
performance_monitoring: bool = True     # Enable performance monitoring

# === ADVANCED FEATURES ===
# Cutting-edge features for maximum performance
cross_market_arbitrage: bool = False    # Enable when arbitrage module ready
multi_model_ensemble: bool = False      # Not wired into the live trading path. The scaffolding lives in src/agents/ — fork it if you want real parallel multi-model voting.
sentiment_analysis: bool = True         # News sentiment analysis (ENABLED)
websocket_streaming: bool = True        # WebSocket real-time data (ENABLED)
options_strategies: bool = False        # Complex options strategies (future)
algorithmic_execution: bool = False     # Smart order execution (future)


@dataclass
class Settings:
    """Main settings class combining all configuration."""
    api: APIConfig = field(default_factory=APIConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ensemble: EnsembleConfig = field(default_factory=EnsembleConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    multi_agent_shadow: MultiAgentShadowConfig = field(default_factory=MultiAgentShadowConfig)

    def validate(self) -> bool:
        """Validate configuration settings."""
        if self.trading.live_trading_enabled and not self.api.kalshi_api_key:
            raise ValueError("KALSHI_API_KEY environment variable is required")

        if self.trading.max_position_size_pct <= 0 or self.trading.max_position_size_pct > 100:
            raise ValueError("max_position_size_pct must be between 0 and 100")

        if self.trading.min_confidence_to_trade <= 0 or self.trading.min_confidence_to_trade > 1:
            raise ValueError("min_confidence_to_trade must be between 0 and 1")

        return True


# Global settings instance
settings = Settings()

# Validate settings on import
try:
    settings.validate()
except ValueError as e:
    print(f"Configuration validation error: {e}")
    print("Please check your environment variables and configuration.") 
