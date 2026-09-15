"""Seed a reproducible, larger subject-level clinical trial fixture.

This is synthetic validation data, not public or real participant data.  It is
kept separate from the public registry/FAERS/label snapshots because only the
subject-level fixture can exercise Week-12 efficacy, adherence and site-quality
calculations.  ``--reset`` is intentionally explicit so a caller cannot erase
the current clinical raw tables by accident.
"""

from __future__ import annotations

import argparse
import json
import os

from data_generator.clinical.generator import generate_clinical_dataset, write_clinical_dataset
from data_generator.clinical.scenarios.site_17 import apply_site_17_scenario


def build_dataset(*, seed: int, participant_count: int, scenario: str):
    dataset = generate_clinical_dataset(seed=seed, participant_count=participant_count)
    if scenario == "site_17":
        dataset = apply_site_17_scenario(dataset)
    return dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="写入可复现的大样本合成临床试验夹具")
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--participant-count", type=int, default=3000, metavar="N")
    parser.add_argument("--scenario", choices=("none", "site_17"), default="site_17")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL") or os.getenv("ENTERPRISE_DATABASE_URL"))
    parser.add_argument("--reset", action="store_true", help="清空 clinical_raw 后再写入；默认不清空")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.database_url:
        raise SystemExit("缺少 --database-url 或 DATABASE_URL/ENTERPRISE_DATABASE_URL")
    if args.participant_count < 1:
        raise SystemExit("--participant-count 必须大于 0")

    dataset = build_dataset(seed=args.seed, participant_count=args.participant_count, scenario=args.scenario)
    manifest = write_clinical_dataset(args.database_url, dataset, reset=args.reset)
    print(json.dumps({
        "status": "written",
        "synthetic": True,
        "scenario": args.scenario,
        "reset": args.reset,
        "seed": manifest.seed,
        "trial_id": manifest.trial_id,
        "table_counts": manifest.table_counts,
        "content_hash": manifest.content_hash,
    }, ensure_ascii=False, indent=2))
    print(f"SyntheticClinicalFixture=written / 合成临床夹具=已写入 participants={manifest.table_counts['participants']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

