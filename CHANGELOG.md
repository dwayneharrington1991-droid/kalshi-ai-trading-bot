# Changelog

All notable changes to the Kalshi AI Trading Bot project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.1] - 2026-06-12

### Fixed
- **`pytest` is now safe to run by default.** Tests that hit the real Kalshi
  API — several of which place real orders — are marked `live` and skipped
  unless `RUN_LIVE_TESTS=1` is set. Previously, running the test suite with a
  configured `.env` could place real-money orders.
- **`pip install -e .` works again.** The declared build backend
  (`setuptools.backends._legacy:_Backend`) does not exist, and `setup.py` was
  an interactive wizard that setuptools executed (and hung on) during every
  install. The backend is now `setuptools.build_meta` and the wizard lives at
  `setup_env.py`.
- Balance guard in order execution: live orders are skipped (fail-closed) if
  the account balance cannot be verified or is insufficient for the order.
- `MODEL_PRICING` was missing entries for `anthropic/claude-sonnet-4.5` (the
  default model), `google/gemini-3-pro-preview`, and `deepseek/deepseek-v3.2`,
  so the daily AI cost limit was not tracking their spend.
- Replaced 6 bare `except:` clauses (which also swallow
  `asyncio.CancelledError` and can block shutdown) with narrow handlers.
- Fixed placeholder `yourusername` URLs in `pyproject.toml` and the broken
  mocked execution test.

### Added
- GitHub Actions CI: test suite (no secrets required) + gitleaks secret scan.
- `SECURITY.md` with vulnerability reporting and credential-handling guidance.
- Regression test: orders exceeding the available balance are refused.

### Removed
- `requirements.txt` (and stale `requirements-dev.txt` /
  `dashboard_requirements.txt` references) — `pyproject.toml` is the single
  source of truth: `pip install -e ".[dev]"` / `".[dashboard]"`.
- One-off debug scripts `verify_fix.py` and `test_live_mode.py` from the
  repo root.

## [Unreleased]

### Changed
- Split the 1,300-line `portfolio_optimization.py` into the
  `src/strategies/portfolio/` package (`models`, `optimizer`, `immediate`,
  `runner`). `portfolio_optimization.py` remains as a compatibility shim, so
  all existing imports — including in forks — keep working unchanged.
- Consolidated the Kelly criterion kernel into `src/utils/position_sizing.py`.
  It was previously implemented three times (twice in
  `portfolio_optimization`, once in `safe_compounder`) with subtle
  differences. Strategy-level policy (fractional multipliers, regime/time
  adjustments, caps) is unchanged; behavior is pinned by 28 characterization
  tests in `tests/test_position_sizing.py`.
- `market_making._calculate_optimal_sizes` docstring now states that its
  formula is a linear edge scaler, not Kelly (the math itself is untouched).

### Added
- Initial public release of Kalshi AI Trading Bot
- Multi-agent AI decision engine with Forecaster, Critic, and Trader agents
- Real-time market scanning and analysis
- Portfolio optimization using Kelly Criterion and risk parity
- Live trading integration with Kalshi API
- Web-based dashboard for monitoring and control
- Performance analytics and reporting
- Market making strategy implementation
- Dynamic exit strategies
- Cost optimization for AI usage
- Comprehensive test suite
- Database management with SQLite support
- Configuration management system
- Logging and monitoring capabilities

### Features
- **Beast Mode Trading**: Aggressive multi-strategy trading system
- **Grok-4 Integration**: Primary AI model for market analysis
- **Real-time Dashboard**: Web interface for monitoring and control
- **Portfolio Management**: Advanced position sizing and risk management
- **Market Making**: Automated spread trading and liquidity provision
- **Performance Tracking**: Comprehensive analytics and reporting

### Technical
- Python 3.12+ compatibility
- Async/await architecture for high performance
- Type hints throughout the codebase
- Comprehensive error handling
- Rate limiting and API management
- Modular design for easy extension

## [1.0.0] - 2024-01-XX

### Added
- Initial release
- Core trading system with AI integration
- Multi-agent decision making
- Portfolio optimization
- Real-time market analysis
- Web dashboard
- Performance monitoring
- Database management
- Configuration system
- Testing framework

---

## Version History

### Version 1.0.0
- **Release Date**: January 2024
- **Status**: Initial public release
- **Key Features**: 
  - Multi-agent AI trading system
  - Real-time market analysis
  - Portfolio optimization
  - Web dashboard
  - Performance tracking

---

## Migration Guide

### From Development to Production
1. Set up environment variables in `.env` file
2. Initialize database with `python init_database.py`
3. Configure trading parameters in `src/config/settings.py`
4. Test with paper trading before live trading
5. Monitor performance and adjust settings as needed

---

## Deprecation Notices

No deprecations in current version.

---

## Breaking Changes

No breaking changes in current version.

---

## Known Issues

- Limited to SQLite database (PostgreSQL support planned)
- Requires manual API key management
- Performance may vary based on market conditions

---

## Future Roadmap

### Planned Features
- PostgreSQL database support
- Additional AI models
- Advanced risk management
- Mobile dashboard
- API rate limit optimization
- Enhanced backtesting capabilities

### Version 1.1.0 (Planned)
- Database migration tools
- Enhanced error handling
- Performance optimizations
- Additional trading strategies

### Version 1.2.0 (Planned)
- PostgreSQL support
- Advanced analytics
- Mobile interface
- API improvements 