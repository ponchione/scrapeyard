# Scrapeyard production-readiness handoff

Date: 2026-07-16

## Release decision

**NO-GO for tagging or publication yet.** The implementation is complete and a
local immutable candidate passed the required security, browser, recovery,
restore, load, and soak gates with no unaccepted Medium, High, or Critical
findings. Publication remains deliberately disabled until the exact reviewed
Git commit is rebuilt and qualified by the `Immutable release candidate`
workflow, the external repository protections below are configured, and the
operator go/no-go steps are approved.

The work is on `agent/production-readiness`. The pushed tip containing this
document is the repository handoff point; obtain its exact identity with
`git rev-parse origin/agent/production-readiness`.

## What changed

- Released the accumulated Unreleased work as version `0.7.0` in package
  metadata and the changelog, with an empty Unreleased section retained.
- Replaced the vulnerable browser stack with Scrapling 0.4.11, Playwright
  1.61.0, Patchright 1.61.2, `cloverlabs-camoufox` 0.6.0, Chromium
  149.0.7827.55 (revision 1228), and Camoufox 150.0.2-beta.25. Local adapters
  preserve basic, dynamic, stealth, Camoufox, screenshot, proxy, debugging,
  sandbox, and connected-IP SSRF behavior.
- Added `security/browser-policy.json`, source-backed expiry/minimum-version
  enforcement, negative tests for Camoufox 135 and Chromium 136.0.7103.25,
  installed-binary inspection, and browser components in the CycloneDX SBOM.
- Made authentication fail closed outside an explicit
  `SCRAPEYARD_ALLOW_UNAUTHENTICATED_LOCAL_DEV` opt-in. Production Compose
  disables it and validates least-privilege API and health credentials.
- Kept cheap `/health/live` liveness and changed production health to the
  authenticated `/health/ready`, with Redis, SQLite, disk, artifact-storage,
  and background-task dependency checks and recovery tests.
- Added OCI source/version/revision/created/documentation labels, package/label
  equality assertions, container provenance inspection, and a build-once
  release workflow that qualifies and optionally promotes the same archived
  image bytes without rebuilding.
- Reworked container security enforcement so every Medium-or-higher finding is
  rejected unless a governed Medium exception supplies an owner, rationale,
  compensating controls, and a short expiry. High and Critical exceptions are
  prohibited.
- Strengthened the secure production preflight for Linux/amd64, AppArmor,
  user namespaces, bridge netfilter, egress proxy/probe settings, encryption
  keys, credentials, one worker process, and one replica. The supported path
  remains `security/deploy-secure-compose.sh`.
- Added protected-main container/browser and quick recovery lanes, scheduled
  browser policy enforcement, pinned Actions, bounded concurrency/retention,
  exact candidate identity checks, and the manual immutable release workflow.
- Updated README and the existing deployment, monitoring, scaling, testing,
  and secret-storage documentation. Intentional single-process/single-replica,
  non-backfilled cron, and at-least-once webhook semantics remain documented.
- Fixed duplicated Scrapling CSS/XPath pseudo-text extraction found by the load
  gate and corrected disk-failure overlay interpolation found by destructive
  qualification.

## Qualified local candidate

This local artifact is retained as implementation evidence, not as the image
to publish after this commit:

- Tag: `scrapeyard:release-candidate-0.7.0-qualified-20260716-r3`
- Image ID:
  `sha256:e7200321c34a308af12f0aaec9685c17c629454bb0e092141474176e1805aac2`
- Platform/size: `linux/amd64`, 3,051,704,904 bytes
- Package version: `0.7.0`
- OCI source revision:
  `cc97cee3d41b51a70fce82c1fd5569b612ab1651+worktree.353500be0ce27c792dcb43b9fe74982e7169f3d2b243619ed1a3de3694bd558d`
- Build time/duration: `2026-07-16T19:04:03Z`, 194 seconds
- Archive: `artifacts/release-final/candidate-image.tar.zst`,
  1,143,562,711 bytes
- Archive SHA-256:
  `a9ec65193e86093bdf29e2094c5f8d1cbd1266d519315cd180927b425b4ea476`
- SBOM SHA-256:
  `0c446aaa1ae075d2633ea11d8266eebda526090d4bc384ab9b28d431128442f5`
- Registry digest: none; the candidate was not published

