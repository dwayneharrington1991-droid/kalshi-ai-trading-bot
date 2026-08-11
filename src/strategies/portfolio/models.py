"""Dataclasses shared by the portfolio-optimization package."""

from dataclasses import dataclass
from typing import Dict


@dataclass
class MarketOpportunity:
    """Represents a trading opportunity with all required metrics for optimization."""
    market_id: str
    market_title: str
    predicted_probability: float
    market_probability: float
    confidence: float
    edge: float  # predicted_prob - market_prob
    volatility: float
    expected_return: float
    max_loss: float
    time_to_expiry: float  # in days
    correlation_score: float  # correlation with portfolio
    
    # Kelly metrics
    kelly_fraction: float
    fractional_kelly: float  # Conservative Kelly
    risk_adjusted_fraction: float
    
    # Portfolio metrics  
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_contribution: float
    recommended_side: str = "UNKNOWN"
    side_probability: float = 0.0
    side_market_probability: float = 0.0
    net_expected_return: float = 0.0
    ranking_score: float = 0.0
    category: str = ""
    sports_phase: str = "NOT_APPLICABLE"
    event_ticker: str = ""
    event_title: str = ""
    market_type: str = "OTHER"
    correlation_group: str = ""
    existing_correlated_exposure: float = 0.0
    correlation_reason: str = "not evaluated"
    proposed_risk: float = 5.0


@dataclass
class PortfolioAllocation:
    """Optimal portfolio allocation across opportunities."""
    allocations: Dict[str, float]  # market_id -> allocation fraction
    total_capital_used: float
    expected_portfolio_return: float
    portfolio_volatility: float
    portfolio_sharpe: float
    max_portfolio_drawdown: float
    diversification_ratio: float
    
    # Risk metrics
    portfolio_var_95: float  # Value at Risk
    portfolio_cvar_95: float  # Conditional Value at Risk
    
    # Kelly metrics
    aggregate_kelly_fraction: float
    portfolio_growth_rate: float

