"""Build the structured answer: FACTS (code) -> HYPOTHESES + RECOMMENDATIONS (LLM) -> number check (code).

Every number shown to the user comes from a fact computed from an executed query.
The LLM may only phrase explanations around those facts, citing fact IDs; any number it
writes that is not in the facts is flagged as unverified.
"""
import json
import math
import numbers
import re

import pandas as pd
import sqlglot
from sqlglot import exp

from llm import generate_json

DIM_TABLE = {"payment_method": "payment_methods", "bank": "banks",
             "merchant_category": "merchants", "region": "merchants"}
ANOMALY_GAP_PP = 10          # a day is 'anomalous' if the segment is >10 pts below all others


def _fmt(n):
    return f"{n:,.0f}" if abs(n) >= 1000 else f"{n:g}"


def _filters_text(filters):
    return ", ".join(f"{f['dimension']} = {f['value']}" for f in filters) or "none"


def _window_text(windows):
    p, c = windows["previous"], windows["current"]
    return f"previous {p[0]} to {p[1]} vs current {c[0]} to {c[1]}"


# ---------- FACTS (deterministic) ----------

def facts_for_change(result):
    w, filt = result["windows"], result["filters"]
    base = {"filters": _filters_text(filt), "window": _window_text(w)}
    facts = []

    def add(text, query_ids, dataset):
        facts.append({"id": f"F{len(facts) + 1}", "text": text, "query_ids": query_ids,
                      "dataset": dataset, **base})

    t = result["totals"]
    if result["metric"] != "success_rate":
        metric = result["metric"]
        prev, cur = result["value_prev"], result["value_cur"]
        add(f"{metric} changed from {_fmt(round(prev, 2))} to {_fmt(round(cur, 2))} "
            f"({_fmt(round(cur - prev, 2))}).", ["seg_payment_method"], "transactions_clean")
        for dim, info in result["drivers"]["top_change_by_dimension"].items():
            add(f"By {dim}, the largest change in {metric} was {info['segment']} ({_fmt(info['change'])}).",
                [f"seg_{dim}"], f"transactions_clean + {DIM_TABLE[dim]}")
        return facts

    d = result["drivers"]
    n0 = int(t.loc["previous", "successful"] + t.loc["previous", "failed"])
    n1 = int(t.loc["current", "successful"] + t.loc["current", "failed"])
    sig = "statistically significant" if d["overall_change_significant"] else "NOT statistically significant (could be random variation)"
    add(f"Success rate moved from {round(100 * result['sr_prev'], 2)}% ({_fmt(n0)} resolved attempts) to "
        f"{round(100 * result['sr_cur'], 2)}% ({_fmt(n1)} resolved attempts): {d['delta_pp']} percentage points; "
        f"z = {d['overall_z']}, {sig}.", ["seg_payment_method"], "transactions_clean")

    for dd in d["dimensions"]:
        add(f"Split by {dd['dimension']}: mix effect {dd['mix_effect_pp']} pp + rate effect "
            f"{dd['rate_effect_pp']} pp = {round(dd['mix_effect_pp'] + dd['rate_effect_pp'], 2)} pp.",
            [f"seg_{dd['dimension']}"], f"transactions_clean + {DIM_TABLE[dd['dimension']]}")

    m = d.get("mix_driver")
    if m:
        add(f"Largest mix shift: {m['segment']} ({m['dimension']}) went from {m['share_prev_pct']}% to "
            f"{m['share_cur_pct']}% of resolved attempts; its success rate was {m['rate_prev_pct']}% then "
            f"{m['rate_cur_pct']}%. This traffic shift alone contributed {m['segment_mix_effect_pp']} pp.",
            [f"seg_{m['dimension']}"], f"transactions_clean + {DIM_TABLE[m['dimension']]}")

    r = d.get("rate_driver")
    if r:
        verdict = (f"significant (|z| = {abs(r['z_vs_rest'])} > threshold {r['z_threshold']} "
                   f"for {r['segments_tested']} segments tested)") if r["significant"] else \
                  (f"not significant (|z| = {abs(r['z_vs_rest'])} < threshold {r['z_threshold']} "
                   f"for {r['segments_tested']} segments tested) - likely noise")
        add(f"Largest segment-specific rate change: {r['segment']} ({r['dimension']}) went from "
            f"{r['rate_prev_pct']}% to {r['rate_cur_pct']}%, while all other {r['dimension']} segments went from "
            f"{r['rest_rate_prev_pct']}% to {r['rest_rate_cur_pct']}%. Excess effect {r['excess_rate_effect_pp']} pp; "
            f"{verdict}.", [f"seg_{r['dimension']}"], f"transactions_clean + {DIM_TABLE[r['dimension']]}")

    dd = result.get("drilldown")
    if dd:
        daily = dd["daily"].pivot_table(index="day", columns="segment", values="success_rate")
        seg = dd["segment"]
        if seg in daily.columns and "all others" in daily.columns:
            gap = daily["all others"] - daily[seg]
            bad = gap[gap > ANOMALY_GAP_PP]
            if len(bad):
                days = "; ".join(f"{day}: {daily.loc[day, seg]}% vs {daily.loc[day, 'all others']}%"
                                 for day in bad.index)
                add(f"{len(bad)} day(s) where {seg} was more than {ANOMALY_GAP_PP} pts below all others: {days}.",
                    ["drill_daily"], f"transactions_clean + {DIM_TABLE[dd['dimension']]}")
            else:
                add(f"No single day where {seg} was more than {ANOMALY_GAP_PP} pts below all others; "
                    f"the drop is spread across the period.", ["drill_daily"],
                    f"transactions_clean + {DIM_TABLE[dd['dimension']]}")
        fr = dd["failure_reasons"]
        cur = fr[fr["period"] == "current"]
        if len(cur):
            top = cur.iloc[0]
            prev_same = fr[(fr["period"] == "previous") & (fr["failure_reason"] == top["failure_reason"])]["failed"].sum()
            add(f"Top failure reason for {seg} in the current period: {top['failure_reason']} with "
                f"{int(top['failed'])} of {int(cur['failed'].sum())} failures (previous period: {int(prev_same)}).",
                ["drill_reasons"], f"transactions_clean + {DIM_TABLE[dd['dimension']]}")

    add(f"PENDING attempts (excluded from success rate): previous {int(t.loc['previous', 'pending'])}, "
        f"current {int(t.loc['current', 'pending'])}.", ["seg_payment_method"], "transactions_clean")
    return facts


