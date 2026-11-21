"""The SQL validator: every rule, checked in code before any query runs."""
import pytest

from validator import validate

CENTRAL, NORTH = "central_analyst", "regional_manager_north"
SUCCESS_RATE_BY_METHOD = (
    "SELECT pm.method_name, ROUND(100.0 * SUM(t.status = 'SUCCESS') / SUM(t.status IN ('SUCCESS','FAILED')), 2) AS sr "
    "FROM transactions_clean t JOIN payment_methods pm ON t.method_id = pm.method_id GROUP BY pm.method_name")


@pytest.mark.parametrize("sql", [
    SUCCESS_RATE_BY_METHOD,
    "WITH w AS (SELECT DATE(txn_ts) AS d, COUNT(*) AS n FROM transactions_clean GROUP BY d) SELECT d, n FROM w",
])
def test_normal_queries_are_allowed(sql):
    assert validate(sql, CENTRAL).ok


@pytest.mark.parametrize("sql, expected", [
    ("DELETE FROM banks", "Forbidden operation: DELETE"),
    ("DROP TABLE transactions", "Forbidden operation: DROP"),
    ("PRAGMA table_info(banks)", "Forbidden operation: PRAGMA"),
    ("SELECT 1; DROP TABLE banks", "Exactly one SQL statement"),
])
def test_unsafe_statements_are_blocked(sql, expected):
    result = validate(sql, CENTRAL)
    assert not result.ok
    assert any(expected in e for e in result.errors)


def test_raw_table_is_not_readable_by_anyone():
    for role in (CENTRAL, NORTH):
        assert not validate("SELECT COUNT(*) FROM transactions", role).ok


def test_hallucinated_column_is_blocked():
    result = validate("SELECT customer_age FROM transactions_clean", CENTRAL)
    assert result.errors == ["Unknown column 'customer_age'."]


def test_north_manager_cannot_read_settlements():
    result = validate("SELECT SUM(txn_count) FROM settlements", NORTH)
    assert not result.ok and "Access denied" in result.errors[0]


def test_north_manager_cannot_name_another_region():
    sql = ("SELECT COUNT(*) FROM transactions_clean t JOIN merchants m "
           "ON t.merchant_id = m.merchant_id WHERE m.region = 'south'")
    assert not validate(sql, NORTH).ok
    assert validate(sql.replace("south", "north"), NORTH).ok


def test_forbidden_table_inside_a_subquery_is_still_caught():
    sql = "SELECT * FROM (SELECT * FROM settlements) s"
    assert not validate(sql, NORTH).ok


def test_limit_is_added_when_missing():
    result = validate("SELECT * FROM banks", CENTRAL)
    assert result.ok and result.sql.endswith("LIMIT 1000")


def test_huge_limit_is_capped():
    result = validate("SELECT * FROM transactions_clean LIMIT 999999", CENTRAL)
    assert result.ok and result.sql.endswith("LIMIT 1000")


def test_dates_outside_the_data_are_a_warning_not_a_block():
    result = validate("SELECT COUNT(*) AS n FROM transactions_clean "
                      "WHERE DATE(txn_ts) BETWEEN '2025-01-01' AND '2025-01-31'", CENTRAL)
    assert result.ok
    assert any("outside the data coverage" in w for w in result.warnings)


def test_unparseable_sql_is_blocked():
    assert not validate("SELEC FROM WHERE", CENTRAL).ok
