"""v5 pipeline: refuses reserved outcomes, keeps one stream computation, trains only on settled markets."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tv5", ROOT / "scripts/train_v5.py")
tv5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tv5)


def test_v5_refuses_to_run_while_post_fix_outcomes_are_reserved(tmp_path):
    art = tmp_path / "a.json"
    art.write_text('{"model": {"components": [{"preprocessor": {"names": ["distance_scaled"]}}]}}')
    with pytest.raises(SystemExit, match="refuses"):
        tv5.run(["unused.json"], "v5", str(art), now=tv5.T_TRAIN_END + timedelta(minutes=29))


def test_legacy_dry_run_never_reads_past_the_first_forward_freeze():
    assert tv5.LEGACY_END == datetime(2026, 11, 20, 6, 15, tzinfo=timezone.utc)


def test_amended_recipe_is_recorded():
    assert max(tv5.LAMBDAS) >= 10000 and tv5.COVERAGE == pytest.approx(1 / 3)
    src = (ROOT / "scripts/train_v5.py").read_text(encoding="utf-8")
    assert "OUT-OF-SAMPLE predictions" in src and "87 receipt-proven inputs" in src


def test_lambda_search_preprocesses_each_training_prefix(monkeypatch):
    import numpy as np

    features = np.arange(100.0)[:, None]
    seen = []
    prepare = tv5.prepare

    def audit(train, validation):
        seen.append((len(train), len(validation)))
        assert train[-1, 0] < validation[0, 0]
        return prepare(train, validation)

    monkeypatch.setattr(tv5, "prepare", audit)
    tv5.choose_lambda(features, np.arange(100) % 2)
    assert seen == [(40, 20), (60, 20), (80, 20)]
