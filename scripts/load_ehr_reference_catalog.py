"""Load an audited V27 EHR reference-range catalog into PostgreSQL.

The loader is intentionally owner-side: the application reader can execute the aggregate
function but cannot write or read the catalog table directly.  It accepts YAML so a catalog can
be code-reviewed before insertion, and it never replaces an existing version in place.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import psycopg
import yaml


INSERT_SQL = """
    INSERT INTO analytics_clinical_core.ehr_reference_ranges (
        catalog_version, concept_code, unit, low, high, boundary_policy,
        population_context, source, source_uri, source_version, source_content_hash,
        review_status, effective_from, effective_to, reviewed_by, reviewed_at
    ) VALUES (
        %(catalog_version)s, %(concept_code)s, %(unit)s, %(low)s, %(high)s,
        'inclusive_normal', %(population_context)s, %(source)s, %(source_uri)s,
        %(source_version)s, %(source_content_hash)s, %(review_status)s,
        %(effective_from)s, %(effective_to)s, %(reviewed_by)s, %(reviewed_at)s
    )
"""


def _required_text(payload: dict[str, Any], key: str, limit: int) -> str:
    value = str(payload.get(key, "")).strip()
    if not value or len(value) > limit:
        raise ValueError(f"{key} must be a non-empty string of at most {limit} characters")
    return value


def _date(value: Any, key: str, *, required: bool) -> str | None:
    text = str(value or "").strip()
    if not text and not required:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{key} must use YYYY-MM-DD") from exc


def _decimal(value: Any, key: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{key} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{key} must be a finite decimal")
    return parsed


def load_payload(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("catalog YAML must contain a mapping")

    catalog_version = _required_text(payload, "catalog_version", 40)
    source = _required_text(payload, "source", 500)
    source_uri = str(payload.get("source_uri") or "").strip() or None
    source_version = str(payload.get("source_version") or "").strip() or None
    if not source_uri and not source_version:
        raise ValueError("published catalog requires source_uri or source_version")
    status = str(payload.get("review_status", "draft")).strip()
    if status not in {"draft", "published", "withdrawn"}:
        raise ValueError("review_status must be draft, published, or withdrawn")
    reviewed_by = str(payload.get("reviewed_by") or "").strip() or None
    reviewed_at_text = str(payload.get("reviewed_at") or "").strip() or None
    reviewed_at = None
    if reviewed_at_text:
        try:
            reviewed_at = datetime.fromisoformat(reviewed_at_text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reviewed_at must be an ISO-8601 datetime") from exc
    if status == "published" and (not reviewed_by or reviewed_at is None):
        raise ValueError("published catalog requires reviewed_by and reviewed_at")

    effective_from = _date(payload.get("effective_from"), "effective_from", required=True)
    effective_to = _date(payload.get("effective_to"), "effective_to", required=False)
    source_content_hash = str(payload.get("source_content_hash") or "").strip() or None
    if source_content_hash is not None and (
        len(source_content_hash) != 64 or any(char not in "0123456789abcdef" for char in source_content_hash)
    ):
        raise ValueError("source_content_hash must be a lowercase SHA-256 hex digest")

    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("catalog entries must be a non-empty list")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"entries[{index}] must be a mapping")
        concept_code = _required_text(item, "concept_code", 200)
        unit = _required_text(item, "unit", 80)
        population_context = str(item.get("population_context", "general")).strip()
        if not population_context or len(population_context) > 120:
            raise ValueError(f"entries[{index}].population_context is invalid")
        low = _decimal(item.get("low"), f"entries[{index}].low")
        high = _decimal(item.get("high"), f"entries[{index}].high")
        if low > high:
            raise ValueError(f"entries[{index}] low must not exceed high")
        key = (catalog_version, concept_code, unit, population_context)
        if key in seen:
            raise ValueError(f"duplicate catalog key: {key}")
        seen.add(key)
        rows.append({
            "catalog_version": catalog_version,
            "concept_code": concept_code,
            "unit": unit,
            "low": low,
            "high": high,
            "population_context": population_context,
            "source": source,
            "source_uri": source_uri,
            "source_version": source_version,
            "source_content_hash": source_content_hash,
            "review_status": status,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        })
    return rows, catalog_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="reviewed catalog YAML file")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("ENTERPRISE_DATABASE_URL", ""),
        help="owner PostgreSQL DSN (defaults to ENTERPRISE_DATABASE_URL)",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate without inserting rows")
    args = parser.parse_args()
    if not args.database_url and not args.dry_run:
        parser.error("--database-url or ENTERPRISE_DATABASE_URL is required unless --dry-run is used")

    rows, version = load_payload(args.catalog)
    if args.dry_run:
        print(f"validated {len(rows)} rows for catalog {version}; no database changes")
        return 0

    with psycopg.connect(args.database_url) as connection:
        with connection.cursor() as cursor:
            for row in rows:
                cursor.execute(INSERT_SQL, row)
    print(f"loaded {len(rows)} rows for catalog {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

