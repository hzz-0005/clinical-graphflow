from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clinical.tabular import TabularFile


MISSING = (None, "")
BOOLEAN_VALUES = {"y", "n", "yes", "no", "true", "false"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


class ColumnProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    column: str
    inferred_type: str
    row_count: int
    non_null_count: int
    missing_rate: float = Field(ge=0, le=1)
    unique_count: int = Field(ge=0)
    unique_ratio: float = Field(ge=0, le=1)
    minimum: float | str | None = None
    maximum: float | str | None = None
    distinct_values: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    conflicting_type_count: int = Field(default=0, ge=0)


class FileProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    format: str
    row_count: int
    columns: tuple[ColumnProfile, ...]


class CandidateKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    filename: str
    columns: tuple[str, ...]
    uniqueness_ratio: float = Field(ge=0, le=1)


class RelationshipProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    left_file: str
    left_column: str
    right_file: str
    right_column: str
    overlap_ratio: float = Field(ge=0, le=1)
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class BatchProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    files: tuple[FileProfile, ...]
    candidate_keys: tuple[CandidateKey, ...]
    relationships: tuple[RelationshipProfile, ...]


def _present(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _text(value: Any) -> str:
    return str(value).strip()


def _as_integer(value: Any) -> int:
    text = _text(value)
    if not re.fullmatch(r"[+-]?\d+", text):
        raise ValueError
    return int(text)


def _as_decimal(value: Any) -> float:
    number = float(_text(value))
    if not math.isfinite(number):
        raise ValueError
    return number


def _as_date(value: Any) -> date:
    return date.fromisoformat(_text(value))


def _as_datetime(value: Any) -> datetime:
    text = _text(value).replace("Z", "+00:00")
    if "T" not in text and " " not in text:
        raise ValueError
    return datetime.fromisoformat(text)


def _all_parse(values: list[Any], parser) -> tuple[bool, list[Any]]:
    parsed = []
    for value in values:
        try:
            parsed.append(parser(value))
        except (TypeError, ValueError, OverflowError):
            return False, []
    return True, parsed


class DataProfiler:
    def __init__(self, max_relationship_columns: int = 80) -> None:
        self._max_relationship_columns = max_relationship_columns

    def profile(self, files: tuple[TabularFile, ...]) -> BatchProfile:
        file_profiles = tuple(self._profile_file(item) for item in files)
        keys = tuple(
            CandidateKey(filename=file.filename, columns=(column.column,), uniqueness_ratio=column.unique_ratio)
            for file in file_profiles
            for column in file.columns
            if column.non_null_count == file.row_count and column.unique_ratio == 1 and file.row_count > 1
        )
        relationships = self._relationships(files)
        return BatchProfile(files=file_profiles, candidate_keys=keys, relationships=relationships)

    def _profile_file(self, item: TabularFile) -> FileProfile:
        return FileProfile(
            filename=item.filename,
            format=item.format,
            row_count=item.row_count,
            columns=tuple(self._profile_column(column, item.rows) for column in item.columns),
        )

    def _profile_column(self, column: str, rows: tuple[dict[str, Any], ...]) -> ColumnProfile:
        raw_values = [row.get(column) for row in rows]
        values = [value for value in raw_values if _present(value)]
        texts = [_text(value) for value in values]
        unique = set(texts)
        row_count = len(raw_values)
        non_null_count = len(values)
        unique_ratio = len(unique) / non_null_count if non_null_count else 0.0
        inferred, parsed, patterns = self._infer(values, texts, unique_ratio)
        minimum: float | str | None = None
        maximum: float | str | None = None
        if inferred in {"integer", "decimal"} and parsed:
            minimum, maximum = float(min(parsed)), float(max(parsed))
            if inferred == "integer":
                minimum, maximum = int(minimum), int(maximum)
        elif inferred in {"date", "datetime"} and parsed:
            minimum, maximum = min(parsed).isoformat(), max(parsed).isoformat()
        distinct = tuple(sorted(unique)) if inferred in {"boolean", "enum"} and len(unique) <= 20 else ()
        return ColumnProfile(
            column=column,
            inferred_type=inferred,
            row_count=row_count,
            non_null_count=non_null_count,
            missing_rate=(row_count - non_null_count) / row_count if row_count else 0,
            unique_count=len(unique),
            unique_ratio=unique_ratio,
            minimum=minimum,
            maximum=maximum,
            distinct_values=distinct,
            patterns=patterns,
        )

    @staticmethod
    def _infer(values: list[Any], texts: list[str], unique_ratio: float) -> tuple[str, list[Any], tuple[str, ...]]:
        if not values:
            return "unknown", [], ()
        lowered = {value.lower() for value in texts}
        if lowered <= BOOLEAN_VALUES:
            return "boolean", texts, ("boolean-token",)
        for name, parser, pattern in (
            ("integer", _as_integer, "integer"),
            ("decimal", _as_decimal, "decimal"),
            ("datetime", _as_datetime, "iso-datetime"),
            ("date", _as_date, "iso-date"),
        ):
            ok, parsed = _all_parse(values, parser)
            if ok:
                return name, parsed, (pattern,)
        if unique_ratio >= 0.9 and all(IDENTIFIER.fullmatch(value) for value in texts):
            return "identifier", texts, ("identifier-like",)
        if len(set(texts)) <= 20 and unique_ratio <= 0.5:
            return "enum", texts, ("low-cardinality",)
        return "string", texts, ()

    def _relationships(self, files: tuple[TabularFile, ...]) -> tuple[RelationshipProfile, ...]:
        candidates: list[RelationshipProfile] = []
        inspected = 0
        for left_index, left in enumerate(files):
            for right in files[left_index + 1:]:
                for left_column in left.columns:
                    left_values = [_text(row.get(left_column)) for row in left.rows if _present(row.get(left_column))]
                    left_set = set(left_values)
                    if not left_set:
                        continue
                    for right_column in right.columns:
                        if inspected >= self._max_relationship_columns:
                            return tuple(candidates)
                        inspected += 1
                        right_values = [_text(row.get(right_column)) for row in right.rows if _present(row.get(right_column))]
                        right_set = set(right_values)
                        if not right_set:
                            continue
                        overlap = len(left_set & right_set) / min(len(left_set), len(right_set))
                        if overlap < 0.8:
                            continue
                        left_unique = len(left_values) == len(left_set)
                        right_unique = len(right_values) == len(right_set)
                        cardinality = (
                            "one_to_one" if left_unique and right_unique
                            else "one_to_many" if left_unique
                            else "many_to_one" if right_unique
                            else "many_to_many"
                        )
                        candidates.append(
                            RelationshipProfile(
                                left_file=left.filename,
                                left_column=left_column,
                                right_file=right.filename,
                                right_column=right_column,
                                overlap_ratio=round(overlap, 4),
                                cardinality=cardinality,
                            )
                        )
        return tuple(candidates)

