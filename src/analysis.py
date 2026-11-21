"""Deterministic analysis engine: explains WHY a metric changed between two periods.

Everything here is code, not LLM:
  1. SQL is generated from the semantic layer (segment_dimensions), then validated like any other query.
  2. success_rate change is split into MIX effect and RATE effect per dimension (midpoint method,
     so the two effects add up EXACTLY to the total change).
  3. Drivers are ranked: the biggest mix shift, and the segment whose rate fell more than the rest
     of its dimension ("excess" rate drop), with a significance test.
  4. The top rate driver is drilled into (daily trend + failure reasons).
"""
import math
from statistics import NormalDist

import pandas as pd
import yaml

import db
from validator import validate

SEMANTIC = yaml.safe_load(open("config/semantic_layer.yaml", encoding="utf-8"))
DIMENSIONS = SEMANTIC["segment_dimensions"]
ADDITIVE_METRICS = {"attempts": "attempts", "successful_txns": "successful", "gmv": "gmv"}
SUPPORTED_METRICS = ["success_rate"] + list(ADDITIVE_METRICS)
ALPHA = 0.05                 # 95% confidence


def z_threshold(num_tests):
    """Bonferroni: testing many segments at once, each needs a stricter threshold,
    otherwise ~1 in 20 segments looks 'significant' by pure chance."""
    return NormalDist().inv_cdf(1 - ALPHA / (2 * max(num_tests, 1)))


def two_proportion_z(k0, n0, k1, n1):
    """z-score for the change in a rate k/n between two periods."""
    if n0 == 0 or n1 == 0:
        return 0.0
    p = (k0 + k1) / (n0 + n1)
    se = math.sqrt(p * (1 - p) * (1 / n0 + 1 / n1))
    return (k1 / n1 - k0 / n0) / se if se > 0 else 0.0


class AnalysisError(Exception):
    """Raised when a generated query is blocked or a request is invalid."""


# ---------- SQL building (from the semantic layer) ----------

def quote(value):
    """Safely quote a text value for SQL ('O'Brien' -> 'O''Brien')."""
    return "'" + str(value).replace("'", "''") + "'"


def joins_for(dims):
    """Unique JOIN clauses needed for the given dimensions (same join is added once)."""
    joins = []
    for d in dims:
        if DIMENSIONS[d]["join"] not in joins:
            joins.append(DIMENSIONS[d]["join"])
    return " ".join(joins)


def filter_sql(filters):
    """[{'dimension': 'region', 'value': 'north'}] -> ' AND m.region = 'north''"""
    return "".join(f" AND {DIMENSIONS[f['dimension']]['column']} = {quote(f['value'])}" for f in filters)


def period_window_sql(windows):
    prev, cur = windows["previous"], windows["current"]
    return (f"(DATE(t.txn_ts) BETWEEN {quote(prev[0])} AND {quote(prev[1])} "
            f"OR DATE(t.txn_ts) BETWEEN {quote(cur[0])} AND {quote(cur[1])})")


def segment_sql(dim, windows, filters):
    """One row per period x segment, with the counts needed for any metric."""
    cur = windows["current"]
    dims_needed = [dim] + [f["dimension"] for f in filters]
    return f"""
SELECT CASE WHEN DATE(t.txn_ts) BETWEEN {quote(cur[0])} AND {quote(cur[1])}
            THEN 'current' ELSE 'previous' END AS period,
       COALESCE({DIMENSIONS[dim]['column']}, '(missing)') AS segment,
       COUNT(*) AS attempts,
       SUM(t.status = 'SUCCESS') AS successful,
       SUM(t.status = 'FAILED') AS failed,
       SUM(t.status = 'PENDING') AS pending,
       ROUND(SUM(CASE WHEN t.status = 'SUCCESS' THEN t.amount ELSE 0 END), 2) AS gmv
FROM transactions_clean t {joins_for(dims_needed)}
WHERE {period_window_sql(windows)}{filter_sql(filters)}
GROUP BY period, segment
ORDER BY segment, period""".strip()


def weekly_trend_sql(filters):
    dims_needed = [f["dimension"] for f in filters]
    where = "WHERE 1 = 1" + filter_sql(filters)
    return f"""
SELECT DATE(t.txn_ts, 'weekday 0', '-6 days') AS week_start,
       COUNT(*) AS attempts,
       SUM(t.status = 'SUCCESS') AS successful,
       SUM(t.status = 'FAILED') AS failed,
       SUM(t.status = 'PENDING') AS pending,
       ROUND(100.0 * SUM(t.status = 'SUCCESS') / NULLIF(SUM(t.status IN ('SUCCESS', 'FAILED')), 0), 2) AS success_rate
FROM transactions_clean t {joins_for(dims_needed)}
{where}
GROUP BY week_start
ORDER BY week_start""".strip()


