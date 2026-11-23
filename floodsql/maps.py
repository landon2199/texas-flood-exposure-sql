"""Figures (matplotlib) and the interactive map (folium)."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import shapely  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

# Ordinal exposure ramp (one warm hue, dark -> light) + neutral for "other".
EXPO = {"in SFHA": "#7a1a12", "in 0.2% zone": "#e0542a", "within 250 m of SFHA": "#f5b96b", "other": "#b8b6b0"}
SFHA_FILL, X02_FILL = "#2a78d6", "#9ec5f0"
SURFACE, INK, INK2 = "#fcfcfb", "#0b0b0b", "#52514e"
TYPE_LABEL = {"hospital": "Hospitals", "nursing_home": "Nursing homes", "public_school": "Public schools",
              "fire_ems": "Fire / EMS stations"}
TYPE_MARK = {"hospital": "P", "nursing_home": "s", "public_school": "o", "fire_ems": "^"}
TYPE_COLOR = {"hospital": "#2a78d6", "nursing_home": "#eb6834", "public_school": "#1baf7a", "fire_ems": "#6250d6"}

plt.rcParams.update({"font.family": "DejaVu Sans", "axes.edgecolor": "#d6d5d0", "axes.labelcolor": INK2,
                     "xtick.color": INK2, "ytick.color": INK2, "figure.facecolor": SURFACE,
                     "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE})


def _gdf(con, sql, crs):
    df = con.execute(sql).df()
    return gpd.GeoDataFrame(df, geometry=shapely.from_wkb(df.pop("wkb").map(bytes)), crs=crs)


def expo_class(df):
    return np.select([df.in_sfha, df.in_02pct, df.near_sfha_250m], list(EXPO)[:3], default="other")


def load_layers(con, crs, tol_m=25):
    counties = _gdf(con, "SELECT county_fips, county_name, ST_AsWKB(ST_SimplifyPreserveTopology(geom, 100)) wkb FROM counties", crs)
    zones = _gdf(con, f"""SELECT county_fips, zone_class, ST_AsWKB(ST_SimplifyPreserveTopology(geom, {tol_m})) wkb
                         FROM flood_zones WHERE zone_class IN ('SFHA','X_0.2PCT')""", crs)
    fac = _gdf(con, "SELECT * EXCLUDE (geom), ST_AsWKB(geom) wkb FROM facility_exposure", crs)
    fac["expo"] = expo_class(fac)
    return counties, zones, fac


def _draw_map(ax, counties, zones, fac, size=14, legend=True):
    zones[zones.zone_class == "X_0.2PCT"].plot(ax=ax, color=X02_FILL, alpha=0.55, linewidth=0)
    zones[zones.zone_class == "SFHA"].plot(ax=ax, color=SFHA_FILL, alpha=0.55, linewidth=0)
    counties.boundary.plot(ax=ax, color="#6b6a66", linewidth=0.8)
    for cls in reversed(list(EXPO)):
        sub = fac[fac.expo == cls]
        for t, g in sub.groupby("ftype"):
            ax.scatter(g.geometry.x, g.geometry.y, s=size if cls != "other" else size * 0.45,
                       marker=TYPE_MARK[t], c=EXPO[cls], edgecolors="white" if cls != "other" else "none",
                       linewidths=0.4, zorder=3 if cls != "other" else 2)
    ax.set_axis_off()
    ax.set_aspect("equal")


def _legend_handles():
    h = [Patch(color=SFHA_FILL, alpha=0.55, label="SFHA (1% annual chance)"),
         Patch(color=X02_FILL, alpha=0.55, label="0.2% annual chance zone")]
    h += [Line2D([], [], marker="o", ls="", markersize=7, markerfacecolor=c, markeredgecolor="white", label=f"Facility {k}")
          for k, c in EXPO.items()]
    h += [Line2D([], [], marker=m, ls="", markersize=7, markerfacecolor="#ffffff", markeredgecolor=INK2,
                 label=TYPE_LABEL[t]) for t, m in TYPE_MARK.items()]
    return h


def county_maps(counties, zones, fac, out: Path):
    names = list(counties.sort_values("county_name").county_name)
    fig, axes = plt.subplots(2, 3, figsize=(15, 10.5))
    for ax, name in zip(axes.flat, names):
        c = counties[counties.county_name == name]
        fips = c.county_fips.iloc[0]
        _draw_map(ax, c, zones[zones.county_fips == fips], fac[fac.county_fips == fips], size=16)
        f = fac[fac.county_fips == fips]
        ax.set_title(f"{name} County  ·  {int(f.in_sfha.sum())} of {len(f)} facilities in SFHA",
                     fontsize=11, color=INK, loc="left")
    leg_ax = axes.flat[-1]
    leg_ax.set_axis_off()
    leg_ax.legend(handles=_legend_handles(), loc="center left", frameon=False, fontsize=10)
    fig.suptitle("Critical facilities and FEMA flood hazard zones, by county", fontsize=15, color=INK, x=0.02, ha="left")
    fig.text(0.02, 0.01, "Data: FEMA National Flood Hazard Layer; HIFLD via FEMA RAPT; USGS National Map; Census TIGERweb.",
             fontsize=8.5, color=INK2)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(out, dpi=110)
    plt.close(fig)


def exposure_bars(summary, out: Path):
    s = summary[(summary.county != "All") & (summary.facility_type != "All")].copy()
    counties = sorted(s.county.unique(), reverse=True)
    fig, axes = plt.subplots(1, 4, figsize=(15, 4.8), sharey=True)
    for ax, t in zip(axes, TYPE_LABEL):
        g = s[s.facility_type == t].set_index("county").reindex(counties)
        a = 100 * g.n_sfha / g.n_facilities
        b = 100 * g.n_02pct / g.n_facilities
        c = 100 * g.n_outside_within_250m / g.n_facilities
        y = np.arange(len(counties))
        left = np.zeros(len(counties))
        for vals, key in ((a, "in SFHA"), (b, "in 0.2% zone"), (c, "within 250 m of SFHA")):
            ax.barh(y, vals, left=left, color=EXPO[key], height=0.62, edgecolor=SURFACE, linewidth=2, label=key)
            left = left + vals.to_numpy()
        for yi, (tot, n, ns) in enumerate(zip(left, g.n_facilities, g.n_sfha)):
            ax.text(tot + 1.5, yi, f"{int(ns)}/{int(n)}", va="center", fontsize=8.5, color=INK2)
        ax.set_yticks(y, counties)
        ax.set_xlim(0, 100)
        ax.set_title(TYPE_LABEL[t], loc="left", fontsize=11, color=INK)
        ax.set_xlabel("% of facilities")
        ax.grid(axis="x", color="#ecebe7", linewidth=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    axes[0].legend(loc="lower center", bbox_to_anchor=(2.3, -0.36), ncol=3, frameon=False, fontsize=10)
    fig.suptitle("Share of facilities in or near mapped flood hazard zones (label: in SFHA / total)",
                 x=0.01, ha="left", fontsize=13, color=INK)
    fig.subplots_adjust(left=0.08, right=0.98, top=0.86, bottom=0.24, wspace=0.12)
    fig.savefig(out, dpi=110)
    plt.close(fig)


def proximity_plot(fac, near=(100, 250), search=1000, out: Path = None):
    fig, ax = plt.subplots(figsize=(9, 5.2))
    outside = fac[~fac.in_sfha]
    for t in TYPE_LABEL:
        g = outside[outside.ftype == t]
        d = np.sort(g.dist_sfha_m.dropna().to_numpy())
        x = np.concatenate([[0], d, [search]])
        y = np.concatenate([[0], np.arange(1, len(d) + 1), [len(d)]]) / len(g) * 100
        ax.step(x, y, where="post", color=TYPE_COLOR[t], linewidth=2, label=f"{TYPE_LABEL[t]} (n={len(g)})")
    for v in near:
        ax.axvline(v, color=INK2, linewidth=0.8, linestyle=(0, (3, 3)))
        ax.text(v + 8, 2, f"{v} m", color=INK2, fontsize=9)
    ax.set_xlim(0, search)
    ax.set_ylim(0, None)
    ax.set_xlabel("Distance to the nearest SFHA polygon (m)")
    ax.set_ylabel("% of facilities outside the SFHA (cumulative)")
    ax.set_title("How close facilities outside the SFHA are to its edge", loc="left", color=INK, fontsize=12)
    ax.grid(color="#ecebe7", linewidth=0.8)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")
    fig.tight_layout()
    fig.savefig(out, dpi=120)
    plt.close(fig)


def hero(counties, zones, fac, totals: dict, out: Path):
    """16:9 thumbnail: Houston-Galveston region with SFHA and exposed facilities."""
    core = counties[counties.county_name.isin(["Harris", "Fort Bend", "Brazoria", "Galveston"])]
    fips = set(core.county_fips)
    fig = plt.figure(figsize=(16, 9), dpi=100)
    ax = fig.add_axes((0.36, 0.0, 0.64, 1.0))
    _draw_map(ax, core, zones[zones.county_fips.isin(fips)], fac[fac.county_fips.isin(fips)], size=22)
    x0, y0, x1, y1 = core.total_bounds
    pad = 0.03 * (x1 - x0)
    ax.set_xlim(x0 - pad, x1 + pad)
    ax.set_ylim(y0 - pad, y1 + pad)
    for _, r in core.iterrows():
        p = r.geometry.representative_point()
        ax.text(p.x, p.y, r.county_name.upper(), fontsize=11, color="#3d3c39", ha="center", alpha=0.75,
                fontweight="bold", zorder=4)
    t = fig.text
    t(0.035, 0.86, "Texas Gulf Coast", fontsize=17, color=INK2)
    t(0.035, 0.75, f"{totals['n_sfha']:,} of {totals['n_facilities']:,}", fontsize=46, color=EXPO["in SFHA"], fontweight="bold")
    t(0.035, 0.665, "critical facilities sit inside FEMA's\n1%-annual-chance flood zone", fontsize=19, color=INK,
      va="top", linespacing=1.25)
    t(0.035, 0.51, f"{totals['n_02pct']:,} more in the 0.2% zone\n{totals['n_outside_within_250m']:,} outside the SFHA but within 250 m of it",
      va="center", linespacing=1.4,
      fontsize=14, color=INK2)
    t(0.035, 0.44, "Hospitals · nursing homes · public schools · fire/EMS\nHarris, Galveston, Brazoria, Fort Bend, Nueces",
      fontsize=12.5, color=INK2, va="top", linespacing=1.4)
    t(0.035, 0.28, "DuckDB spatial SQL  ·  FEMA NFHL  ·  HIFLD  ·  USGS", fontsize=12, color="#2a78d6", fontweight="bold")
    t(0.035, 0.05, "An exposure screen against regulatory flood maps, not a risk rating of any facility.\n"
      "Map: the four Houston-Galveston counties; totals include Nueces County.", fontsize=10, color=INK2, linespacing=1.5)
    lg = fig.add_axes((0.03, 0.08, 0.3, 0.17))
    lg.set_axis_off()
    h = _legend_handles()[:5]
    lg.legend(handles=h, loc="upper left", frameon=False, fontsize=10.5, ncol=1)
    fig.savefig(out, dpi=100)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Interactive map
# ---------------------------------------------------------------------------
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'


def web_map(con, fac, crs, out: Path, tol_m=40):
    import folium
    from branca.element import Element

    # SFHA dissolved per county in SQL, simplified, then rounded to ~1 m: keeps the page small.
    sfha = _gdf(con, f"""SELECT county_fips,
                                ST_AsWKB(ST_Transform(ST_SimplifyPreserveTopology(ST_Union_Agg(geom), {tol_m}),
                                                      '{crs}', 'EPSG:4326', always_xy := true)) wkb
                         FROM flood_sfha GROUP BY county_fips""", "EPSG:4326")
    sfha["geometry"] = shapely.set_precision(sfha.geometry.values, 1e-5)
    m = folium.Map(location=[29.45, -95.3], zoom_start=9, tiles=None, control_scale=True)
    folium.TileLayer(OSM_TILES, attr=OSM_ATTR, name="OpenStreetMap (gray)", max_zoom=19, class_name="gray-base").add_to(m)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap (color)", show=False).add_to(m)
    m.get_root().header.add_child(folium.Element("<style>.gray-base{filter:grayscale(1) brightness(1.06) contrast(0.92);}</style>"))
    folium.GeoJson(sfha.to_json(drop_id=True), name="FEMA SFHA (1% annual chance), simplified",
                   style_function=lambda _: {"fillColor": SFHA_FILL, "color": SFHA_FILL, "weight": 0.3, "fillOpacity": 0.35},
                   interactive=False).add_to(m)
    order = {"in SFHA": 0, "in 0.2% zone": 1, "within 250 m of SFHA": 2, "other": 3}
    d = fac.copy()
    d["Facility"] = d["name"].fillna("(unnamed)")
    d["Type"] = d.ftype.map(TYPE_LABEL)
    d["County"] = d.county_name
    d["Flood zone"] = [(z if isinstance(z, str) else "no NFHL polygon") + (f" ({s.title()})" if isinstance(s, str) else "")
                       for z, s in zip(d.fld_zone, d.zone_subty)]
    d["Screen"] = d.expo
    d["To SFHA"] = ["inside" if i else (f"{x:,.0f} m" if pd.notna(x) else "> 1 km") for i, x in zip(d.in_sfha, d.dist_sfha_m)]
    d["Capacity"] = [f"{int(c):,} {k}" if pd.notna(c) else "n/a" for c, k in zip(d.capacity, d.capacity_kind)]
    d["o"] = d.expo.map(order)
    fields = ["Facility", "Type", "County", "Flood zone", "Screen", "To SFHA", "Capacity"]
    for t, label in TYPE_LABEL.items():
        g = d[d.ftype == t].sort_values("o", ascending=False)
        g = gpd.GeoDataFrame(g[fields], geometry=gpd.points_from_xy(g.lon.round(5), g.lat.round(5)), crs="EPSG:4326")
        folium.GeoJson(
            g.to_json(drop_id=True), name=label,
            marker=folium.CircleMarker(radius=5, weight=0.8, color="#ffffff", fill=True, fill_opacity=0.95),
            style_function=lambda f: {"fillColor": EXPO[f["properties"]["Screen"]],
                                      "radius": 3.5 if f["properties"]["Screen"] == "other" else 6},
            tooltip=folium.GeoJsonTooltip(fields=["Facility"], labels=False),
            popup=folium.GeoJsonPopup(fields=fields),
        ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    items = "".join(f'<div><span style="background:{c};border:1px solid #fff;display:inline-block;width:12px;height:12px;'
                    f'border-radius:50%;margin-right:6px;vertical-align:middle"></span>Facility {k}</div>' for k, c in EXPO.items())
    legend = f"""
    <div style="position:fixed;bottom:28px;left:12px;z-index:9999;background:rgba(252,252,251,.95);padding:10px 12px;
                border:1px solid #d6d5d0;border-radius:6px;font:12px/1.5 system-ui,sans-serif;color:#0b0b0b;max-width:260px">
      <div style="font-weight:600;margin-bottom:4px">Flood exposure screen</div>
      <div><span style="background:{SFHA_FILL};opacity:.5;display:inline-block;width:14px;height:10px;margin-right:6px"></span>
      FEMA SFHA (1% annual chance)</div>{items}
      <div style="color:#52514e;margin-top:6px">Sources: FEMA NFHL; HIFLD (via FEMA RAPT); USGS National Map.
      Exposure screen, not a risk rating.</div>
    </div>"""
    title = """<div style="position:fixed;top:10px;left:56px;z-index:9999;background:rgba(252,252,251,.95);padding:6px 10px;
               border:1px solid #d6d5d0;border-radius:6px;font:600 14px system-ui,sans-serif;color:#0b0b0b">
               Critical facilities in FEMA flood hazard areas · Texas Gulf Coast</div>"""
    m.get_root().header.add_child(Element("<title>Texas Flood Exposure Map</title>"))
    m.get_root().html.add_child(Element(legend + title))
    m.save(str(out))
    return out.stat().st_size
