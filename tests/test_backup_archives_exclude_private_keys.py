import re
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def test_committed_backup_archives_contain_no_private_keys():
    for archive in (ROOT / "backups").glob("*.tar.gz"):
        with tarfile.open(archive, mode="r:gz") as backup:
            for member in backup:
                assert "private_key" not in member.name.lower()
                if not member.isfile():
                    continue
                contents = backup.extractfile(member)
                assert contents is not None
                assert PRIVATE_KEY_HEADER.search(contents.read()) is None
