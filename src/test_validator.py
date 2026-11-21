"""Quick checks that the validator blocks unsafe SQL and allows good SQL.

Run:  python src/test_validator.py
"""
from validator import validate

CASES = [
    # (description, role, sql, should_pass)
    ("normal success-rate query", "central_analyst",
     "SELECT pm.method_name, ROUND(100.0 * SUM(t.status = 'SUCCESS') / SUM(t.status IN ('SUCCESS','FAILED')), 2) AS sr "
     "FROM transactions_clean t JOIN payment_methods pm ON t.method_id = pm.method_id GROUP BY pm.method_name", True),
    ("CTE query", "central_analyst",
     "WITH w AS (SELECT DATE(txn_ts) AS d, COUNT(*) AS n FROM transactions_clean GROUP BY d) SELECT d, n FROM w", True),
    ("delete statement", "central_analyst", "DELETE FROM banks", False),
    ("drop table", "central_analyst", "DROP TABLE transactions", False),
    ("two statements (injection)", "central_analyst", "SELECT 1; DROP TABLE banks", False),
    ("pragma", "central_analyst", "PRAGMA table_info(banks)", False),
    ("raw table not allowed", "central_analyst", "SELECT COUNT(*) FROM transactions", False),
    ("hallucinated column", "central_analyst", "SELECT customer_age FROM transactions_clean", False),
    ("north manager reads settlements", "regional_manager_north", "SELECT SUM(txn_count) FROM settlements", False),
    ("north manager asks about south", "regional_manager_north",
     "SELECT COUNT(*) FROM transactions_clean t JOIN merchants m ON t.merchant_id = m.merchant_id WHERE m.region = 'south'", False),
    ("north manager, own region", "regional_manager_north",
     "SELECT m.category, COUNT(*) AS n FROM transactions_clean t JOIN merchants m ON t.merchant_id = m.merchant_id GROUP BY m.category", True),
    ("huge limit gets capped", "central_analyst", "SELECT * FROM transactions_clean LIMIT 999999", True),
]

passed = 0
for desc, role, sql, should_pass in CASES:
    result = validate(sql, role)
    correct = result.ok == should_pass
    passed += correct
    mark = "PASS" if correct else "FAIL"
    detail = result.errors or result.warnings
    print(f"[{mark}] {desc:35s} -> {'allowed' if result.ok else 'blocked'} {detail}")

print(f"\n{passed}/{len(CASES)} checks behaved as expected")