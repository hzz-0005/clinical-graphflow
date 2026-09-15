from __future__ import annotations

import csv
import io
import json
from typing import Annotated, Callable

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError

from app.clinical.ingestion import CdiscFieldMapping, STANDARD_FIELDS, domain_row_counts
from app.clinical.mapping import ClinicalMappingSuggester, build_mapping_refiner
from app.clinical.tabular import TabularFileParser
from app.enterprise.api import principal_from_header
from app.enterprise.models import DataScope, Role
from app.clinical.operations import OPERATIONS
from app.settings import get_settings

MAX_TOTAL_BYTES = 25_000_000


class CommitImportBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class WithdrawImportBody(BaseModel):
    reason: str = Field(min_length=5, max_length=500)


def _smart_bundle_payloads(parsed, mapping_payload: dict) -> tuple[bytes, bytes, bytes]:
    """Normalize heterogeneous rows into the existing governed CDISC coordinator."""
    by_name = {item.filename: item for item in parsed}
    grouped: dict[str, list[dict]] = {domain: [] for domain in STANDARD_FIELDS}
    for file_item in mapping_payload.get("files", []):
        filename = file_item.get("filename")
        domain = file_item.get("domain")
        source = by_name.get(filename)
        if source is None or domain not in STANDARD_FIELDS:
            raise ValueError("mapping references an unknown file or domain")
        raw_fields = file_item.get("fields", {})
        if isinstance(raw_fields, list):
            fields = {field.get("target_field"): field.get("source_field") for field in raw_fields}
        else:
            fields = raw_fields
        if not isinstance(fields, dict):
            raise ValueError("mapping fields must be an object or suggestion list")
        missing = [target for target in STANDARD_FIELDS[domain] if not fields.get(target)]
        if missing:
            raise ValueError(f"{domain.upper()} mapping is missing: {', '.join(missing)}")
        if any(fields[target] not in source.columns for target in STANDARD_FIELDS[domain]):
            raise ValueError(f"{domain.upper()} mapping contains a column that is not in the uploaded file")
        for row in source.rows:
            grouped[domain].append({target: str(row.get(fields[target]) or "").strip() for target in STANDARD_FIELDS[domain]})
    if any(not rows for rows in grouped.values()):
        raise ValueError("all three clinical domains (DM, ADSL, ADEFF) are required")
    payloads = []
    for domain in ("dm", "adsl", "adeff"):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=STANDARD_FIELDS[domain], lineterminator="\n")
        writer.writeheader()
        writer.writerows(grouped[domain])
        payloads.append(stream.getvalue().encode("utf-8"))
    return tuple(payloads)


def create_smart_import_router(get_runtime: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v7/clinical/imports")

    @router.get("/formats")
    def list_formats(x_insightflow_user: str | None = Header(default=None)):
        """Expose parser capability without exposing provider secrets or uploaded data."""
        principal_from_header(x_insightflow_user)
        parser = TabularFileParser(max_file_bytes=MAX_TOTAL_BYTES)
        optional = {"xlsx", "parquet", "xpt"}
        return {
            "supported": list(parser.supported_formats),
            "available": list(parser.available_formats),
            "optional": sorted(optional),
        }

    @router.post("/inspect")
    async def inspect_files(
        files: Annotated[list[UploadFile], File()],
        provider: Annotated[str, Form()] = "rules",
        x_insightflow_user: str | None = Header(default=None),
    ):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is Role.VIEWER:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        if not 1 <= len(files) <= 20:
            raise HTTPException(status_code=422, detail={"code": "file_count_invalid"})
        payloads = [(item, await item.read()) for item in files]
        if sum(len(payload) for _, payload in payloads) > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail={"code": "upload_too_large"})
        parser = TabularFileParser(max_file_bytes=MAX_TOTAL_BYTES)
        try:
            parsed = tuple(parser.parse(item.filename or "unnamed", item.content_type or "application/octet-stream", payload) for item, payload in payloads)
            suggestion = ClinicalMappingSuggester().suggest(parsed)
            resolved_provider = get_settings().insightflow_ingestion_provider if provider == "auto" else provider
            if resolved_provider not in {"", "rules", "offline_rules"}:
                try:
                    suggestion = build_mapping_refiner(get_settings(), resolved_provider).refine(parsed, suggestion)
                    suggestion = suggestion.model_copy(update={"mapping_provider": resolved_provider})
                except Exception as exc:
                    if provider != "auto":
                        raise HTTPException(status_code=502, detail={"code": "mapping_llm_unavailable", "message": "智能映射模型暂时不可用，请改用规则映射或稍后重试"}) from exc
                    suggestion = suggestion.model_copy(update={"warnings": (*suggestion.warnings, "AI 映射不可用，已安全回退到规则建议")})
            return suggestion.model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "smart_import_invalid", "message": str(exc)}) from exc

    @router.post("/preview")
    async def preview_smart_files(
        files: Annotated[list[UploadFile], File()],
        mapping_json: Annotated[str, Form()],
        x_insightflow_user: str | None = Header(default=None),
    ):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is Role.VIEWER:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        if not 1 <= len(files) <= 20:
            raise HTTPException(status_code=422, detail={"code": "file_count_invalid"})
        payloads = [(item, await item.read()) for item in files]
        if sum(len(payload) for _, payload in payloads) > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail={"code": "upload_too_large"})
        try:
            parsed = tuple(TabularFileParser(max_file_bytes=MAX_TOTAL_BYTES).parse(item.filename or "unnamed", item.content_type or "application/octet-stream", payload) for item, payload in payloads)
            mapping_payload = json.loads(mapping_json)
            if not isinstance(mapping_payload, dict):
                raise ValueError("mapping must be a JSON object")
            bundle = _smart_bundle_payloads(parsed, mapping_payload)
            result = get_runtime().cdisc_imports.preview(*bundle, CdiscFieldMapping(), actor.user_id)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail={"code": "smart_import_mapping_invalid", "message": str(exc)}) from exc
        if not DataScope.from_principal(actor).global_access:
            disallowed = set(result.quality.trial_ids) - set(actor.allowed_trial_ids)
            if disallowed:
                raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        return result.model_dump(mode="json")

    return router


