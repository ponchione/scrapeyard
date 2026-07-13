# Strengthen Authentication and Health Exposure

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

All configured API keys are equivalent full-administration credentials. There is no caller identity, project scope, or audit attribution. `/health` is unauthenticated and can expose dependency, capacity, disk-path, and optional project information. This is acceptable only under the documented trusted-network assumption.

Relevant code:

- `src/scrapeyard/api/middleware.py`
- `src/scrapeyard/main.py`
- `src/scrapeyard/common/settings.py`
- `src/scrapeyard/runtime/health.py`

## Required Outcome

Authentication must identify callers and support least privilege, while public liveness information remains minimal.

## Implementation Scope

1. Replace the unstructured comma-separated key list with named credentials and constant-time secret verification.
2. Define roles/scopes such as submit, read, schedule-admin, delete, and health-detail.
3. Optionally restrict credentials to project namespaces.
4. Attach caller identity to request logs and audit events without logging the key.
5. Split minimal unauthenticated liveness from authenticated readiness/diagnostics.
6. Define key rotation, revocation, and failed-auth monitoring.
7. Preserve a clearly warned local-development mode if no keys are configured.

## Acceptance Criteria

- A read-only credential cannot submit or delete jobs.
- A project-scoped credential cannot access another project.
- Minimal liveness reveals no filesystem paths, project names, or capacity details.
- Detailed readiness remains available to authorized monitoring.
- Rotation can overlap old and new credentials without losing caller identity.

## Verification

- Add middleware and route authorization tests for every role/scope.
- Add timing-insensitive duplicate/malformed header tests.
- Add audit-log redaction tests.
- Run the full API and integration suites.

## Non-Goals

- OAuth/OIDC is optional; a well-defined named-key model is sufficient for this task.
