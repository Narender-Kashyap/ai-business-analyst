"""The analysis engine: the maths, and that it finds the planted story without being told."""
import pandas as pd
import pytest

import analysis as A
from conftest import LAST_WEEK, QUIET_WEEK


def segment_rows(rows):
    """rows: (period, segment, successful, failed) -> the DataFrame shape the segment SQL returns."""
    return pd.DataFrame(rows, columns=["period", "segment", "successful", "failed"])


# ---- pure maths on a hand-made example (Part 6.1 of the study guide) ----

def test_pure_mix_shift_hand_example():
    # A: 90% -> 80% of traffic at 95%; B: 10% -> 20% at 80%. Nothing got worse.
    df = segment_rows([("previous", "A", 855, 45), ("previous", "B", 80, 20),
                       ("current", "A", 760, 40), ("current", "B", 160, 40)])
    t, overall = A.decompose(df)
    t = t.set_index("segment")
    assert overall["sr_prev"] == pytest.approx(0.935)
    assert overall["sr_cur"] == pytest.approx(0.920)
    assert t["rate_effect"].abs().max() == pytest.approx(0.0)              # no segment changed
    assert t.loc["A", "mix_effect"] == pytest.approx(-0.00225)
    assert t.loc["B", "mix_effect"] == pytest.approx(-0.01275)             # more traffic into the weak one
    assert t["total_effect"].sum() == pytest.approx(-0.015)


def test_segment_missing_in_one_period_has_no_rate_effect():
    df = segment_rows([("previous", "A", 90, 10), ("current", "A", 90, 10), ("current", "NEW", 5, 5)])
    t, overall = A.decompose(df)
    new = t.set_index("segment").loc["NEW"]
    assert new["rate_effect"] == pytest.approx(0.0)
    assert t["total_effect"].sum() == pytest.approx(overall["sr_cur"] - overall["sr_prev"])


def test_bonferroni_threshold():
    assert A.z_threshold(1) == pytest.approx(1.96, abs=0.01)
    assert A.z_threshold(20) == pytest.approx(3.02, abs=0.01)


# ---- the planted story ----

@pytest.fixture(scope="module")
def story():
    return A.explain_change("central_analyst", LAST_WEEK)


def test_story_drop_size_and_significance(story):
    d = story["drivers"]
    assert d["delta_pp"] == pytest.approx(-3.36, abs=0.01)
    assert d["overall_change_significant"]


def test_mix_plus_rate_adds_up_exactly_in_every_dimension(story):
    total = story["sr_cur"] - story["sr_prev"]
    for dim, t in story["by_dimension"].items():
        assert t["total_effect"].sum() == pytest.approx(total, abs=1e-12), dim


def test_finds_net_banking_mix_shift(story):
    m = story["drivers"]["mix_driver"]
    assert (m["dimension"], m["segment"]) == ("payment_method", "Net Banking")
    assert m["segment_mix_effect_pp"] < 0


def test_finds_bank_c_and_it_is_significant(story):
    r = story["drivers"]["rate_driver"]
    assert (r["dimension"], r["segment"]) == ("bank", "Bank C")
    assert r["significant"] and abs(r["z_vs_rest"]) > r["z_threshold"]


def test_drilldown_finds_the_outage_days_and_reason(story):
    daily = story["drilldown"]["daily"]
    bank_c = daily[daily["segment"] == "Bank C"].set_index("day")["success_rate"]
    assert bank_c["2026-11-11"] < 60 and bank_c["2026-11-12"] < 60
    reasons = story["drilldown"]["failure_reasons"]
    assert reasons[reasons["period"] == "current"].iloc[0]["failure_reason"] == "ISSUER_UNAVAILABLE"


def test_quiet_week_raises_no_false_alarm():
    d = A.explain_change("central_analyst", QUIET_WEEK)["drivers"]
    assert not d["overall_change_significant"]
    assert not d["rate_driver"]["significant"]


def test_north_role_skips_region_and_cannot_filter_on_south():
    north = A.explain_change("regional_manager_north", LAST_WEEK)
    assert "region" not in north["by_dimension"]
    with pytest.raises(A.AnalysisError, match="not visible"):
        A.explain_change("regional_manager_north", LAST_WEEK, filters=[{"dimension": "region", "value": "south"}])


def test_additive_metric_gmv():
    g = A.explain_change("central_analyst", LAST_WEEK, metric="gmv")
    assert g["value_prev"] > 0 and g["value_cur"] > 0


def test_unsupported_metric_is_refused():
    with pytest.raises(A.AnalysisError):
        A.explain_change("central_analyst", LAST_WEEK, metric="profit")
