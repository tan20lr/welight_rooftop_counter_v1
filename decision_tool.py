"""
WeLight Africa — Village Solar Decision Tool
Combines rooftop detection, solar resource, grid distance and financial modelling
into a single investment decision dashboard.
"""

import streamlit as st
import requests, gzip, csv, math, os, s2sphere, folium
from streamlit_folium import st_folium

# ── Config ─────────────────────────────────────────────────────────────────────
TILE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".tile_cache")
GOB_BASE       = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/climatology/point"
SNAPSHOT_RADII = [1.0, 2.0, 5.0]
SME_AREA_M2    = 150   # buildings >= this area classified as commercial/SME

# JIRAMA electrified towns in northern Madagascar (lat, lon, name)
JIRAMA_TOWNS = [
    (-12.3547, 49.2967, "Antsiranana"),
    (-13.1944, 49.0499, "Ambilobe"),
    (-14.8961, 47.9939, "Mahajanga"),
    (-13.6834, 48.3217, "Ambanja"),
    (-14.2614, 50.1659, "Antalaha"),
    (-15.7232, 46.3197, "Marovoay"),
    (-13.4073, 48.7624, "Nosy Be"),
    (-14.9000, 50.2833, "Sambava"),
    (-14.4395, 47.9955, "Port-Bergé"),
    (-13.5903, 49.7019, "Vohémar"),
    (-16.1667, 49.8333, "Mananara"),
    (-16.8635, 49.9699, "Maroantsetra"),
]

os.makedirs(TILE_CACHE_DIR, exist_ok=True)

st.set_page_config(
    page_title="WeLight — Solar Decision Tool",
    page_icon="☀️",
    layout="wide",
)

for k in ("result",):
    if k not in st.session_state:
        st.session_state[k] = None

# ── S2 / GOB helpers ───────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, a)))

def get_s2_tokens(lat, lon, level=4):
    ll   = s2sphere.LatLng.from_degrees(lat, lon)
    cell = s2sphere.CellId.from_lat_lng(ll).parent(level)
    tokens = {cell.to_token()}
    for nb in cell.get_all_neighbors(level):
        tokens.add(nb.to_token())
    return list(tokens)

def tile_exists(token):
    try:
        r = requests.head(f"{GOB_BASE}/{token}_buildings.csv.gz", timeout=8)
        return r.status_code == 200, int(r.headers.get("Content-Length", 0))
    except Exception:
        return False, 0

def _is_valid_gz(path):
    try:
        with gzip.open(path, "rb") as f:
            while f.read(65536): pass
        return True
    except Exception:
        return False

def download_tile(token):
    path = os.path.join(TILE_CACHE_DIR, f"{token}_buildings.csv.gz")
    if os.path.exists(path):
        if _is_valid_gz(path): return path
        os.remove(path)
    r = requests.get(f"{GOB_BASE}/{token}_buildings.csv.gz", timeout=120, stream=True)
    r.raise_for_status()
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(65536): f.write(chunk)
    if not _is_valid_gz(tmp):
        os.remove(tmp)
        raise IOError(f"Tile {token} corrupted — please retry.")
    os.replace(tmp, path)
    return path

def count_buildings_detailed(lat, lon, tile_paths, radii, min_conf=0.6, sme_threshold=SME_AREA_M2):
    """Returns per-radius counts split into residential vs SME/commercial."""
    max_r = max(radii); BB = max_r / 111.0 * 1.3
    candidates = []
    for path in tile_paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    if float(row.get("confidence", 1)) < min_conf: continue
                    blat = float(row["latitude"]); blon = float(row["longitude"])
                    if abs(blat - lat) > BB or abs(blon - lon) > BB: continue
                    d = haversine(lat, lon, blat, blon)
                    if d > max_r: continue
                    area = float(row.get("area_in_meters", 0) or 0)
                    is_sme = area >= sme_threshold
                    candidates.append((d, blat, blon, area, is_sme))
                except Exception:
                    pass
    result = {}
    for r in radii:
        in_r = [c for c in candidates if c[0] <= r]
        residential = [(c[1], c[2]) for c in in_r if not c[4]]
        sme         = [(c[1], c[2]) for c in in_r if c[4]]
        result[r] = {
            "total":       len(in_r),
            "residential": len(residential),
            "sme":         len(sme),
            "res_coords":  residential,
            "sme_coords":  sme,
        }
    return result

