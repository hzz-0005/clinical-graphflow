from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from data_generator.clinical.domain import (
    ClinicalDataset,
    SpecimenHandlingEvent,
)
from data_generator.clinical.generator import TRIAL_START, _manifest, stable_score


def _scenario_site_map(dataset: ClinicalDataset) -> dict[str, str]:
    """Create center composition without changing region or randomization."""
    site_map = {row.participant_id: row.site_id for row in dataset.participants}
    site_18 = sorted(
        row.participant_id for row in dataset.participants if row.site_id == "SITE-18"
    )
    keep_site_18 = set(site_18[:20])
    for participant in dataset.participants:
        score = stable_score(dataset.seed, f"{participant.participant_id}:site17-concentration")
        if (
            participant.region == "Asia"
            and participant.site_id != "SITE-17"
            and score < 0.22
        ):
            site_map[participant.participant_id] = "SITE-17"
        elif participant.site_id == "SITE-18" and participant.participant_id not in keep_site_18:
            site_map[participant.participant_id] = "SITE-12"
    return site_map


def apply_site_17_scenario(dataset: ClinicalDataset) -> ClinicalDataset:
    """Inject the canonical SITE-17 handling → exposure → outcome mechanism."""
    site_map = _scenario_site_map(dataset)
    arm_map = {row.participant_id: row.arm for row in dataset.assignments}

    participants = []
    for row in dataset.participants:
        new_site = site_map[row.participant_id]
        age_band = row.age_band
        if (
            row.region == "Asia"
            and age_band == "18-39"
            and stable_score(dataset.seed, f"{row.participant_id}:age-distractor") < 0.35
        ):
            age_band = "40-59"
        participants.append(replace(row, site_id=new_site, age_band=age_band))

    exposures = []
    for row in dataset.exposures:
        site_id = site_map[row.participant_id]
        week = int(row.exposure_id.rsplit("-", 1)[1])
        actual_dose = row.actual_dose
        if (
            site_id == "SITE-17"
            and arm_map[row.participant_id] == "treatment"
            and 2 <= week <= 6
            and stable_score(dataset.seed, f"{row.participant_id}:cold-chain:{week}") < 0.78
        ):
            actual_dose = 0.0
        exposures.append(
            replace(
                row,
                actual_dose=actual_dose,
                lot_id=f"LOT-{site_id[-2:]}-{1 + (week - 1) // 4}",
                adherence_status="taken" if actual_dose else "missed",
            )
        )

    outcomes = []
    for row in dataset.outcomes:
        site_id = site_map[row.participant_id]
        outcome = row.outcome_score
        if row.visit_week == 12 and outcome is not None:
            if site_id == "SITE-17" and arm_map[row.participant_id] == "treatment":
                outcome = round(outcome - 6.5, 2)
            elif site_id == "SITE-18" and arm_map[row.participant_id] == "treatment":
                outcome = round(outcome + 15.0, 2)
        outcomes.append(replace(row, outcome_score=outcome))

    visits = []
    for row in dataset.visits:
        if site_map[row.participant_id] == "SITE-09" and row.occurred_at is not None:
            magnitude = 4 + int(
                stable_score(dataset.seed, f"{row.participant_id}:site09-visit:{row.visit_week}") * 4
            )
            direction = -1 if stable_score(
                dataset.seed, f"{row.participant_id}:site09-direction:{row.visit_week}"
            ) < 0.5 else 1
            deviation = magnitude * direction
            visits.append(
                replace(
                    row,
                    occurred_at=row.scheduled_at + timedelta(days=deviation),
                    window_deviation_days=deviation,
                )
            )
        else:
            visits.append(row)

    deviations = [
        replace(row, site_id=site_map[row.participant_id])
        for row in dataset.protocol_deviations
    ]
    handling_events = list(dataset.handling_events)
    handling_events.append(
        SpecimenHandlingEvent(
            handling_event_id="HANDLE-17-EXCURSION",
            site_id="SITE-17",
            lot_id="LOT-17-1",
            event_type="cold_chain_temperature_excursion",
            occurred_at=TRIAL_START + timedelta(weeks=3),
            temperature_c=12.4,
            duration_minutes=240,
            excursion_flag=True,
        )
    )

    result = ClinicalDataset(
        seed=dataset.seed,
        trials=list(dataset.trials),
        sites=list(dataset.sites),
        participants=participants,
        assignments=list(dataset.assignments),
        baselines=list(dataset.baselines),
        outcomes=outcomes,
        exposures=exposures,
        adverse_events=list(dataset.adverse_events),
        protocol_deviations=deviations,
        visits=visits,
        handling_events=handling_events,
    )
    result.manifest = _manifest(result)
    return result

