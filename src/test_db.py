"""Checks for db.py: read-only, row-level security, timeout.

Run:  python src/test_db.py
"""
import db

# 1. Row-level security: each role sees different regions
for role in ["central_analyst", "regional_manager_north"]:
    conn = db.get_connection(role)
    df, _ = db.run_query(conn, "SELECT m.region, COUNT(*) AS n FROM transactions_clean t "
                               "JOIN merchants m ON t.merchant_id = m.merchant_id GROUP BY m.region")
    print(f"{role}: {dict(zip(df['region'], df['n']))}")

# 2. Read-only: a write must fail even though nothing else stops it here
conn = db.get_connection("central_analyst")
try:
    conn.execute("DELETE FROM banks")
    print("WRITE ALLOWED (bad!)")
except Exception as e:
    print("Write blocked:", e)

# 3. Timeout: an expensive cross join is stopped
try:
    db.run_query(conn, "SELECT COUNT(*) FROM transactions a, transactions b", timeout_s=2)
except TimeoutError as e:
    print("Timeout works:", e)