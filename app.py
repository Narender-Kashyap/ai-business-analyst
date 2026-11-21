"""Streamlit web app for the AI Business Analyst.

Run from the project root:  python -m streamlit run app.py
"""
import sys
from pathlib import Path

import pandas as pd
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
        st.warning(f"⚠️ Number check: {len(out['number_check'])} item(s) in the AI's wording could not be "
                   f"matched to a computed fact - marked as unverified below.")
    else:
        st.success("✅ Number check: every number in the AI's wording matches a fact computed from the queries.")

    res = out.get("analysis")
    if res and res["metric"] == "success_rate":
        c1, c2 = st.columns(2)
        c1.plotly_chart(chart_weekly(res["weekly_trend"], res["windows"]), width="stretch")
        c2.plotly_chart(chart_mix_rate(res["drivers"]), width="stretch")
        c3, c4 = st.columns(2)
        m = res["drivers"].get("mix_driver")
        if m:
            c3.plotly_chart(chart_segments(res["by_dimension"][m["dimension"]], m["dimension"]),
                            width="stretch")
        if res.get("drilldown"):
            dd = res["drilldown"]
            c4.plotly_chart(chart_drill(dd["daily"], dd["segment"]), width="stretch")
    elif res:
        st.plotly_chart(chart_weekly(res["weekly_trend"], res["windows"]), width="stretch")

    st.markdown("### ② Facts *(computed by code from executed queries)*")
    st.dataframe(pd.DataFrame([{k: f[k] for k in ["id", "text", "dataset", "filters", "window", "query_ids"]}
                               for f in out["facts"]]), hide_index=True, width="stretch")
    for f in out["facts"]:
        if "table" in f:
            fig = chart_table(f["table"], f["text"].split(":")[0])
            if fig is not None:
                st.plotly_chart(fig, width="stretch")
            st.caption(f"{f['id']} result table")
            st.dataframe(f["table"], hide_index=True)

    st.markdown("### ③ Hypotheses *(plausible explanations - not proven)*")
    for h in n.get("hypotheses", []):
        flag = " ⚠️ *contains an unverified number*" if h.get("unverified") else ""
        st.markdown(f"- **[{h.get('confidence', '?')} confidence]** {h['text']}  \n"
                    f"  Evidence: {', '.join(h.get('evidence', [])) or '—'} • Would confirm: {h.get('would_confirm', '—')}{flag}")

    st.markdown("### ④ Recommended next steps")
    for r in n.get("recommendations", []):
        flag = " ⚠️ *unverified number*" if r.get("unverified") else ""
        st.markdown(f"- {r['text']}  (evidence: {', '.join(r.get('evidence', [])) or '—'}){flag}")

    st.markdown("### ⑤ Caveats and data-quality warnings")
    for c in out["caveats"]:
        st.markdown(f"- {c}")
    render_queries(out)


def render_queries(out):
    with st.expander(f"⑥ Query evidence ({len(out['queries'])} queries, all validated before running)"):
        for q in out["queries"]:
            ok = q.get("ok", True)
            st.markdown(f"**{q['id']}** — {q['purpose']}  •  source: `{q.get('source')}`  •  "
                        f"{'✅ ran' if ok else '❌ blocked'}  •  rows: {q.get('rows', '—')}  •  {q.get('seconds', '—')}s")
            st.code(q["sql"], language="sql")
            for e in q.get("errors") or []:
                st.error(e)
            for w in q.get("warnings") or []:
                st.caption(f"validator: {w}")
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
