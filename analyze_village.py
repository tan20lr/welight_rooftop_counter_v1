#!/usr/bin/env python3
"""
WeLight Village Solar Investment Analyzer
Analyzes solar mini-grid investment potential for rural Madagascar villages.

Usage:
    python analyze_village.py --village "Betsiaka" --lat -13.7103 --lon 49.6583 --households 353
    python analyze_village.py --village "Betsiaka" --lat -13.7103 --lon 49.6583 --households 353 --config config.json
"""

import sys
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import math
import os
import sys
import warnings
from datetime import datetime, timedelta

import folium
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

# ==============================================================================
# DEFAULT CONFIGURATION
# ==============================================================================

DEFAULT_CONFIG = {
    "penetration_rate":                  0.60,
    "monthly_tariff_eur":                1.60,
    "capex_per_kwp_eur":                 1200,
    "consumption_per_household_kwh_day": 0.3,
    "productive_use_factor":             1.3,
    "battery_autonomy_days":             1.5,
    "system_efficiency":                 0.75,
    "opex_pct_capex":                    0.04,
    "project_lifetime_years":            15,
    "discount_rate":                     0.10,
    "max_acceptable_payback_years":      7,
    "acled_api_key":                     "",
    "acled_email":                       "",
    "welight_sites":                     [],
    "output_dir":                        "outputs",
}

# Fallback JIRAMA nodes (major electrified towns, northern Madagascar)
JIRAMA_NODES = [
    ("Antsiranana",  -12.3589, 49.2951),
    ("Ambilobe",     -13.1944, 49.0499),
    ("Ambanja",      -13.6439, 48.4526),
    ("Sambava",      -14.2664, 50.1664),
    ("Antalaha",     -14.8894, 50.2736),
    ("Vohemar",      -13.3617, 50.0053),
    ("Andapa",       -14.6508, 49.6347),
    ("Nosy Be",      -13.3295, 48.2705),
    ("Maroantsetra", -15.4333, 49.7500),
    ("Mananara",     -16.1667, 49.7667),
    ("Mandritsara",  -15.8333, 48.8333),
    ("Mahajanga",    -15.7167, 46.3167),
]

OUTPUT_DIR = "outputs"


# ==============================================================================
# UTILITIES
# ==============================================================================

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))


def load_config(path=None):
    cfg = DEFAULT_CONFIG.copy()
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def compute_irr(cashflows, tol=1e-7, max_iter=500):
    """Newton-Raphson IRR; returns None if non-convergent."""
    r = 0.1
    for _ in range(max_iter):
        npv  = sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows))
        dnpv = sum(-t * cf / (1 + r) ** (t + 1) for t, cf in enumerate(cashflows))
        if abs(dnpv) < 1e-12:
            break
        r_new = r - npv / dnpv
        if abs(r_new - r) < tol:
            return r_new if -1 < r_new < 10 else None
        r = r_new
    return None


def flag(value, green_thresh, amber_thresh, higher_is_better=True):
    """Return 'green' / 'amber' / 'red' CSS color class."""
    if higher_is_better:
        if value >= green_thresh:  return "green"
        if value >= amber_thresh:  return "amber"
        return "red"
    else:
        if value <= green_thresh:  return "green"
        if value <= amber_thresh:  return "amber"
        return "red"


# ==============================================================================
# MODULE 1 -- REVENUE PROJECTION
# ==============================================================================

def module_revenue(households, cfg, has_commercial=False):
    puf         = cfg["productive_use_factor"] if has_commercial else 1.0
    subscribers = households * cfg["penetration_rate"]
    annual_rev  = subscribers * cfg["monthly_tariff_eur"] * 12 * puf
    return {
        "subscribers":              round(subscribers, 1),
        "annual_revenue_eur":       round(annual_rev, 2),
        "productive_use_applied":   has_commercial,
        "productive_use_factor":    puf,
        "monthly_tariff_eur":       cfg["monthly_tariff_eur"],
        "penetration_rate":         cfg["penetration_rate"],
        "_source":                  "computed",
    }


# ==============================================================================
# MODULE 2 -- SOLAR RESOURCE
# ==============================================================================

def _fetch_global_solar_atlas(lat, lon):
    url = f"https://api.globalsolaratlas.info/data/lta?loc={lat},{lon}"
    r   = requests.get(url, timeout=15, headers={"User-Agent": "WeLight-Analyzer/1.0"})
    r.raise_for_status()
    data = r.json()
    # Response structure: {"annual":{"data":{"GHI":5.5,...}}, "monthly":{"data":{"GHI":{...}}}}
    ann_data = data.get("annual", {}).get("data", {})
    ghi_ann  = ann_data.get("GHI") or ann_data.get("ghi")
    if ghi_ann is None:
        raise ValueError(f"GHI not found in response: {list(ann_data.keys())[:5]}")
    mon_data = (data.get("monthly", {}).get("data", {})
                    .get("GHI") or data.get("monthly", {}).get("data", {}).get("ghi") or {})
    monthly = []
    for m in range(1, 13):
        v = mon_data.get(str(m)) or mon_data.get(f"{m:02d}")
        monthly.append(float(v) if v is not None else None)
    return float(ghi_ann), monthly


