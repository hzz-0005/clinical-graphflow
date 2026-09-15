from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from inspect import signature
from typing import Any, Iterable, Literal, Mapping, Protocol

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.models import Evidence, EvidenceType, Hypothesis, HypothesisKind, HypothesisStatus, InvestigationState, InvestigationStatus, InvestigationStep, StepStatus
from app.clinical.tools import MINIMUM_CELL_SIZE
from app.clinical.public_runtime_llm import PublicRuntimeLLM, PublicSynthesis

PublicSpace = Literal["study_registry", "drug_label", "safety_signal", "synthetic_ehr"]

# Synthea OBSERVATION descriptions are English.  These are bounded query aliases only; they do
# not provide medical ranges or infer a code, and keep Chinese longitudinal questions from
# silently compiling to a concept that cannot match the governed staging relation.
PUBLIC_EHR_OBSERVATION_ALIASES = {
    "血糖": "Glucose",
    "葡萄糖": "Glucose",
    "血红蛋白": "Hemoglobin",
    "糖化血红蛋白": "Hemoglobin A1c",
    "血钙": "Calcium",
    "胆固醇": "Cholesterol",
    "血小板": "Platelets",
    "体重": "Body Weight",
}


class EHRTimeGrain(str, Enum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class EHRReferenceRangeStatus(str, Enum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"
    UNIT_MISMATCH = "unit_mismatch"
    UNAVAILABLE = "unavailable"


class EHRQueryCompileStatus(str, Enum):
    COMPILED = "compiled"
    DATA_GAP = "data_gap"
    INVALID_DATE = "invalid_date"
    WINDOW_REQUIRED = "window_required"


def _bucket_start(day: date, grain: EHRTimeGrain) -> date:
    """Return a UTC calendar bucket start for one parsed observation date."""

    if grain is EHRTimeGrain.DAY:
        return day
    if grain is EHRTimeGrain.WEEK:
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def _bucket_count(start: date, end: date, grain: EHRTimeGrain) -> int:
    first = _bucket_start(start, grain)
    last = _bucket_start(end, grain)
    if grain is EHRTimeGrain.DAY:
        return (last - first).days + 1
    if grain is EHRTimeGrain.WEEK:
        return ((last - first).days // 7) + 1
    return (last.year - first.year) * 12 + last.month - first.month + 1


class LongitudinalEHRQuery(BaseModel):
    """Strict, SQL-free parameters for a governed longitudinal EHR query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cohort_query: str = Field(min_length=1, max_length=200)
    concept_query: str = Field(min_length=1, max_length=200)
    time_grain: EHRTimeGrain = EHRTimeGrain.MONTH
    start_date: date | None = None
    end_date: date | None = None
    unit: str | None = Field(default=None, max_length=80)
    reference_catalog_version: str | None = Field(default=None, max_length=40)

    @field_validator("unit", mode="before")
    @classmethod
    def normalize_unit(cls, value: object) -> object:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_window(self) -> "LongitudinalEHRQuery":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("longitudinal EHR start_date and end_date must be supplied together")
        if self.start_date is not None and self.end_date is not None:
            if self.end_date < self.start_date:
                raise ValueError("longitudinal EHR end_date must not precede start_date")
            if _bucket_count(self.start_date, self.end_date, self.time_grain) > 120:
                raise ValueError("window_required: longitudinal EHR window exceeds 120 time buckets")
        return self


class EHRReferenceRange(BaseModel):
    """An explicitly supplied, versioned range; no production medical constants are bundled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str = Field(min_length=1, max_length=40)
    concept_code: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=80)
    low: Decimal
    high: Decimal
    source: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_bounds(self) -> "EHRReferenceRange":
        if not self.low.is_finite() or not self.high.is_finite() or self.low > self.high:
            raise ValueError("reference range bounds must be finite and ordered")
        return self


class EHRDataSnapshot(BaseModel):
    """The governed public EHR batch selected by the service-side query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1, max_length=200)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    published_at: datetime | None = None
    record_count: int = Field(default=0, ge=0)
    manifest: dict[str, Any] = Field(default_factory=dict)


class LongitudinalEHRAggregateRow(BaseModel):
    """Public aggregate row; patient/event identifiers can never be represented here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    time_bucket: date
    concept_code: str
    concept_name: str
    unit: str | None = None
    observation_count: int | None = Field(default=None, ge=0)
    numeric_observation_count: int | None = Field(default=None, ge=0)
    patient_count: int | None = Field(default=None, ge=0)
    classified_patient_count: int | None = Field(default=None, ge=0)
    abnormal_patient_count: int | None = Field(default=None, ge=0)
    abnormal_patient_rate: float | None = Field(default=None, ge=0, le=1)
    reference_range_status: EHRReferenceRangeStatus = EHRReferenceRangeStatus.UNKNOWN
    reference_range_source: str | None = None
    suppressed: bool = False
    suppression_reason: str | None = None


class LongitudinalEHRResult(BaseModel):
    """Bounded result and quality metadata for in-memory/adapter-level validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: LongitudinalEHRQuery
    rows: tuple[LongitudinalEHRAggregateRow, ...] = ()
    data_version_snapshot: EHRDataSnapshot | None = None
    invalid_time_count: int = Field(default=0, ge=0)
    non_numeric_count: int = Field(default=0, ge=0)
    reference_missing_count: int = Field(default=0, ge=0)
    unit_mismatch_count: int = Field(default=0, ge=0)
    status: Literal["ok", "no_data", "window_required", "data_gap"] = "ok"
    data_gap: str | None = None
    limitations: tuple[str, ...] = ()


class LongitudinalEHRCompilation(BaseModel):
    """Result of compiling natural language without allowing free-form SQL parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: EHRQueryCompileStatus
    query: LongitudinalEHRQuery | None = None
    data_gap: str | None = None


def _parse_query_date(value: str) -> date | None:
    text = value.strip()
    for candidate in (text, text.replace("/", "-"), text.replace(".", "-")):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _extract_longitudinal_dates(question: str) -> tuple[date | None, date | None, str | None]:
    date_tokens = re.findall(
        r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日?",
        question,
    )
    if not date_tokens:
        return None, None, None
    parsed = [_parse_query_date(token) for token in date_tokens]
    if any(item is None for item in parsed):
        return None, None, "data_gap: invalid_date"
    if len(parsed) == 1:
        token = date_tokens[0]
        single_day_marker = re.search(
            rf"(?:当日|当天|单日|on|at|于|在)\s*{re.escape(token)}|"
            rf"{re.escape(token)}\s*(?:当日|当天|单日)",
            question,
            flags=re.IGNORECASE,
        )
        if single_day_marker is None:
            return None, None, "data_gap: date range requires a start and end date"
    start = parsed[0]
    end = parsed[-1]
    return start, end, None


def _extract_longitudinal_concept(question: str, subject: str) -> str | None:
    """Extract an explicit observation concept; never turn an ambiguous question into all labs."""

    text = question.strip()
    patterns = (
        r"(?:观察指标|检验指标|检验|化验|测量|观察|concept|measurement|laboratory)\s*[:：]?\s*([A-Za-z][A-Za-z0-9 ._/-]{1,100}|[\u4e00-\u9fff]{2,30})",
        r"(?:患者|队列|cohort)[^。！？?]{0,40}?(?:的|中|for)\s*([A-Za-z][A-Za-z0-9 ._/-]{1,100}|[\u4e00-\u9fff]{2,30})\s*(?:趋势|按|随|异常|参考|trend|over)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip(" ，,。！？?：:")
            if candidate and candidate.casefold() != subject.strip().casefold():
                for alias, canonical in PUBLIC_EHR_OBSERVATION_ALIASES.items():
                    if candidate.casefold() == alias.casefold():
                        return canonical
                return candidate
    # Explicit common observation labels are a bounded vocabulary, not a medical range map.
    lowered = text.casefold()
    aliases = tuple(PUBLIC_EHR_OBSERVATION_ALIASES.items()) + tuple(
        (item, item) for item in ("hba1c", "hemoglobin", "glucose", "calcium", "cholesterol", "platelet", "body weight")
    )
    for alias, canonical in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if alias.casefold() in lowered:
            return canonical
    return None


def compile_ehr_longitudinal_query(
    question: str,
    subject: str,
    reference_catalog_version: str | None = None,
) -> LongitudinalEHRCompilation:
    """Compile a longitudinal request into typed, bounded parameters and explicit gaps."""

    question_text = str(question or "").strip()
    subject_text = str(subject or "").strip()
    if not subject_text:
        return LongitudinalEHRCompilation(status=EHRQueryCompileStatus.DATA_GAP, data_gap="data_gap: cohort concept is missing")
    concept = _extract_longitudinal_concept(question_text, subject_text)
    if concept is None:
        return LongitudinalEHRCompilation(status=EHRQueryCompileStatus.DATA_GAP, data_gap="data_gap: observation concept is ambiguous")

    lowered = question_text.casefold()
    if any(token in lowered for token in ("按日", "每日", "每天", "日趋势", "daily", "per day")):
        grain = EHRTimeGrain.DAY
    elif any(token in lowered for token in ("按周", "每周", "周趋势", "weekly", "per week")):
        grain = EHRTimeGrain.WEEK
    else:
        grain = EHRTimeGrain.MONTH

    start, end, date_gap = _extract_longitudinal_dates(question_text)
    if date_gap:
        status = (
            EHRQueryCompileStatus.INVALID_DATE
            if date_gap == "data_gap: invalid_date"
            else EHRQueryCompileStatus.DATA_GAP
        )
        return LongitudinalEHRCompilation(status=status, data_gap=date_gap)
    try:
        query = LongitudinalEHRQuery(
            cohort_query=normalize_public_ehr_query(subject_text),
            concept_query=concept,
            time_grain=grain,
            start_date=start,
            end_date=end,
            reference_catalog_version=reference_catalog_version,
        )
    except ValueError as exc:
        message = str(exc)
        status = EHRQueryCompileStatus.WINDOW_REQUIRED if "window_required" in message else EHRQueryCompileStatus.DATA_GAP
        return LongitudinalEHRCompilation(status=status, data_gap=message)
    return LongitudinalEHRCompilation(status=EHRQueryCompileStatus.COMPILED, query=query)


def _row_value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def _decimal_value(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text or _DECIMAL_PATTERN.fullmatch(text) is None:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _observation_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    def text(*names: str) -> str:
        value = _row_value(row, *names)
        return "" if value is None else str(value)

    row_key = _row_value(row, "ROW_KEY", "row_key")
    if row_key not in (None, ""):
        return (
            "row_key",
            text("BATCH_ID", "SOURCE_BATCH_ID", "batch_id", "source_batch_id"),
            text("PATIENT_ID", "PATIENT", "patient_id"),
            text("CONCEPT_CODE", "CODE", "concept_code", "code"),
            text("OBSERVED_AT", "DATE", "observed_at"),
            text("ROW_KEY", "row_key"),
        )
    return (
        text("PATIENT_ID", "PATIENT", "patient_id"),
        text("ENCOUNTER_ID", "ENCOUNTER", "encounter_id"),
        text("OBSERVED_AT", "DATE", "observed_at"),
        text("CODE", "CONCEPT_CODE", "code", "concept_code"),
        text("DESCRIPTION", "CONCEPT_NAME", "description", "concept_name"),
        text("VALUE", "value"),
        text("UNITS", "UNIT", "units", "unit"),
    )


def _reference_for(
    query: LongitudinalEHRQuery,
    concept_code: str,
    unit: str | None,
    reference_ranges: tuple[EHRReferenceRange, ...],
) -> tuple[EHRReferenceRangeStatus, EHRReferenceRange | None]:
    if not reference_ranges or not concept_code or not unit:
        return EHRReferenceRangeStatus.UNKNOWN, None
    same_code = [item for item in reference_ranges if item.concept_code == concept_code]
    same_unit = [item for item in same_code if item.unit.strip() == unit]
    if query.reference_catalog_version is not None:
        same_unit = [item for item in same_unit if item.catalog_version == query.reference_catalog_version]
        if not same_unit:
            if any(item.catalog_version == query.reference_catalog_version for item in same_code):
                return EHRReferenceRangeStatus.UNIT_MISMATCH, None
            if same_code:
                return EHRReferenceRangeStatus.UNAVAILABLE, None
            return EHRReferenceRangeStatus.UNKNOWN, None
    elif len({item.catalog_version for item in same_unit}) > 1:
        return EHRReferenceRangeStatus.UNAVAILABLE, None
    if not same_unit:
        return EHRReferenceRangeStatus.UNIT_MISMATCH if same_code else EHRReferenceRangeStatus.UNKNOWN, None
    if len({(item.low, item.high) for item in same_unit}) > 1:
        return EHRReferenceRangeStatus.UNAVAILABLE, None
    return EHRReferenceRangeStatus.AVAILABLE, same_unit[0]


def aggregate_ehr_observations(
    query: LongitudinalEHRQuery,
    observations: Iterable[Mapping[str, Any]],
    reference_ranges: Iterable[EHRReferenceRange] = (),
) -> LongitudinalEHRResult:
    """Aggregate normalized observation rows without returning patient-level data.

    This pure helper is intentionally independent of PostgreSQL.  It defines the Phase 1
    boundary semantics used by the future governed adapter: UTC buckets, strict numeric parsing,
    distinct-patient denominators, explicit reference-range status, and small-cell suppression.
    """

    ranges = tuple(reference_ranges)
    seen: set[tuple[Any, ...]] = set()
    groups: dict[tuple[date, str, str, str | None], dict[str, Any]] = {}
    invalid_time_count = 0
    non_numeric_count = 0
    unit_mismatch_count = 0
    valid_days: list[date] = []

    for raw in observations:
        # A published domain row can carry only one of these governed cohort markers.  When a
        # fixture has no marker, it is already cohort-filtered and is accepted as such.
        cohort_markers = [
            _row_value(raw, name)
            for name in ("COHORT", "COHORT_QUERY", "COHORT_CONCEPT", "cohort", "cohort_query")
        ]
        cohort_markers = [str(item).strip() for item in cohort_markers if item not in (None, "")]
        if cohort_markers and not any(query.cohort_query.casefold() in item.casefold() for item in cohort_markers):
            continue

        code = str(_row_value(raw, "CONCEPT_CODE", "CODE", "concept_code", "code") or "").strip()
        name = str(_row_value(raw, "CONCEPT_NAME", "DESCRIPTION", "concept_name", "description") or "").strip()
        concept_text = f"{code} {name}".casefold()
        if query.concept_query.casefold() not in concept_text:
            continue
        signature = _observation_signature(raw)
        if signature in seen:
            continue
        seen.add(signature)

        observed_at = _utc_datetime(_row_value(raw, "OBSERVED_AT", "DATE", "observed_at"))
        if observed_at is None:
            invalid_time_count += 1
            continue
        observed_day = observed_at.date()
        if query.start_date is not None and observed_day < query.start_date:
            continue
        if query.end_date is not None and observed_day > query.end_date:
            continue
        valid_days.append(observed_day)

        unit_value = _row_value(raw, "UNIT", "UNITS", "unit", "units")
        unit = str(unit_value).strip() if unit_value not in (None, "") else None
        if query.unit is not None and unit != query.unit:
            unit_mismatch_count += 1
            continue
        bucket = _bucket_start(observed_day, query.time_grain)
        key = (bucket, code, name, unit)
        item = groups.setdefault(
            key,
            {
                "concept_code": code,
                "concept_name": name,
                "unit": unit,
                "patients": set(),
                "numeric_patients": set(),
                "abnormal_patients": set(),
                "observation_count": 0,
                "numeric_observation_count": 0,
                "values_by_patient": defaultdict(list),
            },
        )
        patient_id = str(_row_value(raw, "PATIENT_ID", "PATIENT", "patient_id") or "").strip()
        if patient_id:
            item["patients"].add(patient_id)
        item["observation_count"] += 1
        numeric = _decimal_value(_row_value(raw, "VALUE", "value"))
        if numeric is None:
            non_numeric_count += 1
        else:
            item["numeric_observation_count"] += 1
            if patient_id:
                item["numeric_patients"].add(patient_id)
                item["values_by_patient"][patient_id].append(numeric)

    if query.start_date is None and query.end_date is None and valid_days:
        if _bucket_count(min(valid_days), max(valid_days), query.time_grain) > 120:
            return LongitudinalEHRResult(
                query=query,
                status="window_required",
                data_gap="window_required: longitudinal EHR window exceeds 120 time buckets",
                invalid_time_count=invalid_time_count,
                non_numeric_count=non_numeric_count,
                unit_mismatch_count=unit_mismatch_count,
                limitations=("时间窗口超过 120 个时间桶，请显式缩小查询范围",),
            )

    output: list[LongitudinalEHRAggregateRow] = []
    reference_missing_count = 0
    for (bucket, code, name, unit), item in sorted(groups.items()):
        range_status, reference = _reference_for(query, code, unit, ranges)
        if range_status is not EHRReferenceRangeStatus.AVAILABLE:
            reference_missing_count += 1
        abnormal_patients: set[str] = set()
        if reference is not None:
            for patient_id, values in item["values_by_patient"].items():
                if any(value < reference.low or value > reference.high for value in values):
                    abnormal_patients.add(patient_id)
        patient_count = len(item["patients"])
        numeric_patient_count = len(item["numeric_patients"])
        classified_count: int | None = numeric_patient_count if reference is not None else None
        suppressed = (
            patient_count < MINIMUM_CELL_SIZE
            or (
                classified_count is not None
                and classified_count < MINIMUM_CELL_SIZE
            )
        )
        abnormal_count: int | None = len(abnormal_patients) if reference is not None else None
        abnormal_rate: float | None = (
            len(abnormal_patients) / numeric_patient_count if reference is not None and numeric_patient_count else None
        )
        output.append(
            LongitudinalEHRAggregateRow(
                time_bucket=bucket,
                concept_code=code,
                concept_name=name,
                unit=unit,
                observation_count=None if suppressed else item["observation_count"],
                numeric_observation_count=None if suppressed else item["numeric_observation_count"],
                patient_count=None if suppressed else patient_count,
                classified_patient_count=None if suppressed else classified_count,
                abnormal_patient_count=None if suppressed else abnormal_count,
                abnormal_patient_rate=None if suppressed else abnormal_rate,
                reference_range_status=range_status,
                reference_range_source=reference.source if reference is not None else None,
                suppressed=suppressed,
                suppression_reason="minimum_cell_size" if suppressed else None,
            )
        )

    limitations = ["数据来自 Synthea 合成电子健康记录，不代表真实患者或真实人群"]
    if reference_missing_count:
        limitations.append("当前没有匹配的版本化参考范围，异常患者比例不可用")
    if invalid_time_count:
        limitations.append("无法解析的观察时间未进入时间桶")
    if non_numeric_count:
        limitations.append("无法解析的观察值未计入数值观察，不按 0 处理")
    if unit_mismatch_count:
        limitations.append("单位仅做精确匹配，不同单位不会合并")
    return LongitudinalEHRResult(
        query=query,
        rows=tuple(output),
        invalid_time_count=invalid_time_count,
        non_numeric_count=non_numeric_count,
        reference_missing_count=reference_missing_count,
        unit_mismatch_count=unit_mismatch_count,
        status="ok" if output else "no_data",
        limitations=tuple(limitations),
    )


def render_longitudinal_ehr_result(result: LongitudinalEHRResult) -> str:
    """Render only aggregate semantics; unknown is never translated to zero or normal."""

    if result.status == "window_required":
        return "时间窗口超过 120 个时间桶，无法安全聚合；请缩小日期范围。"
    if not result.rows:
        return "当前受治理观察窗口没有可展示的聚合结果；这不等于没有临床观察。"
    lines: list[str] = []
    for row in result.rows:
        label = f"{row.time_bucket.isoformat()} {row.concept_name or row.concept_code}"
        if row.suppressed:
            lines.append(f"{label}：患者单元低于最小披露阈值，结果已抑制。")
        elif row.reference_range_status is EHRReferenceRangeStatus.AVAILABLE and row.abnormal_patient_rate is not None:
            lines.append(f"{label}：可分类患者中的异常患者比例 {row.abnormal_patient_rate:.1%}。")
        else:
            lines.append(f"{label}：参考范围状态为 {row.reference_range_status.value}，异常患者比例不可用。")
    return " ".join(lines)

PUBLIC_METHODS = {
    "study_registry": ("study_screening", ("search_studies", "compare_study_designs")),
    "drug_label": ("drug_safety_review", ("lookup_drug_label", "lookup_faers_signals", "search_studies")),
    "safety_signal": ("drug_safety_review", ("lookup_faers_signals", "lookup_drug_label", "search_studies")),
    "synthetic_ehr": ("ehr_cohort_profile", ("summarize_ehr_cohort", "profile_ehr_concepts")),
    "ehr_longitudinal_profile": ("ehr_longitudinal_profile", ("analyze_ehr_observation_trend",)),
}

# Small governed terminology bootstrap. This is deliberately separated from SQL so it can later be
# replaced by a maintained terminology service (for example RxNorm) without changing the tools.
PUBLIC_DRUG_ALIASES = {
    "二甲双胍": "METFORMIN",
    "盐酸二甲双胍": "METFORMIN",
    "西地那非": "SILDENAFIL",
    "枸橼酸西地那非": "SILDENAFIL",
    "阿司匹林": "ASPIRIN",
    "阿莫西林": "AMOXICILLIN",
    "氯吡格雷": "CLOPIDOGREL",
    "洛伐他汀": "LOVASTATIN",
    "布洛芬": "IBUPROFEN",
    "奥美拉唑": "OMEPRAZOLE",
    "赖诺普利": "LISINOPRIL",
    "利格列汀": "LINAGLIPTIN",
}

# The registry snapshot stores ClinicalTrials.gov terminology in English while the
# workspace accepts Chinese questions.  Keep this small, reviewable bootstrap at
# the query boundary so a model that returns the user's Chinese subject cannot
# silently turn a valid search into zero rows.  A future terminology service can
# replace this map without changing the governed SQL tools.
PUBLIC_STUDY_ALIASES = {
    "非小细胞肺癌": "Non-Small Cell Lung Cancer",
    "小细胞肺癌": "Small Cell Lung Cancer",
    "肺癌": "Lung Cancer",
    "乳腺癌": "Breast Cancer",
    "前列腺癌": "Prostate Cancer",
    "2型糖尿病": "Type 2 Diabetes",
    "二型糖尿病": "Type 2 Diabetes",
    "糖尿病": "Diabetes",
    "高血压": "Hypertension",
    "阿尔茨海默病": "Alzheimer Disease",
    "胃癌": "Gastric Cancer",
    "结直肠癌": "Colorectal Cancer",
}

PUBLIC_EHR_ALIASES = {
    "糖尿病": "Diabetes",
    "二型糖尿病": "Diabetes",
    "2型糖尿病": "Diabetes",
    "高血压": "Hypertension",
    "贫血": "Anemia",
    "哮喘": "Asthma",
}

def normalize_public_study_query(query: str) -> tuple[str, str | None]:
    """Separate a supported registry location filter from the study keywords.

    The external planner may repeat a location mentioned in the question (for example,
    ``Lung Cancer China``).  Treating that word as a full-text condition silently turns a
    valid location request into an empty search because the registry's condition/title fields
    do not contain country names.  The repository consumes the returned location as a governed
    batch filter instead of interpolating it into SQL.
    """

    text = str(query or "").strip()
    location: str | None = None
    replacements = (
        ("中国研究中心", " "),
        ("中国地点", " "),
        ("中国", " "),
    )
    for marker, replacement in replacements:
        if marker in text:
            location = "China"
            text = text.replace(marker, replacement)
    if re.search(r"(?<![A-Za-z])(China|Chinese)(?![A-Za-z])", text, flags=re.IGNORECASE):
        location = "China"
        text = re.sub(r"(?<![A-Za-z])(China|Chinese)(?![A-Za-z])", " ", text, flags=re.IGNORECASE)
    for alias, canonical in sorted(PUBLIC_STUDY_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(alias, canonical)
    return " ".join(text.split()), location


def normalize_public_drug_query(query: str) -> str:
    text=query.strip()
    for alias, canonical in sorted(PUBLIC_DRUG_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in text:
            return canonical
    return text


def normalize_public_ehr_query(query: str) -> str:
    text = query.strip()
    for alias, canonical in sorted(PUBLIC_EHR_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in text:
            return canonical
    return text


class PublicQuestionPlan(BaseModel):
    model_config=ConfigDict(frozen=True)
    space: PublicSpace
    tool: str


class PublicToolResult(BaseModel):
    tool: str
    source: str
    rows: list[dict] = Field(default_factory=list)
    human_summary: str
    limitations: list[str] = Field(default_factory=list)


class PublicClinicalQuestionCompiler:
    def compile(self, question: str, requested_space: PublicSpace | None = None) -> PublicQuestionPlan:
        if requested_space:
            space=requested_space
        else:
            text=question.casefold()
            if any(term in text for term in ("faers", "共同报告", "同时报告", "安全信号", "报告了哪些不良")):
                space="safety_signal"
            elif any(term in text for term in ("标签", "说明书", "禁忌", "黑框", "boxed warning", "label warning")):
                space="drug_label"
            elif any(term in text for term in ("患者", "就诊", "病历", "ehr", "队列", "patient", "cohort", "lab", "measurement", "observation")):
                space="synthetic_ehr"
            else:
                space="study_registry"
        if space == "synthetic_ehr" and any(
            term in question.casefold()
            for term in (
                "趋势", "按日", "按周", "按月", "随时间", "时间序列", "趋势变化",
                "异常比例", "异常患者", "异常率", "参考范围", "trend", "over time",
                "longitudinal", "abnormal", "reference range",
            )
        ):
            tool = "analyze_ehr_observation_trend"
        else:
            tool = {
                "study_registry":"search_studies",
                "drug_label":"lookup_drug_label",
                "safety_signal":"lookup_faers_signals",
                "synthetic_ehr":"summarize_ehr_cohort",
            }[space]
        return PublicQuestionPlan(space=space, tool=tool)


class PublicClinicalRepository(Protocol):
    def search_studies(self, query: str, limit: int = 10) -> PublicToolResult: ...
    def lookup_drug_label(self, query: str, limit: int = 10) -> PublicToolResult: ...
    def lookup_faers_signals(self, query: str, limit: int = 10) -> PublicToolResult: ...
    def summarize_ehr_cohort(self, query: str, limit: int = 10) -> PublicToolResult: ...
    def compare_study_designs(self, query: str, limit: int = 100) -> PublicToolResult: ...
    def profile_ehr_concepts(self, query: str, limit: int = 50) -> PublicToolResult: ...
    def analyze_ehr_observation_trend(
        self, request: LongitudinalEHRQuery, limit: int = 120
    ) -> LongitudinalEHRResult: ...


class PostgresPublicClinicalRepository:
    def __init__(self, database_url: str): self.database_url=database_url

    def _query(self, sql: str, params: tuple) -> list[dict]:
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]

    def inventory(self) -> dict[str,dict]:
        studies=self._query("select count(*)::int record_count from analytics_clinical_marts.mart_study_registry",())
        study_examples=self._query("select conditions label,count(*)::int frequency from analytics_clinical_marts.mart_study_registry where conditions is not null group by 1 order by 2 desc,label limit 10",())
        labels=self._query("select count(*)::int record_count from analytics_clinical_marts.mart_drug_labels",())
        label_examples=self._query("select coalesce(nullif(brand_names,''),generic_names) label from analytics_clinical_marts.mart_drug_labels where coalesce(nullif(brand_names,''),generic_names) is not null order by effective_time desc nulls last limit 10",())
        signals=self._query("select count(distinct medicinal_product)::int drug_count,sum(report_count)::int report_links from analytics_clinical_marts.mart_faers_signal",())
        signal_examples=self._query("select medicinal_product label,sum(report_count)::int frequency from analytics_clinical_marts.mart_faers_signal group by 1 order by 2 desc,label limit 10",())
        ehr=self._query("select count(*)::int patient_count,sum(encounter_count)::int encounter_count,sum(condition_count)::int condition_count,sum(medication_count)::int medication_count from analytics_clinical_marts.mart_ehr_patient_summary",())
        ehr_examples=self._query("select payload_json->>'DESCRIPTION' label,count(*)::int frequency from analytics_clinical_staging.stg_public_domain_records where domain_name='CONDITION' and payload_json->>'DESCRIPTION' is not null group by 1 order by 2 desc,label limit 10",())
        return {
            "study_registry":{**studies[0],"examples":study_examples},
            "drug_label":{**labels[0],"examples":label_examples},
            "safety_signal":{**signals[0],"examples":signal_examples},
            "synthetic_ehr":{**ehr[0],"examples":ehr_examples},
        }

    @staticmethod
    def _study_filter(query: str) -> tuple[str, tuple[str, ...], str, str | None]:
        normalized, location = normalize_public_study_query(query)
        display_query = normalized or str(query or "").strip()
        clauses = ["concat_ws(' ',brief_title,conditions,intervention_names,primary_outcomes) ilike %s"]
        params: list[str] = [f"%{display_query}%"]
        if location == "China":
            # The China snapshot is explicitly filtered at acquisition time.  The mart keeps
            # one row per study and may prefer the global copy, so use the governed source batch
            # as the location index rather than pretending the mart has a missing location field.
            clauses.append(
                "exists (select 1 from analytics_clinical_staging.stg_public_domain_records location_index "
                "where location_index.batch_id='public-china_clinicaltrials' "
                "and location_index.domain_name='STUDY' "
                "and location_index.payload_json->>'STUDY_ID'=mart.study_id)"
            )
        return " and ".join(clauses), tuple(params), display_query, location

    def search_studies(self, query: str, limit: int = 10) -> PublicToolResult:
        where, filter_params, display_query, location = self._study_filter(query)
        params = filter_params + (limit,)
        rows=self._query(f"""with matched as (select mart.study_id,mart.brief_title,mart.study_type,mart.overall_status,mart.phases,mart.enrollment_count,mart.lead_sponsor,mart.conditions,mart.intervention_types,mart.intervention_names,mart.primary_outcomes from analytics_clinical_marts.mart_study_registry mart where {where}) select *,count(*) over()::int matching_study_count from matched order by enrollment_count desc nulls last limit %s""",params)
        total=int(rows[0]["matching_study_count"]) if rows else 0
        location_text = "中国地点且 " if location == "China" else ""
        return PublicToolResult(tool="search_studies",source="ClinicalTrials.gov",rows=rows,human_summary=f"ClinicalTrials.gov 注册数据中共有 {total} 项与“{location_text}{display_query}”匹配的研究，当前展示入组规模最大的 {len(rows)} 项。",limitations=["这是研究注册与设计元数据，不是受试者级疗效结果，不能据此认定治疗有效"])

    def compare_study_designs(self, query: str, limit: int = 100) -> PublicToolResult:
        where, filter_params, display_query, location = self._study_filter(query)
        params = filter_params + (limit,)
        rows=self._query(f"""with matched as (
            select mart.study_id,mart.phases,mart.study_type,mart.overall_status,mart.enrollment_count,mart.intervention_types
            from analytics_clinical_marts.mart_study_registry mart where {where}
        ), expanded as (
            select matched.*,coalesce(nullif(trim(type_name),''),'未标注介入') as intervention_type
            from matched
            left join lateral unnest(string_to_array(coalesce(matched.intervention_types,''),', ')) as types(type_name) on true
        )
        select coalesce(nullif(phases,''),'未标注阶段') phase,
               coalesce(nullif(study_type,''),'未标注类型') study_type,
               coalesce(nullif(overall_status,''),'未标注状态') overall_status,
               intervention_type,
               count(distinct study_id)::int study_count,
               round(avg(enrollment_count))::int average_enrollment
        from expanded
        group by 1,2,3,4
        order by study_count desc,phase,intervention_type
        limit %s""",params)
        location_text = "中国地点且 " if location == "China" else ""
        return PublicToolResult(tool="compare_study_designs",source="ClinicalTrials.gov",rows=rows,human_summary=f"把与“{location_text}{display_query}”匹配的研究按阶段、研究类型、介入类型和招募状态汇总为 {len(rows)} 个设计组合；一项研究包含多种介入时可能出现在多个介入类别。",limitations=["这里只比较注册设计，不包含研究结果，也不能判断哪种设计或干预更有效"])

    def lookup_drug_label(self, query: str, limit: int = 10) -> PublicToolResult:
        canonical = normalize_public_drug_query(query)
        tokens = [item for item in re.split(r"\s+", canonical.strip()) if item]
        token_pattern = r"[^A-Z0-9]+".join(re.escape(item) for item in tokens) if tokens else r"(?!)"
        boundary_pattern = rf"(^|[^A-Z0-9]){token_pattern}($|[^A-Z0-9])"
        # A product called ``Aspirin-Free`` is not an aspirin label.  Keep the negative-name
        # guard generic so the same false-positive class is blocked for other ``*-free`` names.
        negative_pattern = rf"{re.escape(tokens[0])}[-\s]+FREE" if tokens else r"(?!)"
        rows=self._query("""select label_id,brand_names,generic_names,manufacturer_names,product_types,routes,indications_and_usage,contraindications,boxed_warning,warnings,adverse_reactions
            from analytics_clinical_marts.mart_drug_labels
            where (coalesce(generic_names,'') ~* %s or coalesce(brand_names,'') ~* %s)
              and not (coalesce(generic_names,'') ~* %s or coalesce(brand_names,'') ~* %s)
            order by
              case when upper(trim(generic_names)) = upper(%s)
                          or upper(trim(brand_names)) = upper(%s) then 0 else 1 end,
              (length(coalesce(boxed_warning,'')) + length(coalesce(warnings,''))
               + length(coalesce(contraindications,'')) + length(coalesce(adverse_reactions,''))) desc,
              effective_time desc nulls last
            limit %s""",(boundary_pattern,boundary_pattern,negative_pattern,negative_pattern,canonical,canonical,limit))
        return PublicToolResult(tool="lookup_drug_label",source="openFDA Drug Label",rows=rows,human_summary=f"找到 {len(rows)} 份与“{canonical}”匹配的 FDA 药品标签。",limitations=["标签是监管参考文本，不代表个体诊疗建议，也不能单独证明真实世界发生率"])

    def lookup_faers_signals(self, query: str, limit: int = 10) -> PublicToolResult:
        needle=f"%{query.strip()}%"
        rows=self._query("""select medicinal_product,medicinal_product_canonical,medicinal_product_zh,reaction_term,reaction_term_zh,report_count,serious_report_count,first_received_date,last_received_date from analytics_clinical_marts.mart_faers_signal where medicinal_product ilike %s or medicinal_product_canonical ilike %s order by report_count desc,reaction_term limit %s""",(needle,needle,limit))
        total=sum(int(row.get("report_count") or 0) for row in rows)
        return PublicToolResult(tool="lookup_faers_signals",source="openFDA FAERS",rows=rows,human_summary=f"返回 {len(rows)} 个与“{query}”相关的药物–不良反应报告共现组合，前列组合合计 {total} 份去重报告计数。",limitations=["FAERS 是自发报告系统；共现不能证明药物导致反应，也不能用于估计发生率"])

    def summarize_ehr_cohort(self, query: str, limit: int = 10) -> PublicToolResult:
        query = normalize_public_ehr_query(query)
        needle=f"%{query.strip()}%"
        rows=self._query("""with matched as (select distinct payload_json->>'PATIENT_ID' patient_id from analytics_clinical_staging.stg_public_domain_records where domain_name in ('CONDITION','MEDICATION','PROCEDURE','OBSERVATION') and payload_json::text ilike %s) select count(*)::int patient_count,coalesce(sum(p.encounter_count),0)::int encounter_count,coalesce(sum(p.condition_count),0)::int condition_count,coalesce(sum(p.medication_count),0)::int medication_count,coalesce(sum(p.procedure_count),0)::int procedure_count,coalesce(sum(p.observation_count),0)::int observation_count from analytics_clinical_marts.mart_ehr_patient_summary p join matched m using(patient_id)""",(needle,))
        count=int(rows[0]["patient_count"]) if rows else 0
        return PublicToolResult(tool="summarize_ehr_cohort",source="Synthea synthetic EHR",rows=rows,human_summary=f"在 Synthea 合成病历中，“{query}”匹配到 {count} 名虚拟患者。",limitations=["Synthea 是官方合成数据，不包含真实患者，统计仅用于验证查询与调查能力"])

    def profile_ehr_concepts(self, query: str, limit: int = 50) -> PublicToolResult:
        query = normalize_public_ehr_query(query)
        needle=f"%{query.strip()}%"
        rows=self._query(
            """with matched as (
                select distinct payload_json->>'PATIENT_ID' patient_id
                from analytics_clinical_staging.stg_public_domain_records
                where domain_name in ('CONDITION','MEDICATION','PROCEDURE','OBSERVATION')
                  and payload_json::text ilike %s
            ), concepts as (
                select
                    case records.domain_name
                        when 'CONDITION' then '疾病'
                        when 'MEDICATION' then '用药'
                        when 'PROCEDURE' then '操作'
                        else '观察'
                    end concept_type,
                    coalesce(records.payload_json->>'DESCRIPTION', records.payload_json->>'CODE') concept_name,
                    records.payload_json->>'PATIENT_ID' patient_id,
                    nullif(records.payload_json->>'VALUE', '') observation_value,
                    nullif(records.payload_json->>'UNITS', '') observation_units,
                    case
                        when records.domain_name = 'OBSERVATION'
                         and (
                            nullif(records.payload_json->>'UNITS', '') is not null
                            or records.payload_json->>'VALUE' ~ '^-?[0-9]+([.][0-9]+)?$'
                        )
                        then true
                        else false
                    end is_measurement,
                    case
                        when records.domain_name = 'OBSERVATION'
                         and (
                            lower(coalesce(records.payload_json->>'DESCRIPTION', '')) like '%%in blood%%'
                            or lower(coalesce(records.payload_json->>'DESCRIPTION', '')) like '%%in serum%%'
                            or lower(coalesce(records.payload_json->>'DESCRIPTION', '')) like '%%in plasma%%'
                            or lower(coalesce(records.payload_json->>'DESCRIPTION', '')) like '%%in urine%%'
                            or lower(coalesce(records.payload_json->>'DESCRIPTION', '')) like '%%[mass/volume]%%'
                            or lower(coalesce(records.payload_json->>'DESCRIPTION', '')) like '%%[moles/volume]%%'
                        )
                        then true
                        else false
                    end is_laboratory
                from analytics_clinical_staging.stg_public_domain_records records
                join matched on matched.patient_id=records.payload_json->>'PATIENT_ID'
                where records.domain_name in ('CONDITION','MEDICATION','PROCEDURE','OBSERVATION')
            ), unique_values as (
                select distinct concept_type,concept_name,observation_value
                from concepts
                where observation_value is not null
            ), value_ranked as (
                select concept_type,concept_name,observation_value,
                       row_number() over(
                           partition by concept_type,concept_name
                           order by observation_value
                       ) value_rank
                from unique_values
            ), value_samples as (
                select concept_type,concept_name,
                       string_agg(observation_value,' | ' order by observation_value)
                           filter(where value_rank <= 5) example_values
                from value_ranked
                group by 1,2
            ), counts as (
                select concept_type,concept_name,
                       count(distinct patient_id)::int patient_count,
                       bool_or(is_measurement) is_measurement,
                       bool_or(is_laboratory) is_laboratory,
                       min(observation_units) filter(where is_measurement) observation_units
                from concepts
                where concept_name is not null
                group by 1,2
            ), ranked as (
                select counts.*,
                       row_number() over(
                           partition by concept_type
                           order by
                               case when concept_type='观察' and is_measurement then 0 else 1 end,
                               case when concept_type='观察' and is_laboratory then 0 else 1 end,
                               patient_count desc,
                               concept_name
                       ) type_rank
                from counts
            ), selected as (
                select ranked.concept_type,ranked.concept_name,ranked.patient_count,
                       case when ranked.concept_type='观察' then ranked.observation_units end observation_units,
                       case when ranked.concept_type='观察' then value_samples.example_values end example_values,
                       ranked.is_measurement,ranked.is_laboratory,ranked.type_rank
                from ranked
                left join value_samples using(concept_type,concept_name)
                where type_rank<=greatest(1,ceil(%s::numeric/4))
                order by type_rank,
                         case concept_type when '观察' then 1 when '疾病' then 2 when '用药' then 3 else 4 end
                limit %s
            )
            select *
            from selected
            order by case concept_type when '疾病' then 1 when '用药' then 2 when '操作' then 3 else 4 end,type_rank""",
            (needle,limit,limit),
        )
        return PublicToolResult(tool="profile_ehr_concepts",source="Synthea synthetic EHR",rows=rows,human_summary=f"汇总了“{query}”虚拟患者队列中 {len(rows)} 个常见疾病、用药、操作或观察项目。",limitations=["这些是合成患者的描述性画像，不能外推到真实人群或用于诊疗"])

    def analyze_ehr_observation_trend(
        self, request: LongitudinalEHRQuery, limit: int = 120
    ) -> LongitudinalEHRResult:
        """Run the fixed, SECURITY DEFINER-backed longitudinal observation query.

        The reader role can execute the function but cannot select its patient-level staging
        relation.  This adapter only passes typed values as parameters and maps aggregate rows
        into the Phase 1 result contract; it never accepts a relation, column, SQL, or batch id.
        """

        if limit < 1:
            raise ValueError("longitudinal EHR limit must be at least 1")
        effective_limit = min(int(limit), 120)
        params = (
            request.cohort_query,
            request.concept_query,
            request.time_grain.value,
            request.start_date,
            request.end_date,
            request.unit,
            request.reference_catalog_version,
            effective_limit,
        )
        snapshot_sql = """select batch_id,content_hash,published_at,record_count,manifest
               from analytics_clinical_core.get_ehr_observation_snapshot()"""

        def snapshot_from_rows(snapshot_rows: list[dict]) -> EHRDataSnapshot | None:
            if not snapshot_rows or not snapshot_rows[0].get("batch_id") or not snapshot_rows[0].get("content_hash"):
                return None
            snapshot = snapshot_rows[0]
            return EHRDataSnapshot(
                batch_id=str(snapshot["batch_id"]),
                content_hash=str(snapshot["content_hash"]),
                published_at=snapshot.get("published_at"),
                record_count=int(snapshot.get("record_count") or 0),
                manifest=dict(snapshot.get("manifest") or {}),
            )

        data_version_snapshot = None
        try:
            rows = self._query(
                """select time_bucket,concept_code,concept_name,unit,
                          observation_count,numeric_observation_count,patient_count,
                          classified_patient_count,abnormal_patient_count,abnormal_patient_rate,
                           reference_range_status,reference_range_source,suppressed,
                           suppression_reason,invalid_time_count,non_numeric_count,
                           reference_missing_count,unit_mismatch_count,
                           source_batch_id,source_content_hash,source_published_at,
                           source_record_count,source_manifest
                   from analytics_clinical_core.analyze_ehr_observation_trend(
                       %s,%s,%s,%s,%s,%s,%s,%s
                   )""",
                params,
            )
        except psycopg.Error as exc:
            if "window_required" not in str(exc).casefold():
                raise
            data_version_snapshot = snapshot_from_rows(self._query(snapshot_sql, ()))
            return LongitudinalEHRResult(
                query=request,
                data_version_snapshot=data_version_snapshot,
                status="window_required",
                data_gap="window_required: longitudinal EHR window exceeds 120 time buckets",
                limitations=("时间窗口超过 120 个时间桶，请显式缩小查询范围",),
            )

        aggregate_rows = tuple(
            LongitudinalEHRAggregateRow(
                time_bucket=row["time_bucket"],
                concept_code=row["concept_code"],
                concept_name=row["concept_name"],
                unit=row.get("unit"),
                observation_count=row.get("observation_count"),
                numeric_observation_count=row.get("numeric_observation_count"),
                patient_count=row.get("patient_count"),
                classified_patient_count=row.get("classified_patient_count"),
                abnormal_patient_count=row.get("abnormal_patient_count"),
                abnormal_patient_rate=row.get("abnormal_patient_rate"),
                reference_range_status=row.get("reference_range_status", "unknown"),
                reference_range_source=row.get("reference_range_source"),
                suppressed=bool(row.get("suppressed", False)),
                suppression_reason=row.get("suppression_reason"),
            )
            for row in rows
        )
        first = rows[0] if rows else {}
        if first.get("source_batch_id") and first.get("source_content_hash"):
            data_version_snapshot = EHRDataSnapshot(
                batch_id=str(first["source_batch_id"]),
                content_hash=str(first["source_content_hash"]),
                published_at=first.get("source_published_at"),
                record_count=int(first.get("source_record_count") or 0),
                manifest=dict(first.get("source_manifest") or {}),
            )
        if data_version_snapshot is None:
            data_version_snapshot = snapshot_from_rows(self._query(snapshot_sql, ()))
        limitations = ["数据来自 Synthea 合成电子健康记录，不代表真实患者或真实人群"]
        if any(item.reference_range_status is not EHRReferenceRangeStatus.AVAILABLE for item in aggregate_rows):
            limitations.append("当前没有匹配的版本化参考范围，异常患者比例不可用")
        if any(item.suppressed for item in aggregate_rows):
            limitations.append("低于最小披露阈值的时间桶已抑制")
        if int(first.get("invalid_time_count") or 0):
            limitations.append("无法解析的观察时间未进入时间桶")
        if int(first.get("non_numeric_count") or 0):
            limitations.append("无法解析的观察值未计入数值观察，不按 0 处理")
        if int(first.get("unit_mismatch_count") or 0):
            limitations.append("单位仅做精确匹配，不同单位不会合并")
        return LongitudinalEHRResult(
            query=request,
            rows=aggregate_rows,
            data_version_snapshot=data_version_snapshot,
            invalid_time_count=int(first.get("invalid_time_count") or 0),
            non_numeric_count=int(first.get("non_numeric_count") or 0),
            reference_missing_count=int(first.get("reference_missing_count") or 0),
            unit_mismatch_count=int(first.get("unit_mismatch_count") or 0),
            status="ok" if aggregate_rows else "no_data",
            limitations=tuple(limitations),
        )


class PublicClinicalInvestigator:
    def __init__(self, repository: PublicClinicalRepository, planner: PublicRuntimeLLM | None=None): self.repository=repository; self.planner=planner

    @staticmethod
    def _longitudinal_tool_result(result: LongitudinalEHRResult) -> PublicToolResult:
        """Convert the typed aggregate contract to the legacy evidence envelope."""

        rows = [row.model_dump(mode="json") for row in result.rows]
        summary = render_longitudinal_ehr_result(result)
        if result.status == "ok":
            summary = f"纵向 EHR 聚合返回 {len(rows)} 个时间桶结果：{summary}"
        return PublicToolResult(
            tool="analyze_ehr_observation_trend",
            source="Synthea synthetic EHR",
            rows=rows,
            human_summary=summary,
            limitations=list(result.limitations),
        )

    @classmethod
    def _longitudinal_data_gap_state(
        cls,
        question: str,
        subject: str,
        space: PublicSpace,
        compilation: LongitudinalEHRCompilation,
    ) -> InvestigationState:
        """Finish an un-compilable longitudinal request without touching the repository."""

        state = InvestigationState(question=question, domain=space, status=InvestigationStatus.RUNNING)
        hypothesis = state.add_hypothesis(Hypothesis(
            statement="纵向 EHR 请求可以被编译为受治理的时间聚合",
            kind=HypothesisKind.CLINICAL_DATA_QUALITY,
            priority=1.0,
        ))
        state.start_hypothesis(hypothesis.hypothesis_id)
        reason = compilation.data_gap or "data_gap: longitudinal request could not be compiled"
        result = PublicToolResult(
            tool="analyze_ehr_observation_trend",
            source="Synthea synthetic EHR",
            rows=[],
            human_summary=f"纵向 EHR 查询未执行：{reason}。",
            limitations=["未执行纵向查询；请提供明确观察概念和不超过 120 个时间桶的日期范围"],
        )
        evidence = state.add_evidence(Evidence(
            claim=result.human_summary,
            source=result.source,
            sql="deterministic compiler data gap",
            params=[],
            rows=[],
            evidence_type=EvidenceType.ALTERNATIVE,
            quality_flags=result.limitations,
            observation_signal="no_data",
            contradicts=[hypothesis.hypothesis_id],
        ))
        state.resolve_hypothesis(
            hypothesis.hypothesis_id,
            HypothesisStatus.REJECTED,
            counter_evidence_ids=[evidence.evidence_id],
            rationale=reason,
        )
        state.warnings.append(reason)
        state.finish(f"当前无法安全执行纵向 EHR 聚合：{reason}。[{evidence.evidence_id}]")
        state.provider = "governed-deterministic-router"
        state.model = "typed-longitudinal-compiler"
        state.external_model_called = False
        state.audit_metadata = {
            "space": space,
            "method": "ehr_longitudinal_profile",
            "selected_tool": "analyze_ehr_observation_trend",
            "executed_tools": [],
            "subject": subject,
            "routing_rationale": "确定性纵向查询编译器",
            "stop_reason": "data_gap",
            "synthesis": "governed_fallback",
        }
        return state

    def investigate(
        self,
        question: str,
        subject: str,
        space: PublicSpace | None = None,
        reference_catalog_version: str | None = None,
    ) -> InvestigationState:
        plan=PublicClinicalQuestionCompiler().compile(question,space)
        # Longitudinal EHR requests are deterministic follow-ups.  Do not let an external
        # planner replace the typed query or choose a raw observation tool.
        if self.planner and plan.tool != "analyze_ehr_observation_trend":
            decide_parameters=signature(self.planner.decide).parameters
            decision=(
                self.planner.decide(question,subject,allowed_tools=(plan.tool,) if space else None)
                if "allowed_tools" in decide_parameters
                else self.planner.decide(question,subject)
            )
        else:
            decision=None
        selected_tool=decision.tool if decision else plan.tool
        query=decision.query if decision else subject
        routing_override=None
        if space and selected_tool != plan.tool:
            selected_tool=plan.tool
            query=subject
            routing_override="user_selected_space"
        selected_space={"search_studies":"study_registry","lookup_drug_label":"drug_label","lookup_faers_signals":"safety_signal","summarize_ehr_cohort":"synthetic_ehr","analyze_ehr_observation_trend":"synthetic_ehr"}[selected_tool]
        if space:
            selected_space=space
        if selected_tool == "analyze_ehr_observation_trend":
            compilation = compile_ehr_longitudinal_query(
                question,
                subject,
                reference_catalog_version=reference_catalog_version,
            )
            if compilation.query is None:
                return self._longitudinal_data_gap_state(question, subject, selected_space, compilation)
            query = compilation.query
        if selected_tool in {"lookup_drug_label","lookup_faers_signals"}:
            query=normalize_public_drug_query(query)
        elif selected_tool in {"summarize_ehr_cohort", "profile_ehr_concepts"}:
            query=normalize_public_ehr_query(query)
        if selected_tool == "search_studies":
            # Location is a user constraint, not an optional keyword chosen by the
            # router.  Preserve it even when an external model returns only the
            # disease name; the repository then applies the governed batch filter.
            _, question_location = normalize_public_study_query(question)
            _, query_location = normalize_public_study_query(query)
            if question_location and not query_location:
                query = f"{query.rstrip()} {question_location}"
        cross_source=any(term in question.casefold() for term in ("其他来源","其他公开来源","综合","相关研究","是否也","cross-source","other sources"))
        if selected_tool == "analyze_ehr_observation_trend":
            method, method_tools=PUBLIC_METHODS["ehr_longitudinal_profile"]
        elif selected_space in {"drug_label","safety_signal"} and not cross_source:
            method="drug_label_review" if selected_space=="drug_label" else "faers_signal_review"
            method_tools=(selected_tool,)
        else:
            method, method_tools=PUBLIC_METHODS[selected_space]
        state=InvestigationState(question=question,domain=selected_space,status=InvestigationStatus.RUNNING)
        results: list[tuple[PublicToolResult,Evidence]]=[]
        longitudinal_snapshot: dict[str, Any] | None = None
        stop_reason="question_coverage_complete"
        for sequence, tool in enumerate(method_tools, start=1):
            hypothesis=state.add_hypothesis(Hypothesis(
                statement=self._hypothesis_statement(tool,query),
                kind=self._hypothesis_kind(tool),
                priority=max(0.5,1.0-(sequence-1)*0.15),
            ))
            state.start_hypothesis(hypothesis.hypothesis_id)
            step_inputs = {
                "query": query.model_dump(mode="json")
                if tool == "analyze_ehr_observation_trend" and isinstance(query, LongitudinalEHRQuery)
                else query
            }
            step=InvestigationStep(sequence=sequence,tool=tool,status=StepStatus.RUNNING,inputs=step_inputs)
            state.steps.append(step); state.record_step()
            if tool == "analyze_ehr_observation_trend":
                typed_result = self.repository.analyze_ehr_observation_trend(query, limit=120)
                if typed_result.data_version_snapshot is not None:
                    longitudinal_snapshot = typed_result.data_version_snapshot.model_dump(mode="json")
                result = self._longitudinal_tool_result(typed_result)
            else:
                result=getattr(self.repository,tool)(query)
            state.record_query(len(result.rows))
            observed=self._has_observation(tool,result.rows)
            evidence_params = [
                query.model_dump(mode="json")
                if tool == "analyze_ehr_observation_trend" and isinstance(query, LongitudinalEHRQuery)
                else query
            ]
            evidence=state.add_evidence(Evidence(claim=result.human_summary,source=result.source,sql="governed parameterized query",params=evidence_params,rows=result.rows,evidence_type=EvidenceType.ALTERNATIVE,quality_flags=result.limitations,supports=[hypothesis.hypothesis_id] if observed else [],contradicts=[] if observed else [hypothesis.hypothesis_id]))
            state.resolve_hypothesis(
                hypothesis.hypothesis_id,
                HypothesisStatus.SUPPORTED if observed else HypothesisStatus.REJECTED,
                evidence_ids=[evidence.evidence_id] if observed else [],
                counter_evidence_ids=[] if observed else [evidence.evidence_id],
                rationale="查询返回了与假设对应的记录" if observed else "查询没有返回可用于验证该假设的记录",
            )
            step.status=StepStatus.COMPLETED; step.summary=f"形成证据 {evidence.evidence_id}：{result.human_summary}"
            results.append((result,evidence))
            if sequence==1 and not observed and method in {"study_screening","ehr_cohort_profile"}:
                stop_reason="primary_search_empty"
                break
        findings=" ".join(f"{result.human_summary}[{evidence.evidence_id}]" for result,evidence in results)
        limitations="；".join(dict.fromkeys(flag for result,_ in results for flag in result.limitations))
        coverage={"drug_safety_review":["regulatory_label","safety_reports","related_studies","limitations"],"drug_label_review":["regulatory_label","limitations"],"faers_signal_review":["safety_reports","limitations"],"study_screening":["matching_studies","design_comparison","limitations"],"ehr_cohort_profile":["cohort_size","clinical_concepts","limitations"],"ehr_longitudinal_profile":["time_buckets","measurement_completeness","reference_range_status","abnormal_patient_rate","limitations"]}[method]
        if stop_reason=="primary_search_empty": coverage=[coverage[0],"limitations"]
        if method=="drug_safety_review":
            conclusion=self._drug_safety_answer(results,limitations)
        elif method=="drug_label_review":
            conclusion=self._drug_label_only_answer(results[0],limitations)
        elif method=="faers_signal_review":
            conclusion=self._faers_only_answer(results[0],limitations)
        elif method=="study_screening":
            if stop_reason=="primary_search_empty":
                conclusion=f"当前数据中未找到与“{query}”匹配的注册研究，因此没有可供设计比较的研究；这不等于现实中不存在相关研究。[{results[0][1].evidence_id}] 数据边界：{limitations}。"
            else:
                conclusion=self._study_screening_answer(results,limitations)
        elif method=="ehr_longitudinal_profile":
            conclusion=self._ehr_longitudinal_answer(results,limitations)
        else:
            conclusion=self._ehr_cohort_answer(results,limitations)
        synthesis="governed_deterministic"
        if self.planner and method != "ehr_longitudinal_profile":
            try:
                candidate=self.planner.synthesize(question=question,evidence=self._synthesis_evidence(results),required_parts=coverage)
                self._validate_synthesis(candidate,coverage,state.evidence,method)
                conclusion=self._ensure_public_answer_language(
                    question,
                    subject,
                    candidate.answer,
                    method,
                )
                synthesis="external_model_verified"
            except (AttributeError,ValueError) as exc:
                synthesis="governed_fallback"
                state.warnings.append(f"外部模型结论未通过治理校验，已使用确定性摘要：{exc}")
        # Apply the same presentation guard to deterministic fallbacks.  A
        # fallback is still a user-visible answer and must retain Chinese
        # anchors requested by the question (for example 出血 or 共同报告).
        conclusion = self._ensure_public_answer_language(question, subject, conclusion, method)
        state.finish(conclusion)
        state.provider=self.planner.provider if self.planner else "fake"; state.model=self.planner.model if self.planner else "governed-deterministic-router"; state.external_model_called=self.planner is not None and method != "ehr_longitudinal_profile"
        effective_query = query.model_dump(mode="json") if isinstance(query, LongitudinalEHRQuery) else query
        state.audit_metadata={"space":selected_space,"method":method,"selected_tool":selected_tool,"executed_tools":[step.tool for step in state.steps],"subject":subject,"effective_query":effective_query,"routing_rationale":decision.rationale if decision else "确定性问题编译器", "routing_override":routing_override,"evidence_classes":list(dict.fromkeys(result.source for result,_ in results)),"answer_coverage":coverage,"stop_reason":stop_reason,"synthesis":synthesis,"data_version_snapshot":longitudinal_snapshot}
        return state

    @staticmethod
    def _ensure_public_answer_language(
        question: str,
        subject: str,
        answer: str,
        method: str,
    ) -> str:
        """Preserve user-facing Chinese anchors without adding data or citations.

        External models sometimes answer a Chinese request with an English field label (for
        example ``PHASE2``) or with the normalized English disease name.  The label guard keeps
        the result searchable and readable while leaving all values and evidence citations exactly
        as returned by the verified synthesis.
        """

        text = answer.strip()
        q = question.casefold()
        prefixes: list[str] = []
        if method == "study_screening":
            if any(term in q for term in ("阶段", "phase")) and "研究阶段" not in text:
                prefixes.append("研究阶段")
            if any(term in q for term in ("介入", "干预", "intervention")) and "介入" not in text:
                prefixes.append("介入类型")
            if any(term in q for term in ("招募", "状态", "recruitment", "status")) and "招募状态" not in text:
                prefixes.append("招募状态")
            if any(term in q for term in ("边界", "限制")) and "边界" not in text:
                prefixes.append("注册数据边界")
        elif method in {"drug_label_review", "drug_safety_review", "faers_signal_review"}:
            # A verified external answer can retain English regulatory terms (for
            # example ``Bleeding``) even when the user asked in Chinese.  Add
            # only the requested Chinese label; values and citations remain
            # untouched, so this cannot create a new medical claim.
            for term, label in (
                ("出血", "出血风险"),
                ("禁忌", "禁忌"),
                ("警告", "警告"),
                ("不良反应", "不良反应"),
            ):
                if term in q and term not in text:
                    prefixes.append(label)
            if "共同报告" in q and "共同报告" not in text:
                prefixes.append("共同报告")
        # Keep a Chinese subject visible when the router correctly normalized it to an English
        # registry term.  This is especially important for audit readers who entered “胃癌” but
        # should not have to infer that ``Gastric Cancer`` means the same topic.
        if any("\u4e00" <= char <= "\u9fff" for char in subject) and subject not in text:
            prefixes.insert(0, f"主题：{subject}")
        if prefixes:
            return "；".join(prefixes) + "。" + text
        return text

    @classmethod
    def _synthesis_evidence(cls, results: list[tuple[PublicToolResult,Evidence]]) -> list[dict]:
        packets=[]
        for result,evidence in results:
            rows=[]
            row_limit=100 if result.tool=="compare_study_designs" else 50 if result.tool=="profile_ehr_concepts" else 10
            for row in result.rows[:row_limit]:
                rows.append({key:cls._clip(value,1800) for key,value in row.items()})
            packets.append({"evidence_id":evidence.evidence_id,"tool":result.tool,"source":result.source,"summary":result.human_summary,"rows":rows,"limitations":result.limitations})
        return packets

    @staticmethod
    def _validate_synthesis(candidate: PublicSynthesis, required_parts: list[str], evidence: list[Evidence], method: str) -> None:
        missing=set(required_parts)-set(candidate.answered_parts)
        if missing: raise ValueError(f"未覆盖回答部分：{','.join(sorted(missing))}")
        allowed={item.evidence_id for item in evidence}
        cited=set(re.findall(r"\[(E\d{2})\]",candidate.answer))
        if set(candidate.evidence_ids)!=cited or not cited or not cited.issubset(allowed):
            raise ValueError("证据编号缺失或无效")
        if not allowed.issubset(cited): raise ValueError("未引用全部已执行查询的证据")
        if len(re.findall(r"[\u4e00-\u9fff]",candidate.answer))<20: raise ValueError("结论没有形成可读中文摘要")
        if method=="study_screening":
            design_rows=[row for item in evidence for row in item.rows if "study_count" in row]
            observed_counts={str(row.get("study_count")) for row in design_rows if row.get("study_count") is not None}
            if observed_counts and not any(re.search(rf"(?<!\d){re.escape(count)}(?!\d)",candidate.answer) for count in observed_counts):
                raise ValueError("研究设计分布没有列出受治理结果中的实际研究数量")
        if method=="drug_safety_review" and (len(candidate.answer)<300 or not all(term in candidate.answer for term in ("警告","禁忌","不良反应","FAERS","注册"))):
            raise ValueError("药品安全结论没有逐项回答具体内容")
        if method=="drug_safety_review" and re.search(r"(?<!不能)(?<!无法)(?:证实|证明|确认).{0,8}(?:导致|引起)|发生率(?:为|是)\s*\d",candidate.answer):
            raise ValueError("结论包含证据不支持的因果或发生率表述")
        def makes_claim_about(terms: tuple[str, ...]) -> bool:
            clauses = re.split(r"[。；;\n]", candidate.answer)
            affirmative = ("显示", "表明", "发现", "找到", "包括", "列出", "支持", "证实", "提供了")
            negative = ("未提供", "没有提供", "未查询", "没有查询", "不包含", "无法提供")
            return any(
                any(term in clause for term in terms)
                and any(marker in clause for marker in affirmative)
                and not any(marker in clause for marker in negative)
                for clause in clauses
            )

        if method=="faers_signal_review" and makes_claim_about(("FDA 标签","FDA标签","注册研究","ClinicalTrials")):
            raise ValueError("窄范围 FAERS 问题混入了未请求、未查询的来源")
        if method=="drug_label_review" and makes_claim_about(("FAERS","注册研究","ClinicalTrials")):
            raise ValueError("窄范围标签问题混入了未请求、未查询的来源")

    @staticmethod
    def _hypothesis_statement(tool: str, query: str) -> str:
        statements={
            "search_studies":f"注册库中存在与“{query}”相关的研究",
            "compare_study_designs":f"相关研究可按阶段、类型和状态形成可解释的设计分组",
            "lookup_drug_label":f"FDA 标签中存在与“{query}”相关的监管说明",
            "lookup_faers_signals":f"FAERS 样本中存在与“{query}”相关的报告共现信号",
            "summarize_ehr_cohort":f"合成病历中存在与“{query}”匹配的患者队列",
            "profile_ehr_concepts":f"匹配队列存在可描述的疾病、用药、操作或观察画像",
            "analyze_ehr_observation_trend": "纵向观察可以按时间桶安全汇总并保留参考范围与隐私状态",
        }
        return statements[tool]

    @staticmethod
    def _hypothesis_kind(tool: str) -> HypothesisKind:
        if tool in {"lookup_drug_label","lookup_faers_signals"}: return HypothesisKind.CLINICAL_SAFETY
        if tool in {"summarize_ehr_cohort","profile_ehr_concepts","analyze_ehr_observation_trend"}: return HypothesisKind.CLINICAL_DATA_QUALITY
        return HypothesisKind.CLINICAL_EFFICACY

    @staticmethod
    def _has_observation(tool: str, rows: list[dict]) -> bool:
        if not rows: return False
        if tool=="summarize_ehr_cohort": return int(rows[0].get("patient_count") or 0)>0
        return True

    @staticmethod
    def _clip(value: object, limit: int=320) -> str:
        text=" ".join(str(value or "").split())
        return text if len(text)<=limit else text[:limit].rstrip()+"…"

    @classmethod
    def _ehr_longitudinal_answer(cls, results: list[tuple[PublicToolResult,Evidence]], limitations: str) -> str:
        result, evidence = results[0]
        if not result.rows:
            return f"当前受治理观察窗口没有可展示的纵向聚合结果；这不等于没有临床观察。[{evidence.evidence_id}] 数据边界：{limitations}。"
        details: list[str] = []
        for row in result.rows[:120]:
            bucket = cls._clip(row.get("time_bucket") or "未知时间桶", 40)
            concept = cls._clip(row.get("concept_name") or row.get("concept_code") or "未命名观察", 100)
            unit = f"，单位 {cls._clip(row['unit'], 40)}" if row.get("unit") else ""
            status = str(row.get("reference_range_status") or "unknown")
            if row.get("suppressed"):
                details.append(f"{bucket} {concept}{unit}：低于最小披露阈值，结果已抑制")
            elif status != "available":
                details.append(f"{bucket} {concept}{unit}：参考范围状态为 {status}，异常患者比例不可用")
            else:
                rate = row.get("abnormal_patient_rate")
                details.append(f"{bucket} {concept}{unit}：异常患者比例 {float(rate):.1%}" if rate is not None else f"{bucket} {concept}{unit}：异常患者比例不可用")
        return f"纵向 EHR 聚合结果：{'；'.join(details)}。[{evidence.evidence_id}] 数据边界：{limitations}。"

    @classmethod
    def _study_screening_answer(cls, results: list[tuple[PublicToolResult,Evidence]], limitations: str) -> str:
        search_result, search_evidence = results[0]
        design_result, design_evidence = next(
            ((result, evidence) for result, evidence in results if result.tool == "compare_study_designs"),
            (None, None),
        )
        if design_result is None or design_evidence is None:
            return f"已完成研究查找，但当前没有返回可用的设计分布结果。{search_result.human_summary}[{search_evidence.evidence_id}] 数据边界：{limitations}。"
        groups=[]
        for row in design_result.rows[:10]:
            phase=cls._clip(row.get("phase") or "未标注阶段",80)
            study_type=cls._clip(row.get("study_type") or "未标注类型",80)
            status=cls._clip(row.get("overall_status") or "未标注状态",80)
            intervention=cls._clip(row.get("intervention_type") or "未标注介入",80)
            count=row.get("study_count")
            average=row.get("average_enrollment")
            enrollment=f"，平均入组 {average} 人" if average is not None else ""
            groups.append(f"{phase} / {study_type} / {status} / {intervention}：{count} 项{enrollment}")
        distribution="；".join(groups) if groups else "当前没有可展示的设计组合"
        return (
            f"研究查找：{search_result.human_summary}[{search_evidence.evidence_id}] "
            f"设计分布（按研究数量排序，展示前 {len(groups)} 个受治理组合）：{distribution}。[{design_evidence.evidence_id}] "
            "这些数量是注册设计分组，不代表疗效或安全性结果；一项研究含多种介入时可能计入多个介入类别。"
            f"数据边界：{limitations}。"
        )

    @classmethod
    def _ehr_cohort_answer(cls, results: list[tuple[PublicToolResult,Evidence]], limitations: str) -> str:
        """Render a useful EHR answer even when an external synthesis is rejected.

        The fallback intentionally reports aggregate counts and a small, readable concept
        sample.  It never turns a missing row into a clinical claim and keeps numeric
        observations' units and examples next to the concept name.
        """

        cohort_result, cohort_evidence = results[0]
        cohort = cohort_result.rows[0] if cohort_result.rows else {}
        counts = [
            f"{label} {cohort[key]}"
            for key, label in (
                ("patient_count", "虚拟患者"),
                ("encounter_count", "就诊记录"),
                ("condition_count", "疾病记录"),
                ("medication_count", "用药记录"),
                ("procedure_count", "操作记录"),
                ("observation_count", "观察记录"),
            )
            if cohort.get(key) is not None
        ]
        overview = "、".join(counts) if counts else cohort_result.human_summary

        profile_result, profile_evidence = next(
            ((result, evidence) for result, evidence in results if result.tool == "profile_ehr_concepts"),
            (None, None),
        )
        sections: list[str] = []
        if profile_result is not None and profile_evidence is not None:
            for concept_type in ("疾病", "用药", "操作", "观察"):
                items = []
                for row in [item for item in profile_result.rows if item.get("concept_type") == concept_type][:5]:
                    name = cls._clip(row.get("concept_name") or "未命名概念", 120)
                    patient_count = row.get("patient_count")
                    item_text = f"{name}（{patient_count} 人" if patient_count is not None else f"{name}（"
                    if concept_type == "观察" and row.get("is_measurement"):
                        if row.get("observation_units"):
                            item_text += f"；单位 {cls._clip(row['observation_units'], 40)}"
                        if row.get("example_values"):
                            item_text += f"；示例值 {cls._clip(row['example_values'], 120)}"
                    items.append(item_text + "）")
                if items:
                    title = "检验/数值观察" if concept_type == "观察" else concept_type
                    sections.append(f"{title}：" + "、".join(items))
            profile_text = "；".join(sections) if sections else profile_result.human_summary
            return (
                f"队列概况：{overview}。[{cohort_evidence.evidence_id}] "
                f"临床画像：{profile_text}。[{profile_evidence.evidence_id}] "
                f"数据边界：{limitations}。"
            )
        return f"队列概况：{overview}。[{cohort_evidence.evidence_id}] 当前没有返回临床内容画像。数据边界：{limitations}。"

    @classmethod
    def _drug_safety_answer(cls, results: list[tuple[PublicToolResult,Evidence]], limitations: str) -> str:
        by_tool={result.tool:(result,evidence) for result,evidence in results}
        label_result,label_evidence=by_tool["lookup_drug_label"]
        label_parts=[]
        for key,title in (("indications_and_usage","适应证"),("boxed_warning","黑框警告"),("warnings","警告"),("contraindications","禁忌"),("adverse_reactions","标签不良反应")):
            value=next((row.get(key) for row in label_result.rows if row.get(key)),None)
            label_parts.append(f"{title}：{cls._clip(value) if value else '当前下载标签未提供该栏目'}")
        label_text="；".join(label_parts) if label_parts else "当前下载样本没有返回可展示的警告、禁忌或不良反应正文"
        faers_result,faers_evidence=by_tool["lookup_faers_signals"]
        reactions=[]
        for row in faers_result.rows[:5]:
            name=row.get("reaction_term")
            if name: reactions.append(f"{name}（{int(row.get('report_count') or 0)} 份报告）")
        faers_text="、".join(reactions) if reactions else "当前样本没有匹配的共同报告"
        study_result,study_evidence=by_tool["search_studies"]
        titles=[cls._clip(row.get("brief_title"),120) for row in study_result.rows[:3] if row.get("brief_title")]
        study_text="；".join(titles) if titles else "当前样本没有匹配的注册研究"
        return (
            f"FDA 标签回答：{label_text}。[{label_evidence.evidence_id}] "
            f"FAERS 补充信息：当前样本中较常见的共同报告包括 {faers_text}；这只是报告共现，不能证明因果或发生率。[{faers_evidence.evidence_id}] "
            f"注册研究补充信息：找到 {len(study_result.rows)} 项匹配研究，示例包括 {study_text}；注册信息不能证明疗效或安全性。[{study_evidence.evidence_id}] "
            f"数据边界：{limitations}。"
        )

    @classmethod
    def _drug_label_only_answer(cls, item: tuple[PublicToolResult,Evidence], limitations: str) -> str:
        result,evidence=item
        parts=[]
        for key,title in (("indications_and_usage","适应证"),("boxed_warning","黑框警告"),("warnings","警告"),("contraindications","禁忌"),("adverse_reactions","不良反应")):
            value=next((row.get(key) for row in result.rows if row.get(key)),None)
            parts.append(f"{title}：{cls._clip(value) if value else '当前下载标签未提供该栏目'}")
        return f"FDA 标签核对结果：{'；'.join(parts)}。[{evidence.evidence_id}] 数据边界：{limitations}。"

    @classmethod
    def _faers_only_answer(cls, item: tuple[PublicToolResult,Evidence], limitations: str) -> str:
        result,evidence=item
        rows=[]
        for row in result.rows[:10]:
            product_zh=row.get("medicinal_product_zh")
            product_raw=row.get("medicinal_product") or "未知药品名"
            product=f"{product_zh}（{product_raw}）" if product_zh else product_raw
            reaction_zh=row.get("reaction_term_zh")
            reaction_raw=row.get("reaction_term") or "未知事件"
            reaction=f"{reaction_zh}（{reaction_raw}）" if reaction_zh else reaction_raw
            report_count=int(row.get("report_count") or 0)
            serious_count=row.get("serious_report_count")
            serious_text=(f"，其中严重报告 {int(serious_count)} 份" if serious_count is not None else "")
            rows.append(f"{product} 与 {reaction}：{report_count} 份{serious_text}")
        ranking="；".join(rows) if rows else "当前样本没有匹配的共同报告"
        return (
            f"当前 FAERS 下载样本中的前列共同报告组合为：{ranking}。[{evidence.evidence_id}] "
            f"这些数字只表示当前样本内药物名称与事件同时出现在报告中的计数，不是该药最典型不良反应的医学排名。"
            f"数据边界：{limitations}。"
        )

