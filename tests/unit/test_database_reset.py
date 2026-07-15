"""Tests for complete database teardown and reinitialization."""

import pytest

from scrapeyard.storage.database import close_db, get_db, init_db


@pytest.mark.asyncio
@pytest.mark.parametrize("reinit_dir_name", ["db", "other-db"])
async def test_close_db_closes_connections_and_supports_reinitialization(
    tmp_path,
    reinit_dir_name,
):
    initial_dir = tmp_path / "db"
    await init_db(str(initial_dir))

    from scrapeyard.storage import database

    async with get_db("jobs.db") as connection:
        cursor = await connection.execute("SELECT 1")
        assert (await cursor.fetchone())[0] == 1
        old_connection = connection

    assert database._default_manager._connections == {"jobs.db": old_connection}

    await close_db()

    assert database._default_manager._connections == {}
    assert database._default_manager._db_dir is None
    assert database._default_manager._locks == {}
    with pytest.raises(ValueError, match="no active connection"):
        await old_connection.execute("SELECT 1")

    await init_db(str(tmp_path / reinit_dir_name))
    async with get_db("jobs.db") as new_connection:
        cursor = await new_connection.execute("SELECT 1")
        assert (await cursor.fetchone())[0] == 1

    assert new_connection is not old_connection