def _fetch_nasa_power(lat, lon):
    url = "https://power.larc.nasa.gov/api/temporal/climatology/point"
    r   = requests.get(url, params={
        "parameters": "ALLSKY_SFC_SW_DWN",
        "community":  "RE",
        "longitude":  lon,
        "latitude":   lat,
        "format":     "JSON",
    }, timeout=30)
    r.raise_for_status()
    d      = r.json()["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
    annual = float(d["ANN"])
    # NASA POWER uses zero-padded month keys "01"..."12"
    monthly = []
    for m in range(1, 13):
        for key in (f"{m:02d}", str(m)):
            if key in d:
                val = d[key]
                monthly.append(float(val) if val not in (None, -999, "-999") else None)
                break
        else:
            monthly.append(None)
    return annual, monthly


def module_solar_resource(lat, lon):
    errors = []
    for fetcher, name in [(_fetch_global_solar_atlas, "Global Solar Atlas"),
                          (_fetch_nasa_power,          "NASA POWER")]:
        try:
            ghi_annual, ghi_monthly = fetcher(lat, lon)
            return {
                "ghi_annual_kwh_m2_day": round(ghi_annual, 3),
                "ghi_monthly":           ghi_monthly,
                "source":                name,
                "_estimated":            False,
            }
        except Exception as e:
            errors.append(f"{name}: {e}")

    # Fallback: Madagascar average (~5.5 kWh/m&sup2;/day)
    print(f"  [WARN] Solar API failed ({'; '.join(errors)}) -- using fallback 5.5 kWh/m2/day")
    return {
        "ghi_annual_kwh_m2_day": 5.5,
        "ghi_monthly":           [5.0, 5.3, 5.4, 5.5, 5.5, 5.3, 5.3, 5.6, 5.8, 5.9, 5.8, 5.1],
        "source":                "fallback (Madagascar average)",
        "_estimated":            True,
    }


# ==============================================================================
# MODULE 3 -- SYSTEM SIZING
# ==============================================================================

def module_sizing(households, ghi_annual, cfg):
    puf              = cfg["productive_use_factor"]
    daily_kwh        = households * cfg["consumption_per_household_kwh_day"] * puf
    peak_kwp         = daily_kwh / (ghi_annual * cfg["system_efficiency"])
    battery_kwh      = daily_kwh * cfg["battery_autonomy_days"]
    capex            = peak_kwp * cfg["capex_per_kwp_eur"]
    return {
        "daily_consumption_kwh":  round(daily_kwh, 2),
        "peak_power_kwp":         round(peak_kwp, 2),
        "battery_capacity_kwh":   round(battery_kwh, 2),
        "capex_eur":              round(capex, 2),
        "productive_use_factor":  puf,
        "_source":                "computed",
    }


# ==============================================================================
# MODULE 4 -- FINANCIAL MODEL
# ==============================================================================

def module_financials(capex, annual_revenue, cfg):
    n            = cfg["project_lifetime_years"]
    dr           = cfg["discount_rate"]
    annual_opex  = capex * cfg["opex_pct_capex"]
    annual_margin = annual_revenue - annual_opex

    if annual_margin <= 0:
        payback = float("inf")
    else:
        payback = capex / annual_margin

    cashflows = [-capex] + [annual_margin] * n
    npv  = sum(cf / (1 + dr) ** t for t, cf in enumerate(cashflows))
    irr  = compute_irr(cashflows)

    table = []
    cumulative = -capex
    for yr in range(1, n + 1):
        cumulative += annual_margin
        table.append({
            "year":                yr,
            "revenue_eur":         round(annual_revenue, 2),
            "opex_eur":            round(annual_opex, 2),
            "net_margin_eur":      round(annual_margin, 2),
            "cumulative_cf_eur":   round(cumulative, 2),
        })

    return {
        "annual_opex_eur":        round(annual_opex, 2),
        "annual_margin_eur":      round(annual_margin, 2),
        "payback_years":          round(payback, 2) if payback != float("inf") else None,
        "npv_eur":                round(npv, 2),
        "irr_pct":                round(irr * 100, 2) if irr is not None else None,
        "is_viable":              payback <= cfg["max_acceptable_payback_years"],
        "cashflow_table":         table,
        "_source":                "computed",
    }


# ==============================================================================
# MODULE 5 -- CONSTRAINTS
# ==============================================================================

def _grid_distance_overpass(lat, lon, radius_km=100):
    """Query OSM Overpass for power lines and return distance to nearest (km)."""
    bb = radius_km / 111.0
    bbox = f"{lat-bb},{lon-bb},{lat+bb},{lon+bb}"
    query = f"""
[out:json][timeout:20];
(
  way["power"="line"]({bbox});
  way["power"="minor_line"]({bbox});
);
out geom;
"""
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, timeout=25)
        r.raise_for_status()
        elements = r.json().get("elements", [])
        if not elements:
            return None, "no power lines in OSM within 100km"
        min_dist = float("inf")
        for el in elements:
            for node in el.get("geometry", []):
                d = haversine(lat, lon, node["lat"], node["lon"])
                if d < min_dist:
                    min_dist = d
        return round(min_dist, 1), "OSM Overpass"
    except Exception as e:
        return None, str(e)


