"""Plan checks (dates, defaults) and answer checks (number check, narrative clean-up, filters)."""
import pandas as pd
import pytest

import answer
from planner import check_plan, default_windows, valid_window


# ---- planner: dates are decided by code ----

def test_default_windows_come_from_the_data():
    assert default_windows() == {"current": ("2026-11-09", "2026-11-15"),
                                 "previous": ("2026-11-02", "2026-11-08")}


@pytest.mark.parametrize("value, expected", [
    (["2026-11-09", "2026-11-15"], ("2026-11-09", "2026-11-15")),
    ("last week", None),                          # not two dates
    (["2026-11-15", "2026-11-09"], None),         # reversed
    (["2026-02-30", "2026-03-01"], None),         # impossible date
    (["2026-11-09"], None),                       # only one date
])
def test_valid_window(value, expected):
    assert valid_window(value) == expected


def test_check_plan_fills_defaults_and_says_so():
    plan = check_plan({"question_type": "explain_change", "time_window": {"current": "last week"},
                       "filters": [], "queries": [{"id": "q1", "purpose": "x", "sql": "SELECT 1"}]})
    assert plan["windows"] == default_windows()
    assert plan["metric"] == "success_rate"
    assert plan["queries"] == []                   # explain_change SQL always comes from code
    assert any("not two valid dates" in a for a in plan["assumptions"])


def test_check_plan_rejects_unknown_question_type():
    with pytest.raises(ValueError):
        check_plan({"question_type": "do_anything"})


# ---- answer: the number check ----

FACTS = [{"id": "F1", "text": "Success rate moved from 92.29% to 88.93%: -3.36 percentage points.",
          "window": "previous 2026-11-02 to 2026-11-08 vs current 2026-11-09 to 2026-11-15", "filters": "none"},
         {"id": "F2", "text": "2 day(s) where Bank C was more than 10 pts below all others: "
                              "2026-11-11: 57.58% vs 91.05%.", "window": "", "filters": ""}]


def narrative(text, evidence=("F1",)):
    return {"headline": "", "hypotheses": [{"text": text, "evidence": list(evidence)}], "recommendations": []}


@pytest.mark.parametrize("text", [
    "Success rate fell 3.36 points to 88.93%.",          # exact and absolute value
    "It fell about 3.4 points.",                          # rounding
    "Bank C dropped to 57% on 11th November.",            # truncation + date written in words
])
def test_numbers_computed_by_code_are_accepted(text):
    assert answer.check_numbers(narrative(text), FACTS) == []


def test_invented_number_is_flagged():
    problems = answer.check_numbers(narrative("About 1,294 attempts failed."), FACTS)
    assert [p["number"] for p in problems] == [1294.0]


def test_number_from_a_caveat_is_accepted():
    caveat = ["184 of 1963 failed transactions have no failure_reason."]
    assert answer.check_numbers(narrative("184 failures had no reason."), FACTS, caveat) == []


def test_citing_a_fact_that_does_not_exist_is_flagged():
    problems = answer.check_numbers(narrative("Rates fell.", evidence=["F99"]), FACTS)
    assert problems and problems[0]["issue"] == "cites a fact that does not exist"


def test_unverified_hypothesis_is_downgraded():
    n = narrative("About 1,294 attempts failed.")
    n["hypotheses"][0]["confidence"] = "high"
    answer.apply_number_check(n, answer.check_numbers(n, FACTS))
    assert n["hypotheses"][0]["confidence"] == "low" and n["hypotheses"][0]["unverified"]


def test_odd_model_output_is_normalised():
    n = answer.normalise_narrative({"headline": None, "hypotheses": {"text": "one", "evidence": "F1"},
                                    "recommendations": "do the thing"})
    assert n["hypotheses"] == [{"text": "one", "evidence": ["F1"]}]
    assert n["recommendations"] == [{"text": "do the thing", "evidence": []}]


def test_ad_hoc_fact_cites_the_real_where_clause():
    sql = ("SELECT m.category, COUNT(*) AS n FROM transactions_clean t JOIN merchants m "
           "ON t.merchant_id = m.merchant_id WHERE DATE(t.txn_ts) BETWEEN '2026-11-09' AND '2026-11-15' "
           "GROUP BY m.category")
    facts = answer.facts_for_tables([({"id": "q1", "purpose": "count by category", "sql": sql,
                                       "tables": ["merchants", "transactions_clean"]},
                                      pd.DataFrame({"category": ["food"], "n": [5]}))])
    assert "BETWEEN '2026-11-09' AND '2026-11-15'" in facts[0]["filters"]
    assert facts[0]["dataset"] == "merchants, transactions_clean"


def test_quota_and_overload_errors_become_a_short_message(monkeypatch):
    """Main model out of daily quota, fallback overloaded: a short, actionable message,
    no retries on the daily quota, raw errors kept for the trace."""
    import llm
    from google.genai import errors
    calls = []

    class FakeModels:
        def generate_content(self, model, **kw):
            calls.append(model)
            if model == llm.MODELS[0]:
                raise errors.APIError(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message":
                    "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20"}})
            raise errors.APIError(503, {"error": {"status": "UNAVAILABLE", "message": "high demand"}})

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(llm, "MODELS", ["main-model", "fallback-model"])
    monkeypatch.setattr(llm, "_client", FakeClient())
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(llm.LLMUnavailable) as info:
        llm.generate_json("hi")
    message = str(info.value)
    assert "daily free-tier quota" in message and "overloaded" in message
    assert len(message) < 400
    assert calls.count(llm.MODELS[0]) == 1          # no pointless retries on a daily quota
    assert len(info.value.details) == 1 + llm.MAX_ATTEMPTS_PER_MODEL


def test_headline_cannot_borrow_a_caveat_number():
    """Real case: the AI wrote '1510 attempts' (the raw row count from the duplicates caveat)
    when the deduplicated count was 1500. Headlines may only use numbers from facts."""
    table = pd.DataFrame({"status": ["SUCCESS", "FAILED", "PENDING"], "n": [1351, 145, 4],
                          "amount": [1455105.59, 162683.15, 3227.63]})
    facts = [{"id": "F1", "text": "F1 result table", "table": table, "window": "2026-11-15", "filters": ""}]
    caveats = ["10 duplicate rows (0.66% of 1510 raw rows) were removed",
               "10 of 145 failed transactions have no failure_reason"]
    narrative = {"headline": "Yesterday there were 1510 attempts and 1,500 resolved or pending, "
                             "totalling 1,621,016.37.",
                 "hypotheses": [{"text": "10 failures lack a reason", "evidence": ["F1"]}],
                 "recommendations": []}
    flagged = [p["number"] for p in answer.check_numbers(narrative, facts, caveats)]
    assert flagged == [1510.0]          # column totals (1500, 1,621,016.37) are verifiable; caveat use is fine below
