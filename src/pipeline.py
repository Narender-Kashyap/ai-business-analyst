"""End-to-end pipeline: question -> plan -> (code analysis | validated LLM SQL) -> DQ -> answer.

Used by both the command line (ask.py) and the web app (app.py).
"""
import json
import time
from datetime import datetime
from pathlib import Path

import sqlglot
from sqlglot import exp

import analysis
import answer
import db
import dq_checks
from planner import make_plan, repair_sql, valid_window
from validator import validate

TRACE_FILE = Path("traces/trace.jsonl")
# Only honest mistakes may be sent back to the model for repair. Safety and access rejections are
# final: asking the model to "fix" them would be asking it to get around the rules.
REPAIRABLE_PREFIXES = ("Unknown column", "SQL could not be parsed", "Execution failed")


def tables_in(sql):
    try:
        return sorted({t.name for t in sqlglot.parse_one(sql, read="sqlite").find_all(exp.Table)})
    except Exception:
        return []


def run_llm_query(question, q, role, conn):
    """Validate -> run. A fixable failure gets ONE repair by the model; access denials never do."""
    sql, first_errors = q["sql"], None
    for attempt in (1, 2):
        check = validate(sql, role)
        record = {"id": q["id"], "purpose": q["purpose"], "sql": check.sql, "source": "llm",
                  "warnings": check.warnings, "repaired": attempt == 2, "first_errors": first_errors,
                  "tables": tables_in(check.sql)}
        if check.ok:
            try:
                df, seconds = db.run_query(conn, check.sql)
                return df, {**record, "ok": True, "rows": len(df), "seconds": seconds, "errors": []}
            except TimeoutError as e:
                return None, {**record, "ok": False, "errors": [str(e)]}       # slow: don't retry
            except Exception as e:
                check.errors = [f"Execution failed: {e}"]
        repairable = all(err.startswith(REPAIRABLE_PREFIXES) for err in check.errors)
        if not repairable or attempt == 2:
            return None, {**record, "ok": False, "errors": check.errors}
        first_errors = check.errors
        try:
            sql, _ = repair_sql(question, q["purpose"], sql, check.errors)
        except Exception as e:                       # the repair call itself failed: report, don't crash
            return None, {**record, "ok": False, "errors": check.errors + [f"Repair attempt failed: {e}"]}


