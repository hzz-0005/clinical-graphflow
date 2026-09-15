from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class ClinicalTrial:
    trial_id: str
    protocol_version: str
    phase: str
    indication: str
    start_date: date
    planned_end_date: date
    primary_endpoint: str
    status: str


@dataclass(frozen=True, slots=True)
class TrialSite:
    site_id: str
    trial_id: str
    site_name: str
    region: str
    country: str
    opened_at: date
    closed_at: date | None = None


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    trial_id: str
    site_id: str
    region: str
    age_band: str
    sex: str
    enrolled_at: datetime
    discontinued_at: datetime | None = None
    discontinuation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RandomizationAssignment:
    participant_id: str
    trial_id: str
    arm: str
    randomized_at: datetime
    stratification_region: str
    randomization_block: str


@dataclass(frozen=True, slots=True)
class BaselineAssessment:
    assessment_id: str
    participant_id: str
    assessment_date: date
    baseline_score: float
    severity_band: str
    disease_duration_months: int


@dataclass(frozen=True, slots=True)
class OutcomeAssessment:
    assessment_id: str
    participant_id: str
    visit_week: int
    scheduled_date: date
    assessed_at: datetime | None
    outcome_score: float | None
    assessment_status: str
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TreatmentExposure:
    exposure_id: str
    participant_id: str
    dose_date: date
    planned_dose: float
    actual_dose: float
    lot_id: str
    adherence_status: str


@dataclass(frozen=True, slots=True)
class AdverseEvent:
    adverse_event_id: str
    participant_id: str
    term_group: str
    severity: str
    serious: bool
    relatedness: str
    started_at: datetime
    ended_at: datetime | None
    action_taken: str


@dataclass(frozen=True, slots=True)
class ProtocolDeviation:
    deviation_id: str
    participant_id: str
    site_id: str
    category: str
    severity: str
    occurred_at: datetime
    detected_at: datetime
    description_code: str


@dataclass(frozen=True, slots=True)
class ParticipantVisit:
    visit_id: str
    participant_id: str
    visit_week: int
    scheduled_at: datetime
    occurred_at: datetime | None
    visit_status: str
    window_deviation_days: int | None


@dataclass(frozen=True, slots=True)
class SpecimenHandlingEvent:
    handling_event_id: str
    site_id: str
    lot_id: str
    event_type: str
    occurred_at: datetime
    temperature_c: float
    duration_minutes: int
    excursion_flag: bool


@dataclass(frozen=True, slots=True)
class ClinicalManifest:
    seed: int
    trial_id: str
    table_counts: dict[str, int]
    content_hash: str


@dataclass(slots=True)
class ClinicalDataset:
    seed: int
    trials: list[ClinicalTrial] = field(default_factory=list)
    sites: list[TrialSite] = field(default_factory=list)
    participants: list[Participant] = field(default_factory=list)
    assignments: list[RandomizationAssignment] = field(default_factory=list)
    baselines: list[BaselineAssessment] = field(default_factory=list)
    outcomes: list[OutcomeAssessment] = field(default_factory=list)
    exposures: list[TreatmentExposure] = field(default_factory=list)
    adverse_events: list[AdverseEvent] = field(default_factory=list)
    protocol_deviations: list[ProtocolDeviation] = field(default_factory=list)
    visits: list[ParticipantVisit] = field(default_factory=list)
    handling_events: list[SpecimenHandlingEvent] = field(default_factory=list)
    manifest: ClinicalManifest | None = None

