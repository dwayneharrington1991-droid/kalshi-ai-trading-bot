"""The read-only research export tolerates older journals without the quote table."""
import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location("exp", Path(__file__).resolve().parents[1] / "scripts/export_opening_research.py")
exp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exp)


def test_missing_quote_table_is_reported_not_fatal(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE btc_engine_opening (mode, ticker, generated_at, cutoff, closes_at, up, status, payload)")
    db.execute("CREATE TABLE forecast_outcomes (ticker, origin, observed_at, outcome)")
    db.execute("INSERT INTO forecast_outcomes VALUES ('T', 'official', 'x', 1)")
    db.commit()
    db.close()
    out = exp.export(str(tmp_path / "old.db"))
    assert out["opening_quote_snapshots"] == {"cols": [], "rows": [], "missing": True}
    assert out["forecast_outcomes"]["rows"] == [("T", "official", "x", 1)]
