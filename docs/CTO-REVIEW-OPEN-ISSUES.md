# CTO Review: Remaining Issues Requiring R&D

Last updated: 2026-03-20

This memo lists the remaining major issues that should be reviewed before
implementation. The recently fixed items are intentionally omitted:

- scheduled job `enabled` persistence across restart
- duplicate scheduled job conflict handling
- per-run result metadata status accuracy

## 1. Result Delivery Contract Is Not Fully Designed

Current state:

- `GET /results/{job_id}` always returns a JSON envelope
- configured output formats (`markdown`, `html`, `json+markdown`) are not
  exposed through the API as native media types
- `html` format is especially underspecified because the current worker path
  only persists selector-extracted data, not raw page HTML

Why this needs R&D:

- this is not a one-line bug fix; it affects the external API contract
- we need an explicit decision on whether `/results/{job_id}` should:
  - keep returning a JSON wrapper for all formats
  - return native content types directly
  - support both via query parameters or content negotiation
- `json+markdown` also needs a clear retrieval contract:
  - one endpoint returning JSON plus links
  - separate artifact endpoints
  - file download semantics
- `html` mode needs a product decision on what “raw page content” means:
  - first page only vs all pages
  - per-target file layout
  - browser-rendered DOM vs fetched response body

Recommended review questions:

- What should the stable public API contract for non-JSON results be?
- Should artifact retrieval be separated from logical result retrieval?
- Is `html` meant for debugging artifacts, downstream parsing, or both?

## 2. `GET /jobs/{job_id}` Spec Is Broader Than Current Storage/API Model

Current state:

- the endpoint currently returns a lightweight job summary
- the spec describes a richer detail response including:
  - config
  - last run status
  - next scheduled run
  - run history

Why this needs R&D:

- “run history” is not fully defined as a data model or response shape
- exposing scheduler `next_run_at` needs a clear contract between persisted job
  metadata and in-memory APScheduler state
- there is an open design question around whether run history should come from:
  - `results_meta`
  - job rows plus joins
  - a new first-class run table
- once clients consume this endpoint, changing it becomes more expensive

Recommended review questions:

- What exact response shape should `GET /jobs/{job_id}` guarantee?
- Do we want a minimal detail endpoint now, or a fuller run-history model?
- Should run history become a first-class persisted concept?

## 3. Real Browser/System Coverage Still Has Gaps

Current state:

- fast and integration coverage is strong
- live Redis coverage exists
- deterministic end-to-end coverage for the real browser path is still missing

Open gaps:

- real browser system lane with local fixture pages
- browser pagination and `item_selector` end-to-end coverage
- webhook end-to-end transport verification without monkeypatching
- scheduled job real-queue automation
- restart/recovery automation

Why this needs R&D:

- these are not just missing tests; they require a stable system-test harness
- the harness design affects CI runtime, reliability, Docker usage, and local
  developer workflow
- browser-path failures have historically been one of the highest-risk areas

Recommended review questions:

- What test lanes are required for merge blocking vs nightly coverage?
- Should the browser lane run in CI by default or in a scheduled build?
- What level of restart/recovery behavior is considered release-critical?

## 4. Run Model May Need to Be Elevated

Current state:

- jobs track aggregate fields like `last_run_at`, `run_count`, and
  `current_run_id`
- results are indexed in `results_meta`
- errors are stored separately

Why this needs R&D:

- several remaining features point toward a first-class “run” entity:
  - job run history
  - next/last run observability
  - per-run status and artifact references
  - webhook auditability
  - restart/recovery reasoning
- the current split can work, but it may become awkward if we keep layering
  more run-centric features onto job metadata and artifact metadata

Recommended review questions:

- Is the current `jobs` + `results_meta` + `errors` split sufficient long-term?
- Would a dedicated `job_runs` table simplify observability and APIs?
- Should we make that change before expanding `/jobs/{job_id}`?

## Recommendation

The next implementation phase should likely start with a design review rather
than immediate coding. The highest-value topics for CTO review are:

1. Result delivery contract for non-JSON outputs
2. Scope and data model for `GET /jobs/{job_id}` and run history
3. Required system-test lanes for browser, scheduler, and restart behavior
4. Whether Scrapeyard should promote “run” to a first-class persisted model
