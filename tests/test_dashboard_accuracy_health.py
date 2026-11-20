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
    for label in ("DATA HEALTH", "PREDICTIVE EDGE", "Dashboard scoreboard", "All directional calls",
                  "Excluded from the dashboard scoreboard"):
        assert label in block


def test_route_is_read_only_and_cached():
    route = SERVER[SERVER.index('route == "/api/accuracy-health"'):]
    route = route[:route.index("elif route ==")]
    assert "CachedReport" in route and "monitor.journal.path" in route
    assert not re.search(r"\.(execute|write|commit)\(", route)
