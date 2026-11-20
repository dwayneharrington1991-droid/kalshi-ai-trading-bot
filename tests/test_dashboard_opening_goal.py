"""The main display says what the opening call predicts and shows the last locked call's result."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "src/signals_display/dashboard.html").read_text(encoding="utf-8")


def test_opening_card_states_the_settlement_being_predicted():
    card = HTML[HTML.index('id="minimal-opening-card"'):]
    card = card[:card.index("</article>")]
    assert 'id="term-predicting"' in card and "PREDICTING: FINAL OUTCOME AT" in card
    assert "<p" not in card.split('id="term-predicting"')[1].split(">")[0]      # a span, not a hidden <p>
    assert "txt('term-predicting','PREDICTING: FINAL OUTCOME AT '+termTime(i.closes_at))" in HTML
    assert "never changes" in HTML and "i.opening_delay_seconds" in HTML


def test_last_locked_call_is_scored_honestly():
    block = HTML[HTML.index("Last locked opening call vs the official Kalshi result"):]
    block = block[:block.index("</script>")]
    assert "fetch('/api/market-history" in block and "innerHTML" not in block
    for label in ("CORRECT", "WRONG", "WAIT / UNSCORED", "awaiting the official result"):
        assert label in block
