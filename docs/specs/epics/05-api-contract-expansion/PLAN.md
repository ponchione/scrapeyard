# Epic 5: API Contract Expansion — Execution Plan

## Wave 1 (sequential): 01 → 02 → 03 → 04 → 05

All tasks modify `src/scrapeyard/api/routes.py` — must be sequential or done as a single pass.

| Order | Task | Endpoint | Spec |
|-------|------|----------|------|
| 01 | Expand job detail response | `GET /jobs/{job_id}` | §4.1, §8.1 |
| 02 | Update jobs list response | `GET /jobs` | §4.2, §8.2 |
| 03 | Update results endpoint | `GET /results/{job_id}` | §4.3, §8.3 |
| 04 | Update errors endpoint | `GET /errors` | §4.4, §8.4 |
| 05 | Add trigger to scrape enqueue | `POST /scrape` | §8.5 |
