# V16 emergency fallback runbook

V17 Graph is the normal runtime. V16 may only be enabled for a time-bounded incident window;
there is no automatic fallback when Graph execution fails.

Set all fields before starting the API or dynamic Worker:

```powershell
$env:INSIGHTFLOW_RUNTIME_VERSION = "v16"
$env:INSIGHTFLOW_V16_FALLBACK_OWNER = "incident-owner@example.com"
$env:INSIGHTFLOW_V16_FALLBACK_REASON = "INC-0000 Graph canary rollback"
$env:INSIGHTFLOW_V16_FALLBACK_UNTIL = "2026-09-22T23:59:59Z"
```

The owner, reason and UTC expiry are validated at the V16 execution boundary and copied into
`audit_metadata`. Missing, malformed or expired metadata fails closed with the existing dynamic
validation error. Temporal remains Graph-only and is not switched by this runbook.

Before expiry, restore `INSIGHTFLOW_RUNTIME_VERSION=v17`, restart API/Worker, and verify `/ready`,
Graph runtime metadata, PostgreSQL canonical snapshots and the V25/V26 replay/CAS tests. Do not
roll back database migrations, delete Graph snapshots, or modify V0–V3 data. Record the incident,
window, canary scope, conflict/retry metrics and restoration result in the release log.

