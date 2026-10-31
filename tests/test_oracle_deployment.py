import hashlib
import ast
import importlib.util
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "deploy" / "oracle"


def load_sqlite_backup():
    spec = importlib.util.spec_from_file_location("oracle_sqlite_backup", DEPLOY / "sqlite_backup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_shell_scripts_are_valid_and_do_not_copy_live_databases():
    for name in ("configure_host.sh", "preflight.sh", "prepare_baseline.sh", "activate_baseline.sh", "remote_deploy.sh", "rollback.sh", "run_dashboard.sh", "run_learning_refresh.sh", "launch_first_release.sh", "repair_learning_and_deploy.sh"):
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
    completed = subprocess.run(
        [sys.executable, str(DEPLOY / "sqlite_backup.py"), str(source), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "backup_progress" in completed.stdout
    assert "integrity_check_ok" in completed.stdout
    assert "checksum_ok" in completed.stdout
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM journal ORDER BY id").fetchall() == [("a",), ("b",)]
    expected = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert destination.with_suffix(".db.sha256").read_text().split()[0] == expected


def test_sqlite_backup_completes_with_concurrent_wal_writes(tmp_path):
    module = load_sqlite_backup()
    source = tmp_path / "live.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE journal (id INTEGER PRIMARY KEY, value TEXT)")
        db.executemany("INSERT INTO journal(value) VALUES (?)", [("seed",)] * 2_000)
    stop = threading.Event()

    def writer():
        with sqlite3.connect(source, timeout=1) as db:
            while not stop.is_set():
                db.execute("INSERT INTO journal(value) VALUES ('live')")
                db.commit()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        module.backup_database(source, destination, max_seconds=10, stall_seconds=3,
                               max_restarts=50, busy_timeout_ms=1000, pages=32)
    finally:
        stop.set()
        thread.join(timeout=2)
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("SELECT count(*) FROM journal").fetchone()[0] >= 2_000


def test_sqlite_backup_progress_has_bounded_restarts_and_deadline():
    module = load_sqlite_backup()
    guard = module.ProgressGuard(database="live.db", max_seconds=30,
                                 stall_seconds=30, max_restarts=1)
    guard(0, 90, 100)
    guard(0, 95, 100)
    guard(0, 80, 100)
    with pytest.raises(module.BackupLimitExceeded, match="source changed too often"):
        guard(0, 90, 100)
    deadline = module.ProgressGuard(database="live.db", max_seconds=30,
                                    stall_seconds=30, max_restarts=3)
    deadline.deadline = 0
    with pytest.raises(module.BackupLimitExceeded, match="wall-clock deadline"):
        deadline(0, 1, 100)


def test_failed_sqlite_backup_does_not_publish_or_leave_partial(tmp_path, monkeypatch):
    module = load_sqlite_backup()
    source = tmp_path / "live.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE journal (value TEXT)")
        db.executemany("INSERT INTO journal VALUES (?)", [("x",)] * 1_000)

    def fail(*_args, **_kwargs):
        raise module.BackupLimitExceeded("test deadline")

    monkeypatch.setattr(module.ProgressGuard, "__call__", fail)
    with pytest.raises(module.BackupLimitExceeded, match="test deadline"):
        module.backup_database(source, destination, pages=1)
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.partial.*"))


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
    assert "opc primary group must be opc" in setup
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
    assert "zz-codex-versioned-release.conf" in activation
    assert "versioned launcher did not win systemd drop-in precedence" in activation
    assert "health_wait" in activation
    assert 'dropin="$dropin_dir/zz-codex-versioned-release.conf"' in activation
    assert "legacy_override_preserved" in activation
    assert "disabled-by-codex" in activation
    sudoers = (DEPLOY / "sudoers.example").read_text()
    assert "zz-codex-versioned-release.conf" in sudoers
    assert "99-codex-versioned-release.conf.disabled-by-codex" in sudoers


def test_first_repair_deploy_starts_target_directly_and_restores_original_unit_on_failure():
    deploy = (DEPLOY / "remote_deploy.sh").read_text()
    assert "first_mode=${5:-}" in deploy
    assert '[[ "$first_mode" != --first ]]' in deploy
    assert "restore_first_on_error" in deploy
    assert "original systemd configuration restored" in deploy
    assert "ROLLED_BACK_TO_ORIGINAL_SYSTEMD" in deploy
    assert deploy.index("sqlite_backup.py") < deploy.index('ln -s "$release"')
    assert deploy.index('systemctl stop "$SERVICE_NAME"') < deploy.index("sqlite_backup.py")
    assert "stopping_orphan_dashboard" in deploy
    assert 'fuser "$JOURNAL_PATH"' in deploy
    assert "database still held after managed writer shutdown" in deploy
    assert "dashboard_quiesce_complete" in deploy
    assert "trap recover_first_on_exit EXIT" in deploy
    assert "first_recovery_needed=0" in deploy


def test_single_first_release_launcher_is_locked_exact_and_fails_before_backup():
    launch = (DEPLOY / "launch_first_release.sh").read_text()
    target = "7fb41dd323a98571b6d93537386be97987013441"
    assert f'readonly DEFAULT_TARGET_COMMIT="{target}"' in launch
    assert 'readonly TARGET_COMMIT="${1:-$DEFAULT_TARGET_COMMIT}"' in launch
    assert "flock -n" in launch
    assert "production-approved" in launch
    assert "prepared rollback baseline is missing" in launch
    assert "a deployment or database backup is still running" in launch
    assert launch.index("production-approved") < launch.index('git -C "$APP_ROOT" archive')
    assert launch.index("pgrep -u opc") < launch.index('git -C "$APP_ROOT" archive')
    assert 'git -C "$APP_ROOT" archive "$TARGET_COMMIT" deploy/oracle' in launch
    assert '"$tools/remote_deploy.sh" "$TARGET_COMMIT" "$archive" "$archive_sha" "$CONFIG" --first' in launch
    assert "baseline_activated=false" in launch
    assert "src.signals_display.server" in launch
    assert "--mode public" in launch
    assert "curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8765/healthz" in launch
    for unsafe in ("place_order", "cancel_order", "transfer", "git reset", "git clean"):
        assert unsafe not in launch


def test_learning_repair_quiesces_exact_job_and_restores_timer_only_after_deploy():
    repair = (DEPLOY / "repair_learning_and_deploy.sh").read_text()
    deploy_call = '"$temporary" "$target"'
    timer_start = 'systemctl start "$timer"'
    assert "kalshi-learning.timer" in repair
    assert "kalshi-learning.service" in repair
    assert "refresh_learning_staging" in repair
    assert "pgrep -u opc" in repair
    assert "PRAGMA quick_check" in repair
    assert "timeout 600" in repair
    assert repair.index("production-approved") < repair.index('systemctl stop "$timer"')
    assert repair.index('systemctl stop "$timer"') < repair.index(deploy_call)
    assert repair.index(deploy_call) < repair.index(timer_start)
    assert "verify_opening_pipeline.py" in repair
    assert repair.index("verify_opening_pipeline.py") < repair.index(timer_start)
    assert 'systemctl enable "$SERVICE_NAME"' in repair
    assert 'systemctl enable "$timer"' in repair
    assert "zz-codex-bounded-learning.conf" in repair
    assert repair.index('systemctl start "$learning"') < repair.index(timer_start)
    assert "learning_result" in repair and "ExecMainStatus" in repair
    assert "git reset" not in repair and "git clean" not in repair
    sudoers = (DEPLOY / "sudoers.example").read_text()
    assert "KALSHI_LEARNING" in sudoers
    assert "systemctl stop kalshi-learning.timer" in sudoers


def test_bounded_learning_runner_is_read_only_locked_and_cannot_trade():
    runner = (DEPLOY / "run_learning_refresh.sh").read_text()
    override = (DEPLOY / "kalshi-learning.override.conf.example").read_text()
    assert "flock -n" in runner
    assert runner.count("timeout --signal=TERM") == 2
    assert "--scheduled" in runner
    assert "src.signals_display.opening_models" in runner
    assert "TimeoutStartSec=22min" in override
    assert "NoNewPrivileges=true" in override
    assert "run_learning_refresh.sh" in override
    for unsafe in ("place_order", "cancel_order", "transfer", "src.jobs", "src.orders"):
        assert unsafe not in runner
        assert unsafe not in override


def test_opening_pipeline_deployment_gate_requires_live_immutable_opening():
    module_path = DEPLOY / "verify_opening_pipeline.py"
    spec = importlib.util.spec_from_file_location("verify_opening_pipeline", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = {
        "stale": False,
        "remaining_seconds": 600,
        "market": {"ticker": "KXBTC15M-TEST"},
        "prediction_engine": {
            "ticker": "KXBTC15M-TEST",
            "up": 0.61,
            "initial": {
                "ticker": "KXBTC15M-TEST", "up": 0.58,
                "opens_at": "2026-10-30T20:00:00+00:00",
                "data_cutoff": "2026-10-30T20:00:00+00:00",
                "generated_at": "2026-10-30T20:00:02+00:00",
                "closes_at": "2026-10-30T20:15:00+00:00",
                "model": "opening-test",
            },
        },
    }
    value, reason = module.validate(state)
    assert reason is None and value["up"] == 0.58
    state["prediction_engine"]["initial"]["data_cutoff"] = "2026-10-30T20:00:01+00:00"
    value, reason = module.validate(state)
    assert value is None and "cutoff" in reason


def test_deployment_health_uses_live_worker_payload_not_embedded_prior_snapshot():
    module_path = DEPLOY / "health_check.py"
    spec = importlib.util.spec_from_file_location("oracle_health_check", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    health = {
        "state_version": 1,
        "workers": {"workers": {
            "collector": {"thread_alive": True, "cycles": 0},
            "prediction": {"thread_alive": True, "cycles": 1},
        }},
    }
    workers = module.validate_live_workers(health)
    assert workers["collector"]["thread_alive"] is True
    health["workers"]["workers"]["collector"]["thread_alive"] = False
    with pytest.raises(ValueError, match="collector worker thread is not alive"):
        module.validate_live_workers(health)


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
    assert "Restart=on-failure" in override
    assert "KillMode=control-group" in override
    for unsafe in ("src.jobs.execute", "cli.py", "place_order", "cancel_order"):
        assert unsafe not in wrapper
        assert unsafe not in override
