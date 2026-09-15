from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, fields
from datetime import UTC, date, datetime, timedelta
from typing import TypeVar

import psycopg

from data_generator.clinical.domain import (
    AdverseEvent,
    BaselineAssessment,
    ClinicalDataset,
    ClinicalManifest,
    ClinicalTrial,
    OutcomeAssessment,
    Participant,
    ParticipantVisit,
    ProtocolDeviation,
    RandomizationAssignment,
    SpecimenHandlingEvent,
    TreatmentExposure,
    TrialSite,
)


T = TypeVar("T")
TRIAL_ID = "TRIAL-CF-101"
TRIAL_START = datetime(2026, 1, 12, 9, tzinfo=UTC)
REGION_SITES = {
    "Asia": (
        ("SITE-01", "Japan"),
        ("SITE-02", "Singapore"),
        ("SITE-03", "South Korea"),
        ("SITE-04", "Thailand"),
        ("SITE-05", "Malaysia"),
        ("SITE-17", "Japan"),
    ),
    "Europe": (
        ("SITE-06", "Germany"),
        ("SITE-07", "France"),
        ("SITE-08", "Spain"),
        ("SITE-09", "United Kingdom"),
        ("SITE-10", "Italy"),
        ("SITE-11", "Netherlands"),
    ),
    "North America": (
        ("SITE-12", "United States"),
        ("SITE-13", "Canada"),
        ("SITE-14", "United States"),
        ("SITE-15", "Canada"),
        ("SITE-16", "United States"),
        ("SITE-18", "United States"),
    ),
}


