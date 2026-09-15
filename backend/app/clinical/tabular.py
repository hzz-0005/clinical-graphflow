from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Callable, Mapping
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.clinical.tabular_plugins import TabularAdapter, optional_adapters


class TabularFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    filename: str
    format: str
    columns: tuple[str, ...]
    row_count: int
    rows: tuple[dict[str, Any], ...]
    content_hash: str


class _FunctionAdapter:
    available = True
    package_hint = "内置"

    def __init__(self, display_name: str, parser: Callable[[str, str, bytes], tuple[tuple[str, ...], tuple[Mapping[str, Any], ...]]]) -> None:
        self.display_name = display_name
        self._parser = parser

    def parse(self, filename: str, content_type: str, payload: bytes):
        return self._parser(filename, content_type, payload)


def normalize_cell(value: Any) -> Any:
    """Convert optional-library scalars into JSON-safe values before governance checks."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return normalize_cell(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _parse_delimited(filename: str, content_type: str, payload: bytes):
    suffix = Path(filename).suffix.lower().lstrip(".")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{filename} must use UTF-8 encoding") from exc
    # Regulatory labels and narrative safety reports can contain a single
    # long text field.  The default csv module limit (128 KiB) rejects those
    # files even when the overall upload/file-size policy allows them.
    if csv.field_size_limit() < 10_000_000:
        csv.field_size_limit(10_000_000)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t" if suffix == "tsv" else ",")
    return tuple(reader.fieldnames or ()), tuple(dict(row) for row in reader)


def _parse_json(filename: str, content_type: str, payload: bytes):
    suffix = Path(filename).suffix.lower().lstrip(".")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{filename} must use UTF-8 encoding") from exc
    try:
        raw = [json.loads(line) for line in text.splitlines() if line.strip()] if suffix == "jsonl" else json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{filename} contains invalid JSON") from exc
    if isinstance(raw, dict):
        raw = raw.get("records", [raw])
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise ValueError(f"{filename} must contain an object array")
    rows = tuple(dict(row) for row in raw)
    columns = tuple(dict.fromkeys(key for row in rows for key in row))
    return columns, rows


class TabularFileParser:
    """Parse transport bytes through a registry while preserving one safe contract."""

    _core_adapters: dict[str, TabularAdapter] = {
        "csv": _FunctionAdapter("CSV", _parse_delimited),
        "tsv": _FunctionAdapter("TSV", _parse_delimited),
        "json": _FunctionAdapter("JSON", _parse_json),
        "jsonl": _FunctionAdapter("JSONL", _parse_json),
    }

    def __init__(self, max_file_bytes: int = 10_000_000, extra_adapters: Mapping[str, TabularAdapter] | None = None) -> None:
        self._max_file_bytes = max_file_bytes
        self._adapters = dict(self._core_adapters)
        self._adapters.update(optional_adapters())
        for format_name, adapter in (extra_adapters or {}).items():
            self.register_adapter(format_name, adapter)

    @property
    def supported_formats(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    @property
    def available_formats(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, adapter in self._adapters.items() if bool(getattr(adapter, "available", True))))

    def register_adapter(self, format_name: str, adapter: TabularAdapter) -> None:
        normalized = format_name.lower().lstrip(".")
        if not normalized or "/" in normalized or "\\" in normalized:
            raise ValueError("tabular adapter format must be a simple file suffix")
        self._adapters[normalized] = adapter

    def parse(self, filename: str, content_type: str, payload: bytes) -> TabularFile:
        if len(payload) > self._max_file_bytes:
            raise ValueError(f"file exceeds size limit: {filename}")
        suffix = Path(filename).suffix.lower().lstrip(".")
        adapter = self._adapters.get(suffix)
        if adapter is None:
            supported = ", ".join(self.supported_formats)
            raise ValueError(f"unsupported clinical file format: {suffix or 'unknown'}（可用格式：{supported}）")
        if not bool(getattr(adapter, "available", True)):
            display_name = str(getattr(adapter, "display_name", suffix.upper()))
            package_hint = str(getattr(adapter, "package_hint", "insightflow-backend[tabular]"))
            raise ValueError(f"{display_name} 适配器未安装，请安装可选依赖 {package_hint} 后重试")
        columns, raw_rows = adapter.parse(filename, content_type, payload)
        normalized_columns = tuple(str(column).strip() for column in columns)
        if not normalized_columns or any(not column for column in normalized_columns):
            raise ValueError(f"{filename} 必须包含非空表头")
        if len(set(normalized_columns)) != len(normalized_columns):
            raise ValueError(f"{filename} 包含重复列名，无法安全映射")
        rows = tuple({column: normalize_cell(row.get(column)) for column in normalized_columns} for row in raw_rows)
        return TabularFile(
            filename=filename,
            format=suffix,
            columns=normalized_columns,
            row_count=len(rows),
            rows=rows,
            content_hash=hashlib.sha256(payload).hexdigest(),
        )

