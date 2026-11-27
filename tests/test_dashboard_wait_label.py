"""Display consistency: the dashboard shows UP, DOWN or WAIT, never a raw UNCLEAR call.

Stored opening records keep their saved label (UNCLEAR rows are immutable and
scored as non-directional); only the rendering maps every non-UP/DOWN call to
WAIT. The safety gate is untouched: the call is still WAIT whenever the record
is not a qualified UP/DOWN.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "src" / "signals_display" / "dashboard.html"


def html():
    return PAGE.read_text(encoding="utf-8")


def test_no_raw_unclear_call_is_rendered():
    page = html()
    assert 'id="direction">UNCLEAR<' not in page
    assert "txt('direction','UNCLEAR')" not in page
    assert "txt('direction',s.direction)" not in page
    assert "shownCall(m.opening_direction)" in page
    assert "shownCall(p.direction)" in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shown_call_maps_every_non_directional_label_to_wait():
    source = re.search(r"function shownCall\(d\)\{[^}]*\}", html()).group(0)
    labels = ["UP", "DOWN", "UNCLEAR", "WAIT", "NEUTRAL", None, ""]
    script = source + "\nconsole.log(JSON.stringify(" + json.dumps(labels) + ".map(shownCall)))"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout) == ["UP", "DOWN", "WAIT", "WAIT", "WAIT", "WAIT", "WAIT"]
