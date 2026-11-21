"""Plotly charts used by the app and by docs/make_figures.py (README images).

Colours: two slots of a colour-blind-validated categorical palette. One axis per chart,
a legend whenever there are 2+ series, hover tooltips on every mark.
"""
import pandas as pd
import plotly.graph_objects as go

BLUE, ORANGE = "#2a78d6", "#eb6834"          # categorical slots 1 and 2 (validated palette)
INK, TEXT, GRID = "#0b0b0b", "#333333", "#e6e5e1"   # dark text on a white card: readable in light AND dark mode


def base_layout(fig, title, height=320, y_title=None):
    fig.update_layout(title=dict(text=title, font=dict(size=16, color=INK)), height=height,
                      margin=dict(l=20, r=40, t=105, b=20), plot_bgcolor="white", paper_bgcolor="white",
                      font=dict(size=13, color=TEXT), hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0,      # above the plot area,
                                  font=dict(size=13, color=TEXT)))                   # never on top of the data
    fig.update_xaxes(showgrid=False, linecolor=GRID, automargin=True, tickfont=dict(size=12, color=TEXT),
                     title_font=dict(size=13, color=TEXT))
    fig.update_yaxes(gridcolor=GRID, zeroline=False, title=y_title, automargin=True,
                     tickfont=dict(size=12, color=TEXT), title_font=dict(size=13, color=TEXT))
    return fig


def chart_weekly(trend, windows):
    """One line; weeks inside the current period are highlighted in orange and labelled."""
    t = trend.copy()
    cur = windows["current"]
    t["label"] = pd.to_datetime(t["week_start"]).dt.strftime("%d %b")       # week starting (Monday)
    in_cur = (t["week_start"] >= cur[0]) & (t["week_start"] <= cur[1])
    fig = go.Figure(go.Scatter(
        x=t["label"], y=t["success_rate"], mode="lines+markers+text",
        line=dict(color=BLUE, width=2),
        marker=dict(size=9, color=[ORANGE if c else BLUE for c in in_cur]),
        text=[f"{v:.2f}%" if c else "" for v, c in zip(t["success_rate"], in_cur)],
        textposition="bottom center", cliponaxis=False,
        hovertemplate="%{x}<br>Success rate %{y:.2f}%<extra></extra>"))
    fig = base_layout(fig, "Weekly success rate (orange = current period)", y_title="Success rate (%)")
    fig.update_xaxes(title="week starting (Monday)")
    return fig


def chart_mix_rate(drivers):
    dims = pd.DataFrame(drivers["dimensions"])
    fig = go.Figure()
    fig.add_bar(y=dims["dimension"], x=dims["mix_effect_pp"], name="Mix effect (traffic shift)",
                orientation="h", marker_color=BLUE, texttemplate="%{x:.2f}", textposition="outside",
                cliponaxis=False)
    fig.add_bar(y=dims["dimension"], x=dims["rate_effect_pp"], name="Rate effect (segments got worse/better)",
                orientation="h", marker_color=ORANGE, texttemplate="%{x:.2f}", textposition="outside",
                cliponaxis=False)
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.1, hovermode="y unified")
    fig = base_layout(fig, f"Change of {drivers['delta_pp']} pp = mix + rate (per dimension)", height=320)
    values = dims[["mix_effect_pp", "rate_effect_pp"]].to_numpy()
    low, high = min(values.min(), 0), max(values.max(), 0)       # works for a rise AND a decline
    pad = 0.25 * (high - low) + 0.1                              # room for the value labels
    fig.update_xaxes(title="percentage points", range=[low - pad, high + pad])
    return fig


def chart_segments(table, dim):
    t = table.sort_values("share_cur")
    fig = go.Figure()
    fig.add_bar(y=t["segment"], x=100 * t["share_prev"], name="Previous period", orientation="h",
                marker_color=BLUE, hovertemplate="%{y}: %{x:.1f}% of attempts<extra>previous</extra>")
    fig.add_bar(y=t["segment"], x=100 * t["share_cur"], name="Current period", orientation="h",
                marker_color=ORANGE, hovertemplate="%{y}: %{x:.1f}% of attempts<extra>current</extra>")
    fig.update_layout(barmode="group", hovermode="y unified")
    return base_layout(fig, f"Traffic share by {dim} (% of resolved attempts)", height=300)


def chart_drill(daily, segment):
    fig = go.Figure()
    for name, color in [(segment, ORANGE), ("all others", BLUE)]:
        d = daily[daily["segment"] == name]
        fig.add_scatter(x=d["day"], y=d["success_rate"], mode="lines+markers", name=name,
                        line=dict(color=color, width=2), marker=dict(size=8))
    return base_layout(fig, f"Daily success rate: {segment} vs all others", y_title="Success rate (%)")


def chart_table(df, title):
    """Auto chart for an ad-hoc result: one text column + one numeric column, 2-30 rows -> bar chart.

    Returns None when the table doesn't have that simple shape (the table is still shown).
    """
    if not 2 <= len(df) <= 30:
        return None
    text_cols = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if len(text_cols) != 1 or not num_cols:
        return None
    label, value = text_cols[0], num_cols[-1]
    t = df.sort_values(value)
    fig = go.Figure(go.Bar(y=t[label].astype(str), x=t[value], orientation="h", marker_color=BLUE,
                           texttemplate="%{x:,.2f}", textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}: %{x:,.2f}<extra></extra>"))
    fig.update_layout(hovermode="y unified")
    return base_layout(fig, f"{title} ({value} by {label})", height=max(220, 40 * len(t) + 100))