def daily_segment_sql(dim, segment, windows, filters):
    """Daily success rate of one segment vs everything else (drill-down)."""
    col = DIMENSIONS[dim]["column"]
    is_seg = f"COALESCE({col}, '(missing)') = {quote(segment)}"
    dims_needed = [dim] + [f["dimension"] for f in filters]
    return f"""
SELECT DATE(t.txn_ts) AS day,
       CASE WHEN {is_seg} THEN {quote(segment)} ELSE 'all others' END AS segment,
       COUNT(*) AS attempts,
       ROUND(100.0 * SUM(t.status = 'SUCCESS') / NULLIF(SUM(t.status IN ('SUCCESS', 'FAILED')), 0), 2) AS success_rate
FROM transactions_clean t {joins_for(dims_needed)}
WHERE {period_window_sql(windows)}{filter_sql(filters)}
GROUP BY day, segment
ORDER BY day, segment""".strip()


def failure_reason_sql(dim, segment, windows, filters):
    """How the failures of one segment are distributed by reason, per period."""
    cur = windows["current"]
    col = DIMENSIONS[dim]["column"]
    dims_needed = [dim] + [f["dimension"] for f in filters]
    return f"""
SELECT CASE WHEN DATE(t.txn_ts) BETWEEN {quote(cur[0])} AND {quote(cur[1])}
            THEN 'current' ELSE 'previous' END AS period,
       COALESCE(t.failure_reason, '(missing)') AS failure_reason,
       COUNT(*) AS failed
FROM transactions_clean t {joins_for(dims_needed)}
WHERE t.status = 'FAILED' AND COALESCE({col}, '(missing)') = {quote(segment)}
  AND {period_window_sql(windows)}{filter_sql(filters)}
GROUP BY period, failure_reason
ORDER BY period, failed DESC""".strip()


# ---------- Running queries through the SAME safety path as LLM SQL ----------

def run_checked(sql, role, conn, query_id, purpose, log):
    check = validate(sql, role)
    if not check.ok:
        raise AnalysisError(f"{query_id} blocked: {'; '.join(check.errors)}")
    df, seconds = db.run_query(conn, check.sql)
    log.append({"id": query_id, "purpose": purpose, "sql": check.sql, "rows": len(df),
                "seconds": seconds, "warnings": check.warnings, "source": "code"})
    return df


def check_filters(filters, role, conn):
    """Filter values must exist in the data (visible to this role). Prevents typos and injection."""
    for f in filters:
        if f["dimension"] not in DIMENSIONS:
            raise AnalysisError(f"Unknown filter dimension '{f['dimension']}'. "
                                f"Known: {', '.join(DIMENSIONS)}.")
        d = DIMENSIONS[f["dimension"]]
        sql = f"SELECT DISTINCT {d['column']} AS v FROM transactions_clean t {d['join']} LIMIT 1000"
        values = {str(v).lower() for v in pd.read_sql_query(sql, conn)["v"].dropna()}
        if str(f["value"]).lower() not in values:
            raise AnalysisError(f"Value '{f['value']}' for {f['dimension']} is not visible to role "
                                f"'{role}'. Visible values: {', '.join(sorted(values))}.")


# ---------- The maths ----------