@st.cache_data(show_spinner=False)
def geocode(name):
    PRIO = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 4, "locality": 5}
    try:
        r = requests.get(NOMINATIM_URL,
                         params={"q": f"{name}, Madagascar", "format": "json",
                                 "limit": 8, "addressdetails": 1},
                         headers={"User-Agent": "WeLight-DecisionTool/1.0"},
                         timeout=10)
        results = r.json()
    except Exception:
        return []
    return sorted(results,
                  key=lambda x: (PRIO.get(x.get("type", ""), 99), x.get("place_rank", 99)))

@st.cache_data(show_spinner=False)
def get_ghi(lat, lon):
    """Returns (ghi_annual, ghi_monthly[12]) from NASA POWER. Falls back to 5.5."""
    try:
        r = requests.get(
            NASA_POWER_URL,
            params={"parameters": "ALLSKY_SFC_SW_DWN", "community": "RE",
                    "longitude": lon, "latitude": lat, "format": "JSON"},
            timeout=20,
        )
        d = r.json()["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
        ann = float(d.get("ANN", 0) or 0)
        monthly = []
        for m in range(1, 13):
            for key in (f"{m:02d}", str(m)):
                if key in d:
                    v = d[key]
                    monthly.append(float(v) if v not in (None, -999, "-999") else None)
                    break
            else:
                monthly.append(None)
        if ann <= 0 and any(v for v in monthly if v):
            ann = sum(v for v in monthly if v) / sum(1 for v in monthly if v)
        return ann if ann > 0 else 5.5, monthly
    except Exception:
        return 5.5, [None] * 12

def get_grid_distance(lat, lon):
    """Returns (distance_km, nearest_town) from hardcoded JIRAMA list."""
    best_d, best_name = None, None
    for tlat, tlon, tname in JIRAMA_TOWNS:
        d = haversine(lat, lon, tlat, tlon)
        if best_d is None or d < best_d:
            best_d, best_name = d, tname
    return round(best_d, 1), best_name

def compute_financials(n_residential, n_sme, ghi_annual, cfg):
    """
    Returns a dict with sizing, revenue and payback for the given parameters.
    """
    pen_r   = cfg["penetration_residential"]
    pen_s   = cfg["penetration_sme"]
    tar_r   = cfg["tariff_residential_eur"]
    tar_s   = cfg["tariff_sme_eur"]
    cpkwp   = cfg["capex_per_kwp_eur"]
    kwh_r   = cfg["kwh_per_hh_day"]
    kwh_s   = cfg["kwh_per_sme_day"]
    eff     = cfg["system_efficiency"]
    batt    = cfg["battery_autonomy_days"]
    opex_p  = cfg["opex_pct_capex"]
    dr      = cfg["discount_rate"]
    life    = cfg["project_lifetime_years"]

    sub_r = round(n_residential * pen_r)
    sub_s = round(n_sme        * pen_s)

    daily_kwh = sub_r * kwh_r + sub_s * kwh_s
    if daily_kwh <= 0 or ghi_annual <= 0:
        return None

    peak_kwp  = daily_kwh / (ghi_annual * eff)
    batt_kwh  = daily_kwh * batt
    capex     = peak_kwp * cpkwp + batt_kwh * 150   # EUR 150/kWh storage

    ann_rev   = (sub_r * tar_r + sub_s * tar_s) * 12
    opex      = capex * opex_p
    net_ann   = ann_rev - opex

    payback   = capex / net_ann if net_ann > 0 else 999

    # NPV
    npv = -capex + sum(net_ann / (1 + dr) ** t for t in range(1, life + 1))

    # IRR (Newton-Raphson)
    irr = None
    try:
        cashflows = [-capex] + [net_ann] * life
        r = 0.1
        for _ in range(200):
            f  = sum(cashflows[t] / (1 + r) ** t for t in range(life + 1))
            df = sum(-t * cashflows[t] / (1 + r) ** (t + 1) for t in range(1, life + 1))
            if df == 0: break
            r2 = r - f / df
            if abs(r2 - r) < 1e-7:
                irr = r2; break
            r = r2
    except Exception:
        pass

    # Min tariff for 7-yr payback
    opex_per_sub = opex / max(sub_r + sub_s, 1)
    capex_per_sub = capex / max(sub_r + sub_s, 1)
    req_rev_sub = capex_per_sub / 7 + opex_per_sub   # per subscriber per year
    total_subs_weighted = sub_r + sub_s * (tar_s / max(tar_r, 0.01))
    req_tariff_r = req_rev_sub / 12 if total_subs_weighted > 0 else None

    return {
        "subscribers_residential": sub_r,
        "subscribers_sme":         sub_s,
        "peak_kwp":    round(peak_kwp, 1),
        "batt_kwh":    round(batt_kwh, 1),
        "capex_eur":   round(capex),
        "ann_revenue": round(ann_rev),
        "opex":        round(opex),
        "net_annual":  round(net_ann),
        "payback_yrs": round(payback, 1),
        "npv_eur":     round(npv),
        "irr_pct":     round(irr * 100, 1) if irr else None,
        "req_tariff_residential": round(req_tariff_r, 2) if req_tariff_r else None,
    }

def priority_score(fin, ghi, grid_km):
    score = 0; breakdown = {}
    # Financial (40 pts)
    pb = fin["payback_yrs"] if fin else 999
    f = 40 if pb <= 5 else 30 if pb <= 7 else 15 if pb <= 10 else 0
    score += f; breakdown["financial"] = f
    # Solar (20 pts)
    s = min(20, round(ghi / 6.5 * 20))
    score += s; breakdown["solar"] = s
    # Grid isolation (20 pts)
    g = 20 if grid_km > 50 else 10 if grid_km > 20 else 0
    score += g; breakdown["grid_isolation"] = g
    # Market size proxy: SME presence (10 pts) — already baked into financials
    # but we give bonus for SME > 10
    breakdown["score"] = score
    return breakdown

def make_map(lat, lon, res_blds, sme_blds, radius_km, name):
    m = folium.Map(
        location=[lat, lon], zoom_start=15,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
    )
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        attr="Esri Labels", name="Labels", overlay=True, control=True, opacity=0.7,
    ).add_to(m)
    folium.Circle(location=[lat, lon], radius=radius_km * 1000,
                  color="#60A5FA", fill=True, fill_opacity=0.08, weight=2).add_to(m)
    folium.Marker(location=[lat, lon],
                  popup=f"<b>{name}</b>",
                  icon=folium.Icon(color="blue", icon="home", prefix="fa")).add_to(m)
    for blat, blon in res_blds[:2500]:
        folium.CircleMarker(location=[blat, blon], radius=3,
                            color="#FF4500", fill=True, fill_opacity=0.85, weight=0).add_to(m)
    for blat, blon in sme_blds[:500]:
        folium.CircleMarker(location=[blat, blon], radius=6,
                            color="#FFD700", fill=True, fill_opacity=0.9, weight=1,
                            tooltip="Commercial / SME").add_to(m)
    folium.LayerControl().add_to(m)
    return m

