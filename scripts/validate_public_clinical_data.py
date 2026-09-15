"""Validate downloaded public samples against registered clinical domain plugins."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.clinical.domain_registry import DomainRegistry  # noqa: E402
from app.clinical.profiling import DataProfiler  # noqa: E402
from app.clinical.tabular import TabularFileParser  # noqa: E402
from app.clinical.transformation import TransformationValidator  # noqa: E402
from app.clinical.understanding_agent import RulesMappingProvider  # noqa: E402
from app.clinical.understanding_models import MappingContract  # noqa: E402


# Public snapshots are downloaded from an allow-listed source and can contain
# larger normalized tables than a user upload.  The interactive upload limit
# remains unchanged; this limit only applies to the local public-data audit.
PUBLIC_SNAPSHOT_MAX_FILE_BYTES = 250_000_000


def main() -> int:
    root = Path(".data/public_clinical")
    registry = DomainRegistry.from_yaml(Path("semantic/clinical_domains.yml"))
    parser = TabularFileParser(max_file_bytes=PUBLIC_SNAPSHOT_MAX_FILE_BYTES)
    sources: list[dict] = []
    failed = False
    for source_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        files = tuple(
            parser.parse(path.name, "text/csv", path.read_bytes())
            for path in sorted(source_dir.glob("*.csv"))
        )
        profile = DataProfiler().profile(files).model_dump(mode="json")
        schema = {
            "files": [
                {
                    "file_id": str(index),
                    "filename": item.filename,
                    "format": item.format,
                    "row_count": item.row_count,
                    "columns": list(item.columns),
                }
                for index, item in enumerate(files)
            ]
        }
        proposal = RulesMappingProvider(registry).propose(
            {"schema": schema, "profile": profile}
        )
        registered = tuple(
            item for item in proposal["files"] if item["target_domain"] != "custom_candidate"
        )
        custom = tuple(
            item["source_filename"]
            for item in proposal["files"]
            if item["target_domain"] == "custom_candidate"
        )
        contract = MappingContract(
            batch_id=f"public-{source_dir.name}",
            provider="rules",
            model="domain-registry-v1",
            visibility_level="L1",
            files=registered,
        )
        report = TransformationValidator(registry).validate(
            {str(index): item for index, item in enumerate(files)}, contract
        )
        failed = failed or not report.valid
        sources.append(
            {
                "source": source_dir.name,
                "registered_files": len(registered),
                "custom_candidates": list(custom),
                "accepted_registered_rows": report.accepted_rows,
                "rejected_registered_rows": report.rejected_rows,
                "valid": report.valid,
                "mappings": {
                    item["source_filename"]: item["target_domain"]
                    for item in proposal["files"]
                },
            }
        )
    summary = {
        "status": "failed" if failed else "passed",
        "sources": sources,
        "registered_rows": sum(item["accepted_registered_rows"] for item in sources),
        "custom_candidate_files": sum(len(item["custom_candidates"]) for item in sources),
        "policy": "自定义候选保留在隔离区，不强行映射到错误的临床数据域",
    }
    target = root / "domain_acceptance_summary.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        f"PublicDomainAcceptance={summary['status']} / 公开数据域验收="
        f"{'通过' if not failed else '失败'} RegisteredRows={summary['registered_rows']} / "
        f"已注册行数={summary['registered_rows']} CustomFiles={summary['custom_candidate_files']} / "
        f"待注册文件={summary['custom_candidate_files']}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