def where_clause(sql):
    """The filters a query actually applied = its WHERE clause, read from the validated SQL."""
    try:
        where = sqlglot.parse_one(sql, read="sqlite").find(exp.Where)
        return where.this.sql(dialect="sqlite") if where else "none (all rows)"
    except Exception:
        return "see SQL"


def facts_for_tables(query_results):
    """Ad-hoc questions: each executed query's result becomes a fact (first rows shown).

    Dataset = tables the validated SQL read; filters and time window = its WHERE clause.
    """
    facts = []
    for q, df in query_results:
        if df is None:
            continue
        preview = df.head(15)
        rows = "; ".join(", ".join(f"{c}={v}" for c, v in row.items()) for _, row in preview.iterrows())
        more = f" (showing 15 of {len(df)} rows)" if len(df) > 15 else ""
        where = where_clause(q["sql"])
        facts.append({"id": f"F{len(facts) + 1}", "text": f"{q['purpose']}: {rows}{more}",
                      "query_ids": [q["id"]], "dataset": ", ".join(q.get("tables", [])) or "see SQL",
                      "filters": where, "window": where if "txn_ts" in where or "_date" in where
                      else "all dates in the data", "table": df})
    return facts


# ---------- NARRATIVE (LLM, with deterministic fallback) ----------

NARRATIVE_PROMPT = """You are a payments business analyst writing for a business leader.

Question: {question}
Interpretation: {interpretation}

FACTS (computed by code from executed SQL - the only numbers you may use):
{facts}

CAVEATS:
{caveats}

Write:
- "headline": one sentence answering the question.
- "hypotheses": 1-3 plausible business explanations consistent with the facts. These are NOT proven;
  say what would confirm each. Cite supporting fact IDs.
- "recommendations": 1-3 concrete next steps (who should check what). Cite fact IDs.
Rules: use ONLY numbers that appear in the facts (you may round them). Never invent numbers, dates,
causes presented as facts, or data not shown. If the change is not statistically significant, say so.

Output JSON only:
{{"headline": "...",
  "hypotheses": [{{"text": "...", "evidence": ["F1"], "confidence": "high|medium|low", "would_confirm": "..."}}],
  "recommendations": [{{"text": "...", "evidence": ["F2"]}}]}}
"""


def llm_narrative(question, plan, facts, caveats):
    prompt = NARRATIVE_PROMPT.format(
        question=question, interpretation=plan.get("interpretation", ""),
        facts="\n".join(f"{f['id']}: {f['text']}" for f in facts),
        caveats="\n".join(f"- {c}" for c in caveats) or "- none")
    llm = generate_json(prompt)
    text = llm["text"].strip().strip("`")
    text = text[text.find("{"):]
    return normalise_narrative(json.loads(text)), llm


def normalise_narrative(raw):
    """Force the model's JSON into the expected shape, so odd output can't break later steps."""
    def items(key):
        out = []
        values = raw.get(key) or []
        if isinstance(values, (str, dict)):           # a single item instead of a list
            values = [values]
        for x in values:
            if isinstance(x, str):
                x = {"text": x}
            if isinstance(x, dict) and x.get("text"):
                evidence = x.get("evidence") or []
                if isinstance(evidence, str):            # "F1" -> ["F1"], not ["F", "1"]
                    evidence = [evidence]
                x["evidence"] = [str(e) for e in evidence]
                out.append(x)
        return out
    return {"headline": str(raw.get("headline") or ""), "hypotheses": items("hypotheses"),
            "recommendations": items("recommendations")}


