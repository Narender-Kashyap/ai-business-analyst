"""Live smoke test of the full pipeline (calls Gemini twice).

Run:  python src/test_pipeline.py
"""
from pipeline import ask

out = ask("Why did the success rate drop last week?", "central_analyst")
d = (out.get("analysis") or {}).get("drivers", {})
print("status        :", out["status"], "| type:", (out.get("plan") or {}).get("question_type"))
print("headline      :", (out["narrative"] or {}).get("headline"))
print("change (pp)   :", d.get("delta_pp"))
print("mix driver    :", (d.get("mix_driver") or {}).get("segment"))
print("rate driver   :", (d.get("rate_driver") or {}).get("segment"))
print("facts         :", len(out["facts"]), "| caveats:", len(out["caveats"]))
print("number check  :", out["number_check"] or "all numbers verified")
print("llm calls     :", [(c["stage"], c["model"], c["latency_s"]) for c in out["llm_calls"]])
for h in (out["narrative"] or {}).get("hypotheses", []):
    print("hypothesis    :", h.get("confidence"), "|", h["text"])