def _grid_distance_fallback(lat, lon):
    """Distance to nearest known JIRAMA town."""
    best = min(JIRAMA_NODES, key=lambda t: haversine(lat, lon, t[1], t[2]))
    d    = haversine(lat, lon, best[1], best[2])
    return round(d, 1), f"fallback (nearest JIRAMA town: {best[0]})"


def get_grid_distance(lat, lon):
    d, src = _grid_distance_overpass(lat, lon)
    if d is None:
        d, src = _grid_distance_fallback(lat, lon)
    return d, src


def _road_score_overpass(lat, lon, radius_km=50):
    """
    Returns (score 0-10, road_type_str, source).
    10 = paved road within 20km, 5 = track only, 0 = no access.
    """
    bb = radius_km / 111.0
    bbox = f"{lat-bb},{lon-bb},{lat+bb},{lon+bb}"
    query = f"""
[out:json][timeout:20];
way["highway"~"primary|secondary|tertiary|residential|track|path"]({bbox});
out tags center;
"""
    PAVED  = {"primary", "secondary", "trunk", "motorway"}
    MODEST = {"tertiary", "residential", "unclassified"}
    ROUGH  = {"track", "path", "footway"}
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, timeout=25)
        r.raise_for_status()
        elements = r.json().get("elements", [])
        types = {el["tags"].get("highway", "") for el in elements if "tags" in el}
        if types & PAVED:
            return 10, "paved road within 50km", "OSM Overpass"
        if types & MODEST:
            return 7,  "tertiary/residential road", "OSM Overpass"
        if types & ROUGH:
            return 5,  "track/path only", "OSM Overpass"
        return 3, "no classified road found", "OSM Overpass"
    except Exception as e:
        return 5, "unknown (API unavailable)", f"fallback ({e})"


def _acled_score(lat, lon, cfg):
    """
    Query ACLED for security incidents within 100km in last 24 months.
    Returns instability_score 0-10 and event count.
    """
    key   = cfg.get("acled_api_key", "")
    email = cfg.get("acled_email", "")
    if not key or not email:
        return 5, 0, "fallback (no ACLED credentials)"

    end_date   = datetime.utcnow()
    start_date = end_date - timedelta(days=730)
    params = {
        "key":        key,
        "email":      email,
        "country":    "Madagascar",
        "event_date": f"{start_date.strftime('%Y-%m-%d')}|{end_date.strftime('%Y-%m-%d')}",
        "latitude":   lat,
        "longitude":  lon,
        "radius":     100,
        "limit":      1000,
    }
    try:
        r = requests.get("https://api.acleddata.com/acled/read",
                         params=params, timeout=20)
        r.raise_for_status()
        events = r.json().get("data", [])
        weights = {
            "Battles":                     3,
            "Violence against civilians":  2,
            "Explosions/Remote violence":  2,
            "Riots":                       1,
            "Protests":                    0.3,
            "Strategic developments":      0.5,
        }
        weighted = sum(weights.get(e.get("event_type", ""), 0.5) for e in events)
        score = min(10.0, weighted / 5.0)
        return round(score, 1), len(events), "ACLED API"
    except Exception as e:
        return 5, 0, f"fallback (ACLED error: {e})"


def _nearest_welight(lat, lon, sites):
    if not sites:
        return None, None
    best = min(sites, key=lambda s: haversine(lat, lon, s["lat"], s["lon"]))
    return round(haversine(lat, lon, best["lat"], best["lon"]), 1), best.get("name", "Unknown")


def _commercial_activity_osm(lat, lon, radius_km=3):
    """Check if OSM has commercial activity (shops, markets) near the village."""
    bb = radius_km / 111.0
    bbox = f"{lat-bb},{lon-bb},{lat+bb},{lon+bb}"
    query = f"""
[out:json][timeout:10];
(
  node["shop"]({bbox});
  node["amenity"~"marketplace|bank|fuel"]({bbox});
);
out count;
"""
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, timeout=12)
        r.raise_for_status()
        total = int(r.json()["elements"][0]["tags"].get("total", 0))
        return total > 0, total, "OSM Overpass"
    except Exception:
        return False, 0, "fallback"