def fallback_narrative(result):
    """Used when the LLM is unavailable: templated wording built only from driver numbers."""
    if not result or result.get("metric") != "success_rate":
        return {"headline": "Results computed; narrative unavailable (language model not reachable).",
                "hypotheses": [], "recommendations": []}
    d = result["drivers"]
    hyps, recs = [], []
    if d.get("mix_driver") and d["mix_driver"]["segment_mix_effect_pp"] < 0:
        m = d["mix_driver"]
        hyps.append({"text": f"Part of the change is a traffic-mix shift towards {m['segment']}, which has a "
                             f"below-average success rate.", "evidence": ["F1"], "confidence": "high",
                     "would_confirm": f"Check why {m['segment']} traffic grew (campaigns, merchant changes)."})
        recs.append({"text": f"Investigate the source of the {m['segment']} traffic increase.", "evidence": []})
    if d.get("rate_driver") and d["rate_driver"]["significant"]:
        r = d["rate_driver"]
        hyps.append({"text": f"{r['segment']} had a segment-specific problem beyond the general trend.",
                     "evidence": [], "confidence": "high",
                     "would_confirm": f"Confirm incident logs with {r['segment']}."})
        recs.append({"text": f"Raise the {r['segment']} failure pattern with the {r['dimension']} partner.",
                     "evidence": []})
    return {"headline": f"Success rate changed by {d['delta_pp']} pp (narrative generated without the "
                        f"language model).", "hypotheses": hyps, "recommendations": recs}


# ---------- NUMBER CHECK (deterministic) ----------

NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FACT_ID = re.compile(r"\bF\d+\b")


def numbers_in(text):
    text = FACT_ID.sub(" ", DATE.sub(" ", text))
    out = []
    for token in NUMBER.findall(text):
        token = token.replace(",", "").rstrip(".")
        try:
            out.append(float(token))
        except ValueError:
            pass
    return out


def allowed_numbers(facts, extra_texts=()):
    """Every number computed by code: fact text, fact windows/filters, and caveats (data-quality checks)."""
    allowed = set()
    texts = [f"{f['text']} {f.get('window', '')} {f.get('filters', '')}" for f in facts] + list(extra_texts)
    for text in texts:
        for n in numbers_in(text):
            allowed.add(n)
        # Dates may be written in words by the LLM ("11th and 12th November"):
        # allow their year, month and day numbers too.
        for d in DATE.findall(text):
            allowed.update(float(part) for part in d.split("-"))
    for f in facts:
        if "table" in f:
            for v in f["table"].head(50).to_numpy().ravel():
                if isinstance(v, numbers.Number) and not pd.isna(v):
                    allowed.add(float(v))
    # Simple derived numbers people naturally write: absolute values, roundings, truncation
    expanded = set()
    for a in allowed:
        for x in (a, abs(a)):
            expanded.update({x, round(x), round(x, 1), round(x, 2), math.floor(x), math.ceil(x)})
    return expanded


def check_numbers(narrative, facts, caveats=()):
    """Return a list of numbers in the LLM's text that were NOT computed by code (facts or caveats)."""
    allowed = allowed_numbers(facts, caveats)
    fact_ids = {f["id"] for f in facts}
    problems = []
    items = [("headline", narrative.get("headline", ""))]
    items += [(f"hypothesis {i + 1}", h.get("text", "") + " " + h.get("would_confirm", ""))
              for i, h in enumerate(narrative.get("hypotheses", []))]
    items += [(f"recommendation {i + 1}", r.get("text", "")) for i, r in enumerate(narrative.get("recommendations", []))]
    for where, text in items:
        for n in numbers_in(text):
            # compare the absolute value too: "11-12 Nov" is read as 11 and -12
            if not any(abs(n - a) <= 0.051 or abs(abs(n) - a) <= 0.051 for a in allowed):
                problems.append({"where": where, "number": n, "issue": "not found in any fact"})
    for i, h in enumerate(narrative.get("hypotheses", []) + narrative.get("recommendations", [])):
        for e in h.get("evidence", []):
            if e not in fact_ids:
                problems.append({"where": f"item {i + 1}", "number": e, "issue": "cites a fact that does not exist"})
    return problems


def apply_number_check(narrative, problems):
    """Downgrade confidence of any hypothesis that contains an unverified number."""
    bad_places = {p["where"] for p in problems}
    for i, h in enumerate(narrative.get("hypotheses", [])):
        if f"hypothesis {i + 1}" in bad_places:
            h["confidence"] = "low"
            h["unverified"] = True
    for i, r in enumerate(narrative.get("recommendations", [])):
        if f"recommendation {i + 1}" in bad_places:
            r["unverified"] = True
    return narrative
