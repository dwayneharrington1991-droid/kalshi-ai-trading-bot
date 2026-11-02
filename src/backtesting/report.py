"""Dependency-free JSON and CSV exports for shadow evaluation."""

import csv
import io
import json
import re
from typing import Any, Mapping, Sequence


def _sanitize(value: Any) -> Any:
    sensitive = ("secret", "key", "credential", "authorization", "header", "signature", "pem", "private_path")
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if any(part in str(key).lower() for part in sensitive) else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and (
        "-----BEGIN" in value or value.lower().startswith("bearer ")
        or value.lower().endswith(".pem") or re.search(r"\b(?:sk|key)-[A-Za-z0-9_-]{8,}", value)
    ):
        return "[REDACTED]"
    return value


def json_report(result: Mapping[str, Any]) -> str:
    return json.dumps(_sanitize(result), sort_keys=True, indent=2, allow_nan=False)


def csv_report(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    output = io.StringIO()
    clean_rows = [_sanitize(row) for row in rows]
    fields = sorted({key for row in clean_rows for key in row})
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(clean_rows)
    return output.getvalue()
