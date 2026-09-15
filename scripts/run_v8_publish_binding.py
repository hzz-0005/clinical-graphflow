"""V8 governed publication bridge acceptance check.

Purpose
-------
Prove end to end that data uploaded and published through the V8 quarantine lifecycle is not a
dead end. A published version must be *bindable*: its canonical records have to be projected into
the governed ingestion tables that feed the analytics mart, and the dynamic investigation runtime
must be able to reference the very same ``published_batch_id``, while honestly reporting the
domains the selected version does not carry (AE / EX / LB) instead of inventing rows.

The pipeline has two observable phases, so this script exposes both:

``--phase publish``
    1. ``POST /api/v8/imports``               upload a subject level package
    2. ``profile`` -> ``understand`` -> ``validate`` -> ``approve`` -> ``publish``
    3. assert ``projection.bindable`` and that both arms reached the ingestion layer
    4. print ``IF_BATCH=<id>`` so the caller can rebuild the analytics models

``--phase investigate --batch <id>``
    5. ``POST /api/v8/clinical/investigations`` with ``published_batch_id``
    6. assert the runtime retained the batch id, narrowed ``available_domains`` to what the version
       publishes, disclosed the unpublished domains as data gaps, produced a structured evidence
       chain, and never bypassed human approval

Between the two phases the caller must rebuild the analytics models (``dbt build``): publishing
writes canonical records, and the marts are what the governed tools actually read. Without that
rebuild the runtime degrades honestly to ``inconclusive`` instead of fabricating rows, which is
exactly the behaviour ``--allow-degraded`` lets a caller assert.

Only the standard library is used, so the same file runs on the host or inside the compose network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_BASE = os.environ.get("IF_BASE", "http://127.0.0.1:18000").rstrip("/")
USER = os.environ.get("IF_USER", "admin")
PROJECTED_DOMAIN_CEILING = {"DM", "ADSL", "ADEFF"}
# Domains that examples/v8_publish deliberately does not ship; the runtime must disclose them.
EXPECTED_MISSING_DOMAINS = {"AE", "EX", "LB"}

FAILURES: list[str] = []
BASE = DEFAULT_BASE


def call(method: str, path: str, body: dict | None = None, uploads: list | None = None):
    """Issue one governed API call and return ``(status, payload)`` without raising on 4xx/5xx."""

    headers = {"X-InsightFlow-User": USER, "X-Request-Id": "v8bind-" + uuid.uuid4().hex}
    if uploads:
        boundary = "----IF" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for filename, data, ctype in uploads:
            chunks.append(f"--{boundary}\r\n".encode())
            chunks.append(
                f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'.encode()
            )
            chunks.append(f"Content-Type: {ctype}\r\n\r\n".encode())
            chunks.append(data)
            chunks.append(b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode())
        payload = b"".join(chunks)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif body is None:
        payload = None
    else:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(BASE + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}
    except urllib.error.URLError as exc:  # pragma: no cover - transport failure only
        return 0, {"raw": f"transport error: {exc}"}


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' -> ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def step(title: str) -> None:
    print(f"\n==> {title}")


def read_example(name: str) -> bytes:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, os.pardir, "examples", "v8_publish", name)
    with open(path, "rb") as handle:
        return handle.read()


def fail_out() -> int:
    print(f"\nV8PublishBinding=failed count={len(FAILURES)}")
    for item in FAILURES:
        print(f"  - {item}")
    return 1


def phase_publish(trial_id: str) -> int:
    step("Upload subject level package / 上传受试者级数据包")
    status, created = call(
        "POST",
        "/api/v8/imports",
        uploads=[
            ("DM.csv", read_example("DM.csv"), "text/csv"),
            ("ADSL.csv", read_example("ADSL.csv"), "text/csv"),
            ("ADEFF.csv", read_example("ADEFF.csv"), "text/csv"),
        ],
    )
    if status != 201 or not isinstance(created, dict) or "batch_id" not in created:
        print(json.dumps(created, ensure_ascii=False)[:600])
        print("FAIL: upload did not return a batch id")
        return 1
    batch_id = created["batch_id"]
    version = created["version"]
    print(f"  batch_id={batch_id} version={version}")

    step("Quarantine lifecycle / 隔离区生命周期")
    # Bodies are built inside the loop on purpose: each transition returns a new optimistic version,
    # so the next request must be assembled after the previous one is confirmed.
    for name in ("profile", "understand"):
        body = {"version": version} if name == "profile" else {"version": version, "provider": "rules"}
        status, payload = call("POST", f"/api/v8/imports/{batch_id}/{name}", body)
        check(f"{name} accepted", status == 200, f"http={status}")
        if status != 200:
            print(json.dumps(payload, ensure_ascii=False)[:600])
            return 1
        version = payload["batch"]["version"]
    status, validated = call("POST", f"/api/v8/imports/{batch_id}/validate", {"version": version})
    check("validate accepted", status == 200, f"http={status}")
    if status != 200:
        print(json.dumps(validated, ensure_ascii=False)[:600])
        return 1
    report = validated.get("report", {})
    check("validation report is valid", bool(report.get("valid")), f"errors={len(report.get('errors', []))}")
    version = validated["batch"]["version"]
    status, approved = call("POST", f"/api/v8/imports/{batch_id}/approve", {"version": version})
    check("approve accepted", status == 200, f"http={status}")
    if status != 200:
        print(json.dumps(approved, ensure_ascii=False)[:600])
        return 1
    version = approved["version"]

    step("Governed publication bridge / 受治理发布桥")
    status, published = call("POST", f"/api/v8/imports/{batch_id}/publish", {"version": version})
    check("publish accepted", status == 200, f"http={status}")
    if status != 200:
        print(json.dumps(published, ensure_ascii=False)[:600])
        return 1
    projection = published.get("projection") or {}
    print("  projection=" + json.dumps(projection, ensure_ascii=False))
    arms = projection.get("arm_counts") or {}
    check("published version is bindable", projection.get("bindable") is True)
    check(
        "canonical records reached the ingestion layer",
        int(projection.get("record_count") or 0) > 0,
        f"records={projection.get('record_count')}",
    )
    check(
        "both study arms were projected",
        bool(arms.get("control")) and bool(arms.get("treatment")),
        json.dumps(arms, ensure_ascii=False),
    )
    check(
        "published version flipped to published state",
        published.get("status") == "published",
        f"status={published.get('status')} version={published.get('version')}",
    )
    check(
        "projection is scoped to the published trial",
        list(projection.get("trial_ids") or []) == [trial_id],
        ",".join(projection.get("trial_ids") or []),
    )

    if FAILURES:
        return fail_out()
    print(f"\nIF_BATCH={batch_id}")
    print(
        "V8PublishPhase=passed batch={0} records={1} sites={2} domains={3}".format(
            batch_id,
            projection.get("record_count"),
            ",".join(projection.get("site_ids") or []),
            ",".join(projection.get("domains") or []),
        )
    )
    return 0


def phase_investigate(batch_id: str, trial_id: str, question: str, require_analytics: bool) -> int:
    step("Bound dynamic investigation / 绑定批次动态调查")
    status, state = call(
        "POST",
        "/api/v8/clinical/investigations",
        {
            "trial_id": trial_id,
            "question": question,
            "provider": "fake",
            "published_batch_id": batch_id,
        },
    )
    check("bound investigation accepted", status == 200, f"http={status}")
    if status != 200 or not isinstance(state, dict):
        print(json.dumps(state, ensure_ascii=False)[:600])
        return fail_out()

    audit = state.get("audit_metadata") or {}
    steps = state.get("steps") or []
    evidence = state.get("evidence") or []
    warnings = state.get("warnings") or []
    tools = [item.get("tool") for item in steps]
    domains = set(audit.get("available_domains") or [])
    signals = sorted({item.get("observation_signal") for item in evidence if item.get("observation_signal")})
    grounded = [item for item in evidence if item.get("supports") or item.get("contradicts")]

    print(f"  status={state.get('status')} runtime={audit.get('runtime_version')}")
    print(f"  tools={tools}")
    print(f"  available_domains={sorted(domains)}")
    print(f"  signals={signals}")
    for item in warnings:
        print(f"  warning: {item}")

    check("runtime bound the selected published batch", audit.get("published_batch_id") == batch_id)
    check("runtime performed a multi-step investigation", len(steps) >= 3, f"steps={len(steps)}")
    check("runtime produced a structured evidence chain", len(evidence) >= 2, f"evidence={len(evidence)}")
    check(
        "evidence carries observation signals rather than raw SQL rows",
        bool(signals),
        ",".join(signals) or "none",
    )
    check(
        "domain capability was narrowed to the selected version",
        bool(domains) and domains.issubset(PROJECTED_DOMAIN_CEILING),
        ",".join(sorted(domains)),
    )
    disclosed = sorted(domain for domain in EXPECTED_MISSING_DOMAINS if any(domain in item for item in warnings))
    check(
        "unpublished domains were disclosed as data gaps",
        disclosed == sorted(EXPECTED_MISSING_DOMAINS),
        f"disclosed={disclosed} expected={sorted(EXPECTED_MISSING_DOMAINS)}",
    )
    check(
        "conclusion never bypassed human approval",
        state.get("status") in {"pending_approval", "inconclusive"},
        f"status={state.get('status')}",
    )
    check(
        "pending approval is only reachable with grounded evidence",
        state.get("status") != "pending_approval" or bool(grounded),
        f"grounded={len(grounded)}",
    )
    serialized = json.dumps(state, ensure_ascii=False)
    check("no raw participant identifier leaked", "participant_id" not in serialized)
    check(
        "no unsupported medical advice",
        not any(token in serialized.lower() for token in ("recommended dose", "should start", "should stop")),
    )

    if require_analytics:
        check(
            "analytics models exposed the published batch to the runtime",
            state.get("status") == "pending_approval",
            f"status={state.get('status')}",
        )
        check(
            "investigation reached a governed conclusion backed by evidence",
            bool(grounded) and any(signal in {"site_ranking", "treatment_effect"} for signal in signals),
            f"signals={signals}",
        )

    if FAILURES:
        return fail_out()
    print(
        "V8PublishBinding=passed batch={0} tools={1} evidence={2} grounded={3} domains={4} signals={5} status={6}".format(
            batch_id,
            len(steps),
            len(evidence),
            len(grounded),
            ",".join(sorted(domains)),
            ",".join(signals),
            state.get("status"),
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="V8 governed publication bridge acceptance check")
    parser.add_argument("--phase", choices=("publish", "investigate"), default="publish")
    parser.add_argument("--base", default=DEFAULT_BASE, help="API base url, e.g. http://backend:8000")
    parser.add_argument("--batch", default=os.environ.get("IF_BATCH", ""), help="published batch id for the investigate phase")
    parser.add_argument("--trial", default=os.environ.get("IF_TRIAL", "TRIAL-UPLOAD-301"))
    parser.add_argument(
        "--question",
        default=os.environ.get("IF_QUESTION", "Why is the Week-12 efficacy endpoint lower in this published batch?"),
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="accept an inconclusive result, i.e. assert honest degradation when the mart was not rebuilt",
    )
    args = parser.parse_args()

    global BASE
    BASE = args.base.rstrip("/")
    print(f"V8 publication binding acceptance against {BASE} (phase={args.phase})")

    if args.phase == "publish":
        return phase_publish(args.trial)
    if not args.batch:
        print("FAIL: --phase investigate requires --batch <published batch id>")
        return 1
    return phase_investigate(args.batch, args.trial, args.question, require_analytics=not args.allow_degraded)


if __name__ == "__main__":
    sys.exit(main())

