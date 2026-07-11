# Harden and Reproduce the Container Deployment

Priority: P1

## Problem

The local Compose deployment publishes `8420` on all interfaces, relaxes seccomp, and starts the container as root to repair ownership and a setuid browser sandbox before dropping privileges. Base images and OS packages use floating tags, and the Chromium sandbox path is tied to a package revision. Application URL checks also remain vulnerable to DNS validation/connection races without network enforcement.

Relevant files:

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`
- `docs/DEPLOYMENT.md`
- `src/scrapeyard/engine/url_guard.py`

## Required Outcome

The production deployment must be reproducible, least-privileged, private by default, and protected by enforceable ingress/egress policy.

## Implementation Scope

1. Separate local peer-container convenience from a production Compose/orchestrator profile that exposes no public host port.
2. Pin base images and critical runtime assets by digest or otherwise record reproducible versions.
3. Remove hard-coded browser revision paths where package/runtime discovery can provide them safely.
4. Minimize or eliminate runtime root ownership repair; document any unavoidable setuid sandbox requirement.
5. Replace broad `seccomp:unconfined` with the narrowest tested profile possible.
6. Add explicit filesystem, capability, resource, and process limits.
7. Provide enforceable egress examples blocking metadata, loopback, link-local, and private networks while allowing required proxy/Redis traffic.
8. Add image scanning and SBOM generation to release checks.

## Acceptance Criteria

- The production profile is unreachable from untrusted networks by default.
- Browser modes work under the documented least-privilege security profile.
- Dependency updates cannot silently invalidate a hard-coded sandbox path.
- Image rebuilds identify pinned inputs and produce an SBOM.
- Network controls block DNS-rebinding attempts from reaching private targets.

## Verification

- Run full container/browser smoke tests under the hardened profile.
- Verify host and peer-container ingress behavior explicitly.
- Test metadata/private target blocking at the network layer.
- Run image vulnerability and configuration scans.

## Non-Goals

- Application URL guards remain useful defense in depth and must not be removed.