# ── Main search ────────────────────────────────────────────────────────────────

def run_search(village_name, min_conf, sme_threshold):
    res = {"village": village_name, "min_conf": min_conf, "error": None,
           "snapshot": {}, "lat": None, "lon": None, "display_name": None,
           "candidates": [], "tile_count": 0, "ghi_annual": None,
           "ghi_monthly": None, "grid_km": None, "grid_town": None}

    with st.status(f"Analysing **{village_name}**...", expanded=True) as status:
        # 1. Geocode
        st.write("Locating village via OpenStreetMap...")
        candidates = geocode(village_name)
        mada = ([c for c in candidates
                 if "Madagascar" in c.get("display_name", "")
                 or c.get("address", {}).get("country_code") == "mg"]
                or candidates)
        if not mada:
            status.update(label="Village not found", state="error")
            res["error"] = f"No result for '{village_name}' in Madagascar."
            st.session_state["result"] = res; return

        res["candidates"] = mada
        chosen = mada[0]
        lat, lon = float(chosen["lat"]), float(chosen["lon"])
        res["lat"], res["lon"] = lat, lon
        res["display_name"] = chosen.get("display_name", village_name)
        st.write(f"Found: ({lat:.4f}, {lon:.4f})")

        # 2. Solar resource
        st.write("Fetching solar resource (NASA POWER)...")
        ghi_ann, ghi_monthly = get_ghi(lat, lon)
        res["ghi_annual"]  = ghi_ann
        res["ghi_monthly"] = ghi_monthly
        st.write(f"GHI: {ghi_ann:.2f} kWh/m²/day (annual average)")

        # 3. Grid distance
        st.write("Calculating distance to JIRAMA grid...")
        grid_km, grid_town = get_grid_distance(lat, lon)
        res["grid_km"]   = grid_km
        res["grid_town"] = grid_town
        st.write(f"Nearest grid: {grid_town} ({grid_km} km)")

        # 4. Tiles
        st.write("Identifying satellite tiles...")
        tokens  = get_s2_tokens(lat, lon, 4)
        ll      = s2sphere.LatLng.from_degrees(lat, lon)
        pri_tok = s2sphere.CellId.from_lat_lng(ll).parent(4).to_token()
        needed  = [(t, sz) for t in tokens
                   for (ok, sz) in [tile_exists(t)] if ok
                   if t == pri_tok or sz <= 20e6]
        if not needed:
            status.update(label="No satellite data for this area", state="error")
            res["error"] = "No Google Open Buildings tiles available here."
            st.session_state["result"] = res; return
        st.write(f"{len(needed)} tile(s) to process")

        tile_paths = []
        for tok, sz in needed:
            cached = os.path.join(TILE_CACHE_DIR, f"{tok}_buildings.csv.gz")
            label  = f"Cached ({sz/1e6:.1f} MB)" if os.path.exists(cached) else f"Downloading ({sz/1e6:.1f} MB)..."
            st.write(f"   {tok}: {label}")
            try:
                tile_paths.append(download_tile(tok))
            except Exception as e:
                st.warning(f"   Skipped {tok}: {e}")
        if not tile_paths:
            status.update(label="Download failed", state="error")
            res["error"] = "Could not download tiles."
            st.session_state["result"] = res; return

        # 5. Count buildings
        st.write(f"Counting buildings (confidence >= {min_conf:.0%}, SME >= {sme_threshold} m²)...")
        snapshot = count_buildings_detailed(lat, lon, tile_paths, SNAPSHOT_RADII, min_conf, sme_threshold)
        res["snapshot"]   = snapshot
        res["tile_count"] = len(tile_paths)
        n2 = snapshot.get(2.0, {}).get("total", 0)
        status.update(label=f"Done — {n2:,} buildings within 2 km", state="complete", expanded=False)

    st.session_state["result"] = res

