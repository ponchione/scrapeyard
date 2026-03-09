"""Integration tests for scheduled job lifecycle."""

from __future__ import annotations

import pytest


def _scheduled_yaml() -> str:
    return """
project: integ
name: scheduled-job
schedule:
  cron: "*/5 * * * *"
  enabled: true
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


@pytest.mark.asyncio
async def test_scheduled_job_create_list_delete(client):
    create_response = await client.post(
        "/jobs",
        content=_scheduled_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert create_response.status_code == 201
    created = create_response.json()

    job_id = created["job_id"]
    assert created["project"] == "integ"
    assert created["name"] == "scheduled-job"
    assert created["schedule"] == "*/5 * * * *"

    list_response = await client.get("/jobs?project=integ")
    assert list_response.status_code == 200
    jobs = list_response.json()
    assert any(j["job_id"] == job_id for j in jobs)

    delete_response = await client.delete(f"/jobs/{job_id}")
    assert delete_response.status_code == 204

    list_after_delete = await client.get("/jobs?project=integ")
    assert list_after_delete.status_code == 200
    jobs_after_delete = list_after_delete.json()
    assert all(j["job_id"] != job_id for j in jobs_after_delete)
