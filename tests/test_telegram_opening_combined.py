import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import opening_shadow as sh
from src.signals_display import opening_shadow_live as live
from src.signals_display import phone_alerts as pa
from tests.test_opening_shadow import _fitted

NOW = datetime(2026, 11, 19, 3, 0, 5, tzinfo=timezone.utc)
CFG = {"TELEGRAM_BOT_TOKEN": "777:super-secret", "TELEGRAM_CHAT_ID": "-100999"}


def opening(ticker="KXBTC15M-TEST-1", direction="UP", up=0.71, status="CALIBRATED", seconds_old=5,
            frozen=None, stale_flag=False):
    generated = NOW - timedelta(seconds=seconds_old)
    return {"ticker": ticker, "direction": direction, "up": up, "confidence": {"status": status},
            "threshold": 91000.0, "reference_price": 91234.5, "reason": "momentum and calibrated history",
            "generated_at": generated.isoformat(), "opens_at": generated.isoformat(),
            "closes_at": (generated + timedelta(minutes=15)).isoformat(),
            "frozen_features": {"values": frozen if frozen is not None else {"x": 3.0, "z": 1.0}}}


def _shadow_dir(tmp_path, trained_days_ago=1):
    shdir = tmp_path / "shadow"
    shdir.mkdir()
    sh.write_version(shdir, sh.serialize_model(_fitted(), NOW - timedelta(days=trained_days_ago), 160, None))
    return shdir


def _alerts(tmp_path, sent, shadow_dir=None, **kw):
    return pa.OpeningAlerts(tmp_path / "alerts.sqlite3", config_loader=lambda: CFG,
                            sender=lambda ch, cfg, text: sent.append((ch, text)), shadow_dir=shadow_dir, **kw)


# ---- formatting, agreement and labelling -------------------------------------------------------------------------

def test_every_new_opening_gets_one_message_even_when_the_live_call_is_wait(tmp_path):
    sent = []
    a = _alerts(tmp_path, sent)
    assert a.consider(opening(direction="WAIT", up=0.5, status="UNCALIBRATED"), stale=False, now=NOW) is True
    a.queue.join()
    assert sorted(ch for ch, _ in sent) == ["telegram"]          # the configured Telegram destination only
    assert sent[0][1].splitlines()[0] == "KALSHI BTC 15m OPENING: WAIT"
    assert "CURRENT AI: WAIT" in sent[0][1]


def test_a_qualified_call_keeps_every_existing_line_word_for_word(tmp_path):
    sent = []
    a = _alerts(tmp_path, sent)
    a.consider(opening(), stale=False, now=NOW); a.queue.join()
    text = sent[0][1]
    for part in ("KALSHI BTC 15m OPENING: UP", "KXBTC15M-TEST-1", "Confidence 71.0% (calibrated)",
                 "Opening threshold $91,000.00", "BTC reference $91,234.50", "Expires ", "time remaining ",
                 "Why: momentum and calibrated history", "Dashboard: ", "Signal only - place any trade manually."):
        assert part in text, part


@pytest.mark.parametrize("live,shadow_decision,expected", [
    ("UP", "UP", "AGREE"), ("WAIT", "WAIT", "AGREE"), ("UP", "DOWN", "DISAGREE"), ("WAIT", "UP", "DISAGREE"),
    ("NO CALL", "UP", "COMPARISON UNAVAILABLE"), ("UP", None, "COMPARISON UNAVAILABLE"),
])
def test_agreement_is_agree_disagree_or_unavailable(live, shadow_decision, expected):
    assert pa.agreement(live, shadow_decision) == expected


def test_shadow_is_labelled_research_only_and_linked_to_the_same_ticker(tmp_path):
    d = pa.combined_details(opening(), stale=False, now=NOW)[1]
    shadow = {"status": "RECORDED", "decision": "DOWN", "probability": 0.29, "recorded_at": NOW.isoformat(),
              "model": "abc"}
    text = pa.combined_message(d, shadow, "http://127.0.0.1:18768/")
    assert "NEW AI — SHADOW (RESEARCH ONLY" in text
    assert "NEW AI — SHADOW (RESEARCH ONLY · not used for any decision): DOWN · P(UP) 29.0%" in text
    assert "COMPARISON: DISAGREE" in text
    assert text.count("KXBTC15M-TEST-1") == 1


def test_pending_and_no_prediction_are_shown_and_never_substituted():
    d = pa.combined_details(opening(), stale=False, now=NOW)[1]
    pending = pa.combined_message(d, {"status": "PENDING", "reason": "no model version yet"}, "u")
    assert "PENDING — no model version yet (no prediction substituted)" in pending
    assert "COMPARISON: COMPARISON UNAVAILABLE" in pending
    none = pa.combined_message(d, {"status": "NO PREDICTION", "reason": "incomplete frozen inputs"}, "u")
    assert "NO PREDICTION — incomplete frozen inputs" in none


