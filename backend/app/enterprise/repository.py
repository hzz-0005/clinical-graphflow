from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Protocol

import psycopg

from app.agent.models import InvestigationState
from app.clinical.v17_contracts import InvestigationGraphState
from app.clinical.operations import OPERATIONS
from app.enterprise.models import Alert,ApprovalRequest,AuditEvent,DataScope,EnterpriseInvestigation,InvestigationSchedule,Principal,Role


class VersionConflict(ValueError):
    """An optimistic-lock compare-and-swap failed."""

    code = "version_conflict"


class IdempotencyConflict(ValueError):
    """A request id was already used for a different non-replayable operation."""

    code = "idempotency_conflict"


class EnterpriseRepository(Protocol):
    def save_investigation(self,state:InvestigationState,actor:Principal,scope:DataScope,publication_status:str,request_id:str,expected_version:int|None=None)->EnterpriseInvestigation: ...
    def save_investigation_and_approval(self,state:InvestigationState,actor:Principal,scope:DataScope,publication_status:str,request_id:str,approval:ApprovalRequest)->tuple[EnterpriseInvestigation,ApprovalRequest]: ...
    def get_investigation(self,investigation_id:str,actor:Principal)->EnterpriseInvestigation: ...
    def save_graph_investigation(self,state:InvestigationGraphState,actor:Principal,scope:DataScope,publication_status:str,request_id:str,expected_version:int|None=None)->EnterpriseInvestigation: ...
    def save_graph_investigation_and_approval(self,state:InvestigationGraphState,actor:Principal,scope:DataScope,publication_status:str,request_id:str,approval:ApprovalRequest,expected_version:int|None=None)->tuple[EnterpriseInvestigation,ApprovalRequest]: ...
    def get_graph_investigation(self,investigation_id:str,actor:Principal)->InvestigationGraphState: ...


def _visible(item:EnterpriseInvestigation,actor:Principal)->bool:
    if actor.role is Role.ADMIN: return True
    if actor.user_id==item.owner_user_id: return True
    actor_scope=DataScope.from_principal(actor)
    if item.scope.trial_ids:
        if not set(item.scope.trial_ids) & set(actor_scope.trial_ids): return False
        if item.scope.regions and not set(item.scope.regions) & set(actor_scope.regions): return False
        if item.scope.site_ids and actor_scope.site_ids and not set(item.scope.site_ids) & set(actor_scope.site_ids): return False
        return True
    countries=set(item.scope.countries)
    managers=set(item.scope.account_manager_ids)
    return bool(countries & set(actor_scope.countries)) and bool(managers & set(actor_scope.account_manager_ids))


