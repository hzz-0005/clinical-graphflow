"""Publish validated public clinical bundles into the governed PostgreSQL domain store."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.clinical.domain_registry import DomainRegistry  # noqa: E402
from app.clinical.profiling import DataProfiler  # noqa: E402
from app.clinical.tabular import TabularFileParser  # noqa: E402
from app.clinical.transformation import TransformationValidator, _transform  # noqa: E402
from app.clinical.understanding_agent import RulesMappingProvider  # noqa: E402
from app.clinical.understanding_models import MappingContract  # noqa: E402


# This is a local, allow-listed public snapshot loader.  It is intentionally
# separate from the smaller interactive upload limit used by the API.
PUBLIC_SNAPSHOT_MAX_FILE_BYTES = 250_000_000


def _compile_source(source_dir: Path, registry: DomainRegistry):
    parser=TabularFileParser(max_file_bytes=PUBLIC_SNAPSHOT_MAX_FILE_BYTES)
    files=tuple(parser.parse(path.name,"text/csv",path.read_bytes()) for path in sorted(source_dir.glob("*.csv")))
    profile=DataProfiler().profile(files).model_dump(mode="json")
    schema={"files":[{"file_id":str(i),"filename":item.filename,"format":item.format,"row_count":item.row_count,"columns":list(item.columns)} for i,item in enumerate(files)]}
    proposal=RulesMappingProvider(registry).propose({"schema":schema,"profile":profile})
    registered=tuple(item for item in proposal["files"] if item["target_domain"]!="custom_candidate")
    contract=MappingContract(batch_id=f"public-{source_dir.name}",provider="rules",model="domain-registry-v1",visibility_level="L1",files=registered)
    by_id={str(i):item for i,item in enumerate(files)}
    report=TransformationValidator(registry).validate(by_id,contract)
    if not report.valid:raise ValueError(f"{source_dir.name} 公开数据未通过转换校验")
    records=[]; counts={}; trial_ids=set()
    for mapping in contract.files:
        source=by_id[mapping.file_id]; domain=registry.get(mapping.target_domain,mapping.target_version)
        for index,row in enumerate(source.rows,1):
            payload={field.target:_transform(row.get(field.source),field.transform) for field in mapping.fields}
            keys={key:payload.get(key) for key in domain.keys}
            row_key=hashlib.sha256(json.dumps([domain.name,domain.version,source.filename,index,keys],ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
            records.append((domain.name,domain.version,row_key,source.filename,payload));counts[domain.name]=counts.get(domain.name,0)+1
            if payload.get("STUDY_ID"):trial_ids.add(str(payload["STUDY_ID"]))
    manifest=json.loads((source_dir/"manifest.json").read_text(encoding="utf-8"))
    return manifest,records,counts,sorted(trial_ids),[item["source_filename"] for item in proposal["files"] if item["target_domain"]=="custom_candidate"]


def publish(root:Path,database_url:str)->dict:
    registry=DomainRegistry.from_yaml(Path("semantic/clinical_domains.yml")); summaries=[]
    with psycopg.connect(database_url) as connection:
        for source_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            manifest,records,counts,trial_ids,custom=_compile_source(source_dir,registry);batch_id=f"public-{source_dir.name}";now=datetime.now(timezone.utc)
            quality={"source":manifest,"domain_row_counts":counts,"custom_candidates":custom,"causal_use_allowed":False}
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM clinical_ingestion.import_batches WHERE batch_id=%s",(batch_id,))
                cursor.execute("""INSERT INTO clinical_ingestion.import_batches
                    (batch_id,actor_user_id,status,content_hash,trial_ids,record_count,quality,committed_at,published_at,published_by)
                    VALUES (%s,'public-data-loader','published',%s,%s,%s,%s,%s,%s,'public-data-loader')""",
                    (batch_id,manifest["content_hash"],trial_ids,len(records),Jsonb(quality),now,now))
                with cursor.copy("COPY clinical_ingestion.domain_records (batch_id,domain_name,domain_version,row_key,source_filename,payload_json) FROM STDIN") as copy:
                    for domain,version,row_key,filename,payload in records:copy.write_row((batch_id,domain,version,row_key,filename,Jsonb(payload)))
            summaries.append({"batch_id":batch_id,"source":manifest["source_name"],"records":len(records),"domains":counts,"custom_candidates":custom})
    return {"status":"published","batches":summaries,"total_records":sum(item["records"] for item in summaries)}


def main()->int:
    parser=argparse.ArgumentParser(description="将已验证公开临床数据发布到治理数据库")
    parser.add_argument("--root",type=Path,default=Path(".data/public_clinical"));parser.add_argument("--database-url",default=os.getenv("PUBLIC_DATABASE_URL") or os.getenv("DATABASE_URL"))
    args=parser.parse_args()
    if not args.database_url:raise SystemExit("请通过 --database-url 或 PUBLIC_DATABASE_URL 提供管理员数据库连接")
    result=publish(args.root,args.database_url);print(json.dumps(result,ensure_ascii=False,indent=2));print(f"公开数据发布=通过 批次={len(result['batches'])} 入库记录={result['total_records']}");return 0


if __name__=="__main__":
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

