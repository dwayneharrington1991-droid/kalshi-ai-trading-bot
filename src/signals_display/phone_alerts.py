"""Phone alerts (Telegram and/or SMS) for NEW qualified UP/DOWN opening calls.

Signals only: this module sends text messages and nothing else. It never
places, cancels or modifies orders and has no account access.

An alert is sent only when ALL hold:
* the immutable opening record says UP or DOWN (never WAIT/UNCLEAR) with a
  displayable, calibrated confidence status (the same rule the verified
  scoreboard counts) and a probability on the stated side;
* market data is not stale, the call was generated at most MAX_CALL_AGE
  seconds ago, within MAX_AFTER_OPEN seconds of the market opening, and the
  market has not closed (no retroactive or historical alerts);
* this (market, direction, channel) was never alerted before: a separate
  SQLite file (never the journal) records each alert BEFORE it is sent, so a
  restart can never send it twice (at most once per market and channel).

Credentials are read from environment variables or from a private file
(default ~/.config/kalshi-8768-alerts.env, mode 0600), never from the
repository, and are never logged. Sending happens on one background thread
with a bounded queue and short timeouts: an outage or slow provider can never
delay or interrupt predictions.

    python -m src.signals_display.phone_alerts --check   # which channels are configured
    python -m src.signals_display.phone_alerts --test    # sends a clearly marked TEST message
"""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

QUALIFIED_CONFIDENCE = {"CALIBRATED", "EXPERIMENTAL CALIBRATED CANDIDATE"}
MAX_CALL_AGE = 120
MAX_AFTER_OPEN = 300
DEFAULT_FILE = "~/.config/kalshi-8768-alerts.env"
KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM",
        "ALERT_SMS_TO", "ALERT_DASHBOARD_URL")
SEND_TIMEOUT = 10


