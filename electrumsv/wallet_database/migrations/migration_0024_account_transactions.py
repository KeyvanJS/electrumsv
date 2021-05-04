import json
try:
    # Linux expects the latest package version of 3.34.0 (as of pysqlite-binary 0.4.5)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.34.0 (as of 2021-01-13).
    # Windows builds use the official Python 3.9.1 builds and bundled version of 3.33.0.
    import sqlite3 # type: ignore
import time

MIGRATION = 24

def execute(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE VIEW IF NOT EXISTS AccountTransactions (account_id, tx_hash) AS "
    "SELECT DISTINCT KI.account_id, TD.tx_hash FROM TransactionDeltas TD "
    "INNER JOIN KeyInstances KI USING(keyinstance_id)")

    # Switch the state constants over from a value to flags.
    conn.execute("UPDATE PaymentRequests SET state=8 WHERE state=3")
    conn.execute("UPDATE PaymentRequests SET state=4 WHERE state=2")
    conn.execute("UPDATE PaymentRequests SET state=2 WHERE state=1")
    conn.execute("UPDATE PaymentRequests SET state=1 WHERE state=0")

    date_updated = int(time.time())
    conn.execute("UPDATE WalletData SET value=?, date_updated=? WHERE key=?",
        [json.dumps(MIGRATION),date_updated,"migration"])
