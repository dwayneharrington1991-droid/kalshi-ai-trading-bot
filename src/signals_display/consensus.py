"""EXPERIMENTAL CONSENSUS of the live AI and the v4 challenger for one market (research only, NOT VALIDATED).

The average of two P(UP) values is not a calibrated probability and not an independent third model.
UP only when both AIs independently call UP (>= 0.55) and the average is >= 0.55; DOWN only when both call
DOWN (<= 0.45) and the average is <= 0.45; otherwise WAIT. scripts/dual_ai_alerts.py applies the same rule
(tests/test_consensus.py checks they agree).
"""
THRESHOLD = 0.55
LABEL = "COMBINED AI - EXPERIMENTAL CONSENSUS, NOT VALIDATED (average of two AIs; not a calibrated probability or a third model)"


def combine(live_up, challenger_up):
    c = (float(live_up) + float(challenger_up)) / 2
    if live_up >= THRESHOLD and challenger_up >= THRESHOLD and c >= THRESHOLD:
        direction = "UP"
    elif live_up <= 1 - THRESHOLD and challenger_up <= 1 - THRESHOLD and c <= 1 - THRESHOLD:
        direction = "DOWN"
    else:
        direction = "WAIT"
    confidence = 1 - c if direction == "DOWN" else c if direction == "UP" else None
    return {"combined_up": c, "combined": direction, "combined_confidence": confidence,
            "combined_passes_threshold": direction in ("UP", "DOWN"), "combined_label": LABEL}
