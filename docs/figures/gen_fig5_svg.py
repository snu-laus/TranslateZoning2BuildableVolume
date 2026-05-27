"""
Generate Fig. 5 — GIS heatmap (SVG) for Gangnam-gu setback simulation results.

Left panel:  Floor 4-5 (12-15 m) buildable-area change rate (Current -> Proposed)
Right panel: Parcel distribution by residential-zone class

Usage:
    python docs/figures/gen_fig5_svg.py
"""

import sys
import pathlib
import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, BoundaryNorm
from matplotlib.patches import Patch
from mpl_toolkits.axes_grid1 import make_axes_locatable

# ── paths ────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).resolve().parents[2]
SHP_PATH = ROOT / "data" / "Gangnam.shp"
CSV_A = ROOT / "data" / "result" / "result_scenario_A_20260410.csv"
CSV_B = ROOT / "data" / "result" / "result_scenario_B_20260410.csv"
OUT_SVG = ROOT / "docs" / "figures" / "fig5_gis_heatmap.svg"

TARGET_FLOORS = [4, 5]  # 4층(12m) + 5층(15m)

# ── 1. load shapefile ────────────────────────────────────────────────
print("Loading shapefile ...")
gdf = gpd.read_file(SHP_PATH)
gdf["pnu"] = gdf["A1"].astype(np.int64)

# ── 2. load result CSVs (floor 4 & 5) ───────────────────────────────
print("Loading result CSVs ...")
cols = ["pnu", "landuse_code", "landuse", "lot_area_m2",
        "lot_area_inward_1m_m2", "floor", "height_m", "allowed_area_m2"]

df_a = pd.read_csv(CSV_A, usecols=cols)
df_b = pd.read_csv(CSV_B, usecols=cols)

# filter to target floors and aggregate per parcel (sum of 4F + 5F areas)
df_a_tgt = df_a[df_a["floor"].isin(TARGET_FLOORS)]
df_b_tgt = df_b[df_b["floor"].isin(TARGET_FLOORS)]

agg_a = (
    df_a_tgt.groupby("pnu")
    .agg(
        area_A=("allowed_area_m2", "sum"),
        landuse_code=("landuse_code", "first"),
        landuse=("landuse", "first"),
        lot_area_m2=("lot_area_m2", "first"),
        lot_area_inward_1m_m2=("lot_area_inward_1m_m2", "first"),
    )
    .reset_index()
)
agg_b = (
    df_b_tgt.groupby("pnu")
    .agg(area_B=("allowed_area_m2", "sum"))
    .reset_index()
)

# ── 3. compute change rate (%) ───────────────────────────────────────
#   change_rate = (B_sum - A_sum) / lot_area_inward_1m * 100
#   Sum of floor 4 + floor 5 area change, normalised by lot area (1m inward).
merged = agg_a.merge(agg_b, on="pnu", how="inner")
merged["change_rate"] = np.where(
    merged["lot_area_inward_1m_m2"] > 0,
    (merged["area_B"] - merged["area_A"])
    / merged["lot_area_inward_1m_m2"] * 100,
    0.0,
)

print(f"  Parcels with change data: {len(merged):,}")
print(f"  Change rate range: {merged['change_rate'].min():.2f} ~ "
      f"{merged['change_rate'].max():.2f} %")
print(f"  Median: {merged['change_rate'].median():.2f} %")

# ── 4. join to GeoDataFrame ──────────────────────────────────────────
gdf_plot = gdf.merge(
    merged[["pnu", "landuse", "landuse_code", "change_rate",
            "lot_area_inward_1m_m2",
            "area_A", "area_B"]],
    on="pnu", how="left",
)

# parcels that are part of the simulation
gdf_sim = gdf_plot.dropna(subset=["change_rate"]).copy()

# zone labels
ZONE_MAP = {
    13: ("제1종일반주거지역", "#2d6a4f"),   # deep green
    14: ("제2종일반주거지역", "#e89f3c"),   # amber/orange
    15: ("제3종일반주거지역", "#3c8dbc"),   # blue
}

# ── 5. matplotlib figure ─────────────────────────────────────────────
plt.rcParams.update({
    "font.family": "AppleGothic",        # macOS Korean
    "axes.unicode_minus": False,
    "figure.dpi": 150,
    "savefig.dpi": 150,
})

