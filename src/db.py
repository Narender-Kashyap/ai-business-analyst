"""Database access: read-only connection, role-based row filtering, query timeout."""
import sqlite3
import time
from pathlib import Path

import pandas as pd
import yaml

DB_PATH = Path("data/payments.db")
ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]
QUERY_TIMEOUT_S = 5


def ensure_database():
    """Build the seeded synthetic database on first use, so a fresh clone just works."""
    if not DB_PATH.exists():
        import generate_data
        print("data/payments.db not found - generating it (seeded, about 1 second)...")
        generate_data.main()


def get_connection(role):
    """Open a READ-ONLY connection. For region-restricted roles, hide other regions' rows."""
    ensure_database()
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

def data_profile():
    """Facts computed FROM the data (not hardcoded): date coverage and region values."""
    conn = get_connection("central_analyst")
    start, end = conn.execute("SELECT MIN(DATE(txn_ts)), MAX(DATE(txn_ts)) FROM transactions_clean").fetchone()
    regions = [r[0] for r in conn.execute("SELECT DISTINCT region FROM merchants ORDER BY region")]
    conn.close()
    return {"start_date": start, "end_date": end, "regions": regions}
