"""Data-quality checks run BEFORE an answer is given. Each finding becomes a caveat.

These are fixed, code-owned SQL queries (not LLM-written). They run on the same read-only,
role-scoped connection, and they are the only code allowed to look at the RAW transactions
table - so the system can tell the user what the clean view removed.
"""
import yaml

import db

ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]
PENDING_WARN_PCT = 1.0       # caveat if more than 1% of attempts are unresolved
NULL_WARN_PCT = 0.5          # caveat if more than 0.5% of rows miss a key field


def _window_sql(windows, col="t.txn_ts"):
    (p0, p1), (c0, c1) = windows["previous"], windows["current"]
    return f"(DATE({col}) BETWEEN '{p0}' AND '{p1}' OR DATE({col}) BETWEEN '{c0}' AND '{c1}')"


def _one(conn, sql):
    return conn.execute(sql).fetchone()


def run_checks(role, windows):
    """Return a list of findings: {check, status ('ok'|'warn'), message, sql}."""
    conn = db.get_connection(role)
    profile = db.data_profile()
    win = _window_sql(windows)
    # Joining merchants scopes every check to the rows this role can see (temp views for regional roles)
    scope = "JOIN merchants m ON t.merchant_id = m.merchant_id"
    findings = []

    def add(check, warn, message, sql):
        findings.append({"check": check, "status": "warn" if warn else "ok", "message": message, "sql": sql})

    # 1. Coverage: requested periods inside the data
    all_dates = list(windows["previous"]) + list(windows["current"])
    outside = [d for d in all_dates if d < profile["start_date"] or d > profile["end_date"]]
    add("coverage", bool(outside),
        f"Requested dates {', '.join(outside)} are outside the data ({profile['start_date']} to "
        f"{profile['end_date']}); those days have no data." if outside else
        f"Both periods are inside the data coverage ({profile['start_date']} to {profile['end_date']}).",
        "SELECT MIN(DATE(txn_ts)), MAX(DATE(txn_ts)) FROM transactions_clean")

    # 2. Duplicates in the raw log
    sql = f"SELECT COUNT(*), COUNT(DISTINCT t.txn_id) FROM main.transactions t {scope} WHERE {win}"
    raw, unique = _one(conn, sql)
    dupes = raw - unique
    add("duplicates", dupes > 0,
        f"{dupes} duplicate rows ({100 * dupes / max(raw, 1):.2f}% of {raw} raw rows, same txn_id) were "
        f"removed before analysis; all figures use deduplicated data." if dupes else
        "No duplicate transaction IDs in the requested periods.", sql)

    # 3. Missing bank_id (these rows drop out of any per-bank breakdown)
    sql = f"SELECT COUNT(*), SUM(t.bank_id IS NULL) FROM transactions_clean t {scope} WHERE {win}"
    total, null_bank = _one(conn, sql)
    pct = 100 * (null_bank or 0) / max(total, 1)
    add("missing_bank_id", pct > NULL_WARN_PCT,
        f"{null_bank} transactions ({pct:.2f}%) have no bank_id; they appear as '(missing)' in bank "
        f"breakdowns and cannot be attributed to a bank.", sql)

    # 4. Missing failure_reason on failed transactions
    sql = (f"SELECT SUM(t.status = 'FAILED'), SUM(t.status = 'FAILED' AND t.failure_reason IS NULL) "
           f"FROM transactions_clean t {scope} WHERE {win}")
    failed, null_reason = _one(conn, sql)
    pct = 100 * (null_reason or 0) / max(failed or 0, 1)
    add("missing_failure_reason", pct > NULL_WARN_PCT,
        f"{null_reason} of {failed} failed transactions ({pct:.1f}%) have no failure_reason; "
        f"failure-reason breakdowns are incomplete.", sql)

    # 5. Pending (unresolved) share - affects the success-rate denominator
    sql = f"SELECT COUNT(*), SUM(t.status = 'PENDING') FROM transactions_clean t {scope} WHERE {win}"
    total, pending = _one(conn, sql)
    pct = 100 * (pending or 0) / max(total, 1)
    add("pending", pct > PENDING_WARN_PCT,
        f"{pending} attempts ({pct:.2f}%) are PENDING and excluded from success rate "
        f"(definition: SUCCESS / (SUCCESS + FAILED)). If they all failed, success rate would be lower.", sql)

    # 6. Two sources: transactions vs settlements (only if this role may read settlements)
    if "settlements" in ROLES[role]["allowed_tables"]:
        c0, c1 = windows["current"]
        sql_txn = (f"SELECT COUNT(*) FROM transactions_clean t WHERE t.status = 'SUCCESS' "
                   f"AND DATE(t.txn_ts) BETWEEN '{c0}' AND '{c1}'")
        sql_raw = (f"SELECT COUNT(*) FROM main.transactions t WHERE t.status = 'SUCCESS' "
                   f"AND DATE(t.txn_ts) BETWEEN '{c0}' AND '{c1}'")
        sql_set = f"SELECT SUM(txn_count) FROM settlements WHERE settlement_date BETWEEN '{c0}' AND '{c1}'"
        # Cutoff 23:30: late payments on the day before the window settle INTO it,
        # late payments on the last day settle AFTER it.
        sql_cut = (f"SELECT SUM(DATE(t.txn_ts) = DATE('{c0}', '-1 day') AND TIME(t.txn_ts) >= '23:30:00'), "
                   f"SUM(DATE(t.txn_ts) = '{c1}' AND TIME(t.txn_ts) >= '23:30:00') "
                   f"FROM transactions_clean t WHERE t.status = 'SUCCESS'")
        clean_n = _one(conn, sql_txn)[0]
        raw_n = _one(conn, sql_raw)[0]
        settled_n = _one(conn, sql_set)[0] or 0
        late_in, late_out = _one(conn, sql_cut)
        expected = clean_n + (late_in or 0) - (late_out or 0)
        explained = expected == settled_n
        add("source_reconciliation", raw_n != settled_n,
            f"Two sources disagree for {c0} to {c1}: the raw transaction log has {raw_n} successful "
            f"payments, settlements show {settled_n}. Deduplicated transactions: {clean_n}. After the "
            f"23:30 settlement cutoff (+{late_in or 0} settled in from the day before, -{late_out or 0} "
            f"settling after the window) the expected settled count is {expected}"
            + (" - fully reconciled." if explained else f" - {settled_n - expected} still unexplained."),
            "; ".join([sql_raw, sql_txn, sql_set, sql_cut]))
    else:
        add("source_reconciliation", False,
            f"Settlement data is outside role '{role}' access; transactions are not reconciled against "
            f"settlements for this answer.", "-")

    conn.close()
    return findings
