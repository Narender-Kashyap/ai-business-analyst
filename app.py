"""Streamlit web app for the AI Business Analyst.

Run from the project root:  python -m streamlit run app.py
"""
import os
import sys
from pathlib import Path

import pandas as pd
import sqlglot
import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))
import db  # noqa: E402
from charts import chart_drill, chart_mix_rate, chart_segments, chart_table, chart_weekly  # noqa: E402
from pipeline import ask  # noqa: E402
from validator import validate  # noqa: E402

ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]

EXAMPLES = [
    "Why did the success rate drop last week?",
    "Which segments contributed to the success rate decline last week compared to the week before?",
    "What were the top 3 merchant categories by GMV last week?",
    "Show weekly success rate by payment method for the last 4 weeks",
    "How do settled amounts compare with transaction GMV last week?",
    "What is the age distribution of our customers?",
]

st.set_page_config(page_title="AI Business Analyst", layout="wide")


@st.cache_data(ttl=3600, show_spinner=False)
def cached_ask(question, role):
    """Same question + same role -> cached answer for 1 hour (the data is static)."""
    return ask(question, role)


def render_answer(out):
    status = out["status"]
    if status == "access_denied":
        st.error(f"🔒 {out['message']}")
    elif status == "blocked_unsafe":
        st.error(f"⛔ **Blocked as unsafe** (only read-only queries are allowed). {out['message']}")
    elif status == "out_of_scope":
        st.warning(f"**Out of scope.** {out['message']}")
    elif status == "needs_clarification":
        st.info(f"**Please clarify:** {out['message']}")
    elif status in ("query_failed", "error"):
        st.error(f"**Could not answer.** {out['message']}")
        if out.get("error_details"):
            with st.expander("Technical details"):
                st.code("\n".join(out["error_details"]), language=None)

    plan = out.get("plan")
    if plan:
        with st.expander("① How the question was understood (plan)", expanded=status != "ok"):
            st.write(f"**Interpretation:** {plan.get('interpretation')}")
            st.write(f"**Question type:** `{plan['question_type']}`  •  **Metric:** `{plan.get('metric')}`")
            if plan.get("windows"):
                w = plan["windows"]
                st.write(f"**Time window:** current {w['current'][0]} → {w['current'][1]}, "
                         f"compared with {w['previous'][0]} → {w['previous'][1]}")
            if plan.get("filters"):
                st.write("**Filters:** " + ", ".join(f"{f['dimension']} = {f['value']}" for f in plan["filters"]))
            for a in plan.get("assumptions", []):
                st.write(f"- Assumption: {a}")
    if status != "ok":
        render_queries(out)
        return

    n = out["narrative"] or {}
    st.subheader(n.get("headline", ""))
    if out["number_check"]:
        listed = "; ".join(
            f"{p['number']:,.2f}".rstrip("0").rstrip(".") + f" ({p['where']})" if isinstance(p["number"], float)
            else f"{p['number']} ({p['where']}, {p['issue']})" for p in out["number_check"])
        st.warning(f"⚠️ Number check: {len(out['number_check'])} number(s) in the AI's wording could not be "
                   f"matched to a fact computed by code, so treat them as unverified: {listed}.")
    else:
        st.success("✅ Number check: every number in the AI's wording matches a fact computed from the queries.")

    res = out.get("analysis")
    if res and res["metric"] == "success_rate":
        d = res["drivers"]
        c1, c2 = st.columns(2)
        c1.plotly_chart(chart_weekly(res["weekly_trend"], res["windows"]), width="stretch", theme=None)
        c1.caption("**How to read:** each dot is one week's success rate. Orange = the period you asked "
                   "about. A sudden drop after a flat line means something changed that week.")
        c2.plotly_chart(chart_mix_rate(d), width="stretch", theme=None)
        c2.caption(f"**How to read:** each row splits the same {d['delta_pp']}-point change in two, using "
                   "one way of grouping the traffic. **Blue (mix)** = part caused by traffic moving between "
                   "groups, e.g. more payments going to a weaker payment method. **Orange (rate)** = part "
                   "caused by the groups themselves getting worse or better. A row with a big blue bar means "
                   "the traffic shift happened along that grouping. Hover over a row to see both parts. "
                   "Values are rounded to 2 decimals.")
        c3, c4 = st.columns(2)
        m = d.get("mix_driver")
        if m:
            c3.plotly_chart(chart_segments(res["by_dimension"][m["dimension"]], m["dimension"]),
                            width="stretch", theme=None)
            c3.caption(f"**How to read:** blue = share of traffic before, orange = after. "
                       f"{m['segment']} grew from {m['share_prev_pct']}% to {m['share_cur_pct']}%; its success "
                       f"rate ({m['rate_cur_pct']}%) is below average, so more traffic there pulls the total down.")
        if res.get("drilldown"):
            dd = res["drilldown"]
            c4.plotly_chart(chart_drill(dd["daily"], dd["segment"]), width="stretch", theme=None)
            c4.caption(f"**How to read:** daily success rate of {dd['segment']} (orange) vs all others (blue). "
                       "Where the orange line falls far below the blue one, that segment had its own problem.")
    elif res:
        st.plotly_chart(chart_weekly(res["weekly_trend"], res["windows"]), width="stretch", theme=None)

    steps = investigation_steps(out)
    render_steps(out, steps)

    st.markdown("### ③ Facts *(computed by code from executed queries)*")
    step_of = {q["id"]: s["n"] for s in steps if s.get("query") for q in [s["query"]]}
    st.dataframe(pd.DataFrame([{
        "id": f["id"], "fact": f["text"],
        "from step": ", ".join(str(step_of[q]) for q in f["query_ids"] if q in step_of) or "-",
        "dataset": f["dataset"], "filters": f["filters"], "time window": f["window"]}
        for f in out["facts"]]), hide_index=True, width="stretch")
    for f in out["facts"]:
        if "table" in f:
            fig = chart_table(f["table"], f["text"].split(":")[0])
            if fig is not None:
                st.plotly_chart(fig, width="stretch", theme=None)
            st.caption(f"{f['id']} result table")
            st.dataframe(f["table"], hide_index=True)

    st.markdown("### ④ Hypotheses *(plausible explanations - not proven)*")
    for h in n.get("hypotheses", []):
        flag = " ⚠️ *contains an unverified number*" if h.get("unverified") else ""
        st.markdown(f"- **[{h.get('confidence', '?')} confidence]** {h['text']}  \n"
                    f"  Evidence: {', '.join(h.get('evidence', [])) or '—'} • Would confirm: {h.get('would_confirm', '—')}{flag}")

    st.markdown("### ⑤ Recommended next steps")
    for r in n.get("recommendations", []):
        flag = " ⚠️ *unverified number*" if r.get("unverified") else ""
        st.markdown(f"- {r['text']}  (evidence: {', '.join(r.get('evidence', [])) or '—'}){flag}")

    st.markdown("### ⑥ Caveats and data-quality warnings")
    for c in out["caveats"]:
        st.markdown(f"- {c}")
    render_trace(out)


