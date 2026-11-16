"""Preview 8768 installer: static safety invariants and transport checks.

Nothing here contacts Oracle. These tests keep the installer preview-only and
the Windows PowerShell 5.1 bootstrap byte-safe.
"""
import base64
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "oracle" / "install_preview_8768.sh"
DOC = ROOT / "docs" / "ORACLE_PREVIEW_INSTALL.md"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _powershell():
    text = DOC.read_text(encoding="utf-8")
    return text[text.index("```powershell\n") + len("```powershell\n"): text.rindex("```")]


def _bootstrap():
    ps = _powershell()
    start = ps.index("$bootstrap = @'\n") + len("$bootstrap = @'\n")
    return ps[start: ps.index("\n'@\n")]


def test_installer_bash_syntax():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_installer_is_preview_only_and_reuses_guarded_switch():
    source = INSTALLER.read_text()
    assert 'bash "$NEW_ROOT/deploy/oracle/switch_preview_8768.sh"' in source
    assert "readonly PORT=8768" in source
    for forbidden in ("systemctl", "rm -rf", "--account-readonly", "kill -9", "DROP ", "DELETE "):
        assert forbidden not in source, forbidden
    # 8765/8767 are only observed, never signalled.
    assert not re.search(r"kill[^\n]*(8765|8767|pid_8765|pid_8767)", source)
    assert 'listener_pid 8765' in source and 'listener_pid 8767' in source
    assert "port 8765 listener changed" in source and "port 8767 listener changed" in source


def test_installer_preserves_artifact_and_rolls_back_on_postcheck_failure():
    source = INSTALLER.read_text()
    assert "load_artifact(sys.argv[1])" in source
    assert "nothing was changed" in source
    assert 'opening artifact contents changed' in source
    assert '"$STATE_ROOT/rollback.sh"' in source
    assert "execution_capability\") is False" in source
    assert "AUTOMATED_TRADING=DISABLED" in source


def test_powershell_bootstrap_is_valid_bash_and_crlf_safe(tmp_path):
    ps = _powershell()
    assert "-replace \"`r\", ''" in ps
    assert "[Text.Encoding]::UTF8.GetBytes($bootstrap)" in ps
    assert ps.count("@'\n") == 1 and ps.count("\n'@\n") == 1
    assert ps.count("{") == ps.count("}")
    script = _bootstrap().replace("__SHA__", SHA)
    crlf = script.replace("\n", "\r\n").replace("\r", "")  # what the -replace strips
    assert crlf == script
    path = tmp_path / "bootstrap.sh"
    path.write_text(script)
    subprocess.run(["bash", "-n", str(path)], check=True)
    encoded = base64.b64encode(script.encode()).decode()
    decoded = subprocess.run(
        ["bash", "-c", f"echo {encoded} | base64 -d"], capture_output=True, text=True, check=True
    ).stdout
    assert decoded == script


def test_powershell_command_targets_only_preview_and_verifies_commit():
    ps = _powershell()
    bootstrap = _bootstrap()
    assert "127.0.0.1:8768" in ps and "8765" not in bootstrap and "8767" not in bootstrap
    assert 'merge-base --is-ancestor "$SHA" FETCH_HEAD' in bootstrap
    assert 'exec bash "$NEW/deploy/oracle/install_preview_8768.sh" "$SHA"' in bootstrap
    assert "$health.build_commit -ne $Sha" in ps
    assert "ExitOnForwardFailure=yes" in ps
    # The commit appears once; the bootstrap receives it at run time.
    assert ps.count("__FINAL_SHA__") == 1 and "$Sha = '__FINAL_SHA__'" in ps
    assert "SHA=__SHA__" in bootstrap and ".Replace('__SHA__', $Sha)" in ps