def module_constraints(lat, lon, cfg):
    print("  -> Grid distance")
    grid_km, grid_src = get_grid_distance(lat, lon)

    print("  -> Road access")
    road_score, road_type, road_src = _road_score_overpass(lat, lon)

    print("  -> Security (ACLED)...")
    instability, n_events, sec_src = _acled_score(lat, lon, cfg)

    print("  -> Commercial activity")
    has_commercial, n_shops, comm_src = _commercial_activity_osm(lat, lon)

    welight_km, welight_name = _nearest_welight(lat, lon, cfg.get("welight_sites", []))

    return {
        "grid_distance_km":   grid_km,
        "grid_source":        grid_src,
        "road_score":         road_score,
        "road_type":          road_type,
        "road_source":        road_src,
        "instability_score":  instability,
        "acled_events_count": n_events,
        "security_source":    sec_src,
        "has_commercial":     has_commercial,
        "commercial_count":   n_shops,
        "commercial_source":  comm_src,
        "welight_nearest_km": welight_km,
        "welight_nearest_name": welight_name,
    }


# ==============================================================================
# PRIORITY SCORE
# ==============================================================================

def compute_priority_score(fin, solar, constraints):
    """
    0-100 score:
      Financial viability (40pts)
      Solar resource (20pts)
      Grid distance (20pts)
      Road access (10pts)
      Security (10pts)
    """
    payback = fin["payback_years"] or 999

    # Financial (40)
    if payback <= 5:   pts_fin = 40
    elif payback <= 7: pts_fin = 30
    elif payback <= 10: pts_fin = 15
    else:              pts_fin = 0

    # Solar (20) -- max at 6.5 kWh/m&sup2;/day
    ghi = solar["ghi_annual_kwh_m2_day"]
    pts_solar = min(20.0, ghi / 6.5 * 20)

    # Grid distance (20)
    gd = constraints["grid_distance_km"] or 0
    if gd > 50:   pts_grid = 20
    elif gd > 20: pts_grid = 10
    else:         pts_grid = 0

    # Road (10)
    pts_road = constraints["road_score"]

    # Security (10) -- inverted instability
    pts_sec = max(0.0, 10 - constraints["instability_score"])

    total = pts_fin + pts_solar + pts_grid + pts_road + pts_sec
    return {
        "total":     round(total, 1),
        "financial": round(pts_fin, 1),
        "solar":     round(pts_solar, 1),
        "grid":      round(pts_grid, 1),
        "road":      round(pts_road, 1),
        "security":  round(pts_sec, 1),
    }


# ==============================================================================
# MODULE 6 -- OUTPUTS
# ==============================================================================

def build_folium_map(village_name, lat, lon, constraints):
    m = folium.Map(location=[lat, lon], zoom_start=9, tiles="CartoDB positron")

    # 50km radius
    folium.Circle(
        location=[lat, lon], radius=50_000,
        color="#94A3B8", fill=False, weight=1.5, dash_array="6",
        tooltip="50 km radius",
    ).add_to(m)

    # Village marker
    score_color = "green" if constraints.get("instability_score", 5) < 3 else \
                  "orange" if constraints.get("instability_score", 5) < 6 else "red"
    folium.Marker(
        location=[lat, lon],
        popup=f"<b>{village_name}</b>",
        tooltip=village_name,
        icon=folium.Icon(color="blue", icon="bolt", prefix="fa"),
    ).add_to(m)

    # Nearest WeLight site
    sites = constraints.get("_welight_sites", [])
    for s in sites:
        folium.Marker(
            location=[s["lat"], s["lon"]],
            tooltip=f"WeLight: {s.get('name', '')}",
            icon=folium.Icon(color="green", icon="sun-o", prefix="fa"),
        ).add_to(m)

    # Nearest JIRAMA town (fallback grid proxy)
    best_jirama = min(JIRAMA_NODES,
                      key=lambda t: haversine(lat, lon, t[1], t[2]))
    folium.Marker(
        location=[best_jirama[1], best_jirama[2]],
        tooltip=f"JIRAMA: {best_jirama[0]} ({constraints.get('grid_distance_km','?')} km)",
        icon=folium.Icon(color="red", icon="plug", prefix="fa"),
    ).add_to(m)
    folium.PolyLine(
        locations=[[lat, lon], [best_jirama[1], best_jirama[2]]],
        color="#EF4444", weight=1.5, dash_array="4",
        tooltip=f"Distance reseau: {constraints.get('grid_distance_km','?')} km",
    ).add_to(m)

    folium.LayerControl().add_to(m)
    return m._repr_html_()