class InMemoryEnterpriseRepository:
    def __init__(self):
        self.investigations:dict[str,EnterpriseInvestigation]={}; self.audit_events:list[AuditEvent]=[]; self.approvals:dict[str,ApprovalRequest]={}; self.schedules:dict[str,InvestigationSchedule]={}; self.alerts:dict[str,Alert]={}
        self._request_index:dict[str,str]={}
        self._approval_request_index:dict[str,str]={}
        self._lock=threading.RLock()

    @staticmethod
    def _versioned_state(state: InvestigationState, version: int) -> InvestigationState:
        return state.model_copy(update={"state_version": version}, deep=True)

    @staticmethod
    def _versioned_graph_state(state: InvestigationGraphState, version: int) -> InvestigationGraphState:
        """Bind a graph snapshot and its compatibility projection to one row version."""

        return state.with_enterprise_version(version)

    def _audit(self, actor: Principal, action: str, resource_type: str, resource_id: str, request_id: str, metadata: dict[str, Any] | None = None) -> None:
        if any(
            event.action == action
            and event.resource_type == resource_type
            and event.resource_id == resource_id
            and event.request_id == request_id
            for event in self.audit_events
        ):
            return
        self.audit_events.append(AuditEvent(actor_user_id=actor.user_id,action=action,resource_type=resource_type,resource_id=resource_id,request_id=request_id,metadata=metadata or {}))

    def _save_investigation_locked(self,state,actor,scope,publication_status,request_id,expected_version=None):
        if request_id and request_id in self._request_index:
            item=self.investigations[self._request_index[request_id]]
            if not _visible(item,actor): raise KeyError(f"Unknown investigation: {item.investigation_id}")
            return item
        existing=self.investigations.get(state.investigation_id)
        if existing is None:
            if expected_version not in (None, 0, 1):
                raise VersionConflict(f"version_conflict: expected version {expected_version} for new investigation")
            version=1
            item=EnterpriseInvestigation(investigation_id=state.investigation_id,owner_user_id=actor.user_id,scope=scope,publication_status=publication_status,state=self._versioned_state(state,version),version=version,request_id=request_id or None)
            self.investigations[item.investigation_id]=item
            if request_id:self._request_index[request_id]=item.investigation_id
            self._audit(actor,"investigation.created","investigation",item.investigation_id,request_id, {"publication_status":publication_status,**state.audit_metadata})
            return item
        if not _visible(existing,actor): raise KeyError(f"Unknown investigation: {existing.investigation_id}")
        if isinstance(existing.graph_state, InvestigationGraphState):
            raise VersionConflict("version_conflict: canonical Graph investigation requires a Graph snapshot")
        if expected_version is None or existing.version != expected_version:
            raise VersionConflict(f"version_conflict: current version is {existing.version}")
        version=existing.version+1
        item=existing.model_copy(update={"publication_status":publication_status,"state":self._versioned_state(state,version),"updated_at":datetime.now(timezone.utc),"version":version,"request_id":request_id or None},deep=True)
        self.investigations[item.investigation_id]=item
        if request_id:self._request_index[request_id]=item.investigation_id
        self._audit(actor,"investigation.updated","investigation",item.investigation_id,request_id,{"publication_status":publication_status,"version":version})
        return item

    def save_investigation(self,state,actor,scope,publication_status,request_id,expected_version=None):
        with self._lock:
            return self._save_investigation_locked(state,actor,scope,publication_status,request_id,expected_version)

    def _save_graph_investigation_locked(
        self,
        state: InvestigationGraphState,
        actor: Principal,
        scope: DataScope,
        publication_status: str,
        request_id: str,
        expected_version: int | None = None,
    ) -> EnterpriseInvestigation:
        """Persist a typed graph snapshot with its API projection under one CAS boundary."""

        state.assert_internal_version_consistency()
        if request_id and request_id in self._request_index:
            OPERATIONS.record_integrity("duplicate", "request_replay")
            item = self.investigations[self._request_index[request_id]]
            if not _visible(item, actor):
                raise KeyError(f"Unknown investigation: {item.investigation_id}")
            return item.model_copy(deep=True)
        existing = self.investigations.get(state.investigation_id)
        if existing is None:
            if expected_version not in (None, 0, 1):
                raise VersionConflict(f"version_conflict: expected version {expected_version} for new investigation")
            version = 1
            graph = self._versioned_graph_state(
                state.model_copy(update={"publication_status": publication_status}), version
            )
            projection = graph.to_legacy_state()
            item = EnterpriseInvestigation(
                investigation_id=graph.investigation_id,
                owner_user_id=actor.user_id,
                scope=scope,
                publication_status=publication_status,
                state=projection,
                graph_state=graph,
                graph_state_version=version,
                version=version,
                request_id=request_id or None,
            )
            self.investigations[item.investigation_id] = item
            if request_id:
                self._request_index[request_id] = item.investigation_id
            self._audit(
                actor,
                "investigation.created",
                "investigation",
                item.investigation_id,
                request_id,
                {"publication_status": publication_status, "graph_state_version": version},
            )
            return item.model_copy(deep=True)
        if not _visible(existing, actor):
            raise KeyError(f"Unknown investigation: {existing.investigation_id}")
        if expected_version is None or existing.version != expected_version:
            raise VersionConflict(f"version_conflict: current version is {existing.version}")
        version = existing.version + 1
        graph = self._versioned_graph_state(
            state.model_copy(update={"publication_status": publication_status}), version
        )
        projection = graph.to_legacy_state()
        item = existing.model_copy(
            update={
                "publication_status": publication_status,
                "state": projection,
                "graph_state": graph,
                "graph_state_version": version,
                "updated_at": datetime.now(timezone.utc),
                "version": version,
                "request_id": request_id or None,
            },
            deep=True,
        )
        self.investigations[item.investigation_id] = item
        if request_id:
            self._request_index[request_id] = item.investigation_id
        self._audit(
            actor,
            "investigation.updated",
            "investigation",
            item.investigation_id,
            request_id,
            {"publication_status": publication_status, "version": version, "graph_state_version": version},
        )
        return item.model_copy(deep=True)

    def save_graph_investigation(
        self,
        state: InvestigationGraphState,
        actor: Principal,
        scope: DataScope,
        publication_status: str,
        request_id: str,
        expected_version: int | None = None,
    ) -> EnterpriseInvestigation:
        with self._lock:
            try:
                item = self._save_graph_investigation_locked(
                    state, actor, scope, publication_status, request_id, expected_version
                )
            except VersionConflict:
                OPERATIONS.record_integrity("cas", "conflict")
                raise
            else:
                OPERATIONS.record_integrity("cas", "commit")
                return item

    def save_graph_investigation_and_approval(
        self,
        state: InvestigationGraphState,
        actor: Principal,
        scope: DataScope,
        publication_status: str,
        request_id: str,
        approval: ApprovalRequest,
        expected_version: int | None = None,
    ) -> tuple[EnterpriseInvestigation, ApprovalRequest]:
        with self._lock:
            investigations = dict(self.investigations)
            approvals = dict(self.approvals)
            audits = list(self.audit_events)
            request_index = dict(self._request_index)
            approval_request_index = dict(self._approval_request_index)
            try:
                saved = self._save_graph_investigation_locked(
                    state, actor, scope, publication_status, request_id, expected_version
                )
                bound_approval = approval.model_copy(update={"investigation_id": saved.investigation_id})
                created = self._create_approval_locked(bound_approval, actor, request_id)
                OPERATIONS.record_integrity("cas", "commit")
            except Exception as exc:
                if isinstance(exc, VersionConflict):
                    OPERATIONS.record_integrity("cas", "conflict")
                elif isinstance(exc, IdempotencyConflict):
                    OPERATIONS.record_integrity("duplicate", "approval_conflict")
                self.investigations = investigations
                self.approvals = approvals
                self.audit_events = audits
                self._request_index = request_index
                self._approval_request_index = approval_request_index
                raise
            return saved, created

    def get_graph_investigation(self, investigation_id: str, actor: Principal) -> InvestigationGraphState:
        with self._lock:
            item = self.investigations.get(investigation_id)
            if item is None or not _visible(item, actor):
                raise KeyError(f"Unknown investigation: {investigation_id}")
            graph = item.graph_state
            if isinstance(graph, InvestigationGraphState):
                graph.assert_internal_version_consistency()
                if item.graph_state_version != item.version or graph.enterprise_version != item.version:
                    raise VersionConflict("version_conflict: graph snapshot is not bound to enterprise version")
                return graph.model_copy(deep=True)
            # V4-V16 rows have no canonical graph column.  Adapt them only on read; this method
            # never writes the compatibility projection back to the row.
            return InvestigationGraphState.from_legacy_state(item.state, space="clinical_trial")

    def get_investigation(self,investigation_id,actor):
        with self._lock:
            item=self.investigations.get(investigation_id)
            if item is None or not _visible(item,actor): raise KeyError(f"Unknown investigation: {investigation_id}")
            return item.model_copy(deep=True)

    def list_investigations(self,actor):
        with self._lock:
            return [x.model_copy(deep=True) for x in self.investigations.values() if _visible(x,actor)]

    def _create_approval_locked(self,item:ApprovalRequest,actor:Principal,request_id:str):
        if request_id and request_id in self._approval_request_index:
            OPERATIONS.record_integrity("duplicate", "approval_request_replay")
            return self.approvals[self._approval_request_index[request_id]]
        existing=next((approval for approval in self.approvals.values() if approval.investigation_id==item.investigation_id),None)
        if existing is not None:
            OPERATIONS.record_integrity("duplicate", "approval_existing")
            return existing
        if item.approval_id in self.approvals:
            raise IdempotencyConflict("idempotency_conflict: approval id already exists")
        self.approvals[item.approval_id]=item
        if request_id:self._approval_request_index[request_id]=item.approval_id
        self._audit(actor,"approval.created","approval",item.approval_id,request_id,{"investigation_id":item.investigation_id})
        return item

    def create_approval(self,item:ApprovalRequest,actor:Principal,request_id:str):
        with self._lock:
            return self._create_approval_locked(item,actor,request_id)

    def save_investigation_and_approval(self,state,actor,scope,publication_status,request_id,approval):
        with self._lock:
            investigations=dict(self.investigations); approvals=dict(self.approvals); audits=list(self.audit_events)
            request_index=dict(self._request_index); approval_request_index=dict(self._approval_request_index)
            try:
                saved=self._save_investigation_locked(state,actor,scope,publication_status,request_id)
                # A retried HTTP request may have regenerated its state UUID; bind the approval
                # to the durable investigation resolved by the idempotency key.
                bound_approval=approval.model_copy(update={"investigation_id":saved.investigation_id})
                created=self._create_approval_locked(bound_approval,actor,request_id)
            except Exception:
                self.investigations=investigations; self.approvals=approvals; self.audit_events=audits
                self._request_index=request_index; self._approval_request_index=approval_request_index
                raise
            return saved,created

    def list_approvals(self,actor:Principal):
        with self._lock:
            values=list(self.approvals.values()) if actor.role is Role.ADMIN else [x for x in self.approvals.values() if x.requested_by==actor.user_id]
            return [x.model_copy(deep=True) for x in values]

    def get_approval(self,approval_id:str):
        with self._lock:
            try: return self.approvals[approval_id].model_copy(deep=True)
            except KeyError as exc: raise KeyError(f"Unknown approval: {approval_id}") from exc

    def save_approval(self,item:ApprovalRequest,actor:Principal,request_id:str):
        with self._lock:
            current=self.approvals.get(item.approval_id)
            if current is None: raise KeyError(f"Unknown approval: {item.approval_id}")
            if any(event.action==f"approval.{item.status}" and event.resource_id==item.approval_id and event.request_id==request_id for event in self.audit_events):
                OPERATIONS.record_integrity("duplicate", "approval_decision_replay")
                return current
            if current.version != item.version-1:
                raise VersionConflict(f"version_conflict: current approval version is {current.version}")
            self.approvals[item.approval_id]=item
            investigation = self.investigations.get(item.investigation_id)
            if investigation is not None and isinstance(investigation.graph_state, InvestigationGraphState):
                next_version = investigation.version + 1
                graph = self._versioned_graph_state(
                    investigation.graph_state.with_approval(item.status.value, item.version), next_version
                )
                self.investigations[item.investigation_id] = investigation.model_copy(
                    update={
                        "state": graph.to_legacy_state(),
                        "graph_state": graph,
                        "graph_state_version": next_version,
                        "version": next_version,
                        "updated_at": datetime.now(timezone.utc),
                    },
                    deep=True,
                )
            self._audit(actor,f"approval.{item.status}","approval",item.approval_id,request_id)
            OPERATIONS.record_integrity("approval", "decision_committed")
            return item

    def save_approval_and_enqueue(self, item: ApprovalRequest, actor: Principal, request_id: str, event: Any, outbox: Any):
        """Keep the decision and its retry record together for memory-backed tests/dev runs."""

        with self._lock:
            previous = self.approvals.get(item.approval_id)
            previous_investigations = dict(self.investigations)
            audit_length = len(self.audit_events)
            try:
                self.save_approval(item, actor, request_id)
                outbox.enqueue(event)
            except Exception:
                if previous is not None:self.approvals[item.approval_id] = previous
                self.investigations = previous_investigations
                del self.audit_events[audit_length:]
                raise
            return item

    def publish_investigation(self,investigation_id:str):
        with self._lock:
            item=self.investigations[investigation_id]
            if item.publication_status == "published": return item
            version=item.version+1
            update = {
                "publication_status": "published",
                "state": self._versioned_state(item.state, version),
                "version": version,
            }
            if isinstance(item.graph_state, InvestigationGraphState):
                graph = self._versioned_graph_state(
                    item.graph_state.model_copy(update={"publication_status": "published"}), version
                )
                update.update({"state": graph.to_legacy_state(), "graph_state": graph, "graph_state_version": version})
            self.investigations[investigation_id]=item.model_copy(update=update,deep=True)
            return self.investigations[investigation_id]

    def save_schedule(self,item:InvestigationSchedule,actor:Principal,request_id:str):
        self.schedules[item.schedule_id]=item
        self.audit_events.append(AuditEvent(actor_user_id=actor.user_id,action="schedule.created",resource_type="schedule",resource_id=item.schedule_id,request_id=request_id))
        return item

    def get_schedule(self,schedule_id:str):
        try:return self.schedules[schedule_id]
        except KeyError as exc:raise KeyError(f"Unknown schedule: {schedule_id}") from exc

    def list_schedules(self,actor:Principal):
        return list(self.schedules.values()) if actor.role is Role.ADMIN else [x for x in self.schedules.values() if x.owner_user_id==actor.user_id]

    def save_alert(self,item:Alert):self.alerts[item.alert_id]=item;return item
    def list_alerts(self,actor:Principal):return [x for x in self.alerts.values() if actor.role is Role.ADMIN or x.recipient_user_id==actor.user_id]


