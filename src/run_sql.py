"""Run every query in a .sql file against the database and print the results.

Usage (from project root):  python src/run_sql.py sql/verify_story.sql
"""
import sqlite3
import sys

import pandas as pd

DB_PATH = "data/payments.db"


def main():
    sql_file = sys.argv[1]
    with open(sql_file, encoding="utf-8") as f:
        text = f.read()

    with sqlite3.connect(DB_PATH) as conn:
        for statement in text.split(";"):
            lines = [ln for ln in statement.strip().splitlines() if ln.strip()]
            code = [ln for ln in lines if not ln.strip().startswith("--")]
            if not code:
                continue                      # skip empty / comment-only pieces
            title = lines[0] if lines[0].strip().startswith("--") else ""
            print("\n" + title)
            print(pd.read_sql_query(statement, conn).to_string(index=False))


if __name__ == "__main__":
    main()