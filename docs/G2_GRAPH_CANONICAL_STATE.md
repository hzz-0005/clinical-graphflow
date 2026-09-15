# G2 Graph canonical state

## Boundary

V17 `InvestigationGraphState` is the canonical state for Graph runtime writes.  The enterprise
repository stores it in the additive `enterprise.investigations.graph_state_json` column and binds
the snapshot to both `graph_state_version` and the row `version`.  `state_json` remains a private
V4–V16 API compatibility projection and is generated from the Graph snapshot during a Graph write
or a canonical read.

The payload carries:

- `schema_version`: `v17.graph_state.v1`;
- `state_version` and `enterprise_version`: the monotonic enterprise snapshot version;
- `checkpoint_version`: the replay/event boundary, independent of the enterprise CAS version;
- typed Graph node, plan, task cursor, pending tool result, observations, coverage, approval
  lifecycle, gaps and events.

`EnterpriseInvestigation.model_dump()` excludes the private `graph_state` fields, so existing API
serializers continue to return the legacy response shape.  Callers that need to resume Graph work
use `get_graph_investigation()` and must pass the returned `enterprise_version` back as
`expected_version` when writing.

## Transaction and retry contract

`save_graph_investigation()` and `save_graph_investigation_and_approval()` write the canonical
Graph payload, its legacy projection, evidence and audit/approval records on the same PostgreSQL
transaction.  `request_id` is checked first for retry idempotency.  A stale `expected_version`
raises `VersionConflict`; it never overwrites a newer Graph snapshot.

Every task claims `investigation_id:task_id` through the runtime idempotency port before the
gateway call. A competing Worker retries instead of issuing a second clinical query; transient
infrastructure failures release the local claim so the outer Temporal/Worker policy can retry.
Deterministic contract failures become an inconclusive gap, while unexpected database/network/
provider failures propagate to that outer retry policy.

The lease-based clinical Worker keeps this distinction at its durable-job boundary: a V17 Graph
`ConnectionError`, timeout, `OSError`, psycopg transport failure, or claim `RuntimeError` is
requeued while the attempt budget remains, so another lease holder can reclaim it. `ValueError`,
`TypeError`, malformed runtime markers, V16 jobs, and the older V5 shape remain terminal failures;
after the bounded V17 attempt budget is exhausted, the job is also failed with a type-only
`error_code`.

Existing V0–V26 rows are not backfilled.  `get_graph_investigation()` adapts a legacy row in memory
when no canonical column exists, and does not write the adapter result back.  The additive
`019_graph_canonical_state.sql` migration is safe to run repeatedly.

Redis/Temporal checkpoints remain coordination payloads.  A checkpoint whose
`enterprise_version` differs from the durable Graph state is rejected before Graph execution;
this prevents a stale retry from re-running or overwriting a newer investigation. Redis-backed
checkpoint writes use an EVAL compare-and-set script for atomic monotonic version protection;
redis-py WATCH/MULTI and lightweight-client compatibility fallbacks preserve the same validation
contract without becoming a clinical fact store.

