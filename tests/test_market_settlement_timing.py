from datetime import timedelta

from src.signals_display import opening_models
from src.signals_display.monitor import Journal
from tests.test_signals_display import NOW
from tests.test_signals_opening_models import opening


def _journal_with_settlement(tmp_path, observed_at):
    journal = Journal(tmp_path / "j.db")
    record = opening()
    journal.opening_record(record["ticker"], "PUBLIC MARKET DATA", record, NOW + timedelta(seconds=2))
    journal.settle(record["ticker"], "yes", "PUBLIC MARKET DATA", observed_at.isoformat())
    return journal, record


def test_unfinished_market_outcome_before_close_is_never_training_data(tmp_path):
    # An outcome observed before the scheduled close cannot be a finished market.
    journal, _ = _journal_with_settlement(tmp_path, NOW + timedelta(minutes=5))
    with journal.read() as db:
        rows = opening_models.dataset(db, NOW + timedelta(minutes=20))
        coverage = opening_models.dataset_coverage(db, NOW + timedelta(minutes=20))
    assert rows == []
    assert coverage["exclusion_reasons"] == {"INVALID_PREDICTION_LABEL_CHRONOLOGY": 1}


def test_outcome_observed_after_the_cycle_start_is_not_used_by_that_cycle(tmp_path):
    # The market closed at +15 min and its official outcome arrived at +16 min. A cycle that starts
    # at +10 min must not see it, even though the market will later be eligible.
    journal, _ = _journal_with_settlement(tmp_path, NOW + timedelta(minutes=16))
    with journal.read() as db:
        assert opening_models.dataset(db, NOW + timedelta(minutes=10)) == []
        assert len(opening_models.dataset(db, NOW + timedelta(minutes=17))) == 1


def test_finished_market_with_outcome_before_cycle_start_is_used(tmp_path):
    journal, record = _journal_with_settlement(tmp_path, NOW + timedelta(minutes=16))
    with journal.read() as db:
        rows = opening_models.dataset(db, NOW + timedelta(minutes=17))
    assert [r["ticker"] for r in rows] == [record["ticker"]]
    assert rows[0]["label_available_at"] == (NOW + timedelta(minutes=16)).isoformat()
