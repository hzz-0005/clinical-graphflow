from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clinical.canonical import SAFE_CLINICAL_IDENTIFIER

REQUIRED_DM_FIELDS = ("STUDYID", "USUBJID", "SITEID", "ARM")
ARM_MAP = {"control": "control", "treatment": "treatment"}


class CanonicalClinicalRecord(BaseModel):
    """De-identified row contract produced by the minimal CDISC bridge."""

    model_config = ConfigDict(frozen=True)

    participant_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    trial_id: str = Field(pattern=SAFE_CLINICAL_IDENTIFIER)
    site_id: str = Field(pattern=SAFE_CLINICAL_IDENTIFIER)
    arm: Literal["control", "treatment"]
    region: str | None = None
    intention_to_treat: bool = True
    safety_population: bool = True
    per_protocol: bool = False
    baseline_value: float | None = None
    week12_value: float | None = None
    week12_improvement: float | None = None


class CdiscClinicalAdapter:
    """Map a strict synthetic SDTM DM + ADaM ADSL/ADEFF subset to canonical rows."""

    def __init__(self, pseudonym_salt: str) -> None:
        if not pseudonym_salt:
            raise ValueError("pseudonym salt is required")
        self._salt = pseudonym_salt

    def map(
        self,
        dm: Iterable[Mapping[str, Any]],
        adsl: Iterable[Mapping[str, Any]],
        adeff: Iterable[Mapping[str, Any]],
    ) -> tuple[CanonicalClinicalRecord, ...]:
        dm_rows = list(dm)
        self._validate_dm(dm_rows)
        dm_trials = {str(row["USUBJID"]): str(row["STUDYID"]) for row in dm_rows}
        adsl_by_subject = self._index("ADSL", adsl, dm_trials)
        adeff_by_subject = self._index("ADEFF", adeff, dm_trials, efficacy=True)

        records = []
        for row in dm_rows:
            subject = str(row["USUBJID"])
            trial = str(row["STUDYID"])
            arm_name = str(row["ARM"]).strip().lower()
            if arm_name not in ARM_MAP:
                raise ValueError(f"unknown CDISC arm: {row['ARM']}")
            subject_adsl = adsl_by_subject.get(subject, {})
            subject_eff = adeff_by_subject.get(subject, {})
            change = self._number(subject_eff.get("CHG"))
            records.append(
                CanonicalClinicalRecord(
                    participant_key=self._pseudonym(trial, subject),
                    trial_id=trial,
                    site_id=str(row["SITEID"]),
                    arm=ARM_MAP[arm_name],
                    region=self._optional_text(subject_adsl.get("REGION1")),
                    intention_to_treat=subject_adsl.get("ITTFL", "Y") == "Y",
                    safety_population=subject_adsl.get("SAFFL", "Y") == "Y",
                    per_protocol=subject_adsl.get("PPROTFL", "N") == "Y",
                    baseline_value=self._number(subject_eff.get("BASE")),
                    week12_value=self._number(subject_eff.get("AVAL")),
                    week12_improvement=-change if change is not None else None,
                )
            )
        return tuple(records)

    @staticmethod
    def _validate_dm(rows: list[Mapping[str, Any]]) -> None:
        seen: set[tuple[str, str]] = set()
        for row in rows:
            for field in REQUIRED_DM_FIELDS:
                if not str(row.get(field, "")).strip():
                    raise ValueError(f"required CDISC field {field} is missing")
            key = (str(row["STUDYID"]), str(row["USUBJID"]))
            if key in seen:
                raise ValueError(f"duplicate CDISC subject: {key[1]}")
            seen.add(key)

    @staticmethod
    def _index(
        domain: str,
        rows: Iterable[Mapping[str, Any]],
        dm_trials: Mapping[str, str],
        efficacy: bool = False,
    ) -> dict[str, Mapping[str, Any]]:
        indexed: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            subject = str(row.get("USUBJID", ""))
            trial = str(row.get("STUDYID", ""))
            if not subject or not trial:
                raise ValueError(f"required CDISC field missing in {domain}")
            if subject in dm_trials and dm_trials[subject] != trial:
                raise ValueError(f"cross-trial CDISC join rejected for {subject}")
            if efficacy and not (
                row.get("PARAMCD") == "PRIMARY" and row.get("AVISIT") == "Week 12"
            ):
                continue
            if subject in indexed:
                raise ValueError(f"duplicate {domain} record for {subject}")
            indexed[subject] = row
        return indexed

    def _pseudonym(self, trial_id: str, subject_id: str) -> str:
        value = f"{self._salt}:{trial_id}:{subject_id}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _number(value: Any) -> float | None:
        return None if value is None else float(value)

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return None if value is None else str(value)