def _ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_config(environ=None, path=None):
    """Environment variables win; otherwise KEY=VALUE lines from the private file."""
    environ = os.environ if environ is None else environ
    cfg = {}
    file = Path(os.path.expanduser(environ.get("KALSHI_ALERTS_FILE") or path or DEFAULT_FILE))
    try:
        if file.is_file() and (file.stat().st_mode & 0o077) == 0:      # private files only
            for line in file.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key in KEYS:
                    cfg[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    for key in KEYS:
        if environ.get(key):
            cfg[key] = environ[key]
    return cfg


def channels(cfg):
    out = []
    if cfg.get("TELEGRAM_BOT_TOKEN") and cfg.get("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if all(cfg.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "ALERT_SMS_TO")):
        out.append("sms")
    return out


def _post(url, data, headers=None, opener=None):
    req = Request(url, data=urlencode(data).encode(), headers=headers or {}, method="POST")
    with (opener or build_opener()).open(req, timeout=SEND_TIMEOUT) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"HTTP {resp.status}")
        return resp.status


def send(channel, cfg, text, post=_post):
    if channel == "telegram":
        return post(f"https://api.telegram.org/bot{cfg['TELEGRAM_BOT_TOKEN']}/sendMessage",
                    {"chat_id": cfg["TELEGRAM_CHAT_ID"], "text": text, "disable_web_page_preview": "true"})
    if channel == "sms":
        auth = base64.b64encode(f"{cfg['TWILIO_ACCOUNT_SID']}:{cfg['TWILIO_AUTH_TOKEN']}".encode()).decode()
        return post(f"https://api.twilio.com/2010-04-01/Accounts/{cfg['TWILIO_ACCOUNT_SID']}/Messages.json",
                    {"From": cfg["TWILIO_FROM"], "To": cfg["ALERT_SMS_TO"], "Body": text},
                    {"Authorization": "Basic " + auth})
    raise ValueError(channel)


def qualified(opening, *, stale, now):
    """(True, details) for a new, fresh, calibrated UP/DOWN opening call; else (False, reason)."""
    if not isinstance(opening, dict):
        return False, "no opening record"
    direction = opening.get("direction")
    if direction not in ("UP", "DOWN"):
        return False, "not an UP/DOWN call"
    status = (opening.get("confidence") or {}).get("status")
    if status not in QUALIFIED_CONFIDENCE:
        return False, "confidence not calibrated"
    up = opening.get("up")
    if type(up) not in (int, float) or not 0 <= up <= 1 or (direction == "UP") != (up >= 0.5):
        return False, "probability missing or inconsistent with direction"
    if stale:
        return False, "market data stale"
    try:
        generated, opens, closes = _ts(opening["generated_at"]), _ts(opening["opens_at"]), _ts(opening["closes_at"])
    except (KeyError, TypeError, ValueError):
        return False, "missing times"
    if not 0 <= (now - generated).total_seconds() <= MAX_CALL_AGE:
        return False, "call is not new"
    if (generated - opens).total_seconds() > MAX_AFTER_OPEN or now >= closes:
        return False, "market already well past its opening"
    return True, {"ticker": opening.get("ticker"), "direction": direction,
                  "confidence": up if direction == "UP" else 1 - up, "threshold": opening.get("threshold"),
                  "remaining_s": max(0, int((closes - now).total_seconds())), "status": status}


def message(d, dashboard):
    threshold = d.get("threshold")
    strike = f"${threshold:,.2f}" if type(threshold) in (int, float) else "n/a"
    mins, secs = divmod(d["remaining_s"], 60)
    return (f"KALSHI BTC 15m OPENING: {d['direction']}\n{d['ticker']}\n"
            f"Confidence {d['confidence'] * 100:.1f}% ({d['status'].lower()})\n"
            f"Opening threshold {strike}\nTime remaining {mins}m {secs:02d}s\n"
            f"Dashboard: {dashboard}\nSignal only - place any trade manually.")


class OpeningAlerts:
    def __init__(self, dedup_path, config_loader=load_config, sender=send, start=True):
        self.dedup_path = str(dedup_path)
        self.config_loader, self.sender = config_loader, sender
        self.queue = queue.Queue(maxsize=100)
        self.counters = {"sent": 0, "failed": 0, "dropped": 0, "skipped_duplicate": 0}
        with sqlite3.connect(self.dedup_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS opening_alerts (ticker TEXT, direction TEXT, channel TEXT, "
                       "claimed_at TEXT, result TEXT, PRIMARY KEY(ticker, direction, channel))")
        self.thread = threading.Thread(target=self._run, name="opening-alerts", daemon=True)
        if start:
            self.thread.start()

    def _claim(self, ticker, direction, channel):
        with sqlite3.connect(self.dedup_path, timeout=5) as db:
            cur = db.execute("INSERT OR IGNORE INTO opening_alerts VALUES (?,?,?,?,?)",
                             (ticker, direction, channel, datetime.now(timezone.utc).isoformat(), "CLAIMED"))
            return cur.rowcount == 1

    def _result(self, ticker, direction, channel, result):
        with sqlite3.connect(self.dedup_path, timeout=5) as db:
            db.execute("UPDATE opening_alerts SET result=? WHERE ticker=? AND direction=? AND channel=?",
                       (result, ticker, direction, channel))

    def consider(self, opening, *, stale, now=None):
        """Called from the prediction loop; never blocks and never raises outward."""
        try:
            ok, details = qualified(opening, stale=stale, now=now or datetime.now(timezone.utc))
            if not ok:
                return False
            cfg = self.config_loader()
            sent_any = False
            for channel in channels(cfg):
                if not self._claim(details["ticker"], details["direction"], channel):
                    self.counters["skipped_duplicate"] += 1
                    continue
                try:
                    self.queue.put_nowait((channel, details, cfg))
                    sent_any = True
                except queue.Full:
                    self.counters["dropped"] += 1
                    self._result(details["ticker"], details["direction"], channel, "DROPPED_QUEUE_FULL")
            return sent_any
        except Exception:
            self.counters["failed"] += 1
            return False

    def _run(self):
        while True:
            channel, d, cfg = self.queue.get()
            result = "FAILED"
            for attempt in range(2):
                try:
                    self.sender(channel, cfg, message(d, cfg.get("ALERT_DASHBOARD_URL") or "http://127.0.0.1:18768/"))
                    result = "SENT"
                    break
                except Exception as exc:
                    result = "FAILED:" + type(exc).__name__
                    time.sleep(2 * (attempt + 1))
            self.counters["sent" if result == "SENT" else "failed"] += 1
            try:
                self._result(d["ticker"], d["direction"], channel, result)
            except sqlite3.Error:
                pass
            self.queue.task_done()


def from_journal(journal_path):
    """The live bot's notifier, or None when alerts are disabled (ALERTS_DISABLED=1)."""
    if os.environ.get("ALERTS_DISABLED") == "1":
        return None
    return OpeningAlerts(str(journal_path) + ".alerts.sqlite3")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opening phone alerts (signals only)")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    found = channels(cfg)
    print("configured channels:", ", ".join(found) or "none (add credentials to " + DEFAULT_FILE + ", mode 600)")
    if args.test:
        text = ("TEST ALERT - not a trade signal. Your Kalshi 8768 phone alerts are working. "
                f"Dashboard: {cfg.get('ALERT_DASHBOARD_URL') or 'http://127.0.0.1:18768/'}")
        for channel in found:
            try:
                send(channel, cfg, text)
                print(f"TEST_{channel.upper()}: SENT (provider accepted it)")
            except Exception as exc:
                print(f"TEST_{channel.upper()}: FAILED ({type(exc).__name__})")
        if not found:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
