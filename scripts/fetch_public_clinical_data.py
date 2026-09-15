"""Download governed public clinical samples and normalize them for InsightFlow.

The generated `.data/public_clinical` directory is intentionally ignored by Git. Each source gets
its own provenance manifest and normalized CSV tables. Source classes remain separate because a
trial registry record, a spontaneous safety report, and a synthetic EHR record are not
interchangeable evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from typing import Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.clinical.profiling import DataProfiler  # noqa: E402
from app.clinical.public_sources import (  # noqa: E402
    ClinicalTrialsGovSource,
    OpenFdaDrugLabelSource,
    OpenFdaFaersSource,
    PublicDatasetBundle,
    SyntheaCsvSource,
    write_bundle,
)


TRIAL_FIELDS = "|".join(
    (
        "NCTId", "BriefTitle", "OverallStatus", "StartDate", "CompletionDate",
        "LeadSponsorName", "Condition", "StudyType", "Phase", "EnrollmentCount",
        "EnrollmentType", "InterventionType", "InterventionName",
        "InterventionDescription", "InterventionOtherName", "PrimaryOutcomeMeasure",
        "PrimaryOutcomeTimeFrame", "PrimaryOutcomeDescription", "SecondaryOutcomeMeasure",
        "SecondaryOutcomeTimeFrame", "SecondaryOutcomeDescription",
    )
)


def _get(client: httpx.Client, url: str, params: dict | None = None) -> bytes:
    response = client.get(url, params=params)
    if response.status_code == 403:
        # ClinicalTrials.gov currently blocks the TLS fingerprint of some Python HTTP clients while
        # accepting the same public request from curl. Use a non-shell argv fallback so query values
        # cannot become executable input and Windows/Linux both remain supported.
        executable = shutil.which("curl.exe") or shutil.which("curl")
        if executable:
            target = f"{url}?{urlencode(params or {})}" if params else url
            result = subprocess.run(
                [executable, "--fail", "--silent", "--show-error", "--location", target],
                check=True,
                capture_output=True,
            )
            return result.stdout
    response.raise_for_status()
    return response.content


def _save_and_summarize(name: str, bundle: PublicDatasetBundle, root: Path) -> dict:
    paths = write_bundle(bundle, root / name)
    profile = DataProfiler().profile(bundle.files)
    summary = {
        "source": bundle.manifest.source_name,
        "source_class": bundle.manifest.source_class,
        "source_records": bundle.manifest.record_count,
        "normalized_files": len(bundle.files),
        "normalized_rows": sum(item.row_count for item in bundle.files),
        "relationships": len(profile.relationships),
        "causal_use_allowed": bundle.manifest.causal_use_allowed,
        "files": [{"name": item.filename, "rows": item.row_count, "columns": len(item.columns)} for item in bundle.files],
        "written": [str(path) for path in paths],
    }
    (root / name / "profile_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def filter_required_fields(bundle: PublicDatasetBundle) -> tuple[PublicDatasetBundle, list[dict]]:
    """Drop registry child rows that cannot be published and retain an audit list."""

    required = {"interventions.csv": "intervention_name", "outcomes.csv": "measure"}
    cleaned = []
    rejected: list[dict] = []
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


def _get_paged(client: httpx.Client, url: str, total: int) -> bytes:
    """Fetch FAERS in bounded pages to avoid gateway rejection of very large responses."""

    results: list[dict] = []
    for offset in range(0, total, 100):
        limit = min(100, total - offset)
        page = json.loads(
            _get(
                client,
                url,
                {"limit": limit, "skip": offset},
            ).decode("utf-8-sig")
        )
        results.extend(page.get("results", []))
    return json.dumps({"results": results}, ensure_ascii=False).encode("utf-8")


def _collect_trial_pages(total: int, fetch_page: Callable[[str | None, int], dict]) -> bytes:
    studies: list[dict] = []
    token: str | None = None
    while len(studies) < total:
        page=fetch_page(token,min(1000,total-len(studies)))
        studies.extend(page.get("studies",[]))
        token=page.get("nextPageToken")
        if not token or not page.get("studies"): break
    return json.dumps({"studies":studies[:total]},ensure_ascii=False).encode("utf-8")


def _get_trials(client: httpx.Client, total: int) -> bytes:
    def fetch(token: str | None, page_size: int) -> dict:
        params={"format":"json","pageSize":page_size,"query.term":"AREA[StudyType]INTERVENTIONAL","fields":TRIAL_FIELDS}
        if token: params["pageToken"]=token
        return json.loads(_get(client,ClinicalTrialsGovSource.source_url,params).decode("utf-8-sig"))
    return _collect_trial_pages(total,fetch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="获取并验证公开临床测试数据")
    parser.add_argument("--output", type=Path, default=Path(".data/public_clinical"))
    parser.add_argument("--studies", type=int, default=5000, choices=range(1, 100001), metavar="1-100000")
    parser.add_argument("--faers", type=int, default=3000, choices=range(1, 100001), metavar="1-100000")
    parser.add_argument("--labels", type=int, default=3000, choices=range(1, 100001), metavar="1-100000")
    parser.add_argument("--skip-faers", action="store_true", help="只更新研究注册，不改写已有 FAERS 快照")
    parser.add_argument("--skip-labels", action="store_true", help="只更新研究注册，不改写已有 FDA 标签快照")
    parser.add_argument("--skip-synthea", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    summaries = []
    with httpx.Client(timeout=120, follow_redirects=True, headers={"User-Agent": "InsightFlow-Clinical/9 public-data-validation"}) as client:
        studies_payload = _get_trials(client,args.studies)
        studies_bundle, rejected = filter_required_fields(ClinicalTrialsGovSource().parse(studies_payload))
        study_summary = _save_and_summarize("clinicaltrials", studies_bundle, args.output)
        study_summary["rejected_rows"] = len(rejected)
        (args.output / "clinicaltrials" / "rejected_rows.json").write_text(
            json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (args.output / "clinicaltrials" / "profile_summary.json").write_text(
            json.dumps(study_summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summaries.append(study_summary)

        if not args.skip_faers:
            faers_payload = _get_paged(client, OpenFdaFaersSource.source_url, args.faers)
            summaries.append(_save_and_summarize("openfda_faers", OpenFdaFaersSource().parse(faers_payload), args.output))

        if not args.skip_labels:
            labels_payload = _get_paged(client, OpenFdaDrugLabelSource.source_url, args.labels)
            summaries.append(_save_and_summarize("openfda_labels", OpenFdaDrugLabelSource().parse(labels_payload), args.output))

        if not args.skip_synthea:
            synthea_payload = _get(client, SyntheaCsvSource.source_url)
            summaries.append(_save_and_summarize("synthea", SyntheaCsvSource().parse(synthea_payload), args.output))

    acceptance = {
        "status": "passed",
        "sources": summaries,
        "total_normalized_rows": sum(item["normalized_rows"] for item in summaries),
    }
    (args.output / "acceptance_summary.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))
    print(
        f"公开临床数据验收=通过 来源={len(summaries)} "
        f"规范化行数={acceptance['total_normalized_rows']} 输出={args.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