class PostgresEnterpriseRepository:
    def __init__(self,database_url:str): self.database_url=database_url

    @staticmethod
    def _versioned_state(state: InvestigationState, version: int) -> InvestigationState:
        return state.model_copy(update={"state_version": version}, deep=True)

    @staticmethod
    def _versioned_graph_state(state: InvestigationGraphState, version: int) -> InvestigationGraphState:
        return state.with_enterprise_version(version)

    @staticmethod
    def _item_from_row(investigation_id: str, row: tuple[Any, ...]) -> EnterpriseInvestigation:
        graph_state = None
        graph_state_version = None
        # Keep the eight-column reader compatible with V0-V26 fixtures while accepting the
        # additive canonical columns introduced by the graph-state migration.
        if len(row) >= 10 and row[8] is not None:
            graph_state = InvestigationGraphState.from_canonical_snapshot(row[8]) if isinstance(row[8], (str, bytes, bytearray)) else InvestigationGraphState.model_validate(row[8])
            graph_state_version = int(row[9]) if row[9] is not None else None
            graph_state.assert_internal_version_consistency()
            if graph_state_version != row[6] or graph_state.enterprise_version != row[6]:
                raise VersionConflict("version_conflict: graph snapshot is not bound to enterprise version")
            # The graph is canonical; materialize the API projection from it on read so an old
            # state_json value can never overwrite a newer graph snapshot.
            state = graph_state.to_legacy_state()
        else:
            state = InvestigationState.model_validate(row[2])
            # V0-V25 rows may predate the explicit state_version field.  Normalize those legacy
            # snapshots on read so the public model still reflects the durable row version.
            if state.state_version != row[6]:
                state = state.model_copy(update={"state_version": row[6]})
        return EnterpriseInvestigation(
            investigation_id=investigation_id,
            owner_user_id=row[0],
            publication_status=row[1],
            state=state,
            graph_state=graph_state,
            graph_state_version=graph_state_version,
            scope=DataScope.model_validate(row[3]),
            created_at=row[4],
            updated_at=row[5],
            version=row[6],
            request_id=row[7],
        )

    @staticmethod
    def _insert_evidence(cur: Any, investigation_id: str, state: InvestigationState) -> None:
        # Evidence is a snapshot of the state.  It is written only after the investigation CAS
        # succeeds and in the same transaction, so a retry cannot append a second copy.
        cur.execute("DELETE FROM enterprise.evidence WHERE investigation_id=%s", (investigation_id,))
        for sequence,evidence in enumerate(state.evidence,1):
            cur.execute("""INSERT INTO enterprise.evidence(investigation_id,evidence_id,sequence,claim,source,sql_text,rows_json,metric,metric_version,evidence_type,sample_size,effect_json) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)""",(investigation_id,evidence.evidence_id,sequence,evidence.claim,evidence.source,evidence.sql,json.dumps(evidence.rows,default=str),evidence.metric,evidence.metric_version,evidence.evidence_type,evidence.sample_size,json.dumps(evidence.effect_size.model_dump() if evidence.effect_size else None)))

    def _audit(self,cur,actor,action,kind,resource_id,request_id,metadata=None):
        cur.execute("""INSERT INTO enterprise.audit_events(actor_user_id,action,resource_type,resource_id,outcome,metadata_json,request_id)
            VALUES (%s,%s,%s,%s,'success',%s::jsonb,%s)
            ON CONFLICT (action,resource_type,resource_id,request_id) DO NOTHING""",(actor.user_id,action,kind,resource_id,json.dumps(metadata or {}),request_id))

    def _investigation_row(self, cur: Any, investigation_id: str, *, for_update: bool = False):
        suffix = " FOR UPDATE" if for_update else ""
        cur.execute("SELECT owner_user_id,publication_status,state_json,scope_json,created_at,updated_at,version,request_id,graph_state_json,graph_state_version FROM enterprise.investigations WHERE investigation_id=%s" + suffix, (investigation_id,))
        return cur.fetchone()

    def _save_investigation_cursor(self,state,actor,scope,publication_status,request_id,expected_version,cur):
        # Request ids are the first lookup: this handles HTTP/Activity retries even when the
        # retried caller allocated a different in-memory state UUID.
        if request_id:
            cur.execute("SELECT investigation_id FROM enterprise.investigations WHERE request_id=%s FOR UPDATE", (request_id,))
            request_row=cur.fetchone()
            if request_row:
                row=self._investigation_row(cur,str(request_row[0]))
                item=self._item_from_row(str(request_row[0]),row)
                if not _visible(item,actor): raise KeyError(f"Unknown investigation: {item.investigation_id}")
                return item

        inserted_state=self._versioned_state(state,1)
        cur.execute("""INSERT INTO enterprise.investigations
            (investigation_id,owner_user_id,question,metric,status,publication_status,agent_mode,provider,model,confidence,answer,state_json,scope_json,request_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            ON CONFLICT DO NOTHING""",(state.investigation_id,actor.user_id,state.question,state.metric,state.status,publication_status,state.agent_mode,state.provider,state.model,state.confidence,state.answer,inserted_state.model_dump_json(),scope.model_dump_json(),request_id or None))
        if cur.rowcount == 1:
            self._insert_evidence(cur,state.investigation_id,inserted_state)
            self._audit(cur,actor,"investigation.created","investigation",state.investigation_id,request_id,{"publication_status":publication_status,**state.audit_metadata})
            return self._item_from_row(state.investigation_id,self._investigation_row(cur,state.investigation_id))

        row=self._investigation_row(cur,state.investigation_id,for_update=True)
        if row is None:
            # The only remaining conflict is a request id committed by a concurrent transaction;
            # fetch that durable record and return it as the idempotent result.
            if request_id:
                cur.execute("SELECT investigation_id FROM enterprise.investigations WHERE request_id=%s FOR UPDATE", (request_id,))
                request_row=cur.fetchone()
                if request_row:
                    investigation_id=str(request_row[0])
                    item=self._item_from_row(investigation_id,self._investigation_row(cur,investigation_id))
                    if not _visible(item,actor): raise KeyError(f"Unknown investigation: {item.investigation_id}")
                    return item
            raise VersionConflict("version_conflict: investigation insert raced with another update")
        current=self._item_from_row(state.investigation_id,row)
        if not _visible(current,actor): raise KeyError(f"Unknown investigation: {current.investigation_id}")
        if isinstance(current.graph_state, InvestigationGraphState):
            raise VersionConflict("version_conflict: canonical Graph investigation requires a Graph snapshot")
        if request_id and current.request_id == request_id:
            return current
        if expected_version is None or current.version != expected_version:
            raise VersionConflict(f"version_conflict: current version is {current.version}")
        next_state=self._versioned_state(state,current.version+1)
        cur.execute("""UPDATE enterprise.investigations SET question=%s,metric=%s,status=%s,publication_status=%s,agent_mode=%s,provider=%s,model=%s,confidence=%s,answer=%s,state_json=%s::jsonb,scope_json=%s::jsonb,request_id=%s,version=%s,updated_at=now()
            WHERE investigation_id=%s AND version=%s""",(next_state.question,next_state.metric,next_state.status,publication_status,next_state.agent_mode,next_state.provider,next_state.model,next_state.confidence,next_state.answer,next_state.model_dump_json(),scope.model_dump_json(),request_id or None,current.version+1,state.investigation_id,current.version))
        if cur.rowcount != 1: raise VersionConflict("version_conflict: investigation was updated concurrently")
        self._insert_evidence(cur,state.investigation_id,next_state)
        self._audit(cur,actor,"investigation.updated","investigation",state.investigation_id,request_id,{"publication_status":publication_status,"version":current.version+1})
        return self._item_from_row(state.investigation_id,self._investigation_row(cur,state.investigation_id))

    def save_investigation(self,state,actor,scope,publication_status,request_id,expected_version=None):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                return self._save_investigation_cursor(state,actor,scope,publication_status,request_id,expected_version,cur)

    def _save_graph_investigation_cursor(
        self,
        state: InvestigationGraphState,
        actor: Principal,
        scope: DataScope,
        publication_status: str,
        request_id: str,
        expected_version: int | None,
        cur: Any,
    ) -> EnterpriseInvestigation:
        """CAS-write one canonical graph snapshot and its compatibility projection.

        The graph JSON and ``state_json`` are written by one SQL statement; evidence and audit
        rows are then written on the same transaction cursor.  A caller never needs to perform a
        second legacy write after this method succeeds.
        """

        state.assert_internal_version_consistency()
        if request_id:
            cur.execute("SELECT investigation_id FROM enterprise.investigations WHERE request_id=%s FOR UPDATE", (request_id,))
            request_row = cur.fetchone()
            if request_row:
                investigation_id = str(request_row[0])
                row = self._investigation_row(cur, investigation_id)
                item = self._item_from_row(investigation_id, row)
                if not _visible(item, actor):
                    raise KeyError(f"Unknown investigation: {investigation_id}")
                return item

        inserted_graph = self._versioned_graph_state(
            state.model_copy(update={"publication_status": publication_status}), 1
        )
        inserted_projection = inserted_graph.to_legacy_state()
        cur.execute(
            """INSERT INTO enterprise.investigations
                (investigation_id,owner_user_id,question,metric,status,publication_status,agent_mode,provider,model,confidence,answer,state_json,graph_state_json,graph_state_version,scope_json,request_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb,%s)
                ON CONFLICT DO NOTHING""",
            (
                inserted_graph.investigation_id,
                actor.user_id,
                inserted_projection.question,
                inserted_projection.metric,
                inserted_projection.status,
                publication_status,
                inserted_projection.agent_mode,
                inserted_projection.provider,
                inserted_projection.model,
                inserted_projection.confidence,
                inserted_projection.answer,
                inserted_projection.model_dump_json(),
                inserted_graph.model_dump_json(),
                1,
                scope.model_dump_json(),
                request_id or None,
            ),
        )
        if cur.rowcount == 1:
            self._insert_evidence(cur, inserted_graph.investigation_id, inserted_projection)
            self._audit(
                cur,
                actor,
                "investigation.created",
                "investigation",
                inserted_graph.investigation_id,
                request_id,
                {"publication_status": publication_status, "graph_state_version": 1},
            )
            return self._item_from_row(
                inserted_graph.investigation_id,
                self._investigation_row(cur, inserted_graph.investigation_id),
            )

        row = self._investigation_row(cur, state.investigation_id, for_update=True)
        if row is None:
            if request_id:
                cur.execute("SELECT investigation_id FROM enterprise.investigations WHERE request_id=%s FOR UPDATE", (request_id,))
                request_row = cur.fetchone()
                if request_row:
                    investigation_id = str(request_row[0])
                    row = self._investigation_row(cur, investigation_id)
                    item = self._item_from_row(investigation_id, row)
                    if not _visible(item, actor):
                        raise KeyError(f"Unknown investigation: {investigation_id}")
                    return item
            raise VersionConflict("version_conflict: investigation insert raced with another update")
        current = self._item_from_row(state.investigation_id, row)
        if not _visible(current, actor):
            raise KeyError(f"Unknown investigation: {current.investigation_id}")
        if request_id and current.request_id == request_id:
            return current
        if expected_version is None or current.version != expected_version:
            raise VersionConflict(f"version_conflict: current version is {current.version}")
        next_graph = self._versioned_graph_state(
            state.model_copy(update={"publication_status": publication_status}), current.version + 1
        )
        next_projection = next_graph.to_legacy_state()
        cur.execute(
            """UPDATE enterprise.investigations SET
                question=%s,metric=%s,status=%s,publication_status=%s,agent_mode=%s,provider=%s,model=%s,confidence=%s,answer=%s,
                state_json=%s::jsonb,graph_state_json=%s::jsonb,graph_state_version=%s,scope_json=%s::jsonb,request_id=%s,version=%s,updated_at=now()
                WHERE investigation_id=%s AND version=%s""",
            (
                next_projection.question,
                next_projection.metric,
                next_projection.status,
                publication_status,
                next_projection.agent_mode,
                next_projection.provider,
                next_projection.model,
                next_projection.confidence,
                next_projection.answer,
                next_projection.model_dump_json(),
                next_graph.model_dump_json(),
                current.version + 1,
                scope.model_dump_json(),
                request_id or None,
                current.version + 1,
                state.investigation_id,
                current.version,
            ),
        )
        if cur.rowcount != 1:
            raise VersionConflict("version_conflict: investigation was updated concurrently")
        self._insert_evidence(cur, state.investigation_id, next_projection)
        self._audit(
            cur,
            actor,
            "investigation.updated",
            "investigation",
            state.investigation_id,
            request_id,
            {"publication_status": publication_status, "version": current.version + 1, "graph_state_version": current.version + 1},
        )
        return self._item_from_row(
            state.investigation_id,
            self._investigation_row(cur, state.investigation_id),
        )

    def save_graph_investigation(
        self,
        state: InvestigationGraphState,
        actor: Principal,
        scope: DataScope,
        publication_status: str,
        request_id: str,
        expected_version: int | None = None,
    ) -> EnterpriseInvestigation:
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    item = self._save_graph_investigation_cursor(
                        state, actor, scope, publication_status, request_id, expected_version, cur
                    )
        except VersionConflict:
            OPERATIONS.record_integrity("cas", "conflict")
            raise
        else:
            OPERATIONS.record_integrity("cas", "commit")
            return item

    def save_graph_investigation_and_approval(
        self,
        state: InvestigationGraphState,
        actor: Principal,
        scope: DataScope,
        publication_status: str,
        request_id: str,
        approval: ApprovalRequest,
        expected_version: int | None = None,
    ) -> tuple[EnterpriseInvestigation, ApprovalRequest]:
        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    saved = self._save_graph_investigation_cursor(
                        state, actor, scope, publication_status, request_id, expected_version, cur
                    )
                    bound_approval = approval.model_copy(update={"investigation_id": saved.investigation_id})
                    created = self._create_approval_cursor(bound_approval, actor, request_id, cur)
        except VersionConflict:
            OPERATIONS.record_integrity("cas", "conflict")
            raise
        except IdempotencyConflict:
            OPERATIONS.record_integrity("duplicate", "approval_conflict")
            raise
        else:
            OPERATIONS.record_integrity("cas", "commit")
            return saved, created

    def get_graph_investigation(self, investigation_id: str, actor: Principal) -> InvestigationGraphState:
        item = self.get_investigation(investigation_id, actor)
        graph = item.graph_state
        if isinstance(graph, InvestigationGraphState):
            graph.assert_internal_version_consistency()
            if item.graph_state_version != item.version or graph.enterprise_version != item.version:
                raise VersionConflict("version_conflict: graph snapshot is not bound to enterprise version")
            return graph.model_copy(deep=True)
        return InvestigationGraphState.from_legacy_state(item.state, space="clinical_trial")

    def save_investigation_and_approval(self,state,actor,scope,publication_status,request_id,approval):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                saved=self._save_investigation_cursor(state,actor,scope,publication_status,request_id,None,cur)
                bound_approval=approval.model_copy(update={"investigation_id":saved.investigation_id})
                created=self._create_approval_cursor(bound_approval,actor,request_id,cur)
                return saved,created

    def get_investigation(self,investigation_id,actor):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_user_id,publication_status,state_json,scope_json,created_at,updated_at,version,request_id,graph_state_json,graph_state_version FROM enterprise.investigations WHERE investigation_id=%s",(investigation_id,)); row=cur.fetchone()
        if not row: raise KeyError(f"Unknown investigation: {investigation_id}")
        item=self._item_from_row(investigation_id,row)
        if not _visible(item,actor): raise KeyError(f"Unknown investigation: {investigation_id}")
        return item

    def list_investigations(self,actor):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT investigation_id FROM enterprise.investigations ORDER BY created_at DESC LIMIT 100");ids=[str(x[0]) for x in cur.fetchall()]
        items=[]
        for item_id in ids:
            try:items.append(self.get_investigation(item_id,actor))
            except KeyError:pass
        return items

    @staticmethod
    def _approval_from_row(approval_id: str, row: tuple[Any, ...]) -> ApprovalRequest:
        return ApprovalRequest(approval_id=approval_id,investigation_id=str(row[0]),requested_by=row[1],status=row[2],reason=row[3],decided_by=row[4],decision_comment=row[5],created_at=row[6],decided_at=row[7],version=row[8])

    def _create_approval_cursor(self,item,actor,request_id,cur):
        cur.execute("""INSERT INTO enterprise.approval_requests(approval_id,investigation_id,requested_by,status,reason,version)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (investigation_id) DO NOTHING""",(item.approval_id,item.investigation_id,item.requested_by,item.status,item.reason,item.version))
        if cur.rowcount == 1:
            self._audit(cur,actor,"approval.created","approval",item.approval_id,request_id,{"investigation_id":item.investigation_id})
            return item
        cur.execute("SELECT approval_id,investigation_id,requested_by,status,reason,decided_by,decision_comment,created_at,decided_at,version FROM enterprise.approval_requests WHERE investigation_id=%s FOR UPDATE",(item.investigation_id,))
        row=cur.fetchone()
        if row is None: raise IdempotencyConflict("idempotency_conflict: approval could not be created")
        return self._approval_from_row(str(row[0]),row[1:])

    def create_approval(self,item,actor,request_id):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                return self._create_approval_cursor(item,actor,request_id,cur)

    def _sync_graph_approval_cursor(self, item: ApprovalRequest, cur: Any) -> None:
        """Advance a canonical Graph snapshot with the approval lifecycle in one transaction."""

        row = self._investigation_row(cur, item.investigation_id, for_update=True)
        if row is None:
            return
        current = self._item_from_row(item.investigation_id, row)
        if not isinstance(current.graph_state, InvestigationGraphState):
            return
        next_version = current.version + 1
        graph = self._versioned_graph_state(
            current.graph_state.with_approval(item.status.value, item.version), next_version
        )
        projection = graph.to_legacy_state()
        cur.execute(
            """UPDATE enterprise.investigations SET state_json=%s::jsonb,graph_state_json=%s::jsonb,
               graph_state_version=%s,version=%s,updated_at=now()
               WHERE investigation_id=%s AND version=%s""",
            (
                projection.model_dump_json(),
                graph.model_dump_json(),
                next_version,
                next_version,
                item.investigation_id,
                current.version,
            ),
        )
        if cur.rowcount != 1:
            raise VersionConflict("version_conflict: canonical Graph approval update raced")

    def get_approval(self,approval_id):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT investigation_id,requested_by,status,reason,decided_by,decision_comment,created_at,decided_at,version FROM enterprise.approval_requests WHERE approval_id=%s",(approval_id,));r=cur.fetchone()
        if not r:raise KeyError(f"Unknown approval: {approval_id}")
        return ApprovalRequest(approval_id=approval_id,investigation_id=str(r[0]),requested_by=r[1],status=r[2],reason=r[3],decided_by=r[4],decision_comment=r[5],created_at=r[6],decided_at=r[7],version=r[8])

    def list_approvals(self,actor):
        clause="" if actor.role is Role.ADMIN else " WHERE requested_by=%s";params=() if actor.role is Role.ADMIN else (actor.user_id,)
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT approval_id FROM enterprise.approval_requests"+clause+" ORDER BY created_at DESC",params);ids=[str(x[0]) for x in cur.fetchall()]
        return [self.get_approval(x) for x in ids]

    def save_approval(self,item,actor,request_id):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM enterprise.audit_events WHERE action=%s AND resource_type='approval' AND resource_id=%s AND request_id=%s",(f"approval.{item.status}",item.approval_id,request_id))
                if cur.fetchone():
                    OPERATIONS.record_integrity("duplicate", "approval_decision_replay")
                    cur.execute("SELECT investigation_id,requested_by,status,reason,decided_by,decision_comment,created_at,decided_at,version FROM enterprise.approval_requests WHERE approval_id=%s",(item.approval_id,))
                    row=cur.fetchone()
                    if row:return self._approval_from_row(item.approval_id,row)
                cur.execute("UPDATE enterprise.approval_requests SET status=%s,decided_by=%s,decision_comment=%s,decided_at=%s,version=%s WHERE approval_id=%s AND version=%s",(item.status,item.decided_by,item.decision_comment,item.decided_at,item.version,item.approval_id,item.version-1))
                if cur.rowcount!=1:raise VersionConflict("version_conflict: current approval version is newer")
                self._sync_graph_approval_cursor(item, cur)
                self._audit(cur,actor,f"approval.{item.status}","approval",item.approval_id,request_id)
                OPERATIONS.record_integrity("approval", "decision_committed")
        return item

    def save_approval_and_enqueue(self, item, actor, request_id, event, outbox):
        """Atomically persist an approval decision and its Temporal signal outbox record."""

        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE enterprise.approval_requests SET status=%s,decided_by=%s,decision_comment=%s,decided_at=%s,version=%s WHERE approval_id=%s AND version=%s",
                    (item.status, item.decided_by, item.decision_comment, item.decided_at, item.version, item.approval_id, item.version - 1),
                )
                if cur.rowcount != 1:
                    raise ValueError("version_conflict")
                self._sync_graph_approval_cursor(item, cur)
                self._audit(cur, actor, f"approval.{item.status}", "approval", item.approval_id, request_id)
                enqueue_with_cursor = getattr(outbox, "enqueue_with_cursor", None)
                if callable(enqueue_with_cursor):
                    enqueue_with_cursor(cur, event)
                else:
                    # Custom repositories may supply a different outbox implementation.  Its
                    # enqueue call still runs before this connection commits.
                    outbox.enqueue(event)
        return item

    def publish_investigation(self,investigation_id):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                row = self._investigation_row(cur, investigation_id, for_update=True)
                if row is None:
                    raise KeyError(f"Unknown investigation: {investigation_id}")
                current = self._item_from_row(investigation_id, row)
                if current.publication_status == "published":
                    return current
                next_version = current.version + 1
                if isinstance(current.graph_state, InvestigationGraphState):
                    graph = self._versioned_graph_state(
                        current.graph_state.model_copy(update={"publication_status": "published"}),
                        next_version,
                    )
                    projection = graph.to_legacy_state()
                    cur.execute(
                        """UPDATE enterprise.investigations SET publication_status='published',state_json=%s::jsonb,
                           graph_state_json=%s::jsonb,graph_state_version=%s,version=%s,updated_at=now()
                           WHERE investigation_id=%s AND version=%s""",
                        (
                            projection.model_dump_json(),
                            graph.model_dump_json(),
                            next_version,
                            next_version,
                            investigation_id,
                            current.version,
                        ),
                    )
                else:
                    cur.execute(
                        """UPDATE enterprise.investigations SET publication_status='published',
                           state_json=jsonb_set(state_json,'{state_version}',to_jsonb(version+1),true),
                           version=version+1,updated_at=now()
                           WHERE investigation_id=%s AND version=%s""",
                        (investigation_id, current.version),
                    )
                if cur.rowcount != 1:
                    raise VersionConflict("version_conflict: investigation was updated concurrently")
                return self._item_from_row(
                    investigation_id,
                    self._investigation_row(cur, investigation_id),
                )

    @property
    def audit_events(self):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT event_id,actor_user_id,action,resource_type,resource_id,outcome,metadata_json,request_id,created_at FROM enterprise.audit_events ORDER BY created_at DESC LIMIT 100");rows=cur.fetchall()
        return [AuditEvent(event_id=str(r[0]),actor_user_id=r[1],action=r[2],resource_type=r[3],resource_id=r[4],outcome=r[5],metadata=r[6],request_id=r[7],created_at=r[8]) for r in rows]

    def save_schedule(self,item,actor,request_id):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO enterprise.investigation_schedules(schedule_id,owner_user_id,name,question,provider,cron_expression,timezone,enabled,next_run_at,last_run_at,version,scope_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",(item.schedule_id,item.owner_user_id,item.name,item.question,item.provider,item.cron_expression,item.timezone,item.enabled,item.next_run_at,item.last_run_at,item.version,item.scope.model_dump_json()));self._audit(cur,actor,"schedule.created","schedule",item.schedule_id,request_id)
        return item

    def _schedule_from_row(self,r):
        return InvestigationSchedule(schedule_id=str(r[0]),owner_user_id=r[1],name=r[2],question=r[3],provider=r[4],cron_expression=r[5],timezone=r[6],enabled=r[7],next_run_at=r[8],last_run_at=r[9],version=r[10],scope=DataScope.model_validate(r[11]))

    def get_schedule(self,schedule_id):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT schedule_id,owner_user_id,name,question,provider,cron_expression,timezone,enabled,next_run_at,last_run_at,version,scope_json FROM enterprise.investigation_schedules WHERE schedule_id=%s",(schedule_id,));r=cur.fetchone()
        if not r:raise KeyError(f"Unknown schedule: {schedule_id}")
        return self._schedule_from_row(r)

    def list_schedules(self,actor):
        clause="" if actor.role is Role.ADMIN else " WHERE owner_user_id=%s";params=() if actor.role is Role.ADMIN else (actor.user_id,)
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT schedule_id,owner_user_id,name,question,provider,cron_expression,timezone,enabled,next_run_at,last_run_at,version,scope_json FROM enterprise.investigation_schedules"+clause,params);rows=cur.fetchall()
        return [self._schedule_from_row(r) for r in rows]

    def save_alert(self,item):
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("INSERT INTO enterprise.alerts(alert_id,investigation_id,recipient_user_id,severity,title,body,status) VALUES (%s,%s,%s,%s,%s,%s,%s)",(item.alert_id,item.investigation_id,item.recipient_user_id,item.severity,item.title,item.body,item.status))
        return item

    def list_alerts(self,actor):
        clause="" if actor.role is Role.ADMIN else " WHERE recipient_user_id=%s";params=() if actor.role is Role.ADMIN else (actor.user_id,)
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:cur.execute("SELECT alert_id,investigation_id,recipient_user_id,severity,title,body,status,created_at,read_at FROM enterprise.alerts"+clause+" ORDER BY created_at DESC",params);rows=cur.fetchall()
        return [Alert(alert_id=str(r[0]),investigation_id=str(r[1]),recipient_user_id=r[2],severity=r[3],title=r[4],body=r[5],status=r[6],created_at=r[7],read_at=r[8]) for r in rows]

