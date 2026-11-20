# Production monitoring log: 8768 @ 3ce9071 (read-only probes)

| Probe (UTC) | Opening | Generated after open | P(UP) | Call | Telegram | Health | Candles | 8765/8767 | Outcome (filled later) |
|---|---|---|---|---|---|---|---|---|---|
| 04:46:29 | 04:45 | +1.77 s | 0.519 | WAIT | COMBINED SENT 04:45:01.79 | HEALTHY 3ce9071 | READY | off/off | DOWN (WAIT, no call) |
| 05:00:47 | 05:00 | +2.05 s | 0.573 | UP | COMBINED SENT 05:00:02.06 | HEALTHY 3ce9071 | READY | off/off | UP — call correct |
| 05:15:47 | 05:15 | +2.31 s | 0.604 | UP | COMBINED SENT 05:15:02.36 | HEALTHY 3ce9071 | READY | off/off | UP — call correct |
| 05:30:48 | 05:30 | +2.18 s | 0.518 | WAIT | COMBINED SENT 05:30:02.20 | HEALTHY 3ce9071 | READY | off/off | (pending) |

Reference (before deploy, old build a269d08): 03:30 +3.47 s, 03:45 +3.24 s, 04:00 +3.09 s, 04:15 +3.19 s.
04:30 (new build, during soak): +1.60 s, WAIT 0.531, outcome DOWN.

Bot time from threshold receipt to record: 04:30 0.44 s, 04:45 0.35 s, 05:00 0.36 s, 05:15 0.71 s (old build 04:15: 1.61 s).
The rest of each delay is when Kalshi published the threshold (+1.15 to +1.69 s).
