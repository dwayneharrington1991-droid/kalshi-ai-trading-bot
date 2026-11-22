"""One line format for dashboard and phone: real probabilities only, WAIT — N/A when missing, BUY only if validated."""
from src.signals_display import four_lines as fl


def test_format():
    assert fl.fmt("UP", 0.576) == "UP 🟢 — 58%" and fl.fmt("DOWN", 0.26) == "DOWN 🔴 — 74%"
    assert fl.fmt("WAIT", 0.515) == "WAIT 🟡 — 52% UP" and fl.fmt("WAIT", 0.47) == "WAIT 🟡 — 53% DOWN"
    for bad in ((None, 0.6), ("UP", None), ("UP", 1.2), ("MAYBE", 0.5)):
        assert fl.fmt(*bad) == "WAIT 🟡 — N/A"
    assert fl.lines() == ["COMBINED CALL: WAIT 🟡 — N/A", "AI #1: WAIT 🟡 — N/A", "AI #2: WAIT 🟡 — N/A"]


def test_minute6_entry_needs_a_validated_rule_and_current_ask():
    assert fl.ENTRY_RULE is None
    assert fl.minute6_line("UP", 0.94, {"up_ask": 0.80}) == "MINUTE-6: UP 🟢 — 94% | WAIT"
    assert fl.minute6_line("WAIT", 0.62) == "MINUTE-6: WAIT 🟡 — 62% UP"
    rule = {"max_price": 0.85}
    assert fl.minute6_line("UP", 0.94, {"up_ask": 0.80}, rule) == "MINUTE-6: UP 🟢 — 94% | BUY UP ≤ 85¢"
    assert fl.minute6_line("UP", 0.94, {"up_ask": 0.90}, rule) == "MINUTE-6: UP 🟢 — 94% | WAIT"
    assert fl.minute6_line("DOWN", 0.06, {}, rule) == "MINUTE-6: DOWN 🔴 — 94% | WAIT"
