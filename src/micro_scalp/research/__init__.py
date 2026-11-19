"""Isolated research on candidate strategies (PAPER ONLY).

Nothing here changes the baseline ``Engine``/``StrategyConfig`` defaults or
``backtest.py``. Candidates subclass the engine and reuse its exact
execution, fee, risk and accounting code, so every candidate pays the same
costs as the baseline.
"""