def step_title(q, out):
    """Plain-English title for one executed query."""
    drill = (out.get("analysis") or {}).get("drilldown") or {}
    qid = q["id"]
    if qid.startswith("seg_"):
        dim = qid[4:].replace("_", " ")
        return f"Compare every {dim} between the two periods (traffic share and success rate of each)"
    if qid == "weekly_trend":
        return "Weekly success rate over all the data (is this drop unusual?)"
    if qid == "drill_daily":
        return f"Drill into {drill.get('segment', 'the top driver')}: daily success rate vs everyone else"
    if qid == "drill_reasons":
        return f"Drill into {drill.get('segment', 'the top driver')}: why its payments failed"
    return f"{q['purpose']} (SQL written by Gemini, checked by the validator)"


def investigation_steps(out):
    """The investigation in the order it happened: plan -> queries -> analysis -> checks -> wording."""
    plan = out.get("plan") or {}
    steps = [{"n": 1, "title": f"Understand the question → type `{plan.get('question_type')}`, "
                               f"metric `{plan.get('metric')}`", "detail": plan.get("interpretation", "")}]
    analysis_step = None
    if out.get("analysis") and out["analysis"]["metric"] == "success_rate":
        d = out["analysis"]["drivers"]
        rd = d.get("rate_driver") or {}
        analysis_step = {"title": "Split the change into mix vs rate for each dimension and test which "
                                  "segments changed more than chance allows (this picks what to drill into)",
                         "detail": f"Largest traffic shift: {(d.get('mix_driver') or {}).get('segment')}. "
                                   f"Largest segment-specific drop: {rd.get('segment')} "
                                   f"({'significant' if rd.get('significant') else 'not significant'})."}
    for q in out["queries"]:
        if analysis_step and q["id"].startswith("drill_"):      # the analysis decides the drill-down
            steps.append({"n": len(steps) + 1, **analysis_step})
            analysis_step = None
        steps.append({"n": len(steps) + 1, "title": step_title(q, out), "query": q})
    if analysis_step:                                           # no drill-down (nothing significant)
        steps.append({"n": len(steps) + 1, **analysis_step})
    warns = sum(x["status"] == "warn" for x in out["dq"])
    steps.append({"n": len(steps) + 1, "title": f"Data-quality checks ({len(out['dq'])} checks, {warns} warnings "
                  "→ caveats)", "detail": ""})
    steps.append({"n": len(steps) + 1, "title": "Gemini writes hypotheses and recommendations from the facts; "
                  "code checks every number it wrote",
                  "detail": "All numbers verified." if not out["number_check"]
                            else f"{len(out['number_check'])} number(s) could not be verified (marked below)."})
    return steps


