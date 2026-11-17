"""Deterministic no-restart opening scenario shared by the before/after
equivalence test. Uses only fixtures present in both code versions."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path


def run(folder):
    import src.signals_display.server as server_module
    from src.signals_display.monitor import Journal, PublicFeed
    from tests.test_signals_display import NOW
    from tests.test_signals_monitor import frame
    from tests.test_signals_prediction_engine import history, market

    opened = NOW
    clock = [opened - timedelta(seconds=66)]
    server_module.utcnow = lambda: clock[0]
    contract = market()
    candles = history(opened - timedelta(minutes=1), opened - timedelta(seconds=1))
    journal = Journal(Path(folder) / "scenario.db")
    monitor = server_module.Monitor(PublicFeed(lambda *_a, **_k: None, multi_venue=False), journal)
    base = frame()
    base.update(mode="PUBLIC MARKET DATA", markets=[{**contract}])
    # 13 ticks 5 s apart (receipts -69..-5 s), then a final tick at -1 s that
    # also delivers the candle batch received at -1 s.
    for step in range(14):
        now = opened - timedelta(seconds=65 - 5 * step) if step < 13 else opened - timedelta(seconds=1)
        clock[0] = now
        payload = deepcopy(base)
        payload.update(now=now.isoformat(), clock=now.isoformat(),
                       stream_observations=[{"kind": "quote", "source": "Coinbase BTC/USD proxy",
                                             "received_at": (now - timedelta(seconds=4 - i)).isoformat(),
                                             "price": 100000 + 0.1 * (5 * step + i)} for i in range(5)],
                       reference={"price": 100000.0, "source": "Coinbase BTC/USD proxy",
                                  "observed_at": now.isoformat(), "official": False})
        if step == 13:
            payload["btc_candles"] = candles
        payload["markets"][0]["observed_at"] = now.isoformat()
        monitor.tick((payload, now, now, 0))
    for now in (opened + timedelta(seconds=1), opened + timedelta(seconds=125)):
        clock[0] = now
        payload = deepcopy(base)
        payload.update(now=now.isoformat(), clock=now.isoformat(), stream_observations=[],
                       reference={"price": 100000.0, "source": "Coinbase BTC/USD proxy",
                                  "observed_at": now.isoformat(), "official": False})
        payload["btc_candles"] = {**candles, "received_at": (now + timedelta(milliseconds=1)).isoformat()}
        payload["markets"][0]["observed_at"] = now.isoformat()
        monitor.tick((payload, now, now, 0))
    return journal.opening_record(contract["ticker"], "PUBLIC MARKET DATA")
