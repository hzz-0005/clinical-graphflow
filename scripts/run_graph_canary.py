"""Run the isolated V17 Graph canary matrix.

The command is deliberately a test orchestrator rather than an application migration tool.  It
only reads Docker health/configuration and invokes the already isolated Graph integration tests.
The PostgreSQL tests use UUID-keyed rows and clean those rows in ``finally`` blocks; a remote
database is rejected unless the operator explicitly opts in.

Examples::

    # Fast local Graph gate when Docker/PostgreSQL are not available.
    python scripts/run_graph_canary.py --skip-docker

    # Full local canary against the compose PostgreSQL instance.
    $env:ENTERPRISE_DATABASE_URL = 'postgresql://...@localhost:55432/insightflow'
    python scripts/run_graph_canary.py --require-postgres

    # Machine-readable output, with a second pass for a short soak.
    python scripts/run_graph_canary.py --skip-docker --repeat 2 --json
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


@dataclass
class CheckResult:
    name: str
    status: str
    duration_seconds: float = 0.0
    detail: str = ""
    output_tail: str = ""
    repeat: int | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass
class CanaryReport:
    started_at: str
    completed_at: str = ""
    duration_seconds: float = 0.0
    repeat: int = 1
    strict: bool = False
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(check.status == "failed" for check in self.checks)

    @property
    def skipped(self) -> int:
        return sum(check.status == "skipped" for check in self.checks)

    @property
    def ok(self) -> bool:
        return not self.failed and (not self.strict or self.skipped == 0)

    def as_json(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "repeat": self.repeat,
            "strict": self.strict,
            "ok": self.ok,
            "partial": not self.failed and self.skipped > 0,
            "failed": sum(check.status == "failed" for check in self.checks),
            "passed": sum(check.status == "passed" for check in self.checks),
            "skipped": self.skipped,
            "checks": [
                {
                    **asdict(check),
                    "duration_seconds": round(check.duration_seconds, 3),
                }
                for check in self.checks
            ],
        }


# Keep the matrix explicit and reviewable.  The source tests are intentionally scoped to the
# typed Graph/runtime contract; the full backend suite belongs to the release regression gate.
MATRIX: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "graph_node_matrix",
        (
            "backend/tests/test_v17_graph.py",
            "backend/tests/test_v17_contracts.py",
        ),
    ),
    (
        "default_v17_runtime",
        (
            "backend/tests/test_v17_runtime_api.py::test_v17_api_uses_typed_graph_and_keeps_response_shape",
            "backend/tests/test_dynamic_job_execution.py",
        ),
    ),
    (
        "canonical_cas_approval_privacy",
        (
            "backend/tests/test_g2_graph_canonical_persistence.py",
            "backend/tests/test_v17_runtime_ports.py",
        ),
    ),
    (
        "postgres_canonical_transaction",
        (
            "backend/tests/test_g2_graph_canonical_persistence.py::test_postgres_graph_snapshot_and_approval_share_one_transaction",
            "backend/tests/test_v26_postgres_integration.py",
        ),
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tail(output: str, limit: int = 1800) -> str:
    value = output.strip()
    return value if len(value) <= limit else value[-limit:]


def _run(command: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """Run a read-only command with stable locale and captured output."""

    environment = os.environ.copy()
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("LC_ALL", "C")
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _compose_records(raw: str) -> list[dict[str, Any]]:
    """Parse both Compose's JSON-array and one-JSON-object-per-line formats."""

    text = raw.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _compose_service_name(record: dict[str, Any]) -> str:
    return str(record.get("Service") or record.get("service") or "").strip()


def _compose_is_healthy(record: dict[str, Any]) -> bool:
    state = str(record.get("State") or record.get("state") or "").lower()
    health = str(record.get("Health") or record.get("health") or "").lower()
    status = str(record.get("Status") or record.get("status") or "").lower()
    if state and state not in {"running", "up"}:
        return False
    if health in {"unhealthy", "starting"}:
        return False
    return state in {"running", "up"} or "up" in status


def _environment_map(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    if isinstance(environment, list):
        result: dict[str, str] = {}
        for item in environment:
            key, separator, value = str(item).partition("=")
            if separator:
                result[key] = value
        return result
    return {}


def _loopback_candidates(url: str) -> tuple[str, ...]:
    """Prefer IPv4 Docker publishing, but retain localhost compatibility."""

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"localhost", "127.0.0.1"}:
        return (url,)
    try:
        port = parsed.port
    except ValueError:
        return (url,)
    candidates = ("127.0.0.1", "localhost")
    return tuple(
        urlunparse(parsed._replace(netloc=f"{candidate}:{port}" if port is not None else candidate))
        for candidate in candidates
    )


