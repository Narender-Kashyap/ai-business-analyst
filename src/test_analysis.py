"""Checks for the analysis engine on known answers (no LLM involved).

Run:  python src/test_analysis.py
"""
import analysis as A

LAST = {"previous": ("2026-11-02", "2026-11-08"), "current": ("2026-11-09", "2026-11-15")}
QUIET = {"previous": ("2026-10-19", "2026-10-25"), "current": ("2026-10-26", "2026-11-01")}
results = []


def check(name, condition):
    results.append(condition)
    print(f"[{'PASS' if condition else 'FAIL'}] {name}")


story = A.explain_change("central_analyst", LAST)
d = story["drivers"]
check("story: success rate dropped about 3 points", -4.0 < d["delta_pp"] < -2.5)
check("story: mix + rate add up exactly in every dimension",
      all(abs(t["total_effect"].sum() - (story["sr_cur"] - story["sr_prev"])) < 1e-9
          for t in story["by_dimension"].values()))
check("story: mix driver is payment_method / Net Banking",
      d["mix_driver"]["dimension"] == "payment_method" and d["mix_driver"]["segment"] == "Net Banking")
check("story: Net Banking mix effect is negative (moved into a weak segment)",
      d["mix_driver"]["segment_mix_effect_pp"] < 0)
check("story: rate driver is bank / Bank C and significant",
      d["rate_driver"]["segment"] == "Bank C" and d["rate_driver"]["significant"])
reasons = story["drilldown"]["failure_reasons"]
top_reason = reasons[reasons["period"] == "current"].iloc[0]["failure_reason"]
check("story: drill-down finds ISSUER_UNAVAILABLE as top failure reason", top_reason == "ISSUER_UNAVAILABLE")

quiet = A.explain_change("central_analyst", QUIET)
check("quiet week: overall change not significant", not quiet["drivers"]["overall_change_significant"])
check("quiet week: no segment flagged as significant (no false alarm)",
      not quiet["drivers"]["rate_driver"]["significant"])

north = A.explain_change("regional_manager_north", LAST)
check("north role: region is not a usable dimension (only one region visible)",
      "region" not in north["by_dimension"])
try:
    A.explain_change("regional_manager_north", LAST, filters=[{"dimension": "region", "value": "south"}])
    check("north role: filter on south is refused", False)
except A.AnalysisError:
    check("north role: filter on south is refused", True)

gmv = A.explain_change("central_analyst", LAST, metric="gmv")
check("gmv: values computed for both periods", gmv["value_prev"] > 0 and gmv["value_cur"] > 0)

print(f"\n{sum(results)}/{len(results)} analysis checks passed")
