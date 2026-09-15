"""Fetch a large, explicitly scoped China-location ClinicalTrials.gov snapshot.

This is registry metadata only. It is not Chinese participant-level efficacy data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.clinical.profiling import DataProfiler  # noqa: E402
from app.clinical.public_sources import ClinicalTrialsGovSource, write_bundle  # noqa: E402
from fetch_public_clinical_data import TRIAL_FIELDS, _collect_trial_pages, _get  # noqa: E402


def fetch_china_trials(client: httpx.Client, total: int) -> tuple[bytes, str]:
    params_base = {
        "format": "json",
        "query.locn": "China",
        "query.term": "AREA[StudyType]INTERVENTIONAL",
        "fields": TRIAL_FIELDS,
    }

    def fetch(token: str | None, page_size: int) -> dict:
        params = {**params_base, "pageSize": page_size}
        if token:
            params["pageToken"] = token
        return json.loads(_get(client, ClinicalTrialsGovSource.source_url, params).decode("utf-8-sig"))

    query = f"{ClinicalTrialsGovSource.source_url}?{urlencode({**params_base, 'pageSize': total})}"
    return _collect_trial_pages(total, fetch), query


def filter_required_fields(bundle):
    """Keep the standard-domain rows publishable and retain rejects for audit."""
    required = {"interventions.csv": "intervention_name", "outcomes.csv": "measure"}
    cleaned = []
    rejected = []
    for item in bundle.files:
        required_field = required.get(item.filename)
        rows = []
        for row_number, row in enumerate(item.rows, 1):
            if required_field and not str(row.get(required_field) or "").strip():
                rejected.append(
                    {
                        "filename": item.filename,
                        "row_number": row_number,
                        "required_field": required_field,
                        "row": dict(row),
                    }
                )
            else:
                rows.append(dict(row))
        canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        cleaned.append(
            item.model_copy(
                update={
                    "rows": tuple(rows),
                    "row_count": len(rows),
                    "content_hash": hashlib.sha256(canonical).hexdigest(),
                }
            )
        )
    return bundle.model_copy(update={"files": tuple(cleaned)}), rejected


def main() -> int:
    parser = argparse.ArgumentParser(description="获取中国地点临床研究注册元数据")
    parser.add_argument("--studies", type=int, default=10000, choices=range(1, 100001), metavar="1-100000")
    parser.add_argument("--output", type=Path, default=Path(".data/public_clinical/china_clinicaltrials"))
    args = parser.parse_args()

    with httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers={"User-Agent": "InsightFlow-Clinical/16 china-public-data-validation"},
    ) as client:
        payload, query = fetch_china_trials(client, args.studies)

    bundle = ClinicalTrialsGovSource().parse(payload)
    limitations = (
        "这是按研究中心所在国家筛选的研究注册元数据，不是中国受试者级疗效数据",
        "注册信息和汇总结果不能替代受试者级数据，也不能单独证明因果关系",
    )
    bundle = bundle.model_copy(
        update={
            "manifest": bundle.manifest.model_copy(
                update={
                    "source_name": "ClinicalTrials.gov (China locations)",
                    "source_url": query,
                    "limitations": limitations,
                }
            )
        }
    )
    bundle, rejected = filter_required_fields(bundle)
    written = write_bundle(bundle, args.output)
    profile = DataProfiler().profile(bundle.files)
    summary = {
        "source": bundle.manifest.source_name,
        "source_class": bundle.manifest.source_class,
        "source_records": bundle.manifest.record_count,
        "normalized_files": len(bundle.files),
        "normalized_rows": sum(item.row_count for item in bundle.files),
        "relationships": len(profile.relationships),
        "causal_use_allowed": bundle.manifest.causal_use_allowed,
        "rejected_rows": len(rejected),
        "files": [{"name": item.filename, "rows": item.row_count, "columns": len(item.columns)} for item in bundle.files],
        "written": [str(path) for path in written],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    rejected_path = args.output / "rejected_rows.json"
    rejected_path.write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "profile_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "scope.json").write_text(
        json.dumps(
            {
                "source_name": bundle.manifest.source_name,
                "source_url": query,
                "filter": {"location_country": "China", "study_type": "INTERVENTIONAL"},
                "requested_records": args.studies,
                "normalized_records": bundle.manifest.record_count,
                "rejected_rows": len(rejected),
                "causal_use_allowed": False,
                "limitations": list(limitations),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"中国研究注册数据=获取完成 研究数={bundle.manifest.record_count} 规范化行数={summary['normalized_rows']} 拒收行={len(rejected)} 输出={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

