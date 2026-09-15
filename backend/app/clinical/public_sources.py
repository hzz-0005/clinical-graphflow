from __future__ import annotations

import hashlib
import io
import json
import csv
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clinical.tabular import TabularFile, TabularFileParser, normalize_cell
from app.clinical.terminology import normalize_drug_term, reaction_label_zh


SourceClass = Literal["study_registry", "safety_signal", "drug_reference", "synthetic_patient"]


class ProvenanceManifest(BaseModel):
    """Auditable origin and analytical boundary for one acquired public dataset."""

    model_config = ConfigDict(frozen=True)
    source_name: str
    source_class: SourceClass
    source_url: str
    retrieved_at: datetime
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_count: int = Field(ge=0)
    causal_use_allowed: bool
    limitations: tuple[str, ...] = ()


class PublicDatasetBundle(BaseModel):
    model_config = ConfigDict(frozen=True)
    manifest: ProvenanceManifest
    files: tuple[TabularFile, ...]


def write_bundle(bundle: PublicDatasetBundle, output_dir: Path) -> tuple[Path, ...]:
    """Persist normalized tables plus provenance without retaining the raw source payload."""

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in bundle.files:
        filename = Path(item.filename).name
        if filename != item.filename:
            raise ValueError("公开数据输出文件名不安全")
        target = output_dir / filename
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(item.columns), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(item.rows)
        written.append(target)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(bundle.manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written.append(manifest_path)
    return tuple(written)


def _object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("公开数据源返回的 JSON 无法解析") from exc
    if not isinstance(value, dict):
        raise ValueError("公开数据源必须返回 JSON 对象")
    return value


def _rows_file(filename: str, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> TabularFile:
    normalized = tuple({column: normalize_cell(row.get(column)) for column in columns} for row in rows)
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return TabularFile(
        filename=filename,
        format="csv",
        columns=columns,
        row_count=len(normalized),
        rows=normalized,
        content_hash=hashlib.sha256(canonical).hexdigest(),
    )


def _manifest(*, source_name: str, source_class: SourceClass, source_url: str, payload: bytes, record_count: int, causal_use_allowed: bool, limitations: tuple[str, ...]) -> ProvenanceManifest:
    return ProvenanceManifest(
        source_name=source_name,
        source_class=source_class,
        source_url=source_url,
        retrieved_at=datetime.now(timezone.utc),
        content_hash=hashlib.sha256(payload).hexdigest(),
        record_count=record_count,
        causal_use_allowed=causal_use_allowed,
        limitations=limitations,
    )


class ClinicalTrialsGovSource:
    source_url = "https://clinicaltrials.gov/api/v2/studies"

    def parse(self, payload: bytes) -> PublicDatasetBundle:
        raw = _object(payload)
        studies_raw = raw.get("studies", [])
        if not isinstance(studies_raw, list):
            raise ValueError("ClinicalTrials.gov 响应缺少 studies 数组")
        studies: list[dict[str, Any]] = []
        interventions: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        for study in studies_raw:
            protocol = study.get("protocolSection", {}) if isinstance(study, dict) else {}
            identity = protocol.get("identificationModule", {})
            study_id = identity.get("nctId")
            if not study_id:
                continue
            status = protocol.get("statusModule", {})
            design = protocol.get("designModule", {})
            conditions = protocol.get("conditionsModule", {})
            sponsor = protocol.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
            enrollment = design.get("enrollmentInfo", {})
            studies.append({
                "study_id": study_id,
                "brief_title": identity.get("briefTitle"),
                "study_type": design.get("studyType"),
                "overall_status": status.get("overallStatus"),
                "phases": "|".join(design.get("phases", []) or []),
                "enrollment_count": enrollment.get("count"),
                "enrollment_type": enrollment.get("type"),
                "start_date": status.get("startDateStruct", {}).get("date"),
                "completion_date": status.get("completionDateStruct", {}).get("date"),
                "lead_sponsor": sponsor.get("name"),
                "conditions": "|".join(conditions.get("conditions", []) or []),
            })
            arms = protocol.get("armsInterventionsModule", {})
            for sequence, intervention in enumerate(arms.get("interventions", []) or [], 1):
                interventions.append({
                    "study_id": study_id,
                    "intervention_id": f"{study_id}-I{sequence:03d}",
                    "intervention_type": intervention.get("type"),
                    "intervention_name": intervention.get("name"),
                    "description": intervention.get("description"),
                    "other_names": "|".join(intervention.get("otherNames", []) or []),
                })
            outcome_module = protocol.get("outcomesModule", {})
            for outcome_type, key in (("PRIMARY", "primaryOutcomes"), ("SECONDARY", "secondaryOutcomes"), ("OTHER", "otherOutcomes")):
                for sequence, outcome in enumerate(outcome_module.get(key, []) or [], 1):
                    outcomes.append({
                        "study_id": study_id,
                        "outcome_id": f"{study_id}-{outcome_type[0]}{sequence:03d}",
                        "outcome_type": outcome_type,
                        "measure": outcome.get("measure"),
                        "time_frame": outcome.get("timeFrame"),
                        "description": outcome.get("description"),
                    })
        files = (
            _rows_file("studies.csv", ("study_id", "brief_title", "study_type", "overall_status", "phases", "enrollment_count", "enrollment_type", "start_date", "completion_date", "lead_sponsor", "conditions"), studies),
            _rows_file("interventions.csv", ("study_id", "intervention_id", "intervention_type", "intervention_name", "description", "other_names"), interventions),
            _rows_file("outcomes.csv", ("study_id", "outcome_id", "outcome_type", "measure", "time_frame", "description"), outcomes),
        )
        return PublicDatasetBundle(
            manifest=_manifest(source_name="ClinicalTrials.gov", source_class="study_registry", source_url=self.source_url, payload=payload, record_count=len(studies), causal_use_allowed=False, limitations=("研究注册与结果元数据不能替代受试者级临床试验数据，也不能单独证明因果关系",)),
            files=files,
        )


class OpenFdaFaersSource:
    source_url = "https://api.fda.gov/drug/event.json"

    def parse(self, payload: bytes) -> PublicDatasetBundle:
        raw = _object(payload)
        results = raw.get("results", [])
        if not isinstance(results, list):
            raise ValueError("openFDA 响应缺少 results 数组")
        reports: list[dict[str, Any]] = []
        drugs: list[dict[str, Any]] = []
        reactions: list[dict[str, Any]] = []
        for report in results:
            if not isinstance(report, dict) or not report.get("safetyreportid"):
                continue
            report_id = str(report["safetyreportid"])
            patient = report.get("patient", {}) if isinstance(report.get("patient"), dict) else {}
            reports.append({
                "safety_report_id": report_id,
                "received_date": report.get("receivedate"),
                "serious": report.get("serious"),
                "occur_country": report.get("occurcountry"),
                "patient_sex": patient.get("patientsex"),
                "patient_age": patient.get("patientonsetage"),
                "patient_age_unit": patient.get("patientonsetageunit"),
            })
            for sequence, drug in enumerate(patient.get("drug", []) or [], 1):
                medicinal_product=drug.get("medicinalproduct")
                canonical, medicinal_product_zh=normalize_drug_term(medicinal_product)
                drugs.append({
                    "safety_report_id": report_id,
                    "drug_sequence": sequence,
                    "drug_role": drug.get("drugcharacterization"),
                    "medicinal_product": medicinal_product,
                    "medicinal_product_canonical": canonical,
                    "medicinal_product_zh": medicinal_product_zh,
                    "indication": drug.get("drugindication"),
                    "route": drug.get("drugadministrationroute"),
                    "dose_text": drug.get("drugdosagetext"),
                })
            for sequence, reaction in enumerate(patient.get("reaction", []) or [], 1):
                reaction_term=reaction.get("reactionmeddrapt")
                reactions.append({
                    "safety_report_id": report_id,
                    "reaction_sequence": sequence,
                    "reaction_term": reaction_term,
                    "reaction_term_zh": reaction_label_zh(reaction_term),
                    "reaction_outcome": reaction.get("reactionoutcome"),
                })
        files = (
            _rows_file("faers_reports.csv", ("safety_report_id", "received_date", "serious", "occur_country", "patient_sex", "patient_age", "patient_age_unit"), reports),
            _rows_file("faers_drugs.csv", ("safety_report_id", "drug_sequence", "drug_role", "medicinal_product", "medicinal_product_canonical", "medicinal_product_zh", "indication", "route", "dose_text"), drugs),
            _rows_file("faers_reactions.csv", ("safety_report_id", "reaction_sequence", "reaction_term", "reaction_term_zh", "reaction_outcome"), reactions),
        )
        return PublicDatasetBundle(
            manifest=_manifest(source_name="openFDA FAERS", source_class="safety_signal", source_url=self.source_url, payload=payload, record_count=len(reports), causal_use_allowed=False, limitations=("自发报告不能证明药品导致不良反应，也不能用于估算发生率", "同一报告中的多个药品与多个反应没有逐一因果对应关系")),
            files=files,
        )


class OpenFdaDrugLabelSource:
    """FDA Structured Product Labeling used as reference context, never outcome evidence."""

    source_url = "https://api.fda.gov/drug/label.json"

    @staticmethod
    def _join(value: Any) -> str:
        if isinstance(value, list):
            return " | ".join(str(item).strip() for item in value if str(item).strip())
        return "" if value is None else str(value).strip()

    def parse(self, payload: bytes) -> PublicDatasetBundle:
        raw = _object(payload)
        results = raw.get("results", [])
        if not isinstance(results, list):
            raise ValueError("openFDA 药品说明书响应缺少 results 数组")
        rows=[]
        for item in results:
            if not isinstance(item,dict):continue
            openfda=item.get("openfda",{}) if isinstance(item.get("openfda"),dict) else {}
            label_id=item.get("id") or item.get("set_id")
            if not label_id:continue
            rows.append({
                "label_id":label_id,"set_id":item.get("set_id"),"effective_time":item.get("effective_time"),
                "brand_names":self._join(openfda.get("brand_name")),"generic_names":self._join(openfda.get("generic_name")),
                "manufacturer_names":self._join(openfda.get("manufacturer_name")),"product_types":self._join(openfda.get("product_type")),
                "routes":self._join(openfda.get("route")),"indications_and_usage":self._join(item.get("indications_and_usage")),
                "contraindications":self._join(item.get("contraindications")),"boxed_warning":self._join(item.get("boxed_warning")),
                "warnings":self._join(item.get("warnings") or item.get("warnings_and_cautions")),"adverse_reactions":self._join(item.get("adverse_reactions")),
            })
        columns=("label_id","set_id","effective_time","brand_names","generic_names","manufacturer_names","product_types","routes","indications_and_usage","contraindications","boxed_warning","warnings","adverse_reactions")
        return PublicDatasetBundle(
            manifest=_manifest(source_name="openFDA Drug Labels",source_class="drug_reference",source_url=self.source_url,payload=payload,record_count=len(rows),causal_use_allowed=False,limitations=("药品说明书是监管参考文本，不是临床试验疗效数据，也不能替代患者个体医疗建议",)),
            files=(_rows_file("drug_labels.csv",columns,rows),),
        )
class SyntheaCsvSource:
    source_url = "https://synthetichealth.github.io/synthea-sample-data/downloads/latest/synthea_sample_data_csv_latest.zip"

    def __init__(self, max_members: int = 100, max_uncompressed_bytes: int = 250_000_000) -> None:
        self._max_members = max_members
        self._max_uncompressed_bytes = max_uncompressed_bytes

    def parse(self, payload: bytes) -> PublicDatasetBundle:
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise ValueError("Synthea 下载内容不是有效 ZIP") from exc
        with archive:
            members = archive.infolist()
            if len(members) > self._max_members or sum(item.file_size for item in members) > self._max_uncompressed_bytes:
                raise ValueError("Synthea ZIP 超出安全解压限制")
            files: list[TabularFile] = []
            seen: set[str] = set()
            parser = TabularFileParser(max_file_bytes=50_000_000)
            for member in members:
                path = PurePosixPath(member.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Synthea ZIP 包含不安全路径")
                if member.is_dir() or path.suffix.lower() != ".csv":
                    continue
                filename = path.name
                if filename in seen:
                    raise ValueError(f"Synthea ZIP 包含重复文件名：{filename}")
                seen.add(filename)
                files.append(parser.parse(filename, "text/csv", archive.read(member)))
        files.sort(key=lambda item: item.filename)
        return PublicDatasetBundle(
            manifest=_manifest(source_name="Synthea", source_class="synthetic_patient", source_url=self.source_url, payload=payload, record_count=sum(item.row_count for item in files), causal_use_allowed=False, limitations=("数据为合成电子健康记录，不代表真实患者或真实临床试验结果",)),
            files=tuple(files),
        )

