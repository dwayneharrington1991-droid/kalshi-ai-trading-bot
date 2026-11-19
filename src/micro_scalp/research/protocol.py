"""Chronological splits and an append-only holdout ledger.

Pre-registered protocol (fixed before any candidate was run):

* Historical contracts sorted by open time: TRAIN = first 50%,
  VALIDATION = next 25%, TEST = last 25%.
* Parameters may be chosen on TRAIN and VALIDATION only.
* A family is sent to TEST only if its selected configuration has net P&L > 0
  on BOTH train and validation and >= 30 validation trades. Otherwise it is
  REJECTED without touching TEST.
* TEST is evaluated at most once per family (ledger enforced). A second,
  different configuration for the same family is refused.
* A candidate is called a "credible edge" only if on TEST: >= 100 trades,
  net P&L > 0, contract-cluster bootstrap 95% lower bound > 0 and profit
  factor >= 1.2. It must also not lose money on the final fresh holdout
  recorded after the research began.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from ..events import historical_index

MIN_VALIDATION_TRADES = 30
CREDIBLE = {"min_trades": 100, "min_profit_factor": 1.2}


def splits(directory):
    rows = historical_index(directory)
    n = len(rows)
    a, b = n // 2, (3 * n) // 4
    names = lambda rs: [r["ticker"] for r in rs]
    return {"train": names(rows[:a]), "validation": names(rows[a:b]), "test": names(rows[b:])}


def config_hash(cfg) -> str:
    data = asdict(cfg) if is_dataclass(cfg) else cfg
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


class HoldoutLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def lookup(self, family: str, split: str = "test"):
        for e in self.entries():
            if e["family"] == family and e["split"] == split:
                return e
        return None

    def authorize(self, family: str, cfg, split: str = "test"):
        """Return a cached result for an identical config, or None if the
        evaluation may run. Raise if this family already used the holdout."""
        prior = self.lookup(family, split)
        if prior is None:
            return None
        if prior["config_hash"] == config_hash(cfg):
            return prior
        raise PermissionError(
            f"Holdout '{split}' was already used for family '{family}' (config {prior['config_hash']}); "
            "re-testing a modified configuration on the same holdout is refused."
        )

    def record(self, family: str, cfg, result: dict, split: str = "test"):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "family": family,
            "split": split,
            "config_hash": config_hash(cfg),
            "config": asdict(cfg) if is_dataclass(cfg) else cfg,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return entry


def credible(test_stats: dict) -> tuple[bool, str]:
    b = test_stats.get("vs_no_trade") or {}
    lo = (b.get("net_pnl_95ci") or [None])[0]
    checks = [
        (test_stats["completed_trades"] >= CREDIBLE["min_trades"], f"trades {test_stats['completed_trades']} < {CREDIBLE['min_trades']}"),
        (test_stats["net_pnl"] > 0, f"net P&L {test_stats['net_pnl']} <= 0"),
        (lo is not None and lo > 0, f"bootstrap 95% lower bound {lo} not > 0"),
        ((test_stats.get("profit_factor") or 0) >= CREDIBLE["min_profit_factor"], f"profit factor {test_stats.get('profit_factor')} < {CREDIBLE['min_profit_factor']}"),
    ]
    failed = [msg for ok, msg in checks if not ok]
    return (not failed, "all credibility criteria met" if not failed else "; ".join(failed))
