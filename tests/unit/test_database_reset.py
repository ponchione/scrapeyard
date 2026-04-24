"""Test database reset function."""

import pytest

from scrapeyard.storage.database import init_db, reset_db


@pytest.mark.asyncio
async def test_reset_db_clears_state(tmp_path):
    await init_db(str(tmp_path / "db"))
    from scrapeyard.storage import database

    assert database._default_manager._db_dir is not None
    reset_db()
    assert database._default_manager._db_dir is None
