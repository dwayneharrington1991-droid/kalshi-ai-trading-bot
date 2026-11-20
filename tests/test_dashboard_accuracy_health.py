"""The dashboard shows the read-only accuracy health panel from /api/accuracy-health."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
SERVER = (ROOT / "src/signals_display/server.py").read_text(encoding="utf-8")


def test_panel_and_fetch_exist_and_use_text_nodes_only():
    assert 'id="accuracy-health-body"' in HTML
    block = HTML[HTML.index("Accuracy health panel"):]
    block = block[:block.index("</script>")]
    assert "fetch('/api/accuracy-health'" in block
    assert "innerHTML" not in block                      # text nodes only
    for label in ("Data health", "Prediction edge", "Dashboard scoreboard", "Every UP/DOWN call ever recorded",
                  "Not in the dashboard scoreboard"):
        assert label in block


def test_route_is_read_only_and_cached():
    route = SERVER[SERVER.index('route == "/api/accuracy-health"'):]
    route = route[:route.index("elif route ==")]
    assert "CachedReport" in route and "monitor.journal.path" in route
    assert not re.search(r"\.(execute|write|commit)\(", route)


def test_panel_is_not_hidden_by_the_minimal_layout():
    """Production 2026-11-20: the panel existed but `main>:not(#minimal-opening){display:none!important}`
    hid it. It must be explicitly shown, after the hiding rule, with !important."""
    hide = HTML.index("main>:not(#minimal-opening){display:none!important}")
    show = HTML.index("main>#accuracy-health-card{display:block!important")
    assert show > hide
    card = HTML.index('id="accuracy-health-card"')
    assert HTML.rfind("<main", 0, card) > HTML.rfind("</main>", 0, card)   # a direct child of <main>
    assert not re.search(r"#accuracy-health-card[^{]*\{[^}]*display:\s*none", HTML)


def test_visible_scoreboard_links_to_the_panel():
    board = HTML[HTML.index('id="ai-scoreboard"'):]
    board = board[:board.index("</div>")]
    assert 'href="#accuracy-health-card"' in board and "ACCURACY HEALTH" in board


def test_panel_shows_beginner_labels_for_every_required_section():
    block = HTML[HTML.index("Accuracy health panel"):]
    block = block[:block.index("</script>")]
    for label in ("Data health", "Prediction edge", "Connections", "Inputs", "Speed", "Final-minute live probability",
                  "Recent opening results", "Scoreboards", "Not in the dashboard scoreboard", "Model and release"):
        assert label in block