def create_clinical_import_router(get_runtime: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v5/clinical/imports")

    @router.post("/preview")
    async def preview(
        dm: Annotated[UploadFile, File()],
        adsl: Annotated[UploadFile, File()],
        adeff: Annotated[UploadFile, File()],
        mapping_json: Annotated[str, Form()] = "{}",
        x_insightflow_user: str | None = Header(default=None),
    ):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is Role.VIEWER:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        uploads = (dm, adsl, adeff)
        if any(item.content_type not in {"text/csv", "application/csv", "application/vnd.ms-excel"} or not item.filename.lower().endswith(".csv") for item in uploads):
            raise HTTPException(status_code=415, detail={"code": "csv_required"})
        payloads = [await item.read() for item in uploads]
        if sum(map(len, payloads)) > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail={"code": "upload_too_large"})
        try:
            mapping = CdiscFieldMapping.model_validate_json(mapping_json)
            result = get_runtime().cdisc_imports.preview(*payloads, mapping, actor.user_id)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail={"code": "cdisc_validation_failed", "message": str(exc)}) from exc
        if not DataScope.from_principal(actor).global_access:
            disallowed = set(result.quality.trial_ids) - set(actor.allowed_trial_ids)
            if disallowed:
                raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        return result.model_dump(mode="json")

    @router.post("/commit")
    def commit(body: CommitImportBody, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        try:
            return get_runtime().cdisc_imports.commit(body.token, actor.user_id).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "preview_invalid", "message": str(exc)}) from exc

    @router.get("")
    def list_batches(x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        return [item.model_dump(mode="json") for item in get_runtime().cdisc_imports.list_batches()]

    @router.get("/published-batches")
    def list_published_batches(x_insightflow_user: str | None = Header(default=None)):
        """Return only published CDISC versions visible in the caller's trial scope."""
        actor = principal_from_header(x_insightflow_user)
        scope = DataScope.from_principal(actor)
        visible = []
        for item in get_runtime().cdisc_imports.list_batches():
            if item.status != "published":
                continue
            if not scope.global_access and not set(item.trial_ids) & set(scope.trial_ids):
                continue
            payload = item.model_dump(mode="json")
            counts = domain_row_counts(item.quality.rows)
            payload["domain_coverage"] = {
                domain: counts[domain] > 0 for domain in ("dm", "adsl", "adeff")
            }
            payload["arm_counts"] = dict(item.quality.arm_counts)
            visible.append(payload)
        return visible

    @router.get("/compare")
    def compare_batches(left_batch_id: str, right_batch_id: str, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        try:
            result = get_runtime().cdisc_publications.compare(left_batch_id, right_batch_id)
            OPERATIONS.record_event("data_version", "compared")
            return result.model_dump(mode="json")
        except (KeyError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc

    @router.get("/{batch_id}/publish-assessment")
    def publish_assessment(batch_id: str, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        try:
            return get_runtime().cdisc_publications.assess(batch_id).model_dump(mode="json")
        except (KeyError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc

    @router.post("/{batch_id}/publish")
    def publish(batch_id: str, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        try:
            return get_runtime().cdisc_publications.publish(batch_id, actor.user_id).model_dump(mode="json")
        except (KeyError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "publish_gate_failed", "message": str(exc)}) from exc

    @router.post("/{batch_id}/withdraw")
    def withdraw(batch_id: str, body: WithdrawImportBody, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        try:
            result = get_runtime().cdisc_publications.withdraw(batch_id, actor.user_id, body.reason)
            OPERATIONS.record_event("data_version", "withdrawn")
            return result.model_dump(mode="json")
        except (KeyError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "invalid_lifecycle_transition", "message": str(exc)}) from exc

    return router

