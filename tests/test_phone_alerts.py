"""Phone alerts for new calibrated UP/DOWN opening calls. No network: senders are fakes."""
import os
import re
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
    for part in ("UP", "KXBTC15M-26NOV190315-15", "Confidence 71.0%", "$91,234.50", "Expires 03:15 UTC (time remaining 14m", "127.0.0.1:18768",
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
    urls = set(re.findall(r"https://([a-z0-9.\-]+)", code))
    assert hosts == {"api.telegram.org", "api.twilio.com"} and urls == hosts       # only the two providers


def _run(send_fn, statuses=()):
    seq, lines = list(statuses), []
    rc = pa.run_test(CFG, ["sms"], send_fn=send_fn, status_fn=lambda cfg, sid: seq.pop(0) if seq else ("sent", None),
                     wait_s=1, poll_s=0, sleep=lambda s: None, out=lines.append)
    return rc, "\n".join(lines)


def test_sms_test_reports_twilio_acceptance_and_delivery_status_exactly():
    ok = lambda ch, cfg, text: (201, {"sid": "SM0123456789abcdef", "status": "queued"})
    rc, text = _run(ok, [("sent", None), ("delivered", None)])
    assert rc == 0 and "ACCEPTED by Twilio (message ...abcdef, status queued)" in text
    assert "Twilio reports 'delivered'; confirm it arrived on the phone" in text
    rc, text = _run(ok, [("undelivered", 30003)])
    assert rc == 4 and "did not reach the phone" in text and "30003" in text


def test_sms_test_failure_exit_codes_and_reasons():
    def rejected(ch, cfg, text):
        raise pa.ProviderRejected(401, 20003, "Authenticate")
    rc, text = _run(rejected)
    assert rc == 3 and "REJECTED by provider (HTTP 401, code 20003): Authenticate" in text
    rc, text = _run(lambda ch, cfg, text: (201, {}))
    assert rc == 3 and "no message id" in text
    def down(ch, cfg, text):
        raise TimeoutError()
    rc, text = _run(down)
    assert rc == 3 and "FAILED to reach provider (TimeoutError)" in text
    assert pa.run_test(CFG, [], out=lambda *_: None) == 2
    assert "tok" not in text and "sec" not in text.replace("seconds", "")


def test_default_credentials_file_is_found_via_home_and_reread_at_every_alert(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / ".config").mkdir(parents=True)
    cfg_file = home / ".config" / "kalshi-8768-alerts.env"
    monkeypatch.setenv("HOME", str(home))
    for key in pa.KEYS + ("KALSHI_ALERTS_FILE",):
        monkeypatch.delenv(key, raising=False)
    assert pa.channels(pa.load_config()) == []                         # nothing yet: alerts stay off
    cfg_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=-100200\n")
    cfg_file.chmod(0o600)
    assert pa.channels(pa.load_config()) == ["telegram"]               # same path the live service uses
    sent = []
    alerts = pa.OpeningAlerts(tmp_path / "ledger.sqlite3", sender=lambda ch, cfg, text: sent.append((ch, cfg["TELEGRAM_CHAT_ID"])))
    cfg_file.unlink()
    assert alerts.consider(opening(), stale=False, now=NOW) is False    # credentials removed -> no alert
    cfg_file.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=-100200\n"); cfg_file.chmod(0o600)
    assert alerts.consider(opening(ticker="KXBTC15M-OTHER"), stale=False, now=NOW) is True   # picked up without restart
    alerts.queue.join()
    assert sent == [("telegram", "-100200")]


def test_cli_check_reads_the_private_file_from_home_and_never_prints_values(tmp_path):
    home = tmp_path / "home"; (home / ".config").mkdir(parents=True)
    f = home / ".config" / "kalshi-8768-alerts.env"
    f.write_text("TELEGRAM_BOT_TOKEN=999:secret-token\nTELEGRAM_CHAT_ID=4242\n"); f.chmod(0o600)
    env = {k: v for k, v in os.environ.items() if k not in pa.KEYS and k != "KALSHI_ALERTS_FILE"}
    run = subprocess.run([sys.executable, "-m", "src.signals_display.phone_alerts", "--check"], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, env={**env, "HOME": str(home)})
    assert run.returncode == 0 and "configured channels: telegram" in run.stdout
    assert "secret-token" not in run.stdout + run.stderr and "4242" not in run.stdout + run.stderr
    f.chmod(0o644)                                                      # readable by others: refused
    run = subprocess.run([sys.executable, "-m", "src.signals_display.phone_alerts", "--check"], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, env={**env, "HOME": str(home)})
    assert run.returncode == 2 and "configured channels: none" in run.stdout


def test_telegram_test_reports_acceptance_and_rejection_reason():
    lines = []
    assert pa.run_test({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, ["telegram"],
                       send_fn=lambda ch, cfg, text: (200, {"ok": True}), out=lines.append) == 0
    assert lines == ["TEST_TELEGRAM: ACCEPTED by provider"]
    def unauthorized(ch, cfg, text):
        raise pa.ProviderRejected(401, 401, "Unauthorized")
    lines.clear()
    assert pa.run_test({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, ["telegram"], send_fn=unauthorized, out=lines.append) == 3
    assert "REJECTED by provider (HTTP 401, code 401): Unauthorized" in lines[0]


def test_message_holds_every_requested_field_and_never_invents_missing_ones():
    ok, d = pa.qualified(opening(reference_price=91300.25, reason="Momentum and order flow lean up;   calibrated model 71%"),
                         stale=False, now=NOW)
    text = pa.message(d, "http://127.0.0.1:18768/")
    for part in ("OPENING: UP", "KXBTC15M-26NOV190315-15", "Confidence 71.0%", "Opening threshold $91,234.50",
                 "BTC reference $91,300.25", "Expires 03:15 UTC", "Why: Momentum and order flow lean up; calibrated model 71%",
                 "Dashboard: http://127.0.0.1:18768/", "Signal only"):
        assert part in text, part
    ok, d = pa.qualified(opening(threshold=None), stale=False, now=NOW)
    text = pa.message(d, "x")
    assert "Opening threshold n/a" in text and "BTC reference n/a" in text and "Why:" not in text
    for status in ("NO VERIFIED EDGE", "WAIT", None):
        assert not pa.qualified(opening(confidence={"status": status}), stale=False, now=NOW)[0]
    assert not pa.qualified(opening(direction="WAIT", up=0.5), stale=False, now=NOW)[0]


def test_alert_attempts_are_logged_without_credentials_or_text(tmp_path, caplog, monkeypatch):
    import logging
    monkeypatch.setattr(pa.time, "sleep", lambda s: None)
    caplog.set_level(logging.INFO, logger="phone_alerts")
    cfg = {"TELEGRAM_BOT_TOKEN": "777:super-secret", "TELEGRAM_CHAT_ID": "-100999"}
    ok = pa.OpeningAlerts(tmp_path / "a.sqlite3", config_loader=lambda: cfg, sender=lambda ch, c, t: (200, {"ok": True}))
    ok.consider(opening(), stale=False, now=NOW); ok.queue.join()
    def down(ch, c, t):
        raise pa.ProviderRejected(401, 401, "Unauthorized")
    bad = pa.OpeningAlerts(tmp_path / "b.sqlite3", config_loader=lambda: cfg, sender=down)
    bad.consider(opening(), stale=False, now=NOW); bad.queue.join()
    logs = caplog.text
    assert "opening alert KXBTC15M-26NOV190315-15 UP via telegram: SENT" in logs
    assert "via telegram: FAILED:ProviderRejected" in logs
    assert "super-secret" not in logs and "-100999" not in logs and "Confidence" not in logs


def test_live_forecast_changes_never_trigger_a_second_alert(tmp_path):
    sent = []
    a = pa.OpeningAlerts(tmp_path / "a.sqlite3", config_loader=lambda: {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"},
                         sender=lambda ch, c, t: sent.append(t) or (200, {}))
    first = opening()
    assert a.consider(first, stale=False, now=NOW)
    for later in range(1, 30):                     # the same immutable opening seen on every later tick
        a.consider(first, stale=False, now=NOW + timedelta(seconds=later))
    a.queue.join()
    assert len(sent) == 1
