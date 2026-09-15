"""Fetch a reproducible larger sample of public FDA structured labels."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.clinical.profiling import DataProfiler  # noqa: E402
from app.clinical.public_sources import OpenFdaDrugLabelSource, write_bundle  # noqa: E402
from fetch_public_clinical_data import _get_paged  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="获取公开 FDA 结构化药品标签样本")
    parser.add_argument("--labels", type=int, default=10000, choices=range(1, 25001), metavar="1-25000")
    parser.add_argument("--output", type=Path, default=Path(".data/public_clinical/openfda_labels"))
    args = parser.parse_args()

    with httpx.Client(
        timeout=120,
        follow_redirects=True,
        headers={"User-Agent": "InsightFlow-Clinical/16 openfda-label-validation"},
    ) as client:
        payload = _get_paged(client, OpenFdaDrugLabelSource.source_url, args.labels)

    bundle = OpenFdaDrugLabelSource().parse(payload)
    query = f"{OpenFdaDrugLabelSource.source_url}?limit={args.labels}"
    limitations = (
        "药品说明书是监管参考文本，不是临床试验疗效数据，也不能替代患者个体医疗建议",
        "本批次是公开 API 的前 N 条样本，不代表完整标签库",
    )
    bundle = bundle.model_copy(
        update={
            "manifest": bundle.manifest.model_copy(
                update={"source_name": "openFDA Drug Labels (sample)", "source_url": query, "limitations": limitations}
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
        "files": [{"name": item.filename, "rows": item.row_count, "columns": len(item.columns)} for item in bundle.files],
        "written": [str(path) for path in written],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "profile_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "scope.json").write_text(
        json.dumps(
            {
                "source_name": bundle.manifest.source_name,
                "source_url": query,
                "filter": {"api_limit": args.labels},
                "requested_records": args.labels,
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
    print(f"FDA 标签数据=获取完成 标签数={bundle.manifest.record_count} 输出={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

