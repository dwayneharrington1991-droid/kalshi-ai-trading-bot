import json

from src.signals_display import model_lifecycle as lc
from src.signals_display import opening_models as om
from tests.test_signals_opening_models import synthetic_rows


def test_first_run_with_existing_reports_treats_seen_contracts_as_exposed(tmp_path):
    rows = synthetic_rows()
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report-x.json").write_text("{}")
    assert lc._load_exposed(tmp_path, rows) == {r["ticker"] for r in rows}


def test_no_history_means_nothing_exposed(tmp_path):
    assert lc._load_exposed(tmp_path, synthetic_rows()) == set()


def test_scored_evaluation_records_its_test_contracts_and_next_test_is_unseen(tmp_path):
    rows = synthetic_rows()
    candidate, report = om.evaluate(rows, used_test_contracts=set())
    assert report["holdout_reuse_checked"] is True        # the validator's requirement is now satisfiable
    exposed = lc._record_exposed(tmp_path, rows, set(), report)
    recorded = set(json.loads((tmp_path / lc.EXPOSED_FILE).read_text())["tickers"])
    assert recorded == exposed and recorded
    assert lc._load_exposed(tmp_path, rows) == recorded
    # The same contracts cannot be scored again: with no new contracts the next evaluation must wait.
    second_candidate, second = om.evaluate(rows, used_test_contracts=recorded)
    assert second_candidate is None
    assert second["status"] == "INSUFFICIENT_UNSEEN_FORWARD_HOLDOUT"
    assert second["holdout_reuse_checked"] is True


def test_unscored_evaluation_does_not_consume_the_holdout(tmp_path):
    rows = synthetic_rows()
    assert lc._record_exposed(tmp_path, rows, set(), {"status": "INSUFFICIENT_UNSEEN_FORWARD_HOLDOUT"}) == set()
    assert not (tmp_path / lc.EXPOSED_FILE).exists()


def test_exposure_is_never_removed(tmp_path):
    rows = synthetic_rows()
    first = lc._record_exposed(tmp_path, rows, {"OLD-1"}, {"status": "EXPERIMENTAL_FALLBACK_RETAINED"})
    assert "OLD-1" in first
    assert "OLD-1" in set(json.loads((tmp_path / lc.EXPOSED_FILE).read_text())["tickers"])
