from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.clinical.domain_registry import DomainRegistry
from app.clinical.tabular import TabularFile
from app.clinical.understanding_models import MappingContract


class ValidationError(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    file_id: str
    row_reference: str | None = None
    field: str | None = None
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    valid: bool
    accepted_rows: int
    rejected_rows: int
    errors: tuple[ValidationError, ...]


def _transform(value: Any, name: str) -> Any:
    if value in (None, ""): return None
    text = str(value).strip()
    if name in {"identity", "trim"}: return text
    if name == "uppercase": return text.upper()
    if name == "lowercase": return text.lower()
    if name == "integer": return int(text)
    if name == "decimal": return float(text)
    if name == "date": return date.fromisoformat(text).isoformat()
    if name == "datetime": return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    if name == "boolean":
        normalized=text.lower()
        if normalized not in {"y","n","yes","no","true","false"}: raise ValueError
        return normalized in {"y","yes","true"}
    raise ValueError("unsupported transform")


class TransformationValidator:
    def __init__(self, registry: DomainRegistry) -> None:
        self.registry=registry

    def validate(self, files: dict[str, TabularFile], contract: MappingContract) -> ValidationReport:
        errors=[]; accepted=0; rejected=0
        for mapping in contract.files:
            source=files.get(mapping.file_id)
            if source is None:
                errors.append(ValidationError(code="unknown_file",file_id=mapping.file_id,message="映射引用了不存在的文件")); continue
            if mapping.target_domain == "custom_candidate":
                errors.append(ValidationError(code="custom_domain_requires_approval",file_id=mapping.file_id,message="自定义数据域需管理员审批后才能发布")); rejected += source.row_count; continue
            domain=self.registry.get(mapping.target_domain,mapping.target_version)
            targets={f.target for f in mapping.fields}
            for required in (name for name,definition in domain.fields.items() if definition.required):
                if required not in targets:
                    errors.append(ValidationError(code="missing_required_mapping",file_id=mapping.file_id,field=required,message=f"缺少必填字段映射 {required}"))
            for index,row in enumerate(source.rows,1):
                row_errors=[]
                reference=hashlib.sha256(f"{mapping.file_id}:{index}".encode()).hexdigest()[:12]
                for field in mapping.fields:
                    definition=domain.fields[field.target]
                    try: value=_transform(row.get(field.source),field.transform)
                    except (ValueError,TypeError,OverflowError):
                        row_errors.append(ValidationError(code="cast_failed",file_id=mapping.file_id,row_reference=reference,field=field.target,message=f"第 {index} 行无法执行 {field.transform} 转换")); continue
                    if definition.required and value in (None,""):
                        row_errors.append(ValidationError(code="required_value_missing",file_id=mapping.file_id,row_reference=reference,field=field.target,message="必填值缺失"))
                    if definition.controlled_values and value is not None and str(value) not in definition.controlled_values:
                        row_errors.append(ValidationError(code="invalid_controlled_value",file_id=mapping.file_id,row_reference=reference,field=field.target,message="值不在受控词表中"))
                if row_errors: rejected += 1; errors.extend(row_errors)
                else: accepted += 1
        return ValidationReport(valid=not errors,rejected_rows=rejected,accepted_rows=accepted,errors=tuple(errors))

