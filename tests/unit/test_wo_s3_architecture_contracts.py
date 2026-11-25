"""Architectural and hygiene contract tests for WO-S3."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from scrapeyard.storage import cleanup
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.protocols import JobStore, WebhookOutboxStore
from scrapeyard.storage.webhook_outbox import SQLiteWebhookOutboxStore

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "scrapeyard"
TESTS_ROOT = REPO_ROOT / "tests"


def test_tests_do_not_import_private_scrapeyard_symbols() -> None:
    violations: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("scrapeyard")
            ):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        violations.append(f"{rel}:{node.lineno}: {node.module}.{alias.name}")
    assert violations == []


def test_decomposed_modules_stay_within_size_budget() -> None:
    for rel_path in (
        "queue/worker.py",
        "storage/job_store.py",
        "webhook/dispatcher.py",
    ):
        line_count = len((SRC_ROOT / rel_path).read_text(encoding="utf-8").splitlines())
        assert line_count <= 700, f"{rel_path} has {line_count} lines (limit 700)"


def test_run_cleanup_accepts_typed_cleanup_config() -> None:
    config_cls = getattr(cleanup, "CleanupConfig", None)
    assert config_cls is not None
    params = inspect.signature(cleanup.run_cleanup).parameters
    assert "config" in params


def test_dead_symbols_and_private_regex_internals_removed_from_src() -> None:
    forbidden = (
        "_regex_core",
        "get_job_run_stats",
        "redact_deployment_secrets_in_value",
        "firewall_policy_entries",
    )
    matches: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for token in forbidden:
                if token in line:
                    matches.append(f"{rel}:{lineno}: {token}")
    assert matches == []
    assert not hasattr(SQLiteJobStore, "get_job_run_stats")
    assert not hasattr(JobStore, "get_job_run_stats")
    for dead_method in ("enqueue_delivery", "get_delivery", "list_pending"):
        assert not hasattr(SQLiteWebhookOutboxStore, dead_method)
        assert not hasattr(WebhookOutboxStore, dead_method)