def decompose(seg_df):
    """Midpoint mix/rate decomposition of the success-rate change for one dimension.

    For each segment i (shares s and rates r of RESOLVED attempts, 0 = previous, 1 = current):
        mix_i  = (s1 - s0) * ((r0 + r1) / 2 - R)   traffic moved into/out of the segment
        rate_i = (r1 - r0) * (s0 + s1) / 2         the segment itself got better/worse
    where R = average of the overall rates of the two periods.
    Sum over segments of (mix_i + rate_i) = SR1 - SR0 exactly.
    """
    pivot = seg_df.pivot_table(index="segment", columns="period",
                               values=["successful", "failed"], aggfunc="sum", fill_value=0)

    def column(value, period):                    # 0 if a period has no rows at all
        return pivot[(value, period)] if (value, period) in pivot.columns else 0

    t = pd.DataFrame({
        "success_prev": column("successful", "previous"),
        "success_cur": column("successful", "current"),
    })
    t["resolved_prev"] = t["success_prev"] + column("failed", "previous")
    t["resolved_cur"] = t["success_cur"] + column("failed", "current")
    t = t.reset_index().rename(columns={"index": "segment"})
    N0, N1 = t["resolved_prev"].sum(), t["resolved_cur"].sum()
    K0, K1 = t["success_prev"].sum(), t["success_cur"].sum()
    t["share_prev"] = t["resolved_prev"] / N0
    t["share_cur"] = t["resolved_cur"] / N1
    t["rate_prev"] = (t["success_prev"] / t["resolved_prev"]).where(t["resolved_prev"] > 0)
    t["rate_cur"] = (t["success_cur"] / t["resolved_cur"]).where(t["resolved_cur"] > 0)
    # A segment missing in one period: use its other period's rate (so its rate effect is 0)
    t["rate_prev"] = t["rate_prev"].fillna(t["rate_cur"])
    t["rate_cur"] = t["rate_cur"].fillna(t["rate_prev"])

    # Mix uses each segment's rate RELATIVE to the average rate. Shares always sum to 1, so the
    # share changes sum to 0 and subtracting a constant leaves the total unchanged - but now a
    # below-average segment that gains traffic correctly shows a NEGATIVE effect.
    avg_rate = (K0 / N0 + K1 / N1) / 2
    t["mix_effect"] = (t["share_cur"] - t["share_prev"]) * ((t["rate_prev"] + t["rate_cur"]) / 2 - avg_rate)
    t["rate_effect"] = (t["rate_cur"] - t["rate_prev"]) * (t["share_prev"] + t["share_cur"]) / 2
    t["total_effect"] = t["mix_effect"] + t["rate_effect"]

    # Excess rate change vs the rest of this dimension (isolates segment-specific problems)
    excess, z_scores = [], []
    for _, r in t.iterrows():
        rest0 = (K0 - r["success_prev"]) / max(N0 - r["resolved_prev"], 1)
        rest1 = (K1 - r["success_cur"]) / max(N1 - r["resolved_cur"], 1)
        d_seg = r["rate_cur"] - r["rate_prev"]
        d_rest = rest1 - rest0
        avg_share = (r["share_prev"] + r["share_cur"]) / 2
        excess.append((d_seg - d_rest) * avg_share)
        var = 0.0
        for rate, n in [(r["rate_prev"], r["resolved_prev"]), (r["rate_cur"], r["resolved_cur"]),
                        (rest0, N0 - r["resolved_prev"]), (rest1, N1 - r["resolved_cur"])]:
            if n > 0:
                var += rate * (1 - rate) / n
        z_scores.append((d_seg - d_rest) / math.sqrt(var) if var > 0 else 0.0)
    t["excess_rate_effect"] = excess
    t["z_vs_rest"] = z_scores

    overall = {"sr_prev": K0 / N0, "sr_cur": K1 / N1, "resolved_prev": int(N0), "resolved_cur": int(N1),
               "z": two_proportion_z(K0, N0, K1, N1)}
    return t, overall


def to_pp(x):
    """fraction -> percentage points, rounded for display."""
    return round(100 * float(x), 2) + 0.0       # + 0.0 turns -0.0 into 0.0


# ---------- Main entry point ----------

def explain_change(role, windows, metric="success_rate", filters=None, dimensions=None):
    """Explain the change in `metric` between windows['previous'] and windows['current'].

    windows = {"previous": ("YYYY-MM-DD", "YYYY-MM-DD"), "current": (...)}
    filters = [{"dimension": "region", "value": "north"}, ...]
    """
    if metric not in SUPPORTED_METRICS:
        raise AnalysisError(f"Change explanation supports {SUPPORTED_METRICS}, not '{metric}'.")
    filters = filters or []
    filtered_dims = {f["dimension"] for f in filters}
    dimensions = [d for d in (dimensions or list(DIMENSIONS)) if d not in filtered_dims]

    conn = db.get_connection(role)
    try:                                         # always close the connection, even on errors
        return _explain(conn, role, windows, metric, filters, dimensions)
    finally:
        conn.close()


