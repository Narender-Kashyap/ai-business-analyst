"""Question -> structured plan, using Gemini + the semantic layer.

The LLM only INTERPRETS the question (type, metric, period, filters) and writes SQL for
ad-hoc questions. Dates are computed in code; plans are checked in code.
"""
import json
import re
from datetime import date, timedelta

import yaml

import db
from llm import generate_json

SEMANTIC_TEXT = open("config/semantic_layer.yaml", encoding="utf-8").read()
SEMANTIC = yaml.safe_load(SEMANTIC_TEXT)
ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]
QUESTION_TYPES = ["explain_change", "ad_hoc", "needs_clarification", "out_of_scope"]
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def valid_window(value):
    """A window from the LLM must be exactly two real 'YYYY-MM-DD' dates, start <= end; else None."""
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        return None
    try:
        start, end = (date.fromisoformat(d) for d in value if isinstance(d, str) and DATE.fullmatch(d))
    except ValueError:                       # fewer than 2 valid strings, or an impossible date
        return None
    return (str(start), str(end)) if start <= end else None


def default_windows():
    """'last week' and 'previous week' computed from the data's real last date (not by the LLM)."""
    end = date.fromisoformat(db.data_profile()["end_date"])
    last_sunday = end - timedelta(days=(end.weekday() + 1) % 7)     # Mon=0 ... Sun=6
    lw_start = last_sunday - timedelta(days=6)
    return {"current": (str(lw_start), str(last_sunday)),
            "previous": (str(lw_start - timedelta(days=7)), str(lw_start - timedelta(days=1)))}


def time_context():
    p = db.data_profile()
    w = default_windows()
    end = date.fromisoformat(p["end_date"])
    return (f"- Data covers {p['start_date']} to {p['end_date']}.\n"
            f"- as_of_date (today): {end + timedelta(days=1)}\n"
            f"- last week: {w['current'][0]} to {w['current'][1]}\n"
            f"- previous week: {w['previous'][0]} to {w['previous'][1]}\n"
            f"- regions in data: {', '.join(p['regions'])}")


PROMPT_TEMPLATE = """You are the query planner of a payments analytics assistant. You write SQLite SQL.

## Data coverage (computed from the database)
{coverage}

## Semantic layer (agreed definitions - use these, never invent formulas)
{semantic}

## User role
{role}: {role_description}
Tables this role may read: {allowed_tables}

## Classify the question into exactly one question_type
- "explain_change": asks WHY a metric changed / what drove a rise or decline / which segments
  contributed. Metric must be one of: success_rate, attempts, successful_txns, gmv.
  Do NOT write SQL for this type - the analysis engine generates it. Fill metric, time_window, filters.
- "ad_hoc": any other question answerable from the tables (levels, rankings, breakdowns, trends).
  Write 1-3 SQL queries.
- "needs_clarification": cannot be answered without more information (e.g. refers to something
  undefined). Put ONE short clarifying question in "message". Prefer stating an assumption over
  asking when a sensible default exists (e.g. no period given -> last week vs previous week).
- "out_of_scope": needs data that does not exist in these tables (e.g. customer demographics,
  profit, competitors) or that this role may not access. Explain in "message".

## Rules
- Query only tables listed for this role. Never query the raw 'transactions' table.
- Use metric SQL exactly as defined in the semantic layer. Read-only SELECT, one statement, no semicolons.
- Dates: use explicit 'YYYY-MM-DD' dates inside the data coverage. "last week" etc. = the dates above.
- filters use dimension names from segment_dimensions and values exactly as stored in the data.
- If the question names a specific segment (a region, bank, payment method or category), ALWAYS put
  it in filters - even if this role may not be able to see it. Access is checked by code, not by you.
- Record every interpretation you had to make in "assumptions" (period, definition, denominator).

## Output: JSON only, with exactly these keys
{{
  "question_type": "explain_change" | "ad_hoc" | "needs_clarification" | "out_of_scope",
  "interpretation": "one sentence: what you understood the question to be",
  "metric": "metric name or null",
  "time_window": {{"current": ["YYYY-MM-DD", "YYYY-MM-DD"] or null, "comparison": ["YYYY-MM-DD", "YYYY-MM-DD"] or null}},
  "filters": [{{"dimension": "segment dimension name", "value": "value"}}],
  "assumptions": ["..."],
  "message": "clarifying question or out-of-scope explanation, else null",
  "queries": [{{"id": "q1", "purpose": "what this query shows", "sql": "SELECT ..."}}]
}}

## Question
{question}
"""


def build_prompt(question, role):
    r = ROLES[role]
    return PROMPT_TEMPLATE.format(coverage=time_context(), semantic=SEMANTIC_TEXT, role=role,
                                  role_description=r["description"],
                                  allowed_tables=", ".join(r["allowed_tables"]), question=question)


def parse_json(text):
    """Model text -> dict. Strips ```json fences if the model added them."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    return json.loads(text)


def check_plan(plan):
    """Code-level checks on the LLM's plan. Fixes what it safely can, records what it changed."""
    notes = []
    if plan.get("question_type") not in QUESTION_TYPES:
        raise ValueError(f"Unknown question_type {plan.get('question_type')!r}")
    plan.setdefault("assumptions", [])
    plan["filters"] = [f for f in plan.get("filters") or [] if f.get("dimension") and f.get("value")]
    plan["queries"] = plan.get("queries") or []

    if plan["question_type"] == "explain_change":
        tw = plan.get("time_window") or {}
        default = default_windows()
        cur, comp = valid_window(tw.get("current")), valid_window(tw.get("comparison"))
        if tw.get("current") and not cur:
            notes.append(f"The model's period {tw.get('current')!r} was not two valid dates; ignored.")
        if not cur:
            cur = default["current"]
            notes.append(f"No period given: using last week {cur[0]} to {cur[1]}.")
        if not comp:
            # Same-length period immediately before the current one
            start, end = date.fromisoformat(cur[0]), date.fromisoformat(cur[1])
            length = (end - start).days + 1
            comp = (str(start - timedelta(days=length)), str(start - timedelta(days=1)))
            notes.append(f"No comparison period given: comparing with the {length} days before "
                         f"({comp[0]} to {comp[1]}).")
        plan["windows"] = {"current": tuple(cur), "previous": tuple(comp)}
        if not plan.get("metric"):
            plan["metric"] = "success_rate"
            notes.append("No metric named: assuming success_rate.")
        plan["queries"] = []                    # explain_change SQL comes from code, never the LLM
    plan["assumptions"] += notes
    return plan


def make_plan(question, role):
    llm_result = generate_json(build_prompt(question, role))
    plan = check_plan(parse_json(llm_result["text"]))
    return plan, llm_result


REPAIR_TEMPLATE = """You wrote a SQLite query that failed checks or execution.

Semantic layer:
{semantic}

Question: {question}
Purpose of the query: {purpose}
Failed SQL:
{sql}
Errors:
{errors}

Fix the SQL so it answers the same purpose using only valid tables and columns.
Return JSON only: {{"sql": "SELECT ..."}}
"""


def repair_sql(question, purpose, sql, errors):
    """One self-correction attempt: show the model its own error and ask for a fix."""
    prompt = REPAIR_TEMPLATE.format(semantic=SEMANTIC_TEXT, question=question, purpose=purpose,
                                    sql=sql, errors="\n".join(errors))
    llm_result = generate_json(prompt)
    return parse_json(llm_result["text"])["sql"], llm_result
