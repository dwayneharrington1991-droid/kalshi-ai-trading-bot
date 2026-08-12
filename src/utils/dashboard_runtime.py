"""Read-only runtime configuration shared by the monitoring dashboard.

The canary is normally launched from a clean worktree while its persistent
ledger and credentials live in the primary repository.  A Streamlit process
can start from a different working directory, so relative values in ``.env``
must be resolved from the repository containing that file, not from Streamlit's
process directory.
"""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_ENVIRONMENTS = {"demo", "production"}


@dataclass(frozen=True)
class DashboardRuntime:
    """Safe paths and environment selection for dashboard read operations."""

    environment: str
    database_path: Path
    private_key_path: Path


def _resolve_from_root(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def resolve_dashboard_runtime(
    environment: Optional[Mapping[str, str]] = None,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> DashboardRuntime:
    """Load the repository's existing dashboard/canary environment safely.

    No secret is returned.  The caller may pass a test mapping; production code
    loads ``repository_root/.env`` without overriding explicit process values,
    matching the canary's precedence rules.
    """
    root = Path(repository_root).resolve()
    dotenv_path = root / ".env"
    # A clean deployment worktree may symlink .env to the primary repository.
    # Relative credentials and DB_PATH then belong to the real .env parent.
    config_root = dotenv_path.resolve().parent if dotenv_path.exists() else root
    if environment is None:
        load_dotenv(dotenv_path, override=False)
        environment = os.environ

    selected = str(environment.get("KALSHI_ENVIRONMENT", "production")).strip().lower()
    if selected not in SUPPORTED_ENVIRONMENTS:
        raise RuntimeError("dashboard Kalshi environment is invalid")

    key_value = str(environment.get("KALSHI_PRIVATE_KEY_PATH", "")).strip()
    if not key_value:
        raise RuntimeError("dashboard private-key configuration is unavailable")

    db_value = str(environment.get("DB_PATH", "trading_system.db")).strip()
    if not db_value:
        raise RuntimeError("dashboard database configuration is unavailable")

    return DashboardRuntime(
        environment=selected,
        database_path=_resolve_from_root(db_value, config_root),
        private_key_path=_resolve_from_root(key_value, config_root),
    )


def dashboard_credential_status(environment: Optional[Mapping[str, str]] = None) -> dict[str, bool]:
    """Return presence-only status for UI diagnostics; never expose secrets."""
    if environment is None:
        load_dotenv(REPOSITORY_ROOT / ".env", override=False)
        environment = os.environ
    runtime = resolve_dashboard_runtime(environment)
    return {
        "api_key_present": bool(str(environment.get("KALSHI_API_KEY", "")).strip()),
        "private_key_found": runtime.private_key_path.is_file(),
    }
