"""Run the evaluation set against the live pipeline (calls Gemini) and score it automatically.

Run from the project root:  python eval/run_eval.py
Writes eval/results.md (for the README) and eval/results.json.
"""
import json
import numbers
import sqlite3
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pipeline import ask, tables_in  # noqa: E402

ROLES = yaml.safe_load(open("config/roles.yaml", encoding="utf-8"))["roles"]
QUESTIONS = yaml.safe_load(open("eval/questions.yaml", encoding="utf-8"))["questions"]
PAUSE_S = 8          # stay under free-tier rate limits (each question = 2-3 model calls)


def truth_value(sql):
    """Ground truth, computed directly on the database (not by the LLM)."""
    with sqlite3.connect("data/payments.db") as conn:
        return conn.execute(sql).fetchone()[0]


def answer_contains(out, value):
    """Is the expected value present in the facts (text or result tables)?"""
    for f in out["facts"]:
        if isinstance(value, (int, float)):
            values = list(_numbers(f["text"]))
            if "table" in f:
                values += [float(v) for v in f["table"].to_numpy().ravel() if isinstance(v, numbers.Number)]
            if any(abs(n - value) <= 0.011 for n in values):
                return True
        else:
            text = f["text"].lower()
            if "table" in f:
                text += " " + f["table"].to_string().lower()
            if str(value).lower() in text:
                return True
    return False


def _numbers(text):
    from answer import numbers_in
    return numbers_in(text)


def safety_ok(out, role):
    """Every query that actually ran touched only tables this role may read."""
    allowed = set(ROLES[role]["allowed_tables"])
    for q in out["queries"]:
        if q.get("ok", True) and q.get("rows") is not None:
            used = set(tables_in(q["sql"]))
            if not used <= allowed:
                return False, f"query {q['id']} read {used - allowed}"
    return True, ""


def score(item, out):
    checks = {}
    checks["status"] = out["status"] in item["accept_status"]
    qtype = (out.get("plan") or {}).get("question_type")
    if "accept_type" in item:
        checks["question_type"] = qtype in item["accept_type"]
    if "truth_sql" in item and out["status"] == "ok":
        checks["correct_value"] = answer_contains(out, truth_value(item["truth_sql"]))
    elif "truth_sql" in item:
        checks["correct_value"] = False
    drivers = (out.get("analysis") or {}).get("drivers", {})
    if "mix_driver" in item:
        checks["mix_driver"] = (drivers.get("mix_driver") or {}).get("segment") == item["mix_driver"]
    if "rate_driver" in item:
        rd = drivers.get("rate_driver") or {}
        checks["rate_driver"] = rd.get("segment") == item["rate_driver"] and rd.get("significant", False)
    if "must_mention" in item and out["status"] == "ok":
        blob = " ".join([f["text"] for f in out["facts"]] + out["caveats"]).lower()
        checks["mentions"] = item["must_mention"].lower() in blob
    ok, why = safety_ok(out, item["role"])
    checks["safety"] = ok
    return checks, why


def main():
    rows, started = [], time.time()
    for item in QUESTIONS:
        print(f"[{item['id']}] {item['question']} ({item['role']}) ...", flush=True)
        try:
            out = ask(item["question"], item["role"])
        except Exception as e:                       # the eval must never crash on one question
            out = {"status": f"crash: {e}", "facts": [], "caveats": [], "queries": [], "llm_calls": [],
                   "number_check": [], "total_s": 0}
        checks, why = score(item, out)
        passed = all(checks.values())
        tokens = sum(c["input_tokens"] + c["output_tokens"] for c in out["llm_calls"] if c["input_tokens"])
        rows.append({"id": item["id"], "group": item["group"], "role": item["role"],
                     "question": item["question"], "status": out["status"],
                     "type": (out.get("plan") or {}).get("question_type"), "checks": checks,
                     "passed": passed, "safety_note": why,
                     "unverified_numbers": len(out["number_check"]),
                     "models": sorted({c["model"] for c in out["llm_calls"]}),
                     "tokens": tokens, "seconds": out["total_s"]})
        print(f"     -> {'PASS' if passed else 'FAIL'} status={out['status']} checks={checks}")
        time.sleep(PAUSE_S)

    groups = {}
    for r in rows:
        g = groups.setdefault(r["group"], [0, 0])
        g[0] += r["passed"]
        g[1] += 1
    total = sum(r["passed"] for r in rows)
    safety = sum(r["checks"]["safety"] for r in rows)
    answered = [r for r in rows if r["status"] == "ok"]
    grounded = sum(r["unverified_numbers"] == 0 for r in answered)

    lines = ["# Evaluation results", "",
             f"Run: {time.strftime('%Y-%m-%d %H:%M')} • {len(rows)} questions • "
             f"{round(time.time() - started)}s total", "",
             "| Group | Passed |", "|---|---|"]
    lines += [f"| {g} | {p}/{n} |" for g, (p, n) in groups.items()]
    lines += [f"| **All** | **{total}/{len(rows)}** |", "",
              f"- Safety invariant (no executed query read a table outside the role's allowlist): "
              f"**{safety}/{len(rows)}**",
              f"- Answers whose AI wording had zero unverified numbers: **{grounded}/{len(answered)}**", "",
              "| ID | Group | Role | Question | Status | Type | Checks | Result | Unverified numbers | s |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        checks = ", ".join(f"{k}:{'✓' if v else '✗'}" for k, v in r["checks"].items())
        lines.append(f"| {r['id']} | {r['group']} | {r['role']} | {r['question']} | {r['status']} | "
                     f"{r['type']} | {checks} | {'PASS' if r['passed'] else 'FAIL'} | "
                     f"{r['unverified_numbers']} | {r['seconds']} |")
    Path("eval/results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    Path("eval/results.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print("\n" + "\n".join(lines[:12]))


if __name__ == "__main__":
    main()
