"""Database access: read-only connection, role-based row filtering, query timeout."""
import sqlite3
import time
from pathlib import Path

import pandas as pd
import yaml

DB_PATH = Path("data/payments.db")
ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]
QUERY_TIMEOUT_S = 5


def get_connection(role):
    """Open a READ-ONLY connection. For region-restricted roles, hide other regions' rows."""
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    region = ROLES[role]["region"]
    if region:
        # TEMP views shadow the real ones: this connection can only ever see one region.
        conn.execute(f"CREATE TEMP VIEW merchants AS "
                     f"SELECT * FROM main.merchants WHERE region = '{region}'")
        conn.execute("CREATE TEMP VIEW transactions_clean AS "
                     "SELECT * FROM main.transactions_clean WHERE merchant_id IN "
                     "(SELECT merchant_id FROM temp.merchants)")
    return conn


def run_query(conn, sql, timeout_s=QUERY_TIMEOUT_S):
    """Run SQL with a time limit. Returns (DataFrame, seconds taken)."""
    start = time.time()
    # SQLite calls this every 10,000 steps; returning 1 aborts the query.
    conn.set_progress_handler(lambda: 1 if time.time() - start > timeout_s else 0, 10_000)
    try:
        df = pd.read_sql_query(sql, conn)
    except Exception as e:                       # pandas wraps the SQLite error
        if "interrupted" in str(e):
            raise TimeoutError(f"Query exceeded {timeout_s}s time limit and was stopped.") from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
    return df, round(time.time() - start, 3)