# Add Real Container and Browser Smoke Tests

> **Status: completed for 0.6.0.** This file is a historical design record;
> its Problem section describes the pre-implementation state. See the
> [archive index](README.md) for completion evidence.

Priority: P1

## Problem

High coverage currently relies heavily on mocked Scrapling and browser behavior. Static Dockerfile checks cannot prove that Playwright Chromium, rebrowser Chromium, Camoufox assets, the hard-coded sandbox path, seccomp requirements, or runtime privilege transitions actually work together.

Relevant code:

- `Dockerfile`
- `docker-compose.yml`
- `src/scrapeyard/engine/browser_debug.py`
- `src/scrapeyard/engine/scraper.py`
- `tests/unit/test_browser_runtime_contract.py`

## Required Outcome

CI or a documented pre-release lane must build the actual image, start it, and execute controlled basic and browser-backed scrapes.

## Implementation Scope

1. Add a deterministic local HTTP fixture site with static, JavaScript-rendered, consent, scroll, load-more, redirect, and blocked-private-link cases.
2. Build the full Docker image from the lock.
3. Start Scrapeyard and Redis using the same security/runtime settings as deployment.
4. Exercise `basic`, standard `dynamic`, dynamic with stealth enabled, and `stealthy` fetchers.
5. Verify sandbox setup, privilege drop, writable mounted directories, screenshots when enabled, and graceful shutdown.
6. Verify unsafe browser subrequests and redirects are blocked.

## Acceptance Criteria

- All advertised fetcher modes launch and extract expected fixture data inside the built container.
- The process runs scraping work as the non-root user after initialization.
- Browser assets and sandbox paths match locked package versions.
- SSRF fixture cases are blocked without reaching the protected fixture endpoint.
- Failures retain useful classified diagnostics.

## Verification

- Run the smoke lane from a clean Docker cache at least once.
- Run it again with warm cache to prove repeatability.
- Record expected resource requirements and duration in `docs/TESTING.md`.

## Non-Goals

- Do not depend on third-party retail sites or anti-bot behavior for pass/fail results.
