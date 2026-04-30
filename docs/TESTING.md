# Testing

Run the fast checks before merging changes:

```bash
poetry run ruff check src tests
poetry run pytest
```

For queue behavior against a real Redis instance, run:

```bash
./scripts/run_live_redis_tests.sh
```

The live Redis runner starts an isolated Redis container on host port `56379`,
runs the `live_redis` pytest marker, and tears the container down. Normal
`poetry run pytest` executions skip those tests when the isolated Redis
instance is not available.

Useful targeted lanes:

```bash
poetry run pytest tests/unit
poetry run pytest tests/integration
poetry run pytest tests/live_redis
```

The integration tests monkeypatch the worker pool so jobs still exercise the
queue-facing app contract without requiring Redis for the default test suite.
