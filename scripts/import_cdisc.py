from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.clinical.ingestion import CdiscCsvIngestionService


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and de-identify CDISC CSV files")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    salt = os.environ.get("CDISC_PSEUDONYM_SALT")
    if not salt:
        raise SystemExit("CDISC_PSEUDONYM_SALT is required")
    manifest = CdiscCsvIngestionService(salt).read_directory(args.directory)
    print(json.dumps({"source_format": manifest.source_format, "trial_ids": manifest.trial_ids, "record_count": manifest.record_count}, ensure_ascii=False))


if __name__ == "__main__":
    main()