def _explain(conn, role, windows, metric, filters, dimensions):
    check_filters(filters, role, conn)
    log, by_dim, raw_segments = [], {}, {}

    for dim in dimensions:
        df = run_checked(segment_sql(dim, windows, filters), role, conn, f"seg_{dim}",
                         f"{metric} inputs by {dim}, previous vs current period", log)
        if df.empty:
            continue
        raw_segments[dim] = df
        if df["segment"].nunique() < 2:          # e.g. region for a one-region role
            continue
        if metric == "success_rate":
            by_dim[dim], _ = decompose(df)
        else:
            col = ADDITIVE_METRICS[metric]
            wide = df.pivot_table(index="segment", columns="period", values=col,
                                  aggfunc="sum", fill_value=0).reset_index()
            for p in ["previous", "current"]:
                if p not in wide.columns:
                    wide[p] = 0
            wide["change"] = wide["current"] - wide["previous"]
            by_dim[dim] = wide.sort_values("change")

    if not raw_segments:
        raise AnalysisError("No data in the requested periods for this role and filters.")

    any_df = next(iter(raw_segments.values()))
    totals = any_df.groupby("period")[["attempts", "successful", "failed", "pending", "gmv"]].sum()
    for p in ["previous", "current"]:
        if p not in totals.index:
            totals.loc[p] = 0

    result = {"metric": metric, "windows": windows, "filters": filters, "role": role,
              "totals": totals, "by_dimension": by_dim, "queries": log}

    result["weekly_trend"] = run_checked(weekly_trend_sql(filters), role, conn, "weekly_trend",
                                         "weekly success rate over the full data range", log)

    if metric != "success_rate":
        col = ADDITIVE_METRICS[metric]
        result["value_prev"] = float(totals.loc["previous", col])
        result["value_cur"] = float(totals.loc["current", col])
        result["drivers"] = summarise_additive_drivers(by_dim)
        return result

    _, overall = decompose(any_df)
    result.update(sr_prev=overall["sr_prev"], sr_cur=overall["sr_cur"])
    result["drivers"] = summarise_rate_drivers(by_dim, overall)

    rate_driver = result["drivers"].get("rate_driver")
    if rate_driver and rate_driver["significant"]:                 # drill only into real signals
        dim, seg = rate_driver["dimension"], rate_driver["segment"]
        result["drilldown"] = {
            "dimension": dim, "segment": seg,
            "daily": run_checked(daily_segment_sql(dim, seg, windows, filters), role, conn,
                                 "drill_daily", f"daily success rate: {dim} = {seg} vs all others", log),
            "failure_reasons": run_checked(failure_reason_sql(dim, seg, windows, filters), role, conn,
                                           "drill_reasons", f"failure reasons for {dim} = {seg}", log),
        }
    return result


def rest_rate(table, row, period):
    """Success rate of all OTHER segments of the dimension in a period ('prev' or 'cur')."""
    success = table[f"success_{period}"].sum() - row[f"success_{period}"]
    resolved = table[f"resolved_{period}"].sum() - row[f"resolved_{period}"]
    return success / max(resolved, 1)


def summarise_rate_drivers(by_dim, overall):
    """Rank dimensions and segments. Returns plain numbers (percentage points)."""
    delta = overall["sr_cur"] - overall["sr_prev"]
    overall_z = overall["z"]
    dims = []
    for dim, t in by_dim.items():
        dims.append({"dimension": dim, "mix_effect_pp": to_pp(t["mix_effect"].sum()),
                     "rate_effect_pp": to_pp(t["rate_effect"].sum())})
    drivers = {"delta_pp": to_pp(delta), "dimensions": dims,
               "overall_z": round(overall_z, 1),
               "overall_change_significant": bool(abs(overall_z) >= z_threshold(1))}

    # Mix driver: the dimension whose traffic shift explains the most, and its biggest mover
    if dims:
        best = max(dims, key=lambda d: abs(d["mix_effect_pp"]))
        t = by_dim[best["dimension"]]
        top = t.loc[t["mix_effect"].abs().idxmax()]
        drivers["mix_driver"] = {
            "dimension": best["dimension"], "dimension_mix_effect_pp": best["mix_effect_pp"],
            "segment": top["segment"], "segment_mix_effect_pp": to_pp(top["mix_effect"]),
            "share_prev_pct": to_pp(top["share_prev"]), "share_cur_pct": to_pp(top["share_cur"]),
            "rate_prev_pct": to_pp(top["rate_prev"]), "rate_cur_pct": to_pp(top["rate_cur"]),
        }

    # Rate driver: the segment (any dimension) whose rate moved most vs the rest of its dimension
    candidates = []
    for dim, t in by_dim.items():
        for _, r in t.iterrows():
            if r["segment"] == "(missing)":
                continue
            candidates.append((abs(r["excess_rate_effect"]), dim, r))
    if candidates:
        threshold = z_threshold(len(candidates))
        _, dim, r = max(candidates, key=lambda c: c[0])
        drivers["rate_driver"] = {
            "dimension": dim, "segment": r["segment"],
            "excess_rate_effect_pp": to_pp(r["excess_rate_effect"]),
            "rate_prev_pct": to_pp(r["rate_prev"]), "rate_cur_pct": to_pp(r["rate_cur"]),
            "share_cur_pct": to_pp(r["share_cur"]), "z_vs_rest": round(float(r["z_vs_rest"]), 1),
            "rest_rate_prev_pct": to_pp(rest_rate(by_dim[dim], r, "prev")),
            "rest_rate_cur_pct": to_pp(rest_rate(by_dim[dim], r, "cur")),
            "z_threshold": round(threshold, 2), "segments_tested": len(candidates),
            "significant": bool(abs(r["z_vs_rest"]) >= threshold),
        }
    return drivers


def summarise_additive_drivers(by_dim):
    out = {}
    for dim, t in by_dim.items():
        top = t.loc[t["change"].abs().idxmax()]
        out[dim] = {"segment": top["segment"], "change": round(float(top["change"]), 2)}
    return {"top_change_by_dimension": out}
