# Fix POST /scrape UNIQUE Constraint Collision — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent 500 IntegrityError when the same YAML config is submitted to `POST /scrape` more than once, by appending a short UUID suffix to ad-hoc job names.

**Architecture:** Single-line change in the `scrape()` route handler appends `-{uuid4.hex[:8]}` to `config.name` for ad-hoc jobs. Scheduled jobs via `POST /jobs` are unaffected. New integration test validates the fix.

**Tech Stack:** Python, FastAPI, SQLite, pytest, httpx (test client)

**Spec:** `docs/superpowers/specs/2026-03-15-fix-adhoc-unique-constraint-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/scrapeyard/api/routes.py` | Modify | Append short UUID suffix to ad-hoc job name |
| `tests/integration/test_scrape_lifecycle.py` | Modify | Add duplicate-submission integration test |

No new files created.

---

## Chunk 1: Implementation

### Task 1: Write the failing integration test

**Files:**
- Modify: `tests/integration/test_scrape_lifecycle.py`

- [ ] **Step 1: Write the failing test**

Add this test at the end of `tests/integration/test_scrape_lifecycle.py`:

```python
@pytest.mark.asyncio
async def test_duplicate_adhoc_scrape_does_not_collide(client, monkeypatch):
    """Submitting the same ad-hoc config twice must not hit UNIQUE constraint."""
    import re

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    yaml_config = _async_scrape_yaml()

    resp1 = await client.post(
        "/scrape",
        content=yaml_config,
        headers={"content-type": "application/x-yaml"},
    )
    assert resp1.status_code in (200, 202), f"First submission failed: {resp1.status_code}"

    resp2 = await client.post(
        "/scrape",
        content=yaml_config,
        headers={"content-type": "application/x-yaml"},
    )
    assert resp2.status_code in (200, 202), f"Second submission failed: {resp2.status_code}"

    job_id_1 = resp1.json()["job_id"]
    job_id_2 = resp2.json()["job_id"]
    assert job_id_1 != job_id_2, "Two submissions should produce different job IDs"

    # Verify each job has a suffixed name
    job1_resp = await client.get(f"/jobs/{job_id_1}")
    job2_resp = await client.get(f"/jobs/{job_id_2}")
    assert job1_resp.status_code == 200
    assert job2_resp.status_code == 200

    name1 = job1_resp.json()["name"]
    name2 = job2_resp.json()["name"]
    assert name1 != name2, "Two ad-hoc jobs from same config must have different names"

    suffix_pattern = re.compile(r"^async-scrape-[0-9a-f]{8}$")
    assert suffix_pattern.match(name1), f"Name {name1!r} doesn't match expected suffix pattern"
    assert suffix_pattern.match(name2), f"Name {name2!r} doesn't match expected suffix pattern"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_scrape_lifecycle.py::test_duplicate_adhoc_scrape_does_not_collide -v`

Expected: FAIL — second `POST /scrape` returns 500 due to IntegrityError (or the name assertions fail because no suffix is appended).

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/integration/test_scrape_lifecycle.py
git commit -m "test: add failing test for duplicate ad-hoc scrape collision (WO-000)"
```

---

### Task 2: Implement the fix

**Files:**
- Modify: `src/scrapeyard/api/routes.py` — the `name=config.name` line inside the `Job(...)` constructor in `scrape()`

- [ ] **Step 4: Apply the one-line fix**

In `src/scrapeyard/api/routes.py`, inside the `scrape()` function, change the `Job(...)` constructor from:

```python
    job = Job(
        job_id=str(uuid.uuid4()),
        project=config.project,
        name=config.name,
        config_yaml=config_yaml,
    )
```

To:

```python
    job = Job(
        job_id=str(uuid.uuid4()),
        project=config.project,
        name=f"{config.name}-{uuid.uuid4().hex[:8]}",
        config_yaml=config_yaml,
    )
```

Note: `uuid` is already imported on line 4 of this file. No new imports needed.

- [ ] **Step 5: Run the new test to verify it passes**

Run: `python -m pytest tests/integration/test_scrape_lifecycle.py::test_duplicate_adhoc_scrape_does_not_collide -v`

Expected: PASS

- [ ] **Step 6: Run the full test suite to verify no regressions**

Run: `python -m pytest tests/ -v`

Expected: All existing tests pass. Pay attention to:
- `test_scrape_lifecycle_eventually_returns_results` — still passes (ad-hoc scrape works)
- `test_errors_are_recorded_on_failed_scrape` — still passes (error flow unchanged)
- Any unit tests in `tests/unit/` — still pass

- [ ] **Step 7: Commit the fix**

```bash
git add src/scrapeyard/api/routes.py
git commit -m "fix: append short UUID suffix to ad-hoc job names to prevent UNIQUE collision (WO-000)"
```

---

### Task 3: Verify scheduled jobs are unaffected

- [ ] **Step 8: Manual verification**

Confirm that `POST /jobs` in `routes.py` still uses `name=config.name` without any suffix. This is a read-only check — no code changes.

The `create_job()` handler at line 160 should still have:
```python
    job = Job(
        job_id=str(uuid.uuid4()),
        project=config.project,
        name=config.name,
        config_yaml=config_yaml,
        schedule_cron=config.schedule.cron,
    )
```

---

## Done

After all steps pass, WO-000 is complete. The next work order in sequence is WO-001 (WebhookConfig schema).
