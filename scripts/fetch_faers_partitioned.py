"""Fetch a larger FAERS sample by non-overlapping date partitions.

The openFDA endpoint rejects large ``skip`` offsets.  Partitioning by the
indexed ``receivedate`` field keeps every request below that limit and records
the exact periods in the local provenance manifest.  This data remains a
spontaneous-report signal; it is not a causal or incidence dataset.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.clinical.profiling import DataProfiler  # noqa: E402
from app.clinical.public_sources import OpenFdaFaersSource, write_bundle  # noqa: E402
from fetch_public_clinical_data import _get  # noqa: E402


DEFAULT_PERIODS = (
    ("2020-01-01", "2020-12-31"),
    ("2021-01-01", "2021-12-31"),
    ("2022-01-01", "2022-12-31"),
    ("2023-01-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
)


def fetch_period(client: httpx.Client, start: str, end: str, total: int) -> list[dict]:
    search = f"receivedate:[{start} TO {end}]"
    results: list[dict] = []
    for offset in range(0, total, 100):
        limit = min(100, total - offset)
        payload = json.loads(
            _get(
                client,
                OpenFdaFaersSource.source_url,
                {"search": search, "limit": limit, "skip": offset},
            ).decode("utf-8-sig")
        )
        page = payload.get("results", [])
        if not isinstance(page, list) or not page:
            break
        results.extend(item for item in page if isinstance(item, dict))
        if len(page) < limit:
            break
    return results[:total]


def main() -> int:
    parser = argparse.ArgumentParser(description="按接收日期分段获取大样本 FAERS 报告")
    parser.add_argument("--per-period", type=int, default=10000, choices=range(1, 25001), metavar="1-25000")
    parser.add_argument("--output", type=Path, default=Path(".data/public_clinical/openfda_faers"))
    args = parser.parse_args()

    raw_by_id: dict[str, dict] = {}
    periods: list[dict] = []
    with httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers={"User-Agent": "InsightFlow-Clinical/16 faers-partitioned-validation"},
    ) as client:
        for start, end in DEFAULT_PERIODS:
            page = fetch_period(client, start, end, args.per_period)
            accepted = 0
            for item in page:
                report_id = str(item.get("safetyreportid") or "")
                if report_id and report_id not in raw_by_id:
                    raw_by_id[report_id] = item
                    accepted += 1
            periods.append({"start": start, "end": end, "requested": args.per_period, "downloaded": len(page), "new_report_ids": accepted})
            print(f"FAERS 分段完成 {start} 至 {end}：下载 {len(page)} 份，去重新增 {accepted} 份", flush=True)

    raw_payload = json.dumps({"results": list(raw_by_id.values())}, ensure_ascii=False).encode("utf-8")
    bundle = OpenFdaFaersSource().parse(raw_payload)
    query = "&".join(f"search=receivedate%3A%5B{start}%20TO%20{end}%5D" for start, end in DEFAULT_PERIODS)
    limitations = (
        "自发报告不能证明药品导致不良反应，也不能用于估算发生率",
        "同一报告中的多个药品与多个反应没有逐一因果对应关系",
        "本批次按接收日期分段抽取，每段最多取请求数量，不代表完整年度数据",
    )
    bundle = bundle.model_copy(
        update={
            "manifest": bundle.manifest.model_copy(
                update={
                    "source_name": "openFDA FAERS (partitioned sample)",
                    "source_url": f"{OpenFdaFaersSource.source_url}?{query}",
                    "limitations": limitations,
                }
            )
        }
    )
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
        "periods": periods,
        "files": [{"name": item.filename, "rows": item.row_count, "columns": len(item.columns)} for item in bundle.files],
        "written": [str(path) for path in written],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "profile_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "scope.json").write_text(
        json.dumps(
            {
                "source_name": bundle.manifest.source_name,
                "source_url": bundle.manifest.source_url,
                "filter": {"receivedate_periods": periods},
                "requested_records": args.per_period * len(DEFAULT_PERIODS),
                "normalized_records": bundle.manifest.record_count,
                "causal_use_allowed": False,
                "limitations": list(limitations),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"FAERS 分段数据=获取完成 报告数={bundle.manifest.record_count} 规范化行数={summary['normalized_rows']} 输出={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

