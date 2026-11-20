# Opening-call audit (primary goal), 2026-11-20

Goal: at each 15-minute market's opening, make one timely, locked prediction of that market's
official final settlement, and score it honestly.

## Is the bot doing that? (verified, read-only)

| Requirement | Evidence | Status |
|---|---|---|
| Predicts the final settlement, not a short move | Model target `KXBTC15M_OPENING_SETTLEMENT`; labels match Kalshi's official result on 60/60 sampled contracts; the reference is Kalshi's `floor_strike` (60/60) | **Yes** |
| Associated with the right market | 457 records: 0 with cutoff not equal to the open, close not open+15 min, or an off-grid time | **Yes** |
| Uses only pre-open information | Every stored probability reproduces exactly from inputs received at or before the open (365/365); indicators recomputed independently (369/369) | **Yes** |
| Locked (immutable) | One record per ticker (`INSERT OR IGNORE`), no duplicates; live forecasts are stored separately and never overwrite it | **Yes** |
| Every market attempted | 379/387 quarter-hours since 2026-11-16 21:15 (97.9%). Missed: 8 during the 2026-11-19 08:00–10:00 outage. Withheld: 4 (candle gap 21:30–22:15, fixed in `e8e8808`) | Yes, apart from outages |
| Timely | Since 2026-11-19 10:00: median 2.73 s after the open. The 5 openings over 10 s were the first opening after the outage (15.5 s) and the 4 withheld candle-gap openings (about 120 s). Since 2026-11-20 04:19: max 4.7 s | **Yes** |
| WAIT never counted as correct | 245 WAIT/UNCLEAR records are excluded from accuracy; both scoreboards count only UP/DOWN calls | **Yes** |
| Dashboard clarity | The card now says "PREDICTING: FINAL OUTCOME AT <close>", the lock time and delay, and the last locked call's CORRECT / WRONG / WAIT-UNSCORED result (`b1469ea`) | Deployed when CI passes |

## Why calls are wrong (122 dashboard calls, 64.8% correct)

- Confidence does not separate right from wrong calls: mean |P-0.5| was 0.084 when right and 0.082
  when wrong. This is why stricter thresholds do not raise accuracy.
- DOWN calls were right 69.7% of the time (76 calls) and UP calls 56.5% (46), in a period when only
  40% of markets settled UP. Much of the DOWN success reflects the period's trend.
- Technical factors (delay, missing inputs, reference age, update gaps) are not associated with wrong
  calls once 10 comparisons are accounted for.
- Wrong calls lean towards later UTC hours (median 15 vs 11 UTC, p=0.011, not significant after the
  10-comparison correction). This is a lead to test prospectively.
- Calls against the 60-minute trend were right 66.3% (86) vs 61.1% with it (36). There is no
  momentum effect.

## Conclusion

The pipeline does what the goal requires. Accuracy is limited by the model's weak information, not by
software. Improvements must come from better 15-minute-ahead models validated on unseen openings; the
frozen forward test (`c08fb8c`, decision after about 2026-11-22 08:16 UTC) is the next decision point.
