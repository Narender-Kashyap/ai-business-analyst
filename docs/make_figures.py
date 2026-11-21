"""Render the README figures from the real analysis (same charts as the app).

Run from the project root:  pip install kaleido  then  python docs/make_figures.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import analysis  # noqa: E402
from charts import chart_drill, chart_mix_rate, chart_segments, chart_weekly  # noqa: E402

OUT = Path("docs/images")
WINDOWS = {"previous": ("2026-11-02", "2026-11-08"), "current": ("2026-11-09", "2026-11-15")}

result = analysis.explain_change("central_analyst", WINDOWS)
drivers = result["drivers"]
mix_dim = drivers["mix_driver"]["dimension"]
figures = {
    "1_weekly_success_rate.png": chart_weekly(result["weekly_trend"], WINDOWS),
    "2_mix_vs_rate.png": chart_mix_rate(drivers),
    "3_traffic_share.png": chart_segments(result["by_dimension"][mix_dim], mix_dim),
    "4_drilldown_daily.png": chart_drill(result["drilldown"]["daily"], result["drilldown"]["segment"]),
}
OUT.mkdir(parents=True, exist_ok=True)
for name, fig in figures.items():
    fig.update_layout(width=760, height=360)
    fig.write_image(OUT / name, scale=2)
    print("wrote", OUT / name)