# ── UI ─────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
  .title   { font-size:1.9rem; font-weight:800; color:#1F4E79; margin:0 }
  .sub     { font-size:.88rem; color:#6B7280; margin:.2rem 0 1.2rem }
  .badge   { display:inline-block; background:#DBEAFE; color:#1E40AF;
             font-size:.70rem; font-weight:700; padding:2px 9px;
             border-radius:10px; margin-right:5px }
  .kcard   { background:#F0F9FF; border-left:4px solid #0284C7;
             padding:.8rem 1.2rem; border-radius:8px; margin:.4rem 0 }
  .knum    { font-size:2.2rem; font-weight:900; color:#0C4A6E; line-height:1.1 }
  .klbl    { font-size:.8rem; color:#555; margin-top:.2rem }
  .verdict-go   { background:#D1FAE5; border-left:5px solid #10B981;
                  padding:1rem 1.5rem; border-radius:8px; margin:1rem 0 }
  .verdict-meh  { background:#FEF3C7; border-left:5px solid #F59E0B;
                  padding:1rem 1.5rem; border-radius:8px; margin:1rem 0 }
  .verdict-no   { background:#FEE2E2; border-left:5px solid #EF4444;
                  padding:1rem 1.5rem; border-radius:8px; margin:1rem 0 }
  .vhead   { font-size:1.4rem; font-weight:800; margin-bottom:.3rem }
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="title">☀️ WeLight — Village Solar Decision Tool</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub">Enter a village name to get rooftop count, solar resource, grid distance and investment projections.'
    '&nbsp;<span class="badge">Google Open Buildings v3</span>'
    '<span class="badge">NASA POWER GHI</span>'
    '<span class="badge">WeLight Africa</span></p>',
    unsafe_allow_html=True,
)

# ── Sidebar: parameters ────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Parameters")
    st.caption(
        "**Calibrated on WeLight's 172-village Madagascar portfolio.** "
        "Defaults reflect WeLight's observed tariffs (~EUR 2.50/HH/month), "
        "penetration (~70%) and CAPEX efficiency (~EUR 900/kWp at scale). "
        "Adjust for your own context."
    )
    st.subheader("Detection")
    min_conf      = st.selectbox("Confidence threshold", [0.0, 0.5, 0.6, 0.7, 0.8], index=2,
                                 format_func=lambda x: "All" if x == 0.0 else f">={x:.0%}",
                                 help="ML confidence. 60% = recommended.")
    sme_threshold = st.number_input("Min area for SME (m²)", min_value=50, max_value=1000,
                                    value=150, step=25,
                                    help="Buildings >= this area are classified as commercial/SME. Madagascar rural homes average 30-60 m².")

    st.subheader("Residential customers")
    pen_r   = st.slider("Penetration rate — residential", 0.10, 1.0, 0.70, 0.05,
                        format="%.0f%%", help="% of households that subscribe. WeLight achieves ~70% in practice.")
    tar_r   = st.number_input("Monthly tariff — residential (EUR)", 0.5, 20.0, 2.50, 0.10,
                               format="%.2f", help="WeLight charges ~EUR 2.50/month in northern Madagascar.")
    kwh_r   = st.number_input("Daily consumption — residential (kWh)", 0.1, 3.0, 0.30, 0.05,
                               format="%.2f")

    st.subheader("Commercial / SME customers")
    pen_s   = st.slider("Penetration rate — SME", 0.10, 1.0, 0.60, 0.05,
                        format="%.0f%%")
    tar_s   = st.number_input("Monthly tariff — SME (EUR)", 1.0, 100.0, 12.00, 0.50,
                               format="%.2f", help="Commercial customers typically pay EUR 10-15/month.")
    kwh_s   = st.number_input("Daily consumption — SME (kWh)", 0.5, 20.0, 2.00, 0.25,
                               format="%.2f")

    st.subheader("System & finance")
    capex_kwp  = st.number_input("CAPEX per kWp (EUR)", 500, 3000, 900, 50,
                                  help="EUR 900-1000/kWp for an experienced operator like WeLight. EUR 1200+ for first project.")
    eff        = st.slider("System efficiency", 0.50, 0.90, 0.75, 0.01, format="%.0f%%")
    batt_days  = st.slider("Battery autonomy (days)", 0.5, 3.0, 1.5, 0.25)
    opex_pct   = st.slider("OPEX (% of CAPEX / yr)", 0.01, 0.10, 0.04, 0.005, format="%.1f%%")
    dr         = st.slider("Discount rate", 0.05, 0.25, 0.08, 0.01, format="%.0f%%",
                           help="8% for DFI-backed projects (EIB/Triodos concessional). 12-15% for commercial capital.")
    life       = st.number_input("Project lifetime (years)", 5, 30, 15, 1)

    cfg = {
        "penetration_residential": pen_r,
        "penetration_sme":         pen_s,
        "tariff_residential_eur":  tar_r,
        "tariff_sme_eur":          tar_s,
        "kwh_per_hh_day":          kwh_r,
        "kwh_per_sme_day":         kwh_s,
        "capex_per_kwp_eur":       capex_kwp,
        "system_efficiency":       eff,
        "battery_autonomy_days":   batt_days,
        "opex_pct_capex":          opex_pct,
        "discount_rate":           dr,
        "project_lifetime_years":  int(life),
    }

# ── Search bar ─────────────────────────────────────────────────────────────────
col_v, col_b = st.columns([5, 1])
with col_v:
    village_input = st.text_input("Village", placeholder="e.g. Betsiaka, Ambilobe, Farahalana...",
                                  label_visibility="collapsed")
with col_b:
    search = st.button("🔍 Analyse", type="primary", use_container_width=True)

if search:
    if village_input.strip():
        run_search(village_input.strip(), min_conf, sme_threshold)
    else:
        st.warning("Please enter a village name.")

# ── Results ────────────────────────────────────────────────────────────────────
res = st.session_state["result"]
if res:
    if res["error"]:
        st.error(res["error"])
    else:
        # Candidate selector
        if len(res["candidates"]) > 1:
            opts = {
                f"{c['display_name'][:75]}  ({float(c['lat']):.3f}, {float(c['lon']):.3f})": i
                for i, c in enumerate(res["candidates"][:5])
            }
            idx = opts[st.selectbox("Multiple OSM results — choose the right one:", list(opts.keys()))]
            ch = res["candidates"][idx]
            res["lat"], res["lon"] = float(ch["lat"]), float(ch["lon"])
            res["display_name"] = ch.get("display_name", res["village"])

        lat   = res["lat"]; lon = res["lon"]
        name  = res["village"]
        snap  = res["snapshot"]
        ghi   = res["ghi_annual"] or 5.5
        ghim  = res["ghi_monthly"] or [None] * 12
        gkm   = res["grid_km"] or 0
        gtown = res["grid_town"] or "unknown"

        d2 = snap.get(2.0, {})
        n_res = d2.get("residential", 0)
        n_sme = d2.get("sme", 0)
        n_tot = d2.get("total", 0)

        # Recompute financials live with current sidebar params
        fin = compute_financials(n_res, n_sme, ghi, cfg)
        score = priority_score(fin, ghi, gkm) if fin else {}

        # ── Verdict ──────────────────────────────────────────────────────────
        st.markdown("---")
        if fin:
            pb = fin["payback_yrs"]
            sc = score.get("score", 0)
            if pb <= 7 and sc >= 50:
                vcls, vicon, vtxt = "verdict-go",  "✅ INVEST",       "Strong fundamentals: short payback and good solar+grid isolation."
            elif pb <= 12 and sc >= 30:
                vcls, vicon, vtxt = "verdict-meh", "🔶 TO EVALUATE",  "Mixed signals — adjust parameters or plan a field visit."
            else:
                vcls, vicon, vtxt = "verdict-no",  "❌ DO NOT INVEST","Payback too long or score too low under current assumptions."
            st.markdown(f"""
            <div class="{vcls}">
              <div class="vhead">{vicon} &nbsp; {name.upper()}</div>
              <div>{vtxt} &nbsp; Score: <b>{sc}/80</b> &nbsp;·&nbsp; Payback: <b>{pb} yrs</b></div>
            </div>""", unsafe_allow_html=True)

        # ── Key metrics row ───────────────────────────────────────────────────
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("🏠 Residential buildings (2 km)", f"{n_res:,}")
        c2.metric("🏪 Commercial / SME (2 km)", f"{n_sme:,}",
                  help=f"Buildings >= {sme_threshold} m²")
        c3.metric("☀️ GHI (annual avg)", f"{ghi:.2f} kWh/m²/d")
        c4.metric("🔌 Grid distance", f"{gkm} km",
                  help=f"Nearest JIRAMA: {gtown}")
        c5.metric("👥 Pop. estimate", f"{n_tot*4:,}",
                  help="Total buildings × 4 persons/household")

        # ── Tabs ──────────────────────────────────────────────────────────────
        tab1, tab2, tab3 = st.tabs(["📊 Financial model", "🗺️ Map", "📈 Solar profile"])

        # Tab 1: Financial model
        with tab1:
            if fin:
                st.subheader("Revenue & investment projection")
                fc1, fc2, fc3 = st.columns(3)
                fc1.metric("System size", f"{fin['peak_kwp']} kWp")
                fc1.metric("Battery storage", f"{fin['batt_kwh']} kWh")
                fc2.metric("Total CAPEX", f"EUR {fin['capex_eur']:,}")
                fc2.metric("Annual revenue", f"EUR {fin['ann_revenue']:,}")
                fc3.metric("Payback period", f"{fin['payback_yrs']} yrs",
                           delta=f"{'OK' if fin['payback_yrs'] <= 7 else 'Too long'}",
                           delta_color="normal" if fin["payback_yrs"] <= 7 else "inverse")
                fc3.metric("NPV (15 yr)", f"EUR {fin['npv_eur']:,}",
                           delta_color="normal" if fin["npv_eur"] > 0 else "inverse")
                if fin["irr_pct"]:
                    fc3.metric("IRR", f"{fin['irr_pct']}%")

                st.markdown("#### Subscriber breakdown")
                sc1, sc2 = st.columns(2)
                sc1.metric("Residential subscribers",
                           f"{fin['subscribers_residential']:,}",
                           help=f"{pen_r:.0%} of {n_res:,} residential buildings")
                sc2.metric("SME subscribers",
                           f"{fin['subscribers_sme']:,}",
                           help=f"{pen_s:.0%} of {n_sme:,} SME buildings")

                rev_res = fin["subscribers_residential"] * tar_r * 12
                rev_sme = fin["subscribers_sme"]         * tar_s * 12
                if rev_res + rev_sme > 0:
                    sme_share = rev_sme / (rev_res + rev_sme) * 100
                    st.caption(f"SME revenue share: **{sme_share:.0f}%** "
                               f"(EUR {rev_sme:,.0f} / yr) vs residential EUR {rev_res:,.0f} / yr")

                if fin["req_tariff_residential"]:
                    req = fin["req_tariff_residential"]
                    if req > tar_r:
                        st.warning(
                            f"For a 7-year payback, the residential tariff needs to be "
                            f"**EUR {req:.2f}/month** (currently EUR {tar_r:.2f}). "
                            f"Try increasing the SME tariff or penetration rate instead."
                        )
                    else:
                        st.success(f"Current tariff (EUR {tar_r:.2f}) exceeds the minimum "
                                   f"required (EUR {req:.2f}) for 7-year payback. ✓")

                # 15-year cashflow
                st.markdown("#### 15-year cash flow")
                import io
                years  = list(range(0, int(life) + 1))
                ann_net = fin["net_annual"]
                cf = [-fin["capex_eur"]] + [ann_net] * int(life)
                cum = []
                s = 0
                for v in cf:
                    s += v; cum.append(s)

                # Build a simple bar-chart data dict for streamlit
                chart_data = {"Year": years, "Cumulative cash flow (EUR)": cum}
                import pandas as pd
                df_cf = pd.DataFrame(chart_data).set_index("Year")
                st.line_chart(df_cf)
            else:
                st.info("No subscribers with current parameters — adjust penetration rates or building counts.")

        # Tab 2: Map
        with tab2:
            bld_res = d2.get("res_coords", [])
            bld_sme = d2.get("sme_coords", [])
            st_folium(make_map(lat, lon, bld_res, bld_sme, 2.0, name),
                      width=None, height=520, returned_objects=[])
            st.caption(
                "🔴 Residential buildings &nbsp;·&nbsp; 🟡 Commercial / SME (hover for label) &nbsp;·&nbsp; "
                "🔵 2 km radius &nbsp;·&nbsp; "
                f"[Open in Google Maps](https://maps.google.com/?q={lat},{lon})"
            )

            # Multi-radius table
            st.markdown("#### Building counts by radius")
            rows = []
            for r in SNAPSHOT_RADII:
                d = snap.get(r, {})
                rows.append({
                    "Radius": f"{r} km",
                    "Total": d.get("total", 0),
                    "Residential": d.get("residential", 0),
                    "Commercial / SME": d.get("sme", 0),
                    "Pop. estimate": d.get("total", 0) * 4,
                })
            import pandas as pd
            st.dataframe(pd.DataFrame(rows).set_index("Radius"), use_container_width=True)

        # Tab 3: Solar profile
        with tab3:
            st.subheader("Monthly GHI profile")
            months = ["Jan","Feb","Mar","Apr","May","Jun",
                      "Jul","Aug","Sep","Oct","Nov","Dec"]
            if any(v for v in ghim if v):
                import pandas as pd
                df_ghi = pd.DataFrame({
                    "Month": months,
                    "GHI (kWh/m²/day)": [v if v else 0 for v in ghim],
                }).set_index("Month")
                st.bar_chart(df_ghi)
                min_m = min((v for v in ghim if v), default=0)
                max_m = max((v for v in ghim if v), default=0)
                st.caption(
                    f"Annual average: **{ghi:.2f}** kWh/m²/day &nbsp;·&nbsp; "
                    f"Min month: **{min_m:.2f}** &nbsp;·&nbsp; Max month: **{max_m:.2f}** &nbsp;·&nbsp; "
                    f"Source: NASA POWER climatology"
                )
                if min_m < 4.0:
                    st.warning(
                        f"Low solar months detected ({min_m:.2f} kWh/m²/day). "
                        "Consider increasing battery autonomy for year-round reliability."
                    )
            else:
                st.info(f"Monthly data unavailable. Using annual average: {ghi:.2f} kWh/m²/day")

            st.markdown("#### Grid context")
            gcol1, gcol2 = st.columns(2)
            gcol1.metric("Distance to JIRAMA grid", f"{gkm} km",
                         help=f"Nearest electrified town: {gtown}")
            if gkm > 50:
                gcol2.markdown("🟢 **Highly isolated** — very low risk of grid extension reaching the village within 10 years.")
            elif gkm > 20:
                gcol2.markdown("🟡 **Moderate isolation** — grid extension possible in 5-10 years. Factor into project horizon.")
            else:
                gcol2.markdown("🔴 **Near grid** — JIRAMA extension likely. Consider shorter project horizon or co-investment model.")

# ── Footer ─────────────────────────────────────────────────────────────────────
st.divider()
st.markdown(
    "<small>☀️ WeLight Africa Solar Decision Tool &nbsp;·&nbsp; "
    "Google Open Buildings v3 &nbsp;·&nbsp; NASA POWER GHI &nbsp;·&nbsp; "
    "OpenStreetMap Nominatim &nbsp;·&nbsp; v1.0</small>",
    unsafe_allow_html=True,
)