def ask(question, role):
    """Answer a question. Returns one dict with everything the UI needs."""
    start = time.time()
    out = {"question": question, "role": role, "time": datetime.now().isoformat(timespec="seconds"),
           "llm_calls": [], "queries": [], "facts": [], "caveats": [], "dq": [],
           "narrative": None, "number_check": [], "status": "ok", "message": None}

    def log_llm(stage, llm):
        out["llm_calls"].append({"stage": stage, "model": llm["model"], "attempts": llm["attempts"],
                                 "failures": llm["failures"], "latency_s": llm["latency_s"],
                                 "input_tokens": llm["input_tokens"], "output_tokens": llm["output_tokens"]})

    # 1. PLAN (LLM interprets; code checks the plan)
    try:
        plan, llm = make_plan(question, role)
        log_llm("plan", llm)
    except Exception as e:
        out.update(status="error", message=f"Could not plan the question: {e}")
        return _finish(out, start)
    out["plan"] = plan
    qtype = plan["question_type"]

    if qtype in ("needs_clarification", "out_of_scope"):
        default = ("The question needs data these tables don't contain, or that your role can't access."
                   if qtype == "out_of_scope" else "Please rephrase the question with more detail.")
        out.update(status=qtype, message=plan.get("message") or default)
        return _finish(out, start)

    # 2. EXECUTE
    result = None
    windows = None
    try:
        if qtype == "explain_change":
            windows = plan["windows"]
            result = analysis.explain_change(role, windows, plan["metric"], plan["filters"])
            out["analysis"] = result
            out["queries"] = result["queries"]
            out["facts"] = answer.facts_for_change(result)
        else:
            conn = db.get_connection(role)
            results = []
            try:
                for q in plan["queries"][:3]:
                    df, record = run_llm_query(question, q, role, conn)
                    out["queries"].append(record)
                    # cite the VALIDATED (possibly repaired) SQL, not the model's first draft
                    results.append(({**q, "sql": record["sql"], "tables": record["tables"]}, df))
            finally:
                conn.close()
            out["facts"] = answer.facts_for_tables(results)
            blocked = [r for r in out["queries"] if not r["ok"]]
            if not plan["queries"]:
                out.update(status="query_failed", message="The planner returned no query for this question.")
            elif blocked and not out["facts"]:
                errors = [e for r in blocked for e in r["errors"]]
                if any("Access denied" in e for e in errors):
                    status = "access_denied"
                elif any(e.startswith(("Forbidden", "Only SELECT", "Exactly one")) for e in errors):
                    status = "blocked_unsafe"
                else:
                    status = "query_failed"
                out.update(status=status, message=" ".join(errors))
            cur = valid_window((plan.get("time_window") or {}).get("current"))
            windows = {"current": cur, "previous": cur} if cur else None
    except analysis.AnalysisError as e:
        denied = "not visible to role" in str(e) or "Access denied" in str(e) or "blocked" in str(e)
        message = str(e)
        if message.startswith("No data"):
            p = db.data_profile()
            message += f" The data covers {p['start_date']} to {p['end_date']}."
        out.update(status="access_denied" if denied else "query_failed", message=message)
        return _finish(out, start)
    except Exception as e:                               # never crash the UI on an unexpected error
        out.update(status="error", message=f"Unexpected error while answering: {type(e).__name__}: {e}")
        return _finish(out, start)

    # 3. DATA QUALITY (code) -> caveats
    if windows is None:
        p = db.data_profile()
        windows = {"current": (p["start_date"], p["end_date"]), "previous": (p["start_date"], p["end_date"])}
    out["dq"] = dq_checks.run_checks(role, windows)
    out["caveats"] = _caveats(out, plan)

    if out["status"] != "ok":
        return _finish(out, start)

    # 4. NARRATIVE (LLM) + NUMBER CHECK (code). If the LLM fails, a templated narrative is used.
    try:
        narrative, llm = answer.llm_narrative(question, plan, out["facts"], out["caveats"])
        log_llm("narrative", llm)
        out["narrative_source"] = "llm"
    except Exception as e:
        narrative = answer.fallback_narrative(result)
        out["narrative_source"] = f"fallback ({type(e).__name__})"
    problems = answer.check_numbers(narrative, out["facts"], out["caveats"])
    out["narrative"] = answer.apply_number_check(narrative, problems)
    out["number_check"] = problems
    return _finish(out, start)


def _caveats(out, plan):
    caveats = ["Success rate = SUCCESS / (SUCCESS + FAILED); PENDING is excluded and reported separately."]
    caveats += [f"Assumption: {a}" for a in plan.get("assumptions", [])]
    caveats += [d["message"] for d in out["dq"] if d["status"] == "warn"]
    for q in out["queries"]:
        caveats += [w for w in q.get("warnings", []) if "LIMIT" not in w]
        if q.get("ok") is False:
            caveats.append(f"Query {q['id']} ({q['purpose']}) was NOT run: {'; '.join(q['errors'])} "
                           f"The answer is based only on the queries that ran.")
        if q.get("repaired"):
            caveats.append(f"Query {q['id']} failed first ({'; '.join(q['first_errors'] or [])}) and was "
                           f"corrected once by the model; the corrected SQL passed validation.")
    if plan["question_type"] == "explain_change":
        caveats.append("Mix/rate effects are computed separately for each dimension; effects from different "
                       "dimensions overlap and should not be added together.")
        caveats.append("Drivers show where the change happened, not why: causes are hypotheses until confirmed.")
    return list(dict.fromkeys(caveats))          # remove duplicates, keep order


def _finish(out, start):
    out["total_s"] = round(time.time() - start, 2)
    TRACE_FILE.parent.mkdir(exist_ok=True)
    trace = {k: out.get(k) for k in ["time", "question", "role", "status", "message", "total_s", "llm_calls",
                                     "number_check", "narrative_source"]}
    trace["plan_type"] = (out.get("plan") or {}).get("question_type")
    trace["narrative"] = out.get("narrative")
    trace["queries"] = [{k: q.get(k) for k in ["id", "source", "ok", "rows", "seconds", "errors", "warnings",
                                               "repaired", "sql"]} for q in out["queries"]]
    with open(TRACE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace, default=str) + "\n")
    return out