def pretty_sql(sql):
    """Format SQL over several lines for reading (display only; the executed SQL is unchanged)."""
    try:
        return sqlglot.transpile(sql, read="sqlite", write="sqlite", pretty=True)[0]
    except Exception:
        return sql


def render_steps(out, steps):
    facts_of = {}
    for f in out["facts"]:
        for q in f["query_ids"]:
            facts_of.setdefault(q, []).append(f["id"])
    st.markdown("### ② How the answer was reached *(in the order it happened)*")
    for s in steps:
        q = s.get("query")
        if not q:
            st.markdown(f"**Step {s['n']}.** {s['title']}" + (f"  \n{s['detail']}" if s["detail"] else ""))
            continue
        ok = q.get("ok", True)
        status = f"✅ {q.get('rows', 0)} rows in {q.get('seconds', '—')}s" if ok else "❌ blocked"
        made = ", ".join(facts_of.get(q["id"], [])) or "no facts (context only)"
        with st.expander(f"Step {s['n']}. {s['title']} — {status} — produced: {made}"):
            st.caption(f"Query id `{q['id']}` • written by: {'code (semantic layer)' if q.get('source') == 'code' else 'Gemini'}"
                       " • checked by the validator before running")
            st.code(pretty_sql(q["sql"]), language="sql")
            for e in q.get("errors") or []:
                st.error(e)
            for w in q.get("warnings") or []:
                st.caption(f"validator: {w}")


def render_queries(out):
    """Used when there is no full answer (refused, blocked, failed): show whatever queries were tried."""
    if out["queries"]:
        with st.expander(f"Query evidence ({len(out['queries'])} queries, all validated before running)"):
            for q in out["queries"]:
                ok = q.get("ok", True)
                st.markdown(f"**{q['id']}** — {q['purpose']}  •  {'✅ ran' if ok else '❌ blocked'}  •  "
                            f"rows: {q.get('rows', '—')}")
                st.code(pretty_sql(q["sql"]), language="sql")
                for e in q.get("errors") or []:
                    st.error(e)
    render_trace(out)


def render_trace(out):
    with st.expander("⑦ Trace (models, tokens, latency)"):
        st.dataframe(pd.DataFrame(out["llm_calls"]), hide_index=True)
        st.caption(f"Total time {out['total_s']}s • narrative source: {out.get('narrative_source', '—')}")
        if out["number_check"]:
            st.write("Number-check details:", out["number_check"])


# ---------- Page ----------
st.title("AI Business Analyst — payments")
profile = db.data_profile()
with st.sidebar:
    role = st.selectbox("Your role", list(ROLES), help="Access rules are enforced in code, not by the AI.")
    st.caption(ROLES[role]["description"])
    st.caption(f"Tables: {', '.join(ROLES[role]['allowed_tables'])}")
    st.divider()
    st.caption(f"Data covers {profile['start_date']} to {profile['end_date']} (synthetic, seeded).")
    if not os.getenv("GEMINI_API_KEY"):
        st.warning("No GEMINI_API_KEY found. Copy `.env.example` to `.env` and add a key to ask questions. "
                   "The validator demo tab works without one.")
    st.markdown("**Example questions**")
    for ex in EXAMPLES:
        if st.button(ex, width="stretch"):
            st.session_state["question"] = ex

tab_ask, tab_sql = st.tabs(["Ask a question", "Validator demo (run your own SQL)"])

with tab_ask:
    question = st.text_input("Ask about success rate, GMV, segments, banks, payment methods…",
                             key="question")
    if st.button("Ask", type="primary") and question.strip():
        with st.spinner("Planning, validating, querying, checking data quality…"):
            st.session_state["last"] = cached_ask(question.strip(), role)
            if st.session_state["last"]["status"] == "error":
                cached_ask.clear()                   # never cache a failure: asking again should really retry
    if st.session_state.get("last"):
        render_answer(st.session_state["last"])

with tab_sql:
    st.write("Type any SQL. It goes through the **same validator** the AI's SQL goes through, "
             "for the role selected in the sidebar.")
    sql = st.text_area("SQL", "SELECT * FROM settlements LIMIT 5", height=120)
    if st.button("Validate and run"):
        check = validate(sql, role)
        if not check.ok:
            for e in check.errors:
                st.error(e)
        else:
            for w in check.warnings:
                st.caption(f"validator: {w}")
            st.code(check.sql, language="sql")
            conn = db.get_connection(role)
            try:
                df, seconds = db.run_query(conn, check.sql)
                st.dataframe(df, hide_index=True)
                st.caption(f"{len(df)} rows in {seconds}s")
            except Exception as e:
                st.error(str(e))
            finally:
                conn.close()
