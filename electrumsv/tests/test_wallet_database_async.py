"""
Test:

* :class:`electrumsv.wallet_database.sqlite_support.SqliteExecutor`.

  * . . .

"""

import os
try:
    # Linux expects the latest package version of 3.34.0 (as of pysqlite-binary 0.4.5)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.34.0 (as of 2021-01-13).
    # Windows builds use the official Python 3.9.1 builds and bundled version of 3.33.0.
    import sqlite3 # type: ignore
import tempfile

import pytest

from electrumsv.wallet_database.sqlite_support import DatabaseContext


def _db_context():
    wallet_path = os.path.join(tempfile.mkdtemp(), "wallet_create")
    assert not os.path.exists(wallet_path)
    return DatabaseContext(wallet_path)


@pytest.fixture
def db_context() -> None:
    value = _db_context()
    yield value
    value.close()



@pytest.mark.asyncio
async def test_executor_propagates_exception(db_context: DatabaseContext) -> None:
    def _test(db: sqlite3.Connection) -> None:
        1/0 # pylint: disable=pointless-statement
    # NOTE: This should only be called from the :mod:`electrumsv.wallet_database` module. Higher
    # level code should call the exposed API methods that SQL only appears in that module.
    with pytest.raises(ZeroDivisionError):
        await db_context.run_in_thread_async(_test)


@pytest.mark.asyncio
async def test_executor_wraps_database_access(db_context: DatabaseContext) -> None:
    def _test(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE FirstTable ("
            "ft_id INTEGER PRIMARY KEY,"
            "ft_name TEXT NOT NULL"
        ")")
        cursor = db.executemany("INSERT INTO FirstTable (ft_name) VALUES (?)",
            [
                ("Alice",), ("Bob",), ("Carol",),
            ])
        return cursor.rowcount
    # NOTE: This should only be called from the :mod:`electrumsv.wallet_database` module. Higher
    # level code should call the exposed API methods that SQL only appears in that module.
    inserts = await db_context.run_in_thread_async(_test)
    assert inserts == 3

