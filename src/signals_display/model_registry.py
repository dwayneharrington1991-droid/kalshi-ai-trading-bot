"""Offline immutable JSON model versions and explicit activation/rollback.

No network or trading client. A scheduled run may archive a candidate; it cannot
activate one. Holdout exposure is recorded so the next scheduled comparison cannot
quietly reuse already inspected test contracts as an untouched test period.
"""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sqlite3

from .learning import validate_artifact
from .model import timestamp


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".model-", suffix=".tmp", delete=False
        ) as handle:
            name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        name = None
        # Persist the directory entry as well as the complete JSON contents.
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


class ModelRegistry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.versions = self.directory / "versions"
        self.versions.mkdir(exist_ok=True)
        self.manifest = self.directory / "manifest.json"

    @contextmanager
    def lock(self):
        with (self.directory / ".registry.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def update_lock(self):
        """Do not let two scheduled comparisons expose the same holdout together."""
        with (self.directory / ".scheduled-update.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Another offline model comparison is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def state(self):
        if not self.manifest.is_file():
            return {
                "format": "kxbtc-model-registry-v1",
                "active_version": None,
                "versions": {},
                "activations": [],
                "evaluations": [],
                "last_attempt_at": None,
            }
        if self.manifest.stat().st_size > 5_000_000:
            raise ValueError("Registry manifest exceeds bounded size")
        data = json.loads(self.manifest.read_text())
        if data.get("format") != "kxbtc-model-registry-v1" or not isinstance(
            data.get("versions"), dict
        ):
            raise ValueError("Unsupported registry manifest")
        return data

    @staticmethod
    def _identity(artifact):
        return hashlib.sha256(
            json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    @staticmethod
    def _version(value):
        if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{24}", value) is None:
            raise ValueError("Model version must be a registry-issued 24-character hexadecimal ID")
        return value

    def register(self, artifact, now):
        validate_artifact(artifact)
        digest = self._identity(artifact)
        version = digest[:24]
        with self.lock():
            state = self.state()
            path = self.versions / (version + ".json")
            if path.exists():
                saved = json.loads(path.read_text())
                if self._identity(saved) != digest:
                    raise ValueError("Immutable model version conflicts with its recorded contents")
            else:
                atomic_json(path, artifact)
            state["versions"].setdefault(
                version,
                {
                    "sha256": digest,
                    "model_id": artifact["model_id"],
                    "registered_at": now.isoformat(),
                    "labels_available_through": artifact["labels_available_through"],
                    "status": "EXPERIMENTAL",
                    "protocol": artifact.get("protocol"),
                },
            )
            atomic_json(self.manifest, state)
        return version

    def read(self, version):
        version = self._version(version)
        state = self.state()
        metadata = state["versions"].get(version)
        if metadata is None:
            raise ValueError("Model version is not registered")
        path = self.versions / (version + ".json")
        if path.stat().st_size > 2_000_000:
            raise ValueError("Model JSON exceeds bounded size")
        artifact = validate_artifact(json.loads(path.read_text()))
        if self._identity(artifact) != metadata["sha256"]:
            raise ValueError("Model checksum mismatch; activation refused")
        return artifact

    def activate(self, version, output, now, *, rollback=False):
        with self.lock():
            artifact = self.read(version)
            if timestamp(artifact["labels_available_through"]) > now:
                raise ValueError("Future-trained model cannot be activated")
            state = self.state()
            previous = state.get("active_version")
            # This complete-file replacement never truncates the active artifact.
            atomic_json(output, artifact)
            state["active_version"] = version
            state["activations"].append(
                {
                    "version": version,
                    "previous": previous,
                    "at": now.isoformat(),
                    "action": "ROLLBACK" if rollback else "EXPLICIT_ACTIVATION",
                }
            )
            atomic_json(self.manifest, state)
        return version

    def rollback(self, output, now):
        state = self.state()
        previous = next(
            (
                r["previous"]
                for r in reversed(state["activations"])
                if r.get("previous") and r["version"] == state.get("active_version")
            ),
            None,
        )
        if previous is None:
            raise ValueError("No earlier explicitly activated model is available")
        return self.activate(previous, output, now, rollback=True)

    def due(self, now, minimum_hours=24):
        if not 1 <= minimum_hours <= 24 * 365:
            raise ValueError("Minimum update interval must be 1..8760 hours")
        previous = self.state().get("last_attempt_at")
        return previous is None or now >= timestamp(previous) + timedelta(hours=minimum_hours)

    def used_test_contracts(self):
        return {
            ticker
            for r in self.state().get("evaluations", [])
            for ticker in r.get("test_contracts", [])
        }

    def record_evaluation(self, report, now):
        with self.lock():
            state = self.state()
            state["last_attempt_at"] = now.isoformat()
            state["last_status"] = report["status"]
            if report.get("test_contracts"):
                state["evaluations"].append(
                    {
                        "at": now.isoformat(),
                        "test_contracts": report["test_contracts"],
                        "test_fingerprint": report.get("test_fingerprint"),
                        "protocol": report.get("protocol"),
                        "status": report["status"],
                    }
                )
            atomic_json(self.manifest, state)


def scheduled_evaluation(journal, directory, now=None):
    """Read only public prediction history; archive candidates, never activate."""
    from .learning import prepare_comparison

    now = now or datetime.now(timezone.utc)
    registry = ModelRegistry(directory)
    with registry.update_lock():
        if not registry.due(now):
            return {
                "status": "MODEL_UPDATE_NOT_DUE",
                "active_model_changed": False,
                "next_check": "Daily eligibility, hourly scheduler wakeup",
            }
        with closing(
            sqlite3.connect(Path(journal).resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            artifact, report, _rows = prepare_comparison(
                db, now, used_test_contracts=registry.used_test_contracts()
            )
        registry.record_evaluation(report, now)
        atomic_json(registry.directory / "latest-evaluation.json", report)
        version = registry.register(artifact, now) if artifact else None
        return {
            "status": report["status"],
            "official_contracts": report["official_contracts"],
            "trained": report["trained"],
            "archived_version": version,
            "active_model_changed": False,
            "reason": "Scheduled comparison cannot activate or promote a model, change risk limits, or place trades",
        }


def main():
    parser = argparse.ArgumentParser(
        description="Offline experimental JSON model registry; activation is always explicit"
    )
    parser.add_argument("--registry", type=Path, default=Path("signals_model_registry"))
    parser.add_argument("--output", type=Path, default=Path("signals_model.json"))
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--register", type=Path)
    actions.add_argument("--activate")
    actions.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    registry = ModelRegistry(args.registry)
    now = datetime.now(timezone.utc)
    try:
        if args.status:
            print(json.dumps(registry.state(), indent=2))
        elif args.register:
            if args.register.stat().st_size > 2_000_000:
                raise ValueError("Model JSON exceeds bounded size")
            version = registry.register(json.loads(args.register.read_text()), now)
            print("Archived experimental version " + version + "; active model unchanged")
        elif args.activate:
            print(
                "Explicitly activated experimental version "
                + registry.activate(args.activate, args.output, now)
            )
        else:
            print("Rolled back to experimental version " + registry.rollback(args.output, now))
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