def stable_score(seed: int, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def stable_choice(seed: int, key: str, values: tuple[T, ...]) -> T:
    index = min(int(stable_score(seed, key) * len(values)), len(values) - 1)
    return values[index]


def _noise(seed: int, key: str, spread: float) -> float:
    return (stable_score(seed, key) - 0.5) * 2 * spread


def _manifest(dataset: ClinicalDataset) -> ClinicalManifest:
    table_map = {
        "clinical_trials": dataset.trials,
        "trial_sites": dataset.sites,
        "participants": dataset.participants,
        "randomization_assignments": dataset.assignments,
        "baseline_assessments": dataset.baselines,
        "outcome_assessments": dataset.outcomes,
        "treatment_exposures": dataset.exposures,
        "adverse_events": dataset.adverse_events,
        "protocol_deviations": dataset.protocol_deviations,
        "participant_visits": dataset.visits,
        "specimen_handling_events": dataset.handling_events,
    }
    payload = [[asdict(row) for row in rows] for rows in table_map.values()]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return ClinicalManifest(
        seed=dataset.seed,
        trial_id=TRIAL_ID,
        table_counts={name: len(rows) for name, rows in table_map.items()},
        content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
    )


def generate_clinical_dataset(
    seed: int,
    participant_count: int = 3_000,
) -> ClinicalDataset:
    """Generate a neutral, reproducible two-arm synthetic clinical trial."""
    if participant_count < 1:
        raise ValueError("participant_count must be at least 1")

    trial = ClinicalTrial(
        trial_id=TRIAL_ID,
        protocol_version="1.0",
        phase="Phase III (synthetic)",
        indication="Synthetic inflammatory condition",
        start_date=TRIAL_START.date(),
        planned_end_date=date(2027, 1, 31),
        primary_endpoint="Week-12 improvement score",
        status="active",
    )
    site_specs = [item for sites in REGION_SITES.values() for item in sites]
    site_region = {
        site_id: region
        for region, specs in REGION_SITES.items()
        for site_id, _ in specs
    }
    sites = [
        TrialSite(
            site_id=site_id,
            trial_id=TRIAL_ID,
            site_name=f"Synthetic Research Center {site_id[-2:]}",
            region=site_region[site_id],
            country=country,
            opened_at=TRIAL_START.date() - timedelta(days=30),
        )
        for site_id, country in site_specs
    ]

    dataset = ClinicalDataset(seed=seed, trials=[trial], sites=sites)
    age_bands = ("18-39", "40-59", "60-74", "75+")
    for number in range(1, participant_count + 1):
        participant_id = f"PT-{number:05d}"
        site_id, _ = stable_choice(seed, f"{participant_id}:site", tuple(site_specs))
        region = site_region[site_id]
        enrollment_offset = int(stable_score(seed, f"{participant_id}:enrolled") * 150)
        enrolled_at = TRIAL_START + timedelta(days=enrollment_offset)
        arm = "treatment" if stable_score(seed, f"{participant_id}:arm") < 0.5 else "control"
        age_band = stable_choice(seed, f"{participant_id}:age", age_bands)
        sex = stable_choice(seed, f"{participant_id}:sex", ("female", "male"))
        discontinued = stable_score(seed, f"{participant_id}:discontinue") < 0.04
        discontinued_at = enrolled_at + timedelta(days=50) if discontinued else None

        dataset.participants.append(
            Participant(
                participant_id=participant_id,
                trial_id=TRIAL_ID,
                site_id=site_id,
                region=region,
                age_band=age_band,
                sex=sex,
                enrolled_at=enrolled_at,
                discontinued_at=discontinued_at,
                discontinuation_reason="participant_withdrew" if discontinued else None,
            )
        )
        dataset.assignments.append(
            RandomizationAssignment(
                participant_id=participant_id,
                trial_id=TRIAL_ID,
                arm=arm,
                randomized_at=enrolled_at,
                stratification_region=region,
                randomization_block=f"{region[:2].upper()}-{1 + (number - 1) // 4:04d}",
            )
        )
        baseline_score = round(45 + _noise(seed, f"{participant_id}:baseline", 15), 2)
        severity = "high" if baseline_score < 40 else ("moderate" if baseline_score < 52 else "low")
        dataset.baselines.append(
            BaselineAssessment(
                assessment_id=f"BASE-{number:05d}",
                participant_id=participant_id,
                assessment_date=enrolled_at.date(),
                baseline_score=baseline_score,
                severity_band=severity,
                disease_duration_months=3 + int(stable_score(seed, f"{participant_id}:duration") * 117),
            )
        )

        adherence = 0.94 + _noise(seed, f"{participant_id}:adherence", 0.05)
        for week in range(1, 13):
            actual_dose = 100.0 if stable_score(seed, f"{participant_id}:dose:{week}") < adherence else 0.0
            dataset.exposures.append(
                TreatmentExposure(
                    exposure_id=f"DOSE-{number:05d}-{week:02d}",
                    participant_id=participant_id,
                    dose_date=(enrolled_at + timedelta(weeks=week)).date(),
                    planned_dose=100.0,
                    actual_dose=actual_dose,
                    lot_id=f"LOT-{site_id[-2:]}-{1 + (week - 1) // 4}",
                    adherence_status="taken" if actual_dose else "missed",
                )
            )

        missing_week12 = discontinued or stable_score(seed, f"{participant_id}:missing12") < 0.05
        for week in (4, 12):
            scheduled = enrolled_at + timedelta(weeks=week)
            missing = week == 12 and missing_week12
            treatment_gain = (3.0 if week == 4 else 9.0) if arm == "treatment" else (1.5 if week == 4 else 4.5)
            outcome = None if missing else round(
                baseline_score + treatment_gain + _noise(seed, f"{participant_id}:outcome:{week}", 4.0),
                2,
            )
            dataset.outcomes.append(
                OutcomeAssessment(
                    assessment_id=f"OUT-{number:05d}-{week:02d}",
                    participant_id=participant_id,
                    visit_week=week,
                    scheduled_date=scheduled.date(),
                    assessed_at=None if missing else scheduled + timedelta(hours=2),
                    outcome_score=outcome,
                    assessment_status="missing" if missing else "completed",
                    missing_reason="discontinued" if missing and discontinued else ("visit_missed" if missing else None),
                )
            )

        for week in (0, 4, 8, 12):
            scheduled = enrolled_at + timedelta(weeks=week)
            missed = week == 12 and missing_week12
            deviation_days = int(_noise(seed, f"{participant_id}:visit:{week}", 3))
            dataset.visits.append(
                ParticipantVisit(
                    visit_id=f"VISIT-{number:05d}-{week:02d}",
                    participant_id=participant_id,
                    visit_week=week,
                    scheduled_at=scheduled,
                    occurred_at=None if missed else scheduled + timedelta(days=deviation_days),
                    visit_status="missed" if missed else "completed",
                    window_deviation_days=None if missed else deviation_days,
                )
            )

        if stable_score(seed, f"{participant_id}:ae") < 0.10:
            serious = stable_score(seed, f"{participant_id}:sae") < 0.08
            started = enrolled_at + timedelta(days=10 + int(stable_score(seed, f"{participant_id}:ae-day") * 60))
            dataset.adverse_events.append(
                AdverseEvent(
                    adverse_event_id=f"AE-{number:05d}",
                    participant_id=participant_id,
                    term_group=stable_choice(seed, f"{participant_id}:ae-term", ("headache", "fatigue", "nausea")),
                    severity="severe" if serious else "mild",
                    serious=serious,
                    relatedness="possible" if arm == "treatment" else "unlikely",
                    started_at=started,
                    ended_at=started + timedelta(days=3),
                    action_taken="dose_interrupted" if serious else "none",
                )
            )
        if stable_score(seed, f"{participant_id}:deviation") < 0.03:
            occurred = enrolled_at + timedelta(days=28)
            dataset.protocol_deviations.append(
                ProtocolDeviation(
                    deviation_id=f"DEV-{number:05d}",
                    participant_id=participant_id,
                    site_id=site_id,
                    category="visit_window",
                    severity="minor",
                    occurred_at=occurred,
                    detected_at=occurred + timedelta(days=2),
                    description_code="VISIT_OUTSIDE_WINDOW",
                )
            )

    for site_id, _ in site_specs:
        for lot_number in range(1, 4):
            dataset.handling_events.append(
                SpecimenHandlingEvent(
                    handling_event_id=f"HANDLE-{site_id[-2:]}-{lot_number}",
                    site_id=site_id,
                    lot_id=f"LOT-{site_id[-2:]}-{lot_number}",
                    event_type="routine_temperature_check",
                    occurred_at=TRIAL_START + timedelta(weeks=lot_number * 4),
                    temperature_c=round(4.0 + _noise(seed, f"{site_id}:temp:{lot_number}", 0.5), 2),
                    duration_minutes=15,
                    excursion_flag=False,
                )
            )

    dataset.manifest = _manifest(dataset)
    return dataset


def _row_values(row) -> tuple:
    return tuple(getattr(row, item.name) for item in fields(row))


def write_clinical_dataset(
    database_url: str,
    dataset: ClinicalDataset,
    *,
    reset: bool,
) -> ClinicalManifest:
    """Write every clinical table in one transaction and return its manifest."""
    if dataset.manifest is None:
        raise ValueError("dataset manifest is required")
    inserts = (
        ("clinical_trials", dataset.trials),
        ("trial_sites", dataset.sites),
        ("participants", dataset.participants),
        ("randomization_assignments", dataset.assignments),
        ("baseline_assessments", dataset.baselines),
        ("outcome_assessments", dataset.outcomes),
        ("treatment_exposures", dataset.exposures),
        ("adverse_events", dataset.adverse_events),
        ("protocol_deviations", dataset.protocol_deviations),
        ("participant_visits", dataset.visits),
        ("specimen_handling_events", dataset.handling_events),
    )
    column_names = {
        table: ", ".join(item.name for item in fields(rows[0])) if rows else ""
        for table, rows in inserts
    }
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            if reset:
                cursor.execute(
                    """TRUNCATE TABLE
                    clinical_raw.specimen_handling_events,
                    clinical_raw.participant_visits,
                    clinical_raw.protocol_deviations,
                    clinical_raw.adverse_events,
                    clinical_raw.treatment_exposures,
                    clinical_raw.outcome_assessments,
                    clinical_raw.baseline_assessments,
                    clinical_raw.randomization_assignments,
                    clinical_raw.participants,
                    clinical_raw.trial_sites,
                    clinical_raw.clinical_trials"""
                )
            for table, rows in inserts:
                if not rows:
                    continue
                placeholders = ", ".join("%s" for _ in fields(rows[0]))
                cursor.executemany(
                    f"INSERT INTO clinical_raw.{table} ({column_names[table]}) VALUES ({placeholders})",
                    [_row_values(row) for row in rows],
                )
    return dataset.manifest