fig, (ax_left, ax_right) = plt.subplots(
    1, 2, figsize=(20, 11), gridspec_kw={"wspace": 0.08}
)

# ── title ────────────────────────────────────────────────────────────
fig.suptitle(
    "Fig. 5 \u2014 \uac15\ub0a8\uad6c \uc77c\ubc18\uc8fc\uac70\uc9c0\uc5ed"
    " \uc77c\uc870\uad8c \uc0ac\uc120\uc81c\ud55c \uac1c\uc815\uc548"
    " \uc2dc\ubbac\ub808\uc774\uc158 \uacb0\uacfc"
    " \u2014 4\u30015\uce35 \ud569\uc0b0 (21,280\ud544\uc9c0)",
    fontsize=16, fontweight="bold", y=0.97,
)

# ───────────── LEFT: heatmap ─────────────────────────────────────────
ax_left.set_title(
    "4\u30015\uce35(12\u201315m) \uac74\ucd95 \uac00\ub2a5 \uba74\uc801 \uc99d\uac00\uc728\n"
    "(\ud604\ud589 \u2192 \uac1c\uc815\uc548)",
    fontsize=13, pad=12,
)

# background: all Gangnam parcels in very light grey
gdf.plot(ax=ax_left, color="#ededed", edgecolor="#cccccc", linewidth=0.1)

# heatmap color: warm palette (light yellow → orange → dark red)
cmap = LinearSegmentedColormap.from_list(
    "area_change",
    ["#ffffcc", "#fed976", "#fd8d3c", "#e31a1c", "#800026"],
)
bounds = np.arange(0, 32, 2)   # 0 ~ 30 %  (4F+5F 합산 → 범위 확장)
norm = BoundaryNorm(bounds, cmap.N, clip=True)

# clip negative changes to 0 for display
gdf_sim["change_display"] = gdf_sim["change_rate"].clip(lower=0)

gdf_sim.plot(
    ax=ax_left,
    column="change_display",
    cmap=cmap,
    norm=norm,
    edgecolor="none",
    linewidth=0.0,
)

# colorbar
sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
sm.set_array([])
divider = make_axes_locatable(ax_left)
cax = divider.append_axes("right", size="3%", pad=0.12)
cbar = fig.colorbar(sm, cax=cax, ticks=np.arange(0, 32, 5))
cbar.set_label("% \uba74\uc801 \uc99d\uac00\uc728", fontsize=11)
cbar.ax.tick_params(labelsize=10)

# footnote
total_sim = len(gdf_sim)
ax_left.annotate(
    f"\ucd1d \uc2dc\ubbac\ub808\uc774\uc158 \ud544\uc9c0: {total_sim:,}",
    xy=(0.02, 0.03), xycoords="axes fraction",
    fontsize=9, color="#555555",
    bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#cccccc", alpha=0.85),
)

ax_left.set_axis_off()

# ───────────── RIGHT: zone distribution ──────────────────────────────
ax_right.set_title(
    "\uc2dc\ubbac\ub808\uc774\uc158 \ub300\uc0c1 \ud544\uc9c0 \ubd84\ud3ec\n"
    "(\uc6a9\ub3c4\uc9c0\uc5ed\ubcc4)",
    fontsize=13, pad=12,
)

# background
gdf.plot(ax=ax_right, color="#ededed", edgecolor="#cccccc", linewidth=0.1)

# color by zone
legend_handles = []
for code, (label, color) in ZONE_MAP.items():
    subset = gdf_sim[gdf_sim["landuse_code"] == code]
    if len(subset) == 0:
        continue
    subset.plot(ax=ax_right, color=color, edgecolor="none", linewidth=0.0)
    legend_handles.append(Patch(facecolor=color, label=f"{label} ({len(subset):,}\ud544\uc9c0)"))

ax_right.legend(
    handles=legend_handles,
    loc="lower right",
    fontsize=10,
    frameon=True,
    facecolor="white",
    edgecolor="#cccccc",
    framealpha=0.9,
    title="\uc6a9\ub3c4\uc9c0\uc5ed",
    title_fontsize=11,
)
ax_right.set_axis_off()

# ── 6. save SVG ──────────────────────────────────────────────────────
fig.savefig(OUT_SVG, format="svg", bbox_inches="tight", pad_inches=0.3)
plt.close(fig)
print(f"\nSaved: {OUT_SVG}")
print("Done.")
