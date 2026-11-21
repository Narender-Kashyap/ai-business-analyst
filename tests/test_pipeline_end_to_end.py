"""The whole pipeline with a fake Gemini: routing, refusals, repairs, partial blocks."""
import pipeline
from conftest import plan

GMV_SQL = ("SELECT m.category, ROUND(SUM(CASE WHEN t.status = 'SUCCESS' THEN t.amount ELSE 0 END), 2) AS gmv "
           "FROM transactions_clean t JOIN merchants m ON t.merchant_id = m.merchant_id "
           "WHERE DATE(t.txn_ts) BETWEEN '2026-11-09' AND '2026-11-15' GROUP BY m.category ORDER BY gmv DESC LIMIT 3")


def test_why_question_is_answered_by_code(use_fake_llm):
    use_fake_llm(plan("explain_change", metric="success_rate"),
                 {"headline": "Success rate fell 3.36 points to 88.93%.", "hypotheses": [], "recommendations": []})
    out = pipeline.ask("Why did the success rate drop last week?", "central_analyst")
    assert out["status"] == "ok"
    assert all(q["source"] == "code" for q in out["queries"])
    assert out["analysis"]["drivers"]["rate_driver"]["segment"] == "Bank C"
    assert out["number_check"] == []
    assert any("reconciled" in c for c in out["caveats"])


def test_ad_hoc_question_returns_the_right_table(use_fake_llm):
    use_fake_llm(plan("ad_hoc", queries=[{"id": "q1", "purpose": "GMV by category", "sql": GMV_SQL}]))
    out = pipeline.ask("Top 3 categories by GMV last week?", "central_analyst")
    assert out["status"] == "ok"
    assert list(out["facts"][0]["table"]["category"]) == ["food", "travel", "grocery"]


def test_delete_is_blocked_and_never_sent_for_repair(use_fake_llm, monkeypatch):
    repairs = []
    monkeypatch.setattr(pipeline, "repair_sql", lambda *a: repairs.append(a) or ("SELECT 1", {}))
    use_fake_llm(plan("ad_hoc", queries=[{"id": "q1", "purpose": "x", "sql": "DELETE FROM banks"}]))
    out = pipeline.ask("Delete all banks", "central_analyst")
    assert out["status"] == "blocked_unsafe"
    assert repairs == []


def test_honest_mistake_gets_one_repair(use_fake_llm, monkeypatch):
    monkeypatch.setattr(pipeline, "repair_sql",
                        lambda *a: ("SELECT method_id, COUNT(*) AS n FROM transactions_clean GROUP BY method_id", {}))
    use_fake_llm(plan("ad_hoc", queries=[{"id": "q1", "purpose": "count by method",
                                          "sql": "SELECT methd, COUNT(*) AS n FROM transactions_clean GROUP BY methd"}]))
    out = pipeline.ask("Count by method", "central_analyst")
    assert out["status"] == "ok" and out["queries"][0]["repaired"]
    assert any("corrected once" in c for c in out["caveats"])


def test_partial_block_is_reported_not_hidden(use_fake_llm):
    use_fake_llm(plan("ad_hoc", queries=[
        {"id": "q1", "purpose": "count", "sql": "SELECT COUNT(*) AS n FROM transactions_clean"},
        {"id": "q2", "purpose": "settled", "sql": "SELECT SUM(txn_count) AS s FROM settlements"}]))
    out = pipeline.ask("Count and settlements", "regional_manager_north")
    assert out["status"] == "ok"
    assert any("q2" in c and "NOT run" in c for c in out["caveats"])


def test_north_manager_asking_about_south_is_denied(use_fake_llm):
    use_fake_llm(plan("explain_change", metric="success_rate",
                      filters=[{"dimension": "region", "value": "south"}]))
    out = pipeline.ask("Why did south drop?", "regional_manager_north")
    assert out["status"] == "access_denied"


def test_out_of_scope_without_a_message_gets_a_default(use_fake_llm):
    use_fake_llm(plan("out_of_scope"))
    out = pipeline.ask("Average customer age?", "central_analyst")
    assert out["status"] == "out_of_scope" and out["message"]


def test_dates_outside_the_data_give_a_clear_message(use_fake_llm):
    use_fake_llm(plan("explain_change", metric="success_rate",
                      time_window={"current": ["2025-01-06", "2025-01-12"], "comparison": None}))
    out = pipeline.ask("Why did it drop in January 2025?", "central_analyst")
    assert out["status"] == "query_failed" and "2026-09-21 to 2026-11-15" in out["message"]


def test_planner_failure_never_crashes(use_fake_llm, monkeypatch):
    import planner

    def broken(prompt):
        raise RuntimeError("All models failed")
    monkeypatch.setattr(planner, "generate_json", broken)
    out = pipeline.ask("anything", "central_analyst")
    assert out["status"] == "error"


# ---- robustness to how a reviewer might phrase things ----

def test_filter_value_in_different_case_still_matches(use_fake_llm):
    use_fake_llm(plan("explain_change", metric="success_rate",
                      filters=[{"dimension": "category", "value": "Travel"}]))       # alias + capital T
    out = pipeline.ask("Why did success rate drop for Travel merchants?", "central_analyst")
    assert out["status"] == "ok"
    assert out["plan"]["filters"] == [{"dimension": "merchant_category", "value": "travel"}]
    assert out["analysis"]["drivers"]["rate_driver"]["segment"] == "Bank C"


def test_metric_name_variants_are_understood(use_fake_llm):
    use_fake_llm(plan("explain_change", metric="Success Rate"))
    out = pipeline.ask("Why did it drop?", "central_analyst")
    assert out["status"] == "ok" and out["plan"]["metric"] == "success_rate"


def test_raw_table_is_repaired_to_the_clean_view(use_fake_llm, monkeypatch):
    monkeypatch.setattr(pipeline, "repair_sql",
                        lambda *a: ("SELECT COUNT(*) AS n FROM transactions_clean", {}))
    use_fake_llm(plan("ad_hoc", queries=[{"id": "q1", "purpose": "count",
                                          "sql": "SELECT COUNT(*) AS n FROM transactions"}]))
    out = pipeline.ask("How many transactions are there?", "central_analyst")
    assert out["status"] == "ok" and out["queries"][0]["repaired"]
    assert out["facts"][0]["table"]["n"].iloc[0] == 84_000


def test_missing_api_key_gives_a_clear_message(monkeypatch):
    import llm
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_client", None)
    out = pipeline.ask("Why did the success rate drop?", "central_analyst")
    assert out["status"] == "error" and "GEMINI_API_KEY is not set" in out["message"]


def test_new_dimension_added_in_config_needs_no_code_change(use_fake_llm, monkeypatch):
    import analysis
    dims = dict(analysis.DIMENSIONS)
    dims["hour_of_day"] = {"join": "", "column": "CAST(strftime('%H', t.txn_ts) AS INTEGER)"}
    monkeypatch.setattr(analysis, "DIMENSIONS", dims)
    use_fake_llm(plan("explain_change", metric="success_rate"))
    out = pipeline.ask("Why did the success rate drop last week?", "central_analyst")
    assert out["status"] == "ok", out.get("message")
    assert "hour_of_day" in out["analysis"]["by_dimension"]