def _score_bar(score, max_score=100):
    pct  = score / max_score * 100
    color = "#22C55E" if score >= 60 else "#F59E0B" if score >= 35 else "#EF4444"
    return f"""
    <div style="background:#E5E7EB;border-radius:4px;height:10px;width:100%">
      <div style="background:{color};width:{pct:.0f}%;height:10px;border-radius:4px"></div>
    </div>"""


def _flag_row(label, value_str, flag_color, note=""):
    icons = {"green": "?", "amber": "==============================================================================", "red": "?"}
    icon  = icons.get(flag_color, "==============================================================================")
    color_map = {"green": "#166534", "amber": "#92400E", "red": "#9F1239"}
    color = color_map.get(flag_color, "#111")
    return f"""
    <tr>
      <td style="padding:6px 10px;color:#6B7280;font-size:13px">{label}</td>
      <td style="padding:6px 10px;font-weight:600;color:{color};font-size:13px">{icon} {value_str}</td>
      <td style="padding:6px 10px;color:#9CA3AF;font-size:12px">{note}</td>
    </tr>"""


def generate_html_report(village_name, lat, lon, households, cfg,
                          revenue, solar, sizing, fin, constraints, score):
    os.makedirs(cfg.get("output_dir", OUTPUT_DIR), exist_ok=True)
    slug  = village_name.lower().replace(" ", "_")
    path  = os.path.join(cfg.get("output_dir", OUTPUT_DIR), f"{slug}_analysis.html")

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    total_score = score["total"]
    score_color = "#22C55E" if total_score >= 60 else \
                  "#F59E0B" if total_score >= 35 else "#EF4444"
    score_label = ("Priority" if total_score >= 60 else
                   "To evaluate" if total_score >= 35 else "Low potential")

    payback_str = f"{fin['payback_years']:.1f} yrs" if fin["payback_years"] else "N/A"
    irr_str     = f"{fin['irr_pct']:.1f}%" if fin["irr_pct"] is not None else "N/A"
    npv_str     = f"&euro;{fin['npv_eur']:,.0f}"

    # Flags
    gd  = constraints["grid_distance_km"] or 0
    pb  = fin["payback_years"] or 999
    ins = constraints["instability_score"]
    rd  = constraints["road_score"]

    flag_grid  = flag(gd,  50, 20, higher_is_better=True)
    flag_pay   = flag(pb,  cfg["max_acceptable_payback_years"], 10, higher_is_better=False)
    flag_sec   = flag(ins, 3,  6,  higher_is_better=False)
    flag_road  = flag(rd,  8,  5,  higher_is_better=True)

    # Cashflow table rows
    table_rows = ""
    for row in fin["cashflow_table"]:
        cf_color = "#166534" if row["cumulative_cf_eur"] >= 0 else "#9F1239"
        table_rows += f"""
        <tr style="border-top:1px solid #F3F4F6">
          <td style="padding:5px 8px;text-align:center">{row['year']}</td>
          <td style="padding:5px 8px;text-align:right">?{row['revenue_eur']:,.0f}</td>
          <td style="padding:5px 8px;text-align:right;color:#9F1239">?{row['opex_eur']:,.0f}</td>
          <td style="padding:5px 8px;text-align:right">?{row['net_margin_eur']:,.0f}</td>
          <td style="padding:5px 8px;text-align:right;color:{cf_color};font-weight:600">
            ?{row['cumulative_cf_eur']:,.0f}
          </td>
        </tr>"""

    # Score breakdown table
    score_rows = ""
    for label, pts, maxpts in [
        ("Financial viability", score["financial"], 40),
        ("Solar resource",      score["solar"],     20),
        ("Grid distance",       score["grid"],      20),
        ("Road access",         score["road"],      10),
        ("Security",            score["security"],  10),
    ]:
        pct = pts / maxpts * 100
        c   = "#22C55E" if pct >= 60 else "#F59E0B" if pct >= 30 else "#EF4444"
        score_rows += f"""
        <tr style="border-top:1px solid #F3F4F6">
          <td style="padding:6px 10px;font-size:13px">{label}</td>
          <td style="padding:6px 10px;font-size:13px;font-weight:600;color:{c}">{pts:.0f}/{maxpts}</td>
          <td style="padding:6px 10px;width:150px">{_score_bar(pct, 100)}</td>
        </tr>"""

    map_html = build_folium_map(village_name, lat, lon, constraints)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WeLight -- {village_name} Solar Analysis</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ font-family:Arial,Helvetica,sans-serif; background:#F8FAFC; color:#111827 }}
  .page {{ max-width:960px; margin:0 auto; padding:20px }}
  .header {{ background:#1F4E79; color:white; padding:20px 28px;
             border-radius:8px; margin-bottom:20px; display:flex;
             justify-content:space-between; align-items:center }}
  .header h1 {{ font-size:22px; font-weight:700 }}
  .header .meta {{ font-size:12px; color:#93C5FD; text-align:right }}
  .score-circle {{ width:90px; height:90px; border-radius:50%;
                   background:{score_color}; display:flex; flex-direction:column;
                   align-items:center; justify-content:center; color:white;
                   font-weight:700; flex-shrink:0 }}
  .score-circle .num {{ font-size:28px; line-height:1 }}
  .score-circle .lbl {{ font-size:10px; margin-top:3px }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px }}
  .grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-bottom:16px }}
  .card {{ background:white; border-radius:8px; padding:16px 20px;
            box-shadow:0 1px 3px rgba(0,0,0,.08) }}
  .card h2 {{ font-size:14px; color:#6B7280; font-weight:600;
               text-transform:uppercase; letter-spacing:.04em; margin-bottom:12px }}
  .kpi-row {{ display:flex; gap:20px; flex-wrap:wrap }}
  .kpi {{ flex:1; min-width:100px }}
  .kpi .val {{ font-size:22px; font-weight:700; color:#1F4E79 }}
  .kpi .lbl {{ font-size:11px; color:#9CA3AF; margin-top:2px }}
  table {{ width:100%; border-collapse:collapse }}
  th {{ background:#F1F5F9; padding:7px 10px; text-align:left;
        font-size:12px; color:#6B7280; font-weight:600 }}
  .section-title {{ font-size:15px; font-weight:700; color:#1F4E79;
                    margin:20px 0 10px; border-left:4px solid #2563EB;
                    padding-left:10px }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:12px;
             font-size:12px; font-weight:700; background:{score_color};
             color:white }}
  .green {{ color:#166534 }} .amber {{ color:#92400E }} .red {{ color:#9F1239 }}
  .note {{ font-size:11px; color:#9CA3AF; margin-top:8px }}
  .map-container {{ height:400px; border-radius:8px; overflow:hidden;
                    box-shadow:0 1px 3px rgba(0,0,0,.08); margin-bottom:16px }}
  .footer {{ text-align:center; font-size:11px; color:#9CA3AF; margin-top:24px }}
</style>
</head>
<body>
<div class="page">

  <!-- Header -->
  <div class="header">
    <div>
      <div style="font-size:12px;color:#93C5FD;margin-bottom:4px">WeLight Africa -- Solar Investment Analysis</div>
      <h1>? {village_name}</h1>
      <div style="font-size:13px;color:#BAE6FD;margin-top:4px">
        {lat:.4f}?, {lon:.4f}? &nbsp;?&nbsp; {households:,} households
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:16px">
      <div class="score-circle">
        <span class="num">{total_score:.0f}</span>
        <span class="lbl">/ 100</span>
      </div>
      <div class="meta">
        <div class="badge">{score_label}</div>
        <div style="margin-top:8px">{ts}</div>
        <div style="color:#93C5FD">GHI: {solar['ghi_annual_kwh_m2_day']:.2f} kWh/m&sup2;/day</div>
        <div style="color:#93C5FD">Source: {solar['source']}</div>
      </div>
    </div>
  </div>

  <!-- KPI summary -->
  <div class="section-title">Summary Scorecard</div>
  <div class="card" style="margin-bottom:16px">
    <div class="kpi-row">
      <div class="kpi">
        <div class="val" style="color:{'#22C55E' if fin['is_viable'] else '#EF4444'}">{payback_str}</div>
        <div class="lbl">Payback period</div>
      </div>
      <div class="kpi">
        <div class="val">{irr_str}</div>
        <div class="lbl">IRR</div>
      </div>
      <div class="kpi">
        <div class="val">{npv_str}</div>
        <div class="lbl">NPV ({cfg['project_lifetime_years']}yr)</div>
      </div>
      <div class="kpi">
        <div class="val">{sizing['peak_power_kwp']:.1f} kWp</div>
        <div class="lbl">Peak power</div>
      </div>
      <div class="kpi">
        <div class="val">&euro;{sizing['capex_eur']:,.0f}</div>
        <div class="lbl">CAPEX</div>
      </div>
      <div class="kpi">
        <div class="val">&euro;{revenue['annual_revenue_eur']:,.0f}</div>
        <div class="lbl">Annual revenue</div>
      </div>
    </div>
  </div>

  <!-- Score breakdown -->
  <div class="grid2">
    <div class="card">
      <h2>Priority Score Breakdown</h2>
      <table>
        <thead><tr>
          <th>Criterion</th><th>Points</th><th style="width:150px">Bar</th>
        </tr></thead>
        <tbody>{score_rows}</tbody>
      </table>
    </div>
    <div class="card">
      <h2>Constraints</h2>
      <table>
        <tbody>
          {_flag_row("Grid distance", f"{gd:.0f} km", flag_grid,
                     constraints.get('grid_source',''))}
          {_flag_row("Payback period", payback_str, flag_pay,
                     f"max {cfg['max_acceptable_payback_years']} yrs")}
          {_flag_row("Security (ACLED)", f"{ins:.1f}/10", flag_sec,
                     f"{constraints['acled_events_count']} events ? {constraints['security_source']}")}
          {_flag_row("Road access", constraints['road_type'], flag_road,
                     constraints['road_source'])}
          {_flag_row("Commercial activity",
                     "Yes" if constraints['has_commercial'] else "No",
                     "green" if constraints['has_commercial'] else "amber",
                     f"{constraints['commercial_count']} OSM nodes")}
        </tbody>
      </table>
      {'<p class="note">Nearest WeLight site: ' + str(constraints["welight_nearest_km"]) + ' km -- ' + str(constraints["welight_nearest_name"]) + '</p>' if constraints["welight_nearest_km"] else ''}
    </div>
  </div>

  <!-- System sizing -->
  <div class="section-title">System Sizing</div>
  <div class="card" style="margin-bottom:16px">
    <div class="kpi-row">
      <div class="kpi">
        <div class="val">{sizing['daily_consumption_kwh']:.1f} kWh</div>
        <div class="lbl">Daily consumption</div>
      </div>
      <div class="kpi">
        <div class="val">{sizing['peak_power_kwp']:.1f} kWp</div>
        <div class="lbl">Peak PV power</div>
      </div>
      <div class="kpi">
        <div class="val">{sizing['battery_capacity_kwh']:.1f} kWh</div>
        <div class="lbl">Battery capacity</div>
      </div>
      <div class="kpi">
        <div class="val">&euro;{sizing['capex_eur']:,.0f}</div>
        <div class="lbl">CAPEX (&euro;{cfg['capex_per_kwp_eur']}/kWp)</div>
      </div>
      <div class="kpi">
        <div class="val">{solar['ghi_annual_kwh_m2_day']:.2f}</div>
        <div class="lbl">GHI (kWh/m&sup2;/day)</div>
      </div>
      <div class="kpi">
        <div class="val">{revenue['subscribers']:.0f}</div>
        <div class="lbl">Subscribers ({cfg['penetration_rate']:.0%})</div>
      </div>
    </div>
    {'<p class="note">? Productive use factor applied (' + str(sizing['productive_use_factor']) + 'x) -- commercial activity detected nearby.</p>' if revenue['productive_use_applied'] else ''}
  </div>

  <!-- Financial projections -->
  <div class="section-title">Financial Projections -- Year 0 to {cfg['project_lifetime_years']}</div>
  <div class="card" style="margin-bottom:16px;overflow-x:auto">
    <table>
      <thead><tr>
        <th>Year</th>
        <th style="text-align:right">Revenue (&euro;)</th>
        <th style="text-align:right">Opex (&euro;)</th>
        <th style="text-align:right">Net margin (&euro;)</th>
        <th style="text-align:right">Cum. CF (&euro;)</th>
      </tr></thead>
      <tbody>
        <tr style="background:#FEF2F2">
          <td style="padding:5px 8px;text-align:center;font-weight:700">0</td>
          <td style="padding:5px 8px;text-align:right">--</td>
          <td style="padding:5px 8px;text-align:right">--</td>
          <td style="padding:5px 8px;text-align:right;color:#9F1239;font-weight:700">
            &minus;&euro;{sizing['capex_eur']:,.0f}
          </td>
          <td style="padding:5px 8px;text-align:right;color:#9F1239;font-weight:700">
            &minus;&euro;{sizing['capex_eur']:,.0f}
          </td>
        </tr>
        {table_rows}
      </tbody>
    </table>
  </div>

  <!-- Map -->
  <div class="section-title">Location Map</div>
  <div class="map-container">
    {map_html}
  </div>

  <div class="footer">
    Generated by WeLight Village Solar Analyzer ? {ts} ?
    Solar: {solar['source']} ? Grid: {constraints['grid_source']}
    {'? ============================================================================== Some data estimated -- check _estimated flags in JSON' if solar['_estimated'] else ''}
  </div>
</div>
</body>
</html>"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def generate_json_output(village_name, lat, lon, households, cfg,
                          revenue, solar, sizing, fin, constraints, score):
    os.makedirs(cfg.get("output_dir", OUTPUT_DIR), exist_ok=True)
    slug = village_name.lower().replace(" ", "_")
    path = os.path.join(cfg.get("output_dir", OUTPUT_DIR), f"{slug}_data.json")

    out = {
        "meta": {
            "village":    village_name,
            "lat":        lat,
            "lon":        lon,
            "households": households,
            "generated":  datetime.utcnow().isoformat() + "Z",
            "config":     cfg,
        },
        "priority_score": score,
        "revenue":         {k: v for k, v in revenue.items() if not k.startswith("_")},
        "solar_resource":  solar,
        "sizing":          {k: v for k, v in sizing.items() if not k.startswith("_")},
        "financials": {
            k: v for k, v in fin.items() if k != "cashflow_table"
        },
        "cashflow_table": fin["cashflow_table"],
        "constraints":    {k: v for k, v in constraints.items()
                           if not k.startswith("_")},
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return path


def print_cli_summary(village_name, lat, lon, households, score, fin, solar,
                       sizing, constraints):
    SEP = "-" * 56
    total = score["total"]
    color_lbl = ("PRIORITY" if total >= 60 else
                 "TO EVALUATE" if total >= 35 else "LOW POTENTIAL")
    payback = f"{fin['payback_years']:.1f} yrs" if fin["payback_years"] else "N/A"
    irr     = f"{fin['irr_pct']:.1f}%"          if fin["irr_pct"] is not None else "N/A"
    lifetime = fin.get("_n", 15)

    print(f"\n{SEP}")
    print(f"  WeLight -- {village_name} ({lat:.4f}, {lon:.4f})")
    print(f"  {households} households  |  {solar['source']}")
    print(SEP)
    print(f"  PRIORITY SCORE   {total:.0f}/100  [{color_lbl}]")
    print(SEP)
    print(f"  Payback period   {payback:<15}  IRR: {irr}")
    print(f"  CAPEX            EUR {sizing['capex_eur']:>10,.0f}")
    print(f"  Annual revenue   EUR {fin['annual_margin_eur'] + fin['annual_opex_eur']:>10,.0f}")
    print(f"  NPV ({lifetime}yr)        EUR {fin['npv_eur']:>10,.0f}")
    print(f"  Peak power       {sizing['peak_power_kwp']:.1f} kWp   GHI {solar['ghi_annual_kwh_m2_day']:.2f} kWh/m2/day")
    print(f"  Grid distance    {constraints['grid_distance_km']:.0f} km   Road: {constraints['road_type']}")
    print(f"  Security         {constraints['instability_score']:.1f}/10 instability  ({constraints['acled_events_count']} ACLED events)")
    print(SEP)


# ==============================================================================
# MAIN
# ==============================================================================

def analyze(village_name, lat, lon, households, config_path=None):
    cfg = load_config(config_path)
    os.makedirs(cfg.get("output_dir", OUTPUT_DIR), exist_ok=True)

    print(f"\n[WeLight Analyzer] {village_name} ({lat}, {lon}) -- {households} households")

    print("[1/5] Revenue projection")
    print("  -> Checking commercial activity")
    has_comm, _, _ = _commercial_activity_osm(lat, lon)
    revenue = module_revenue(households, cfg, has_commercial=has_comm)

    print("[2/5] Solar resource")
    solar = module_solar_resource(lat, lon)

    print("[3/5] System sizing")
    sizing = module_sizing(households, solar["ghi_annual_kwh_m2_day"], cfg)

    print("[4/5] Financial model")
    fin = module_financials(sizing["capex_eur"], revenue["annual_revenue_eur"], cfg)
    # Patch lifetime into fin for CLI display
    fin["_n"] = cfg["project_lifetime_years"]

    print("[5/5] Constraints")
    constraints = module_constraints(lat, lon, cfg)
    constraints["_welight_sites"] = cfg.get("welight_sites", [])

    score = compute_priority_score(fin, solar, constraints)

    print("\nGenerating outputs")
    html_path = generate_html_report(
        village_name, lat, lon, households, cfg,
        revenue, solar, sizing, fin, constraints, score)
    json_path = generate_json_output(
        village_name, lat, lon, households, cfg,
        revenue, solar, sizing, fin, constraints, score)

    print_cli_summary(village_name, lat, lon, households, score,
                       fin, solar, sizing, constraints)
    print(f"\n  HTML report -> {html_path}")
    print(f"  JSON data   -> {json_path}\n")
    return score


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WeLight -- Village Solar Investment Analyzer"
    )
    parser.add_argument("--village",    required=True, help="Village name")
    parser.add_argument("--lat",        required=True, type=float, help="Latitude")
    parser.add_argument("--lon",        required=True, type=float, help="Longitude")
    parser.add_argument("--households", required=True, type=int,
                        help="Number of households (from Google Open Buildings)")
    parser.add_argument("--config",     default="config.json",
                        help="Path to config JSON (default: config.json)")
    args = parser.parse_args()

    # Load cfg globally for print_cli_summary reference
    cfg = load_config(args.config)

    analyze(args.village, args.lat, args.lon, args.households, args.config)
