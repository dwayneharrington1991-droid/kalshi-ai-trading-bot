"""The four prediction lines shown on the dashboard and in phone alerts (one format, stdlib only).

  COMBINED CALL: UP — 56%  experimental consensus of AI #1 and AI #2 (shown first, most prominent)
  AI #1: UP — 58%          live opening AI (locked at the open)
  AI #2: WAIT — 52% UP     challenger (locked at the open, experimental)
  MINUTE-6: DOWN — 97% | WAIT   minute-6 research reading (locked at minute 6) + entry field

The percentage is the probability of the stated direction; a WAIT shows its lean. Missing or unverified
readings are "WAIT 🟡 — N/A". Directions carry UP 🟢 / DOWN 🔴 / WAIT 🟡. The minute-6 entry field shows a BUY price only when ENTRY_RULE is validated
(it is None: no entry rule has passed validation), so it reads "| WAIT". dashboard.html mirrors fmt()
exactly (tests/test_four_lines.py runs both).
"""
import math

ENTRY_RULE = None      # e.g. {"max_price": 0.85} only after a pre-registered validation passes


def pct(p):
    return f"{math.floor(100 * p + 0.5):d}%"


MARK = {"UP": "UP 🟢", "DOWN": "DOWN 🔴", "WAIT": "WAIT 🟡"}


def fmt(direction, p_up):
    if direction not in ("UP", "DOWN", "WAIT") or not isinstance(p_up, (int, float)) or not 0 <= p_up <= 1:
        return f"{MARK['WAIT']} — N/A"
    if direction == "UP":
        return f"{MARK['UP']} — {pct(p_up)}"
    if direction == "DOWN":
        return f"{MARK['DOWN']} — {pct(1 - p_up)}"
    return f"{MARK['WAIT']} — {pct(p_up)} UP" if p_up >= 0.5 else f"{MARK['WAIT']} — {pct(1 - p_up)} DOWN"


def entry(direction, quotes=None, rule=ENTRY_RULE):
    """'BUY UP ≤ 85¢' only under a validated rule with a current ask inside it; otherwise 'WAIT'."""
    if not rule or direction not in ("UP", "DOWN") or not quotes:
        return "WAIT"
    ask = quotes.get("up_ask" if direction == "UP" else "down_ask")
    if not isinstance(ask, (int, float)) or not 0 < ask <= rule["max_price"]:
        return "WAIT"
    return f"BUY {direction} ≤ {math.floor(100 * rule['max_price'] + 1e-9):d}¢"


def minute6_line(direction, p_up, quotes=None, rule=ENTRY_RULE):
    base = fmt(direction, p_up)
    return f"MINUTE-6: {base}" + (f" | {entry(direction, quotes, rule)}" if direction in ("UP", "DOWN") else "")


PENDING = "MINUTE-6: PENDING 🟡"
UNAVAILABLE = "MINUTE-6: WAIT 🟡 — N/A"


def lines(live=(None, None), challenger=(None, None), combined=(None, None), minute6=None):
    """Always four lines. minute6: None before the minute-6 lock (PENDING), "INVALID" for a reading with fewer
    than two eligible components (WAIT — N/A), else (direction, p_up[, quotes])."""
    out = [f"COMBINED CALL: {fmt(*combined)}", f"AI #1: {fmt(*live)}", f"AI #2: {fmt(*challenger)}"]
    out.append(PENDING if minute6 is None else UNAVAILABLE if minute6 == "INVALID" else minute6_line(*minute6))
    return out


FOOTER = "Manual trading only"