def _check_docker(ready_url: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    started = time.perf_counter()
    try:
        result = _run(["docker", "compose", "ps", "--format", "json"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [CheckResult("docker_services", "failed", time.perf_counter() - started, str(exc))]
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "docker compose ps failed").strip()
        return [CheckResult("docker_services", "failed", time.perf_counter() - started, _tail(detail))]

    records = {_compose_service_name(item): item for item in _compose_records(result.stdout)}
    expected = ("postgres", "backend", "clinical-worker")
    missing = [service for service in expected if service not in records]
    unhealthy = [service for service in expected if service in records and not _compose_is_healthy(records[service])]
    if missing or unhealthy:
        detail_parts = []
        if missing:
            detail_parts.append("missing=" + ",".join(missing))
        if unhealthy:
            detail_parts.append("unhealthy=" + ",".join(unhealthy))
        checks.append(
            CheckResult(
                "docker_services",
                "failed",
                time.perf_counter() - started,
                "; ".join(detail_parts),
            )
        )
    else:
        checks.append(
            CheckResult(
                "docker_services",
                "passed",
                time.perf_counter() - started,
                "postgres, backend and clinical-worker are running",
            )
        )

    started = time.perf_counter()
    try:
        # Render the optional Temporal profile as well, even when it is not running.  This
        # validates that the emergency fallback contract reaches every runtime-capable container
        # without pretending that a stopped profile is healthy.
        config = _run(["docker", "compose", "--profile", "temporal", "config", "--format", "json"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        checks.append(CheckResult("docker_runtime_version", "failed", time.perf_counter() - started, str(exc)))
    else:
        if config.returncode != 0:
            checks.append(
                CheckResult(
                    "docker_runtime_version",
                    "failed",
                    time.perf_counter() - started,
                    _tail(config.stderr or config.stdout or "docker compose config failed"),
                )
            )
        else:
            runtime_services: dict[str, dict[str, str]] = {}
            try:
                payload = json.loads(config.stdout)
                services = payload.get("services", {})
                runtime_services = {
                    service: _environment_map(value)
                    for service, value in services.items()
                    if service in {"backend", "clinical-worker", "clinical-temporal-worker", "clinical-temporal-outbox"}
                }
                versions = {
                    service: environment.get("INSIGHTFLOW_RUNTIME_VERSION", "")
                    for service, environment in runtime_services.items()
                }
                mismatched = {service: version for service, version in versions.items() if version.lower() != "v17"}
            except (TypeError, ValueError, AttributeError) as exc:
                versions = {}
                mismatched = {}
                config_error = str(exc)
            else:
                config_error = ""
            if config_error:
                checks.append(CheckResult("docker_runtime_version", "failed", time.perf_counter() - started, config_error))
            elif not versions:
                checks.append(CheckResult("docker_runtime_version", "failed", time.perf_counter() - started, "no runtime service configuration found"))
            elif mismatched:
                checks.append(
                    CheckResult(
                        "docker_runtime_version",
                        "failed",
                        time.perf_counter() - started,
                        "runtime versions must all be v17: " + repr(mismatched),
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        "docker_runtime_version",
                        "passed",
                        time.perf_counter() - started,
                        "runtime services=" + repr(versions),
                    )
                )

            # The V16 emergency runbook is only actionable when its ownership window reaches
            # every process that can execute or resume a job.  Check key presence and consistency,
            # but never print the owner, incident reason, or expiry value into the canary report.
            started = time.perf_counter()
            fallback_keys = (
                "INSIGHTFLOW_V16_FALLBACK_OWNER",
                "INSIGHTFLOW_V16_FALLBACK_REASON",
                "INSIGHTFLOW_V16_FALLBACK_UNTIL",
            )
            missing_fallback = [
                service
                for service, environment in runtime_services.items()
                if any(key not in environment for key in fallback_keys)
            ]
            fallback_values = {
                tuple(environment.get(key, "<missing>") for key in fallback_keys)
                for environment in runtime_services.values()
            }
            if missing_fallback:
                checks.append(
                    CheckResult(
                        "docker_v16_fallback_config",
                        "failed",
                        time.perf_counter() - started,
                        "fallback keys missing from=" + ",".join(sorted(missing_fallback)),
                    )
                )
            elif len(fallback_values) != 1:
                checks.append(
                    CheckResult(
                        "docker_v16_fallback_config",
                        "failed",
                        time.perf_counter() - started,
                        "fallback keys are not consistent across runtime services",
                    )
                )
            else:
                configured = any(value for value in next(iter(fallback_values)))
                checks.append(
                    CheckResult(
                        "docker_v16_fallback_config",
                        "passed",
                        time.perf_counter() - started,
                        "fallback keys are present and consistent (configured=" + str(configured).lower() + ")",
                    )
                )

            # Do not invent a Temporal workflow API call here.  When Temporal is explicitly
            # enabled, the canary only gates on the compose profile's process health and the
            # non-sensitive target/queue configuration.  Workflow semantics stay in the
            # injected-SDK tests and the real Temporal deployment's own probes.
            started = time.perf_counter()
            temporal_services = {
                service: _environment_map(value)
                for service, value in services.items()
                if service in {"temporal-postgres", "temporal", "redis", "clinical-temporal-worker", "clinical-temporal-outbox"}
            }
            temporal_enabled = any(
                environment.get("INSIGHTFLOW_TEMPORAL_ENABLED", "").lower() in {"1", "true", "yes", "on"}
                for environment in runtime_services.values()
            )
            if not temporal_enabled:
                checks.append(
                    CheckResult(
                        "temporal_service_gate",
                        "skipped",
                        time.perf_counter() - started,
                        "INSIGHTFLOW_TEMPORAL_ENABLED is false",
                    )
                )
            else:
                required_temporal = {"temporal-postgres", "temporal", "redis", "clinical-temporal-worker", "clinical-temporal-outbox"}
                missing_temporal = sorted(required_temporal - set(records))
                unhealthy_temporal = sorted(
                    service for service in required_temporal
                    if service in records and not _compose_is_healthy(records[service])
                )
                worker_environment = temporal_services.get("clinical-temporal-worker", {})
                missing_temporal_config = [
                    key for key in ("INSIGHTFLOW_TEMPORAL_TARGET", "INSIGHTFLOW_TEMPORAL_NAMESPACE", "INSIGHTFLOW_TEMPORAL_TASK_QUEUE")
                    if not worker_environment.get(key, "").strip()
                ]
                detail_parts = []
                if missing_temporal:
                    detail_parts.append("missing=" + ",".join(missing_temporal))
                if unhealthy_temporal:
                    detail_parts.append("unhealthy=" + ",".join(unhealthy_temporal))
                if missing_temporal_config:
                    detail_parts.append("missing_config=" + ",".join(missing_temporal_config))
                checks.append(
                    CheckResult(
                        "temporal_service_gate",
                        "failed" if detail_parts else "passed",
                        time.perf_counter() - started,
                        "; ".join(detail_parts) if detail_parts else "Temporal profile services are healthy and configured",
                    )
                )

    # A developer shell may have an HTTP proxy configured.  The canary endpoint is a local
    # compose port, so a proxy response is not evidence about backend health.
    local_http = build_opener(ProxyHandler({}))
    for path, name in ((ready_url, "http_ready"), (_health_url(ready_url), "http_health")):
        started = time.perf_counter()
        last_error = ""
        for candidate in _loopback_candidates(path):
            try:
                request = Request(candidate, method="GET")
                with local_http.open(request, timeout=5) as response:
                    body = response.read().decode("utf-8", errors="replace")
                    status_code = response.status
                payload = json.loads(body)
                good = status_code == 200 and payload.get("status") in {"ready", "ok"}
                if name == "http_ready":
                    good = good and all(value == "ok" for value in payload.get("checks", {}).values())
                if good:
                    checks.append(
                        CheckResult(
                            name,
                            "passed",
                            time.perf_counter() - started,
                            f"HTTP 200 {payload.get('status')} via {candidate}",
                        )
                    )
                    break
                last_error = _tail(body)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = str(exc)
        else:
            checks.append(CheckResult(name, "failed", time.perf_counter() - started, last_error))
    return checks


def _health_url(ready_url: str) -> str:
    base = ready_url.rstrip("/")
    return base[: -len("/ready")] + "/health" if base.endswith("/ready") else base + "/health"


def _postgres_target_is_local(dsn: str) -> bool:
    hostname = (urlparse(dsn).hostname or "").lower()
    return hostname in {"localhost", "127.0.0.1", "::1", "postgres"}


def _pytest_environment(dsn: str | None) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(ROOT), str(BACKEND), environment.get("PYTHONPATH", "")) if item
    )
    environment.setdefault("INSIGHTFLOW_RUNTIME_VERSION", "v17")
    if dsn:
        environment["ENTERPRISE_DATABASE_URL"] = dsn
    else:
        # A rejected/absent target must never leak the caller's database into a subprocess.  The
        # in-memory matrix is safe to run without this variable; PostgreSQL selectors are gated
        # separately in ``main``.
        environment.pop("ENTERPRISE_DATABASE_URL", None)
    return environment


def _run_pytest(name: str, selectors: Iterable[str], *, repeat: int, dsn: str | None) -> list[CheckResult]:
    results: list[CheckResult] = []
    for round_number in range(1, repeat + 1):
        started = time.perf_counter()
        command = [sys.executable, "-m", "pytest", *selectors, "-q", "--tb=short"]
        try:
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=_pytest_environment(dsn),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=600,
                check=False,
            )
            output = (completed.stdout or "") + (completed.stderr or "")
            status = "passed" if completed.returncode == 0 else "failed"
            detail = output.strip().splitlines()[-1] if output.strip() else f"pytest exit {completed.returncode}"
            results.append(
                CheckResult(
                    name,
                    status,
                    time.perf_counter() - started,
                    detail,
                    "" if status == "passed" else _tail(output),
                    round_number,
                )
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                CheckResult(
                    name,
                    "failed",
                    time.perf_counter() - started,
                    "pytest timed out after 600 seconds",
                    _tail(str(exc)),
                    round_number,
                )
            )
        if results[-1].status == "failed":
            # A repeated soak is useful only while every round is healthy; continue collecting
            # the remaining matrix in this round, but do not amplify a known failure.
            break
    return results


def _human_report(report: CanaryReport) -> str:
    lines = [
        "Graph canary/soak",
        f"started={report.started_at}  duration={report.duration_seconds:.2f}s  repeat={report.repeat}",
    ]
    for check in report.checks:
        marker = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}.get(check.status, check.status.upper())
        round_suffix = f" (round {check.repeat})" if check.repeat is not None else ""
        line = f"{marker:<4} {check.name}{round_suffix} [{check.duration_seconds:.2f}s]"
        if check.detail:
            line += f" — {check.detail}"
        lines.append(line)
        if check.status == "failed" and check.output_tail:
            lines.extend("      " + line for line in check.output_tail.splitlines()[-8:])
    lines.append(
        f"result={'FAILED' if not report.ok else ('PARTIAL' if report.skipped else 'PASSED')} "
        f"passed={sum(item.status == 'passed' for item in report.checks)} "
        f"skipped={report.skipped} failed={sum(item.status == 'failed' for item in report.checks)}"
    )
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit only a machine-readable JSON report")
    parser.add_argument("--json-output", type=Path, help="also write the JSON report to this path")
    parser.add_argument("--skip-docker", action="store_true", help="skip Docker service/config/readiness checks")
    parser.add_argument("--require-postgres", action="store_true", help="fail when the isolated PostgreSQL DSN is not configured")
    parser.add_argument("--strict", action="store_true", help="treat any skipped Docker/PostgreSQL gate as a failure")
    parser.add_argument("--allow-remote-postgres", action="store_true", help="allow integration tests against a non-local DSN")
    parser.add_argument("--postgres-dsn", help="isolated PostgreSQL DSN (otherwise ENTERPRISE_DATABASE_URL)")
    parser.add_argument("--ready-url", default="http://127.0.0.1:18000/ready", help="backend readiness URL")
    parser.add_argument("--repeat", type=int, default=1, help="number of sequential canary rounds (default: 1)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.repeat < 1 or args.repeat > 100:
        raise SystemExit("--repeat must be between 1 and 100")
    report = CanaryReport(started_at=_now(), repeat=args.repeat, strict=args.strict)
    report_started = time.perf_counter()

    if args.skip_docker:
        report.checks.append(CheckResult("docker_gate", "skipped", detail="--skip-docker"))
    else:
        report.checks.extend(_check_docker(args.ready_url))

    dsn = args.postgres_dsn or os.getenv("ENTERPRISE_DATABASE_URL")
    if not dsn:
        if args.require_postgres:
            report.checks.append(CheckResult("postgres_target", "failed", detail="ENTERPRISE_DATABASE_URL or --postgres-dsn is required"))
        else:
            report.checks.append(CheckResult("postgres_target", "skipped", detail="no isolated PostgreSQL DSN configured"))
    elif not args.allow_remote_postgres and not _postgres_target_is_local(dsn):
        report.checks.append(
            CheckResult(
                "postgres_target",
                "failed",
                detail="refusing non-local PostgreSQL target; use --allow-remote-postgres only after isolating the database",
            )
        )
    else:
        report.checks.append(CheckResult("postgres_target", "passed", detail="target accepted as isolated canary database"))

    # Do not run a mutating integration test if the target gate failed.  The in-memory matrix is
    # still useful and remains completely isolated from enterprise data.
    integration_allowed = bool(dsn) and (args.allow_remote_postgres or _postgres_target_is_local(dsn))
    for name, selectors in MATRIX:
        if name == "postgres_canonical_transaction" and not integration_allowed:
            report.checks.append(
                CheckResult(
                    name,
                    "skipped",
                    detail="PostgreSQL integration disabled until an accepted isolated DSN is supplied",
                )
            )
            continue
        report.checks.extend(_run_pytest(name, selectors, repeat=args.repeat, dsn=dsn if integration_allowed else None))

    report.duration_seconds = time.perf_counter() - report_started
    report.completed_at = _now()
    payload = json.dumps(report.as_json(), ensure_ascii=False, indent=2)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print(_human_report(report))
        if args.json_output:
            print(f"JSON report: {args.json_output}")
    return 1 if not report.ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

