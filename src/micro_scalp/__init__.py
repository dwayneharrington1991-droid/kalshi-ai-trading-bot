"""Bitcoin 15-minute micro-scalping PAPER simulator.

Everything in this package is simulation only. It never submits, modifies or
cancels a real Kalshi order and never reads trading credentials. Every
command-line entry point calls ``install_guard()`` first, which blocks
order-capable modules, refuses live-trading flags and scrubs Kalshi key
material from the process. See ``safety.py``.
"""
from .safety import PAPER_TRADING_ONLY, install_guard

__all__ = ["PAPER_TRADING_ONLY", "install_guard"]