The OCI revision records the pre-commit implementation tree as the old `main`
tip plus a deterministic worktree fingerprint. This handoff file was added
after qualification, so the release workflow must build the exact pushed
commit and must not promote the local candidate above. Local evidence is under
the ignored `artifacts/release-final/` directory, especially
`candidate-manifest.json`, `qualification-record.json`, `provenance.json`,
`qualification-full/qualification-report.json`, and `final-inventory.txt`.

## Verification completed

- `poetry check --lock`: passed.
- `poetry run ruff check src tests scripts`: passed.
- `poetry run mypy src/scrapeyard`: passed for 96 source files.
- `poetry run python scripts/audit_dependencies.py all`: passed; both locked
  graphs had no known vulnerabilities. The normal pip-audit no-deps/editable
  advisories were recorded.
- `poetry run pytest -W error`: 2,097 passed, 15 isolated live-Redis tests
  skipped, 88.74% coverage, 171.55 seconds.
- `./scripts/run_live_redis_tests.sh`: 15 passed in 6.71 seconds.
- Fresh wheel/sdist build, inspection, and clean-install smoke: passed with 20
  packaged migrations. Wheel SHA-256
  `6fd7c44786cb7ef3cb5f984c36275f49e1131e826090d1153e3c38f1a1c32738`;
  sdist SHA-256
  `743ee63f2c2552628a2e8d485fd468ee3ee1f9e9ed36007e71d72016554dd074`.
- Production, local, smoke, qualification, and secure-deployment Compose
  configuration validation: passed.
- Browser policy audit and negative vulnerable/expired-policy tests: passed.
  Review is dated 2026-07-16 and expires 2026-08-15.
- Browser-aware SBOM/security/configuration scan: 12,245 components; zero
  unaccepted Medium, High, or Critical findings; zero Redis/config findings.
  The 25 accepted Medium package instances across 11 CVEs are owned by the
  Scrapeyard security maintainer, have compensating controls, and expire
  2026-08-15 in `security/container-scan-exceptions.json`.
- Cold browser smoke: passed in 49 seconds wall/37 seconds runtime. Warm browser
  smoke: passed in 50/36 seconds. All four fetchers, screenshots, proxying,
  sandboxing, authentication, and SSRF negative cases were exercised.
- Quick release qualification: passed in 457 seconds. Full no-cache
  qualification: passed in 1,226 seconds including a 900-second soak. Peak
  memory was 246.9 MiB, growth 38.3 MiB, with zero file-descriptor and task
  growth.
- Backup validation and destructive fresh-volume restore/state comparison:
  passed, including Redis persistence and encrypted application state.
- Production preflight: passed using the privileged host helper.
- `git diff --check`: passed. Final inventory found zero test-owned containers,
  networks, volumes, or firewall chains. The production
  `scrapeyard-chromium` AppArmor profile remains enforced; unrelated existing
  Docker resources were preserved.

Two earlier candidates are intentionally disqualified and retained only as
diagnostic evidence: `artifacts/disqualified-9d743c6a` exposed duplicated text
extraction, and `artifacts/disqualified-734c3123` exposed the qualification
disk-injection overlay error. Both defects were fixed before the candidate
above was built.

## Where to resume

1. Configure the `main` branch/ruleset to require pull requests and the nine
   exact checks listed in `docs/TESTING.md`, dismiss stale approvals, require
   resolved threads, and prohibit direct/force pushes.
2. Protect the `production-release` environment with an independent required
   reviewer and prevent self-review.
3. Set production-host `vm.overcommit_memory=1`; the local Redis host reported
   `0`, although persistence, restart, backup, and restore gates all passed.
4. Review the governed Medium exceptions and browser policy before their
   2026-08-15 expiry; replace affected packages when fixed Ubuntu builds exist
   rather than extending acceptance by default.
5. Exercise real encryption-key escrow, run representative staging workloads,
   review reconciliation in dry-run mode, and record any destructive
   reconciliation promotion acknowledgement.
6. After independent review, dispatch the exact pushed commit without
   publication:

   ```bash
   gh workflow run release.yml \
     --ref agent/production-readiness \
     -f promote=false
   ```

   Match the workflow manifest's commit, package version, image ID, archive
   hash, and SBOM hash before approving promotion. A later authorized run may
   use `-f promote=true`; tag creation remains a separate explicit action.

No Git tag, GitHub release, image publication, branch-protection change, or
external approval has been claimed or performed in this handoff.
