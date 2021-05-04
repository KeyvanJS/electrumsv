import json
try:
    # Linux expects the latest package version of 3.34.0 (as of pysqlite-binary 0.4.5)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.34.0 (as of 2021-01-13).
    # Windows builds use the official Python 3.9.1 builds and bundled version of 3.33.0.
    import sqlite3 # type: ignore
import time

MIGRATION = 25

def execute(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS Invoices ("
        "invoice_id INTEGER PRIMARY KEY,"
        "account_id INTEGER NOT NULL,"
        "tx_hash BLOB DEFAULT NULL,"
        "payment_uri TEXT NOT NULL,"
        "description TEXT NULL,"
        "invoice_flags INTEGER NOT NULL,"
        "value INTEGER NOT NULL,"
        "invoice_data BLOB NOT NULL,"
        "date_expires INTEGER DEFAULT NULL,"
        "date_created INTEGER NOT NULL,"
        "date_updated INTEGER NOT NULL,"
        "FOREIGN KEY (account_id) REFERENCES Accounts (account_id),"
        "FOREIGN KEY (tx_hash) REFERENCES Transactions (tx_hash)"
    ")")

    # The unique constraint is also required for any upsert operation to work.
    # But really we added it to prevent people from making duplicate invoices.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_Invoices_unique ON Invoices(payment_uri)")

    date_updated = int(time.time())
    conn.execute("UPDATE WalletData SET value=?, date_updated=? WHERE key=?",
        [json.dumps(MIGRATION),date_updated,"migration"])
