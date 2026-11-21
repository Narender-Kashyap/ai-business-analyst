"""Database layer: read-only, row-level security, timeout, facts read from the data."""
import pytest

import db

REGION_COUNT_SQL = ("SELECT m.region, COUNT(*) AS n FROM transactions_clean t "
                    "JOIN merchants m ON t.merchant_id = m.merchant_id GROUP BY m.region")


def test_central_analyst_sees_every_region():
    df, _ = db.run_query(db.get_connection("central_analyst"), REGION_COUNT_SQL)
    assert set(df["region"]) == {"east", "north", "south", "west"}
    assert df["n"].sum() == 84_000


def test_north_manager_sees_only_north_rows():
    df, _ = db.run_query(db.get_connection("regional_manager_north"), REGION_COUNT_SQL)
    assert list(df["region"]) == ["north"]
    assert df["n"].sum() == 21_422


def test_connection_is_read_only():
    conn = db.get_connection("central_analyst")
    with pytest.raises(Exception, match="readonly"):
        conn.execute("DELETE FROM banks")


def test_slow_query_is_stopped_by_the_timeout():
    conn = db.get_connection("central_analyst")
    with pytest.raises(TimeoutError):
        db.run_query(conn, "SELECT COUNT(*) FROM transactions a, transactions b", timeout_s=1)


def test_data_profile_is_read_from_the_data():
    p = db.data_profile()
    assert (p["start_date"], p["end_date"]) == ("2026-09-21", "2026-11-15")
    assert p["regions"] == ["east", "north", "south", "west"]


def test_clean_view_removes_exactly_the_duplicates():
    conn = db.get_connection("central_analyst")
    raw = conn.execute("SELECT COUNT(*) FROM main.transactions").fetchone()[0]
    clean = conn.execute("SELECT COUNT(*) FROM transactions_clean").fetchone()[0]
    assert (raw, clean) == (84_420, 84_000)