def test_message_never_contains_credentials_and_respects_the_length_limit(tmp_path):
    d = pa.combined_details(opening(), stale=False, now=NOW)[1]
    text = pa.combined_message(d, {"status": "RECORDED", "decision": "UP", "probability": 0.7,
                                   "recorded_at": NOW.isoformat()}, "u" * 6000)
    assert "777:super-secret" not in text and "-100999" not in text
    assert len(text) <= pa.MESSAGE_LIMIT


# ---- eligibility and timing --------------------------------------------------------------------------------------

def test_only_new_openings_inside_the_window_are_messaged():
    assert pa.combined_details({"broken": True}, stale=False, now=NOW)[0] is False
    assert pa.combined_details(opening(seconds_old=pa.MAX_CALL_AGE + 1), stale=False, now=NOW)[0] is False
    late = opening(); late["generated_at"] = (NOW - timedelta(seconds=5)).isoformat()
    late["opens_at"] = (NOW - timedelta(seconds=pa.MAX_AFTER_OPEN + 60)).isoformat()
    assert pa.combined_details(late, stale=False, now=NOW)[0] is False


def test_the_prediction_loop_never_waits_for_the_shadow_work(tmp_path, monkeypatch):
    slow_done = threading.Event()

    def slow(*args, **kwargs):
        time.sleep(1.0)
        slow_done.set()
        return {"status": "NO PREDICTION", "reason": "slow test"}

    monkeypatch.setattr(live, "shadow_for_market", slow)
    sent = []
    a = _alerts(tmp_path, sent, shadow_dir=tmp_path)
    started = time.monotonic()
    assert a.consider(opening(), stale=False, now=NOW) is True
    returned_after = time.monotonic() - started
    assert returned_after < 0.2                       # the loop got control back immediately
    a.queue.join()
    assert slow_done.is_set() and sent                # the message still goes out, after the shadow step


