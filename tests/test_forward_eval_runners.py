"""Forward-test runners use the compressed, chunked, verified download (single long transfers were cut off)."""
from pathlib import Path

S = Path(__file__).resolve().parents[1] / "scripts"


def test_runners_use_chunked_verified_export():
    common = (S / "ForwardEvalCommon.ps1").read_text(encoding="utf-8")
    assert "gzip -9" in common and "tail -c +" in common and "head -c" in common and "rm -f $tmp" in common
    assert "Length -eq $want" in common                      # every chunk's size is verified
    for name in ("Run-ForwardEval.ps1", "Run-ForwardEvalV2.ps1", "Run-ForwardEvalV3.ps1", "Run-ForwardEvalV4.ps1"):
        text = (S / name).read_text(encoding="utf-8")
        assert "Get-RemoteExport" in text and "ForwardEvalCommon.ps1" in text


def test_runners_still_refuse_an_edited_frozen_evaluator():
    assert "git -C $Repo diff --quiet $FreezeCommit -- scripts/forward_eval_openings.py" in (S / "Run-ForwardEval.ps1").read_text(encoding="utf-8")
    assert "scripts/forward_eval_v2.py scripts/research_opening_v2.py" in (S / "Run-ForwardEvalV2.ps1").read_text(encoding="utf-8")


def test_each_runner_scores_its_own_evaluator():
    for runner, evaluator in (("Run-ForwardEvalV2.ps1", "forward_eval_v2.py"), ("Run-ForwardEvalV3.ps1", "forward_eval_v3.py"),
                              ("Run-ForwardEvalV4.ps1", "forward_eval_v4.py")):
        text = (S / runner).read_text(encoding="utf-8")
        assert "scripts\\" + evaluator in text
        others = {"forward_eval_v2.py", "forward_eval_v3.py", "forward_eval_v4.py"} - {evaluator}
        assert not any("scripts\\" + o in text for o in others)
