from __future__ import annotations

from app.clinical.domain_registry import ColumnSignal, DomainRegistry
from app.clinical.quarantine import QuarantineRepository


class DataUnderstandingTools:
    ALLOWED = frozenset({"inspect_schema", "inspect_profile", "inspect_relationships", "search_domain_registry", "inspect_domain"})

    def __init__(self, repository: QuarantineRepository, registry: DomainRegistry) -> None:
        self.repository, self.registry = repository, registry

    def invoke(self, name: str, arguments: dict, actor: str) -> dict | list:
        if name not in self.ALLOWED:
            raise ValueError(f"unknown understanding tool: {name}")
        batch_id = str(arguments.get("batch_id", ""))
        batch = self.repository.get_batch(batch_id, actor)
        files = self.repository.files_for(batch_id, actor)
        if name == "inspect_schema":
            return {"batch_id": batch_id, "visibility_level": batch.visibility_level, "files": [{"file_id": f.file_id, "filename": f.filename, "format": f.format, "row_count": f.row_count, "columns": list(f.columns)} for f in files]}
        profile = self.repository.profile_for(batch_id, actor)
        if name == "inspect_profile":
            return profile
        if name == "inspect_relationships":
            return {"relationships": profile.get("relationships", []), "candidate_keys": profile.get("candidate_keys", [])}
        if name == "inspect_domain":
            domain = self.registry.get(str(arguments["domain"]), arguments.get("version"))
            return domain.model_dump(mode="json")
        file_id = str(arguments.get("file_id", ""))
        selected = next((f for f in files if f.file_id == file_id), None)
        if selected is None:
            raise ValueError("unknown file_id")
        fp = next((p for p in profile["files"] if p["filename"] == selected.filename), {"columns": []})
        signals = {p["column"]: ColumnSignal(inferred_type=p["inferred_type"], distinct_values=tuple(p.get("distinct_values", ()))) for p in fp["columns"]}
        return [m.model_dump(mode="json") for m in self.registry.search(str(arguments.get("query", "")), selected.columns, signals)]

