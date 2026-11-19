"""Phone alerts for new calibrated UP/DOWN opening calls. No network: senders are fakes."""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.signals_display import phone_alerts as pa

NOW = datetime(2026, 11, 19, 3, 0, 5, tzinfo=timezone.utc)
CFG = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42", "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "sec",
       "TWILIO_FROM": "+15550000000", "ALERT_SMS_TO": "+15551111111"}


def opening(**kw):
    o = {"ticker": "KXBTC15M-26NOV190315-15", "direction": "UP", "up": 0.71, "threshold": 91234.5,
         "confidence": {"status": "CALIBRATED"}, "generated_at": (NOW - timedelta(seconds=3)).isoformat(),
         "opens_at": NOW.replace(second=0).isoformat(), "closes_at": (NOW.replace(second=0) + timedelta(minutes=15)).isoformat()}
    o.update(kw)
    return o


def test_only_fresh_calibrated_up_down_calls_qualify():
    assert pa.qualified(opening(), stale=False, now=NOW)[0]
    assert pa.qualified(opening(direction="DOWN", up=0.3), stale=False, now=NOW)[0]
    for bad in (opening(direction="UNCLEAR"), opening(direction="WAIT"), opening(confidence={"status": "NO VERIFIED EDGE"}),
                opening(up=None), opening(direction="DOWN", up=0.7),
                opening(generated_at=(NOW - timedelta(minutes=10)).isoformat()),                  # not new
                opening(generated_at=(NOW + timedelta(minutes=6)).isoformat(),
                        opens_at=(NOW - timedelta(minutes=1)).isoformat())):                      # future-dated
        assert not pa.qualified(bad, stale=False, now=NOW)[0], bad
    assert not pa.qualified(opening(), stale=True, now=NOW)[0]
    late = NOW + timedelta(minutes=16)
    assert not pa.qualified(opening(generated_at=(late - timedelta(seconds=2)).isoformat()), stale=False, now=late)[0]


def test_at_most_one_alert_per_market_and_channel_even_across_restarts(tmp_path):
    sent = []
    make = lambda: pa.OpeningAlerts(tmp_path / "a.sqlite3", config_loader=lambda: CFG,
                                    sender=lambda ch, cfg, text: sent.append((ch, text)))
    a = make()
    assert a.consider(opening(), stale=False, now=NOW) is True
    assert a.consider(opening(), stale=False, now=NOW) is False
    a.queue.join()
    b = make()                                                     # restart
    assert b.consider(opening(), stale=False, now=NOW) is False
    b.queue.join()
    assert sorted(ch for ch, _ in sent) == ["sms", "telegram"]
    text = sent[0][1]
    for part in ("UP", "KXBTC15M-26NOV190315-15", "Confidence 71.0%", "$91,234.50", "Time remaining 14m", "127.0.0.1:18768",
                 "Signal only"):
        assert part in text
    with sqlite3.connect(tmp_path / "a.sqlite3") as db:
        assert {r[0] for r in db.execute("SELECT result FROM opening_alerts")} == {"SENT"}


def test_provider_outage_never_raises_and_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(pa.time, "sleep", lambda s: None)
    def boom(ch, cfg, text):
        raise TimeoutError("provider down")
    a = pa.OpeningAlerts(tmp_path / "a.sqlite3", config_loader=lambda: CFG, sender=boom)
    t = time.perf_counter()
    assert a.consider(opening(), stale=False, now=NOW) is True
    assert time.perf_counter() - t < 0.5                           # the prediction loop is never delayed
    a.queue.join()
    with sqlite3.connect(tmp_path / "a.sqlite3") as db:
        assert {r[0] for r in db.execute("SELECT result FROM opening_alerts")} == {"FAILED:TimeoutError"}
    assert a.consider({"broken": True}, stale=False, now=NOW) is False


def test_no_channels_configured_means_no_alerts(tmp_path):
    a = pa.OpeningAlerts(tmp_path / "a.sqlite3", config_loader=lambda: {}, sender=lambda *a: 1 / 0)
    assert a.consider(opening(), stale=False, now=NOW) is False


def test_credentials_only_from_private_file_or_environment(tmp_path):
    f = tmp_path / "alerts.env"
    f.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=7\nUNRELATED=x\n")
    f.chmod(0o644)
    assert pa.load_config({"KALSHI_ALERTS_FILE": str(f)}) == {}               # readable by others: refused
    f.chmod(0o600)
    cfg = pa.load_config({"KALSHI_ALERTS_FILE": str(f), "TELEGRAM_CHAT_ID": "9"})
    assert cfg == {"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "9"} and pa.channels(cfg) == ["telegram"]


def test_provider_requests():
    calls = []
    post = lambda url, data, headers=None: calls.append((url, data, headers)) or 200
    pa.send("telegram", CFG, "hi", post=post)
    pa.send("sms", CFG, "hi", post=post)
    assert calls[0][0] == "https://api.telegram.org/bottok/sendMessage" and calls[0][1]["chat_id"] == "42"
    assert calls[1][0].endswith("/Accounts/AC1/Messages.json") and calls[1][1]["To"] == "+15551111111"
    assert calls[1][2]["Authorization"].startswith("Basic ")


def test_test_command_without_credentials_reports_and_exits_2(tmp_path):
    run = subprocess.run([sys.executable, "-m", "src.signals_display.phone_alerts", "--test"],
                         cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                         env={**{k: v for k, v in os.environ.items() if k not in pa.KEYS},
                              "KALSHI_ALERTS_FILE": str(tmp_path / "missing.env")})
    assert run.returncode == 2 and "configured channels: none" in run.stdout


def test_module_never_trades():
    code = (Path(__file__).resolve().parents[1] / "src/signals_display/phone_alerts.py").read_text()
    for forbidden in ("/portfolio/", "trade-api", "create_order", "cancel_order", "amend", "kalshi_client"):
        assert forbidden not in code.lower()
    hosts = {h for h in ("api.telegram.org", "api.twilio.com") if h in code}
    assert hosts == {"api.telegram.org", "api.twilio.com"} and code.count("https://") == 2  # only the two providers
