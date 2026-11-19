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
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener

LOG = logging.getLogger("phone_alerts")
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


class ProviderRejected(RuntimeError):
    """The provider answered with an error: ``code`` and ``reason`` are its own (never credentials)."""

    def __init__(self, status, code, reason):
        super().__init__(f"HTTP {status} code {code}: {reason}")
        self.status, self.code, self.reason = status, code, reason


def _request(url, data=None, headers=None, opener=None):
    """(HTTP status, JSON body or {}); provider errors raise ProviderRejected."""
    from urllib.error import HTTPError
    req = Request(url, data=urlencode(data).encode() if data is not None else None, headers=headers or {},
                  method="POST" if data is not None else "GET")
    try:
        with (opener or build_opener()).open(req, timeout=SEND_TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        raise ProviderRejected(exc.code, body.get("code") or body.get("error_code"),
                               str(body.get("message") or body.get("description") or "rejected")[:200]) from None
    if not 200 <= status < 300:
        raise ProviderRejected(status, None, "unexpected status")
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {}


def _post(url, data, headers=None, opener=None):
    return _request(url, data, headers, opener)


def _twilio_auth(cfg):
    return {"Authorization": "Basic " + base64.b64encode(
        f"{cfg['TWILIO_ACCOUNT_SID']}:{cfg['TWILIO_AUTH_TOKEN']}".encode()).decode()}


def twilio_status(cfg, message_sid, get=_request):
    """Twilio's own delivery status for one message (queued/sent/delivered/undelivered/failed)."""
    _, body = get(f"https://api.twilio.com/2010-04-01/Accounts/{cfg['TWILIO_ACCOUNT_SID']}/Messages/{message_sid}.json",
                  None, _twilio_auth(cfg))
    return body.get("status"), body.get("error_code")


def send(channel, cfg, text, post=_post):
    if channel == "telegram":
        return post(f"https://api.telegram.org/bot{cfg['TELEGRAM_BOT_TOKEN']}/sendMessage",
                    {"chat_id": cfg["TELEGRAM_CHAT_ID"], "text": text, "disable_web_page_preview": "true"})
    if channel == "sms":
        return post(f"https://api.twilio.com/2010-04-01/Accounts/{cfg['TWILIO_ACCOUNT_SID']}/Messages.json",
                    {"From": cfg["TWILIO_FROM"], "To": cfg["ALERT_SMS_TO"], "Body": text}, _twilio_auth(cfg))
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
    reason = opening.get("reason")
    return True, {"ticker": opening.get("ticker"), "direction": direction,
                  "confidence": up if direction == "UP" else 1 - up, "threshold": opening.get("threshold"),
                  "reference": opening.get("reference_price"), "closes_at": closes,
                  "explanation": " ".join(str(reason).split())[:180] if reason else None,
                  "remaining_s": max(0, int((closes - now).total_seconds())), "status": status}


def _usd(value):
    return f"${value:,.2f}" if type(value) in (int, float) else "n/a"


def message(d, dashboard):
    """Only values the immutable opening record actually holds; missing ones show as n/a."""
    mins, secs = divmod(d["remaining_s"], 60)
    closes = d.get("closes_at")
    expires = closes.strftime("%H:%M UTC") if hasattr(closes, "strftime") else "n/a"
    lines = [f"KALSHI BTC 15m OPENING: {d['direction']}", d["ticker"],
             f"Confidence {d['confidence'] * 100:.1f}% ({d['status'].lower()})",
             f"Opening threshold {_usd(d.get('threshold'))}",
             f"BTC reference {_usd(d.get('reference'))}",
             f"Expires {expires} (time remaining {mins}m {secs:02d}s)"]
    if d.get("explanation"):
        lines.append(f"Why: {d['explanation']}")
    lines += [f"Dashboard: {dashboard}", "Signal only - place any trade manually."]
    return "\n".join(lines)


# One message per market opening, carrying the current AI and the research-only shadow AI.
# "QUALIFIED_ONLY" restores the previous behaviour (alerts only for qualified UP/DOWN calls).
COMBINED_SCOPE = "EVERY_OPENING"
COMBINED_KEY = "COMBINED"
RESEARCH_LABEL = "NEW AI — SHADOW (RESEARCH ONLY · not used for any decision)"
MESSAGE_LIMIT = 4000


def combined_details(opening, *, stale, now):
    """(True, details) for every new market opening, whatever the live call; else (False, reason)."""
    if not isinstance(opening, dict):
        return False, "no opening record"
    ticker = opening.get("ticker")
    if not isinstance(ticker, str) or not ticker:
        return False, "no market ticker"
    try:
        generated, opens, closes = _ts(opening["generated_at"]), _ts(opening["opens_at"]), _ts(opening["closes_at"])
    except (KeyError, TypeError, ValueError):
        return False, "missing times"
    if not 0 <= (now - generated).total_seconds() <= MAX_CALL_AGE:
        return False, "call is not new"
    if (generated - opens).total_seconds() > MAX_AFTER_OPEN or now >= closes:
        return False, "market already well past its opening"
    up = opening.get("up")
    up_ok = type(up) in (int, float) and 0 <= up <= 1
    direction = opening.get("direction")
    if stale:
        live, no_call = "NO CALL", "market data stale"
    elif direction in ("UP", "DOWN", "WAIT"):
        live, no_call = direction, None
    else:
        live, no_call = "NO CALL", "no call recorded"
    confidence = None
    if live in ("UP", "DOWN") and up_ok:
        confidence = up if live == "UP" else 1 - up
    reason = opening.get("reason")
    return True, {"ticker": ticker, "live_decision": live, "no_call_reason": no_call,
                  "live_p_up": up if up_ok else None, "confidence": confidence,
                  "status": (opening.get("confidence") or {}).get("status") or "n/a",
                  "threshold": opening.get("threshold"), "reference": opening.get("reference_price"),
                  "closes_at": closes, "generated_at": opening["generated_at"], "opens_at": opening["opens_at"],
                  "remaining_s": max(0, int((closes - now).total_seconds())),
                  "explanation": " ".join(str(reason).split())[:180] if reason else None}


def agreement(live, shadow_decision):
    if live in ("UP", "DOWN", "WAIT") and shadow_decision in ("UP", "DOWN", "WAIT"):
        return "AGREE" if live == shadow_decision else "DISAGREE"
    return "COMPARISON UNAVAILABLE"


def _pct(value):
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def combined_message(d, shadow, dashboard):
    """The existing opening lines are kept word for word; the current and research calls are added around them.
    Anything missing says so; nothing is invented or substituted."""
    mins, secs = divmod(d["remaining_s"], 60)
    closes = d.get("closes_at")
    expires = closes.strftime("%H:%M UTC") if hasattr(closes, "strftime") else "n/a"
    live = d["live_decision"]
    if live in ("UP", "DOWN", "WAIT", "NO CALL"):
        head = f"KALSHI BTC 15m OPENING: {live}"
    else:
        head = "KALSHI BTC 15m OPENING: NO CALL"
    if live in ("UP", "DOWN"):
        current = (f"CURRENT AI: {live} · P(UP) {_pct(d['live_p_up'])} · "
                   f"Confidence {_pct(d['confidence'])} ({str(d['status']).lower()})")
    elif live == "WAIT":
        current = f"CURRENT AI: WAIT · P(UP) {_pct(d['live_p_up'])} · no directional call"
    else:
        current = f"CURRENT AI: NO CALL ({d.get('no_call_reason') or 'unavailable'})"
    recorded = shadow.get("status") == "RECORDED"
    if recorded:
        when = _ts(shadow["recorded_at"]).strftime("%H:%M:%S UTC") if shadow.get("recorded_at") else "n/a"
        research = (f"{RESEARCH_LABEL}: {shadow['decision']} · P(UP) {_pct(shadow.get('probability'))} · "
                    f"recorded {when} before the outcome")
    else:
        research = (f"{RESEARCH_LABEL}: {shadow.get('status', 'NO PREDICTION')} — "
                    f"{shadow.get('reason', 'unavailable')} (no prediction substituted)")
    lines = [head, d["ticker"], current,
             f"Opening threshold {_usd(d.get('threshold'))}",
             f"BTC reference {_usd(d.get('reference'))}",
             f"Expires {expires} (time remaining {mins}m {secs:02d}s)"]
    if d.get("explanation"):
        lines.append(f"Why: {d['explanation']}")
    lines += [research,
              f"COMPARISON: {agreement(live, shadow['decision'] if recorded else None)}",
              f"Dashboard: {dashboard}", "Signal only - place any trade manually."]
    return "\n".join(lines)[:MESSAGE_LIMIT]


class OpeningAlerts:
    def __init__(self, dedup_path, config_loader=load_config, sender=send, start=True, shadow_dir=None):
        self.dedup_path = str(dedup_path)
        self.config_loader, self.sender = config_loader, sender
        self.shadow_dir = None if shadow_dir is None else Path(shadow_dir)
        self.queue = queue.Queue(maxsize=100)
        self.counters = {"sent": 0, "failed": 0, "dropped": 0, "skipped_duplicate": 0}
        self._handled = set()
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
        """Called from the prediction loop; never blocks, never waits for any shadow work, never raises outward."""
        try:
            now = now or datetime.now(timezone.utc)
            if COMBINED_SCOPE == "EVERY_OPENING":
                return self._consider_combined(opening, stale=stale, now=now)
            return self._consider_qualified(opening, stale=stale, now=now)
        except Exception:
            self.counters["failed"] += 1
            return False

    def _consider_combined(self, opening, *, stale, now):
        """One combined message per opening on Telegram. Other channels keep their existing qualified-only alerts."""
        ok, details = combined_details(opening, stale=stale, now=now)
        if not ok:
            return False
        ticker = details["ticker"]
        if ticker in self._handled:
            self.counters["skipped_duplicate"] += 1
            return False
        cfg = self.config_loader()
        sent_any = False
        frozen = (opening.get("frozen_features") or {}).get("values")
        qualified_ok, qualified_details = qualified(opening, stale=stale, now=now)
        for channel in channels(cfg):
            if channel == "telegram":
                payload, key = details, COMBINED_KEY
                extra = {"key": COMBINED_KEY, "frozen": frozen, "shadow_dir": self.shadow_dir}
            elif qualified_ok:
                payload, key = qualified_details, qualified_details["direction"]
                extra = {"key": key, "frozen": None, "shadow_dir": None, "legacy": True}
            else:
                continue
            if not self._claim(ticker, key, channel):
                self.counters["skipped_duplicate"] += 1
                continue
            try:
                self.queue.put_nowait((channel, payload, cfg, extra))
                sent_any = True
            except queue.Full:
                self.counters["dropped"] += 1
                LOG.warning("opening alert %s via %s: DROPPED_QUEUE_FULL", ticker, channel)
                self._result(ticker, key, channel, "DROPPED_QUEUE_FULL")
        self._handled.add(ticker)
        if len(self._handled) > 2000:
            self._handled = set(list(self._handled)[-1000:])
        return sent_any

    def _consider_qualified(self, opening, *, stale, now):
        ok, details = qualified(opening, stale=stale, now=now)
        if not ok:
            return False
        cfg = self.config_loader()
        sent_any = False
        for channel in channels(cfg):
            if not self._claim(details["ticker"], details["direction"], channel):
                self.counters["skipped_duplicate"] += 1
                continue
            try:
                self.queue.put_nowait((channel, details, cfg, {"key": details["direction"], "frozen": None,
                                                               "shadow_dir": None, "legacy": True}))
                sent_any = True
            except queue.Full:
                self.counters["dropped"] += 1
                LOG.warning("opening alert %s %s via %s: DROPPED_QUEUE_FULL", details["ticker"], details["direction"], channel)
                self._result(details["ticker"], details["direction"], channel, "DROPPED_QUEUE_FULL")
        return sent_any

    def _run(self):
        while True:
            channel, d, cfg, extra = self.queue.get()
            try:
                result = "FAILED"
                key = extra["key"]
                for attempt in range(2):
                    try:
                        dashboard = cfg.get("ALERT_DASHBOARD_URL") or "http://127.0.0.1:18768/"
                        if extra.get("legacy"):
                            text = message(d, dashboard)
                        else:
                            try:
                                from .opening_shadow_live import shadow_for_market
                                shadow = shadow_for_market(extra["shadow_dir"], ticker=d["ticker"], opens_at=d["opens_at"],
                                                           generated_at=d["generated_at"], frozen_values=extra["frozen"])
                            except Exception as exc:          # a shadow failure is shown; the alert is still sent
                                shadow = {"status": "NO PREDICTION", "reason": "shadow error: " + type(exc).__name__}
                            text = combined_message(d, shadow, dashboard)
                        self.sender(channel, cfg, text)
                        result = "SENT"
                        break
                    except Exception as exc:
                        result = "FAILED:" + type(exc).__name__
                        time.sleep(2 * (attempt + 1))
                self.counters["sent" if result == "SENT" else "failed"] += 1
                # Never credentials or message text: ticker, channel and result only.
                shown = d.get("direction") or d.get("live_decision")
                (LOG.info if result == "SENT" else LOG.warning)("opening alert %s %s via %s: %s", d["ticker"], shown, channel, result)
                try:
                    self._result(d["ticker"], key, channel, result)
                except sqlite3.Error:
                    pass
            finally:
                self.queue.task_done()


def from_journal(journal_path):
    """The live bot's notifier, or None when alerts are disabled (ALERTS_DISABLED=1)."""
    if os.environ.get("ALERTS_DISABLED") == "1":
        return None
    return OpeningAlerts(str(journal_path) + ".alerts.sqlite3", shadow_dir=Path(journal_path).parent / "shadow")


def run_test(cfg, found, send_fn=send, status_fn=twilio_status, wait_s=60, poll_s=5, sleep=time.sleep, out=print):
    """Send one clearly marked TEST message per channel. Exit code: 0 every channel accepted
    (and Twilio has not reported a failure); 3 a provider rejected it or could not be reached;
    4 Twilio accepted it but then reported failed/undelivered; 2 nothing configured."""
    if not found:
        return 2
    text = ("TEST ALERT - not a trade signal. Your Kalshi 8768 phone alerts are working. "
            f"Dashboard: {cfg.get('ALERT_DASHBOARD_URL') or 'http://127.0.0.1:18768/'}")
    rc = 0
    for channel in found:
        name = channel.upper()
        try:
            result = send_fn(channel, cfg, text)
        except ProviderRejected as exc:
            out(f"TEST_{name}: REJECTED by provider (HTTP {exc.status}, code {exc.code}): {exc.reason}")
            rc = max(rc, 3)
            continue
        except Exception as exc:
            out(f"TEST_{name}: FAILED to reach provider ({type(exc).__name__})")
            rc = max(rc, 3)
            continue
        body = result[1] if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict) else {}
        if channel != "sms":
            out(f"TEST_{name}: ACCEPTED by provider")
            continue
        sid, status = body.get("sid"), body.get("status")
        if not sid:
            out(f"TEST_{name}: FAILED (Twilio returned no message id)")
            rc = max(rc, 3)
            continue
        out(f"TEST_{name}: ACCEPTED by Twilio (message ...{sid[-6:]}, status {status})")
        deadline, final = time.monotonic() + wait_s, status
        while final not in ("delivered", "undelivered", "failed") and time.monotonic() < deadline:
            sleep(poll_s)
            try:
                final, error = status_fn(cfg, sid)
            except Exception:
                final, error = final, None
        if final in ("undelivered", "failed"):
            out(f"TEST_{name}_STATUS: {final} (Twilio error {error}); the text did not reach the phone")
            rc = max(rc, 4)
        else:
            out(f"TEST_{name}_STATUS: Twilio reports '{final}'; confirm it arrived on the phone")
    return rc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opening phone alerts (signals only)")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    found = channels(cfg)
    print("configured channels:", ", ".join(found) or "none (add credentials to " + DEFAULT_FILE + ", mode 600)")
    if args.test:
        raise SystemExit(run_test(cfg, found))
    raise SystemExit(0 if found else 2)


if __name__ == "__main__":
    main()
