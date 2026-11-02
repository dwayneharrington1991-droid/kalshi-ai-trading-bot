import hashlib
import importlib.util
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location("verify_restore", Path(__file__).resolve().parents[1] / "deploy/oracle/verify_restore.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def backup(tmp_path):
    path = tmp_path / "backup.db"
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO evidence(value) VALUES ('preserved')")
    path.with_suffix('.db.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest() + '  backup.db\n')
    return path


def test_restore_checks_integrity_counts_and_private_permissions(tmp_path):
    source = backup(tmp_path)
    original = source.read_bytes()
    destination = tmp_path / 'restored.db'
    result = module.verify_restore(source, destination)
    assert result['status'] == 'PASS'
    assert result['off_host_durability'] == 'NOT_VERIFIED'
    assert source.read_bytes() == original
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as db:
        assert db.execute('SELECT value FROM evidence').fetchone() == ('preserved',)


def test_restore_refuses_overwrite_and_tampered_backup(tmp_path):
    source = backup(tmp_path)
    destination = tmp_path / 'existing.db'
    destination.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):
        module.verify_restore(source, destination)
    assert destination.read_bytes() == b'preserve'
    with source.open('ab') as stream:
        stream.write(b'tampered')
    with pytest.raises(ValueError, match='checksum'):
        module.verify_restore(source, tmp_path / 'new.db')
    assert not (tmp_path / 'new.db').exists()
