"""Preview 8768 installer: static safety invariants and transport checks.

Nothing here contacts Oracle. These tests keep the installer preview-only and
the Windows PowerShell 5.1 bootstrap byte-safe.
"""
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert 'bash "$new_root/deploy/oracle/switch_preview_8768.sh"' in source
    assert "readonly PREVIEW_PORT=8768" in source
    for forbidden in ("systemctl", "rm -rf", "kill -9", "DROP ", "DELETE "):
        assert forbidden not in source, forbidden
    # Account mode is only ever named to verify it is absent from the new process.
    account_lines = [line.strip() for line in source.splitlines() if "--account-readonly" in line]
    assert account_lines == [
        'if argv_has "$new_pid" --account-readonly || argv_has "$new_pid" --private-dashboard; then'
    ]
    # 8765/8767 are only observed, never signalled.
    assert not re.search(r"kill[^\n]*(8765|8767|pid_8765|pid_8767)", source)
    assert 'listener_pid 8765' in source and 'listener_pid 8767' in source
    assert "port 8765 listener changed" in source and "port 8767 listener changed" in source


def test_installer_preserves_artifact_and_rolls_back_on_postcheck_failure():
    source = INSTALLER.read_text()
    assert "load_artifact(sys.argv[1])" in source
    assert "nothing was changed" in source
    assert 'opening artifact contents changed' in source
    assert '"$PREVIEW_STATE_ROOT/rollback.sh"' in source
    assert "execution_capability\") is False" in source
    assert "AUTOMATED_TRADING=DISABLED" in source


def test_powershell_bootstrap_is_valid_bash_and_crlf_safe(tmp_path):
    ps = _powershell()
    assert "-replace \"`r\", ''" in ps
    assert "$ErrorActionPreference = 'Continue'" in ps and "$remoteExit = $LASTEXITCODE" in ps
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
    assert '"+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"' in bootstrap
    assert 'merge-base --is-ancestor "$SHA" "refs/remotes/origin/$BRANCH"' in bootstrap
    assert 'exec bash "$NEW/deploy/oracle/install_preview_8768.sh" "$SHA"' in bootstrap
    assert "$health.build_commit -ne $Sha" in ps
    assert "ExitOnForwardFailure=yes" in ps
    # The commit appears once; the bootstrap receives it at run time.
    assert ps.count("__FINAL_SHA__") == 1 and "$Sha = '__FINAL_SHA__'" in ps
    assert "SHA=__SHA__" in bootstrap and ".Replace('__SHA__', $Sha)" in ps


# ---------------------------------------------------------------- functional


def _source_and_run(snippet, *args):
    """Source the installer (main must not run) and execute one snippet."""
    script = f'source "{INSTALLER}"; {snippet}'
    return subprocess.run(["bash", "-c", script, "installer-test", *args],
                          capture_output=True, text=True)


def test_sourcing_installer_defines_helpers_without_running_main():
    result = _source_and_run("declare -F main artifact_loads_with_new_code argv_value argv_has")
    assert result.returncode == 0, result.stderr
    assert "PREVIEW_INSTALL" not in result.stdout + result.stderr


@pytest.fixture(scope="module")
def valid_opening_artifact(tmp_path_factory):
    from src.signals_display import opening_models, rigorous_synthetic
    from src.signals_display.model_registry import atomic_json

    artifact, report = opening_models.evaluate(
        rigorous_synthetic.opening_rows(400, seed=400, signal=1.2), used_test_contracts=[]
    )
    if artifact is None:
        pytest.skip(f"synthetic artifact not produced: {report['status']}")
    path = tmp_path_factory.mktemp("artifact") / "opening_shadow.json"
    atomic_json(path, artifact)
    return path


def test_artifact_validation_accepts_a_loadable_artifact(valid_opening_artifact):
    result = _source_and_run(
        'artifact_loads_with_new_code "$1" "$2" "$3"', sys.executable, str(ROOT), str(valid_opening_artifact)
    )
    assert result.returncode == 0, result.stderr


def test_incompatible_tampered_or_missing_artifact_is_blocked(valid_opening_artifact, tmp_path):
    tampered = json.loads(valid_opening_artifact.read_text())
    tampered["model"]["mixture_weights" if "mixture_weights" in tampered["model"] else "intercept"] = (
        [0.5, 0.5] if "mixture_weights" in tampered["model"] else 99.0
    )
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(tampered))
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    for path in (bad, corrupt, tmp_path / "missing.json"):
        result = _source_and_run(
            'if ! artifact_loads_with_new_code "$1" "$2" "$3"; then echo BLOCKED_AS_EXPECTED; fi',
            sys.executable, str(ROOT), str(path),
        )
        assert "BLOCKED_AS_EXPECTED" in result.stdout, (path, result.stderr)


def test_argv_helpers_read_the_running_process_identity():
    process = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(30)",
        "--mode", "public", "--opening-model-artifact", "/home/opc/.kalshi-deploy/preview-8768/models/a b.json",
    ])
    try:
        pid = str(process.pid)
        result = _source_and_run(
            'argv_value "$1" --opening-model-artifact; argv_value "$1" --mode; '
            'argv_has "$1" --account-readonly && echo ACCOUNT || echo NO_ACCOUNT; '
            'argv_value "$1" --absent; echo END', pid,
        )
    finally:
        process.kill()
    assert result.stdout.splitlines() == [
        "/home/opc/.kalshi-deploy/preview-8768/models/a b.json", "public", "NO_ACCOUNT", "END",
    ]


def test_artifact_is_validated_before_switch_and_rollback_is_guarded():
    source = INSTALLER.read_text()
    validation = source.index('if ! artifact_loads_with_new_code "$python" "$new_root" "$artifact"; then')
    switch = source.index('bash "$new_root/deploy/oracle/switch_preview_8768.sh"')
    assert validation < switch
    # A rerun on an already-running target never kills it or restores an older build.
    assert '[[ -n $new_pid && $new_pid == "$old_pid" ]] && switched=0' in source
    assert "target was already running before this install; left unchanged" in source
    # Rollback is verified, not fire-and-forget.
    assert 'wait_preview_healthy "$python" 120' in source
    assert "PREVIEW_INSTALL_ROLLBACK_FAILED" in source
    # Signals-only identity of the new process.
    assert '[[ $(argv_value "$new_pid" --mode) == public ]]' in source
    assert 'argv_has "$new_pid" --account-readonly' in source
    # Rollback restarts the exact previous process, environment (PYTHONPATH) included.
    assert 'mapfile -d \'\' -t old_env <"/proc/$old_pid/environ"' in source
    assert 'nohup env -i "${old_env[@]}" "${old_argv[@]}"' in source
    # No heredoc-into-|| constructions remain.
    assert "<<'PY' ||" not in source
