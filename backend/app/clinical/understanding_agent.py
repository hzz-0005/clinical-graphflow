from __future__ import annotations

from typing import Protocol

from app.clinical.domain_registry import ColumnSignal, DomainRegistry
from app.clinical.quarantine import QuarantineRepository
from app.clinical.understanding_models import MappingContract
from app.clinical.understanding_tools import DataUnderstandingTools
import re


class MappingProvider(Protocol):
    provider: str
    model: str
    def propose(self, context: dict) -> dict: ...

class RulesMappingProvider:
    provider="rules"; model="domain-registry-v1"
    def __init__(self,registry:DomainRegistry): self.registry=registry
    @staticmethod
    def _normal(value:str)->str: return re.sub(r"[^a-z0-9\u4e00-\u9fff]","",value.lower())
    @staticmethod
    def _transform_for(definition)->str:
        if definition.controlled_values == ("Y", "N"):
            return "uppercase"
        if definition.controlled_values and all(value == value.lower() for value in definition.controlled_values):
            return "lowercase"
        if definition.data_type in {"integer", "decimal", "date", "datetime", "boolean"}:
            return definition.data_type
        return "identity"
    def propose(self,context:dict)->dict:
        outputs=[]
        profiles={f["filename"]:f for f in context["profile"]["files"]}
        for file in context["schema"]["files"]:
            fp=profiles.get(file["filename"],{"columns":[]}); signals={x["column"]:ColumnSignal(inferred_type=x["inferred_type"],distinct_values=tuple(x.get("distinct_values",[]))) for x in fp["columns"]}
            matches=self.registry.search(file["filename"],tuple(file["columns"]),signals)
            best=matches[0]
            custom = best.domain == "custom_candidate"
            if not custom:
                candidate = self.registry.get(best.domain, best.version)
                required = {
                    name for name, definition in candidate.fields.items() if definition.required
                }
                filename = self._normal(file["filename"].rsplit(".", 1)[0])
                domain_names = {
                    self._normal(candidate.name),
                    *(self._normal(alias) for alias in candidate.aliases),
                }
                filename_affinity = filename in domain_names
                required_covered = required.issubset(set(best.matched_fields))
                # Shared identifiers such as Id/PATIENT are not enough to call a claims table an
                # encounter. A known filename may use a smaller signature; arbitrary filenames need
                # stronger column evidence. Uncertain files remain quarantined as custom candidates.
                custom = (
                    not required_covered
                    or best.score < 10
                    or (not filename_affinity and best.score < 18)
                )
            fields=[]
            if not custom:
                domain=self.registry.get(best.domain,best.version)
                for source in file["columns"]:
                    ns=self._normal(source)
                    for target,definition in domain.fields.items():
                        choices={self._normal(target),*(self._normal(a) for a in definition.aliases)}
                        # Only audited normalized aliases are accepted. Suffix matching used to map
                        # CITY to ETHNICITY and is unsafe for clinical schemas.
                        if ns in choices:
                            transform=self._transform_for(definition)
                            fields.append({"source":source,"target":target,"transform":transform}); break
            outputs.append({"file_id":file["file_id"],"source_filename":file["filename"],"target_domain":"custom_candidate" if custom else best.domain,"target_version":None if custom else best.version,"status":"custom_candidate" if custom else "standard_candidate","confidence":min(.99,best.score/20) if not custom else .25,"rationale":best.reason,"fields":fields})
        return {"files":outputs}


class DataUnderstandingAgent:
    def __init__(self, tools: DataUnderstandingTools, repository: QuarantineRepository, registry: DomainRegistry) -> None:
        self.tools, self.repository, self.registry = tools, repository, registry

    def understand(self, batch_id: str, actor: str, provider: MappingProvider) -> MappingContract:
        batch = self.repository.get_batch(batch_id, actor)
        if batch.status != "profiled":
            raise ValueError("batch must be profiled before understanding")
        schema = self.tools.invoke("inspect_schema", {"batch_id": batch_id}, actor)
        profile = self.tools.invoke("inspect_profile", {"batch_id": batch_id}, actor)
        relationships = self.tools.invoke("inspect_relationships", {"batch_id": batch_id}, actor)
        proposal = provider.propose({"schema": schema, "profile": profile, "relationships": relationships})
        contract = MappingContract(batch_id=batch_id, provider=provider.provider, model=provider.model, visibility_level=batch.visibility_level, **proposal)
        self.validate_contract(contract, actor)
        self.repository.save_contract(batch_id, actor, batch.version, contract.model_dump(mode="json"))
        return contract

    def validate_contract(self, contract: MappingContract, actor: str) -> None:
        batch_id = contract.batch_id
        known_files = {f.file_id: f for f in self.repository.files_for(batch_id, actor)}
        if {f.file_id for f in contract.files} != set(known_files):
            raise ValueError("contract must map every batch file exactly once")
        for mapping in contract.files:
            source = known_files[mapping.file_id]
            if mapping.source_filename != source.filename or any(field.source not in source.columns for field in mapping.fields):
                raise ValueError("mapping references an unknown file or column")
            if mapping.target_domain == "custom_candidate":
                if mapping.status != "custom_candidate": raise ValueError("unknown domain must remain custom_candidate")
                continue
            domain = self.registry.get(mapping.target_domain, mapping.target_version)
            if mapping.status != "standard_candidate" or any(field.target not in domain.fields for field in mapping.fields):
                raise ValueError("mapping violates registered domain contract")