def test_a_shadow_exception_is_shown_and_the_alert_is_still_sent(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(live, "shadow_for_market", broken)
    sent = []
    a = _alerts(tmp_path, sent, shadow_dir=tmp_path)
    a.consider(opening(), stale=False, now=NOW); a.queue.join()
    assert sent and "NO PREDICTION — shadow error: RuntimeError" in sent[0][1]
    assert a.counters["sent"] == 1


def test_a_provider_failure_is_recorded_and_never_raised_into_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(pa.time, "sleep", lambda s: None)

    def down(ch, cfg, text):
        raise pa.ProviderRejected(401, 401, "Unauthorized")

    a = pa.OpeningAlerts(tmp_path / "x.sqlite3", config_loader=lambda: CFG, sender=down)
    assert a.consider(opening(), stale=False, now=NOW) is True        # the loop is not disturbed
    a.queue.join()
    assert a.counters["failed"] == 1                                # one message, two attempts, one failure
    with sqlite3.connect(tmp_path / "x.sqlite3") as db:
        assert {r[0] for r in db.execute("SELECT result FROM opening_alerts")} == {"FAILED:ProviderRejected"}


# ---- duplicates and immutability ---------------------------------------------------------------------------------

def test_one_message_per_market_per_channel_including_across_restarts(tmp_path):
    sent = []
    a = _alerts(tmp_path, sent)
    assert a.consider(opening(), stale=False, now=NOW) is True
    assert a.consider(opening(), stale=False, now=NOW) is False
    a.queue.join()
    b = _alerts(tmp_path, sent)                       # restart with the same dedupe ledger
    assert b.consider(opening(), stale=False, now=NOW) is False
    b.queue.join()
    assert len(sent) == 1                             # one Telegram message, nothing more


def test_a_recorded_shadow_row_is_final_and_never_recomputed(tmp_path):
    shdir = _shadow_dir(tmp_path)
    scorer_row = {"ticker": "KXBTC15M-TEST-1", "opens_at": NOW.isoformat(), "generated_at": NOW.isoformat(),
                  "scored_at": NOW.isoformat(), "seconds_after_open": 0.1, "inputs_complete": 1,
                  "probability": 0.11, "decision": "DOWN", "model_sha256": "scorer-version"}
    sh.write_ledger(shdir / "shadow_ledger.sqlite3", [scorer_row])
    result = live.shadow_for_market(shdir, ticker="KXBTC15M-TEST-1", opens_at=NOW.isoformat(),
                                    generated_at=NOW.isoformat(), frozen_values={"x": 3.0, "z": 1.0}, now=NOW)
    assert result["decision"] == "DOWN" and result["probability"] == 0.11
    with sqlite3.connect(shdir / "shadow_ledger.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM shadow_openings WHERE ticker='KXBTC15M-TEST-1'").fetchone() == (1,)


def test_a_new_market_is_scored_once_from_its_own_frozen_inputs_and_linked_to_its_ticker(tmp_path):
    shdir = _shadow_dir(tmp_path)
    first = live.shadow_for_market(shdir, ticker="KXBTC15M-NEW", opens_at=NOW.isoformat(),
                                   generated_at=NOW.isoformat(), frozen_values={"x": 3.0, "z": 1.0}, now=NOW)
    again = live.shadow_for_market(shdir, ticker="KXBTC15M-NEW", opens_at=NOW.isoformat(),
                                   generated_at=NOW.isoformat(), frozen_values={"x": -3.0, "z": 1.0},
                                   now=NOW + timedelta(seconds=9))
    assert first["status"] == "RECORDED" and first == again   # the second call reads the first, never recomputes
    with sqlite3.connect(shdir / "shadow_ledger.sqlite3") as db:
        ticker, = db.execute("SELECT ticker FROM shadow_openings").fetchone()
        assert ticker == "KXBTC15M-NEW"


def test_no_model_yet_is_pending_and_writes_nothing(tmp_path):
    shdir = tmp_path / "shadow"
    shdir.mkdir()
    result = live.shadow_for_market(shdir, ticker="KXBTC15M-EARLY", opens_at=NOW.isoformat(),
                                    generated_at=NOW.isoformat(), frozen_values={"x": 1.0, "z": 1.0}, now=NOW)
    assert result["status"] == "PENDING"
    assert not (shdir / "shadow_ledger.sqlite3").exists()


def test_incomplete_inputs_are_no_prediction_and_writes_nothing(tmp_path):
    shdir = _shadow_dir(tmp_path)
    result = live.shadow_for_market(shdir, ticker="KXBTC15M-PARTIAL", opens_at=NOW.isoformat(),
                                    generated_at=NOW.isoformat(), frozen_values={"x": 1.0}, now=NOW)
    assert result["status"] == "NO PREDICTION" and "incomplete" in result["reason"]


def test_no_shadow_directory_is_not_configured_and_the_alert_still_goes_out(tmp_path):
    sent = []
    a = _alerts(tmp_path, sent, shadow_dir=None)
    a.consider(opening(), stale=False, now=NOW); a.queue.join()
    assert "NEW AI — SHADOW (RESEARCH ONLY · not used for any decision): NOT CONFIGURED" in sent[0][1]


def test_the_legacy_qualified_only_scope_is_still_available(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "COMBINED_SCOPE", "QUALIFIED_ONLY")
    sent = []
    a = _alerts(tmp_path, sent)
    assert a.consider(opening(direction="WAIT", up=0.5, status="UNCALIBRATED"), stale=False, now=NOW) is False
    assert a.consider(opening(), stale=False, now=NOW) is True
    a.queue.join()
    assert sent and "Confidence 71.0% (calibrated)" in sent[0][1] and "CURRENT AI" not in sent[0][1]


def test_the_combined_message_goes_to_telegram_and_sms_keeps_its_qualified_only_alerts(tmp_path):
    both = {"TELEGRAM_BOT_TOKEN": "777:super-secret", "TELEGRAM_CHAT_ID": "-100999",
            "TWILIO_ACCOUNT_SID": "AC" + "1" * 32, "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM": "+15550000000",
            "ALERT_SMS_TO": "+15551111111"}
    sent = []
    a = pa.OpeningAlerts(tmp_path / "both.sqlite3", config_loader=lambda: both,
                         sender=lambda ch, cfg, text: sent.append((ch, text)))
    a.consider(opening(direction="WAIT", up=0.5, status="UNCALIBRATED"), stale=False, now=NOW)
    a.queue.join()
    assert [ch for ch, _ in sent] == ["telegram"]                  # a WAIT opening adds no SMS
    a.consider(opening(ticker="KXBTC15M-TEST-2"), stale=False, now=NOW)
    a.queue.join()
    by_channel = {ch: text for ch, text in sent}
    assert by_channel["telegram"].startswith("KALSHI BTC 15m OPENING: UP") and "CURRENT AI" in by_channel["telegram"]
    assert "CURRENT AI" not in [t for c, t in sent if c == "sms"][0]   # SMS text unchanged from today
    assert sorted(ch for ch, _ in sent).count("telegram") == 2


def test_consider_never_modifies_the_immutable_opening_record(tmp_path):
    import copy
    record = opening()
    before = copy.deepcopy(record)
    a = _alerts(tmp_path, [], shadow_dir=tmp_path)
    a.consider(record, stale=False, now=NOW); a.queue.join()
    assert record == before
