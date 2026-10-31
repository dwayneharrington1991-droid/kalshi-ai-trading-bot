import hashlib
import ast
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "deploy" / "oracle"


def test_deployment_shell_scripts_are_valid_and_do_not_copy_live_databases():
    for name in ("configure_host.sh", "preflight.sh", "prepare_baseline.sh", "activate_baseline.sh", "remote_deploy.sh", "rollback.sh", "run_dashboard.sh"):
        subprocess.run(["bash", "-n", str(DEPLOY / name)], check=True)
    deploy = (DEPLOY / "remote_deploy.sh").read_text()
    assert "sqlite_backup.py" in deploy
    assert 'cp "$DATA_ROOT"' not in deploy
    assert "production-approved" in deploy
    assert "systemctl restart" in deploy


def test_sqlite_backup_is_consistent_and_has_a_verified_manifest(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "backup" / "live.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO journal(value) VALUES (?)", [("a",), ("b",)])
    subprocess.run(
        [sys.executable, str(DEPLOY / "sqlite_backup.py"), str(source), str(destination)],
        check=True,
    )
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM journal ORDER BY id").fetchall() == [("a",), ("b",)]
    expected = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert destination.with_suffix(".db.sha256").read_text().split()[0] == expected


def test_workflow_requires_tests_protected_environment_and_strict_ssh():
    workflow = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text()
    assert "needs: test" in workflow
    assert "branches: [main]" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "environment: oracle-production" in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "StrictHostKeyChecking=no" not in workflow
    assert "ORACLE_SSH_HOST_KEY" in workflow
    assert "vars.ORACLE_HOST" in workflow
    assert "vars.ORACLE_USER" in workflow
    assert "persist-credentials: false" in workflow
    assert "python -m pytest -q" in workflow


def test_service_wrapper_reports_exact_release_commit_and_is_signals_only():
    wrapper = (DEPLOY / "run_dashboard.sh").read_text()
    assert '.deployed-commit' in wrapper
    assert '--build-commit "$commit"' in wrapper
    assert "--mode public" in wrapper
    assert "account-monitoring.flags" in wrapper
    assert "--account-readonly --private-dashboard --require-readonly-scope" in wrapper
    forbidden = ("place_order", "cancel_order", "transfer", "--mode trading")
    assert not any(token in wrapper for token in forbidden)


def test_oracle_paths_and_dirty_worktree_are_preserved_by_versioned_deploy():
    config = (DEPLOY / "deployment.env.example").read_text()
    deploy = (DEPLOY / "remote_deploy.sh").read_text()
    assert "DATA_ROOT=/home/opc/kalshi-ai-trading-bot/data" in config
    assert "data/signals_monitor.db" in config
    assert "data/signals_account_monitor.db" in config
    assert "ALLOW_SAME_DISK_BACKUP=1" in config
    assert "find \"$DATA_ROOT\" -type f -name '*.db'" in deploy
    assert "oracle-worktree.patch" in deploy
    assert "monitor-comparison.diff" in deploy
    assert "git reset" not in deploy
    assert "git clean" not in deploy
    assert "sqlite_backup.py" in deploy
    assert "SAME_DISK_BACKUP_APPROVED_TEMPORARILY" in deploy
    assert "BACKUP_DURABILITY_WARNING.txt" in deploy
    assert "stat -c %d \"$DATA_ROOT\"" in deploy
    assert "stat -c %d \"$BACKUP_ROOT\"" in deploy
    assert "required_free=$((bytes + bytes / 4 + 1073741824))" in deploy


def test_baseline_preserves_dirty_source_and_captures_existing_account_mode():
    baseline = (DEPLOY / "prepare_baseline.sh").read_text()
    config = (DEPLOY / "deployment.env.example").read_text()
    assert "MODEL_REGISTRY=/home/opc/kalshi-ai-trading-bot/data/models/registry" in config
    assert "ENABLE_ACCOUNT_READONLY=" not in config
    assert "--account-readonly" in baseline
    assert "account-monitoring.flags" in baseline
    assert "--exclude='./data'" in baseline
    assert "--exclude='./.venv'" in baseline
    assert "--exclude='./.env*'" in baseline
    assert "--exclude='*.db'" in baseline
    assert "Python 3.12" in baseline
    assert "systemctl restart" not in baseline
    assert "production-approved" not in baseline


def test_host_configuration_is_derived_and_never_creates_approval_or_restarts():
    setup = (DEPLOY / "configure_host.sh").read_text()
    assert "data/models/registry" in setup
    assert "refusing to invent" in setup
    assert "ALLOW_SAME_DISK_BACKUP=1" in setup
    assert "systemctl restart" not in setup
    assert "production-approved" not in setup
    assert 'install -d -o root -g "$deploy_group" -m 0750' in setup
    assert 'install -o root -g "$deploy_group" -m 0640' in setup
    assert "chmod 0640" in setup
    assert "-m 0600" not in setup
    sudoers = (DEPLOY / "sudoers.example").read_text()
    assert "KALSHI_CONFIG" in sudoers
    assert "kalshi-deployment.env.candidate" in sudoers
    assert "KALSHI_BASELINE" in sudoers


def test_baseline_activation_backs_up_before_systemd_switch_and_can_rollback():
    activation = (DEPLOY / "activate_baseline.sh").read_text()
    assert activation.index("sqlite_backup.py") < activation.index("systemctl restart")
    assert "model-artifacts.tar.gz.sha256" in activation
    assert "refusing to overwrite it" in activation
    assert "baseline activation rolled back" in activation
    assert "production_approval_marker_created=false" in activation


def test_signals_service_import_boundary_excludes_execution_modules():
    signals = ROOT / "src" / "signals_display"
    forbidden = ("src.clients", "src.jobs", "src.orders", "src.strategies")
    for path in signals.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(name.startswith(forbidden) for name in imports), (path, imports)
    wrapper = (DEPLOY / "run_dashboard.sh").read_text()
    override = (DEPLOY / "kalshi-signals.override.conf.example").read_text()
    assert "-m src.signals_display.server" in wrapper
    assert "run_dashboard.sh" in override
    for unsafe in ("src.jobs.execute", "cli.py", "place_order", "cancel_order"):
        assert unsafe not in wrapper
        assert unsafe not in override
