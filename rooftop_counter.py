"""
WeLight Africa — Madagascar Village Rooftop Counter
Counts buildings from Google Open Buildings v3 satellite data.
"""

import streamlit as st
import requests, gzip, csv, math, os, s2sphere, folium
from streamlit_folium import st_folium

# ── Config ─────────────────────────────────────────────────────────────────────
TILE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".tile_cache")
GOB_BASE       = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
SNAPSHOT_RADII = [1.0, 2.0, 5.0]
os.makedirs(TILE_CACHE_DIR, exist_ok=True)

st.set_page_config(
    page_title="WeLight — Rooftop Counter",
    page_icon="🛰️",
    layout="centered",
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

def count_buildings_multi(lat, lon, tile_paths, radii, min_conf=0.6):
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
                    if d <= max_r: candidates.append((d, blat, blon))
                except Exception: pass
    return {r: (len([1 for d, _, _ in candidates if d <= r]),
                [(b, c) for d, b, c in candidates if d <= r])
            for r in radii}

@st.cache_data(show_spinner=False)
def geocode(name):
    PRIO = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 4, "locality": 5}
    try:
        r = requests.get(NOMINATIM_URL,
                         params={"q": f"{name}, Madagascar", "format": "json",
                                 "limit": 8, "addressdetails": 1},
                         headers={"User-Agent": "WeLight-RooftopCounter/3.0"},
                         timeout=10)
        results = r.json()
    except Exception:
        return []
    return sorted(results,
                  key=lambda x: (PRIO.get(x.get("type", ""), 99), x.get("place_rank", 99)))

def make_map(lat, lon, buildings, radius_km, name):
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
                  popup=f"<b>{name}</b><br>{len(buildings):,} buildings",
                  icon=folium.Icon(color="blue", icon="home", prefix="fa")).add_to(m)
    for blat, blon in buildings[:3000]:
        folium.CircleMarker(location=[blat, blon], radius=3,
                            color="#FF4500", fill=True, fill_opacity=0.85, weight=0).add_to(m)
    if len(buildings) > 3000:
        folium.Marker(
            location=[lat + radius_km / 111 * 0.85, lon],
            icon=folium.DivIcon(
                html=f'<div style="font-size:11px;color:#fff;background:rgba(0,0,0,.6);padding:2px 6px;border-radius:3px;">Shown: 3,000 / {len(buildings):,}</div>',
                icon_size=(200, 22)),
        ).add_to(m)
    folium.LayerControl().add_to(m)
    return m

# ── Search ─────────────────────────────────────────────────────────────────────

def run_search(village_name, min_conf):
    res = {"village": village_name, "min_conf": min_conf, "error": None,
           "snapshot": {}, "lat": None, "lon": None,
           "display_name": None, "candidates": [], "tile_count": 0}

    with st.status(f"Counting buildings in **{village_name}**...", expanded=True) as status:
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

        st.write("Identifying satellite tiles...")
        tokens   = get_s2_tokens(lat, lon, 4)
        ll       = s2sphere.LatLng.from_degrees(lat, lon)
        pri_tok  = s2sphere.CellId.from_lat_lng(ll).parent(4).to_token()
        needed   = [(t, sz) for t in tokens
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
            res["error"] = "Could not download tiles."; st.session_state["result"] = res; return

        st.write(f"Counting buildings (confidence >= {min_conf:.0%})...")
        snapshot = count_buildings_multi(lat, lon, tile_paths, SNAPSHOT_RADII, min_conf)
        res["snapshot"]   = snapshot
        res["tile_count"] = len(tile_paths)
        n2 = snapshot.get(2.0, (0, []))[0]
        status.update(label=f"Done — {n2:,} buildings within 2 km",
                      state="complete", expanded=False)

    st.session_state["result"] = res

# ── UI ─────────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
  .title  { font-size:1.8rem; font-weight:800; color:#1F4E79; margin:0 }
  .sub    { font-size:.88rem; color:#6B7280; margin:.2rem 0 1.2rem }
  .rbox   { background:#EBF5FB; border-left:5px solid #2563EB;
            padding:1rem 1.5rem; border-radius:8px; margin:1rem 0 }
  .rnum   { font-size:3.2rem; font-weight:900; color:#1E3A5F; line-height:1 }
  .rlbl   { font-size:.85rem; color:#555; margin:.25rem 0 .1rem }
  .rsrc   { font-size:.70rem; color:#94A3B8 }
  .badge  { display:inline-block; background:#DBEAFE; color:#1E40AF;
            font-size:.70rem; font-weight:700; padding:2px 9px;
            border-radius:10px; margin-right:5px }
</style>
""", unsafe_allow_html=True)

st.markdown('<p class="title">🛰️ Madagascar Village Rooftop Counter</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub">Enter any Malagasy village name to count its buildings from satellite data.'
    '&nbsp;<span class="badge">Google Open Buildings v3</span>'
    '<span class="badge">~2.5B buildings worldwide</span>'
    '<span class="badge">Africa-optimized ML model</span></p>',
    unsafe_allow_html=True,
)

# Input row
col_v, col_c = st.columns([4, 1])
with col_v:
    village_input = st.text_input(
        "Village", placeholder="e.g. Betsiaka, Ambilobe, Farahalana, Andrafiabe...",
        label_visibility="collapsed",
    )
with col_c:
    min_conf = st.selectbox(
        "Confidence", [0.0, 0.5, 0.6, 0.7, 0.8], index=2,
        format_func=lambda x: "All" if x == 0.0 else f">={x:.0%}",
        help="ML confidence threshold. 0.6 = recommended (best precision/recall balance).",
    )

if st.button("🔍 Count rooftops", type="primary", use_container_width=True):
    if village_input.strip():
        run_search(village_input.strip(), min_conf)
    else:
        st.warning("Please enter a village name.")

# Results
res = st.session_state["result"]
if res:
    if res["error"]:
        st.error(res["error"])
    else:
        # Candidate selector if multiple OSM results
        if len(res["candidates"]) > 1:
            opts = {
                f"{c['display_name'][:75]}  ({float(c['lat']):.3f}, {float(c['lon']):.3f})": i
                for i, c in enumerate(res["candidates"][:5])
            }
            idx = opts[st.selectbox("Multiple OSM results — choose the right one:", list(opts.keys()))]
            ch = res["candidates"][idx]
            res["lat"], res["lon"] = float(ch["lat"]), float(ch["lon"])
            res["display_name"] = ch.get("display_name", res["village"])

        snap  = res["snapshot"]
        lat   = res["lat"]; lon = res["lon"]
        name  = res["village"]
        dname = res["display_name"]
        conf  = res["min_conf"]
        n1, n2, n5 = (snap.get(r, (0, []))[0] for r in [1.0, 2.0, 5.0])

        # Alert for suspect coordinates
        if n2 < 30 and any(t in dname.lower() for t in
                           ["district", "province", "region", "diana", "sava", "sofia"]):
            st.warning(
                "Coordinates may be off — OSM result looks like an administrative boundary. "
                f"[Verify on Google Maps](https://maps.google.com/?q={lat},{lon})"
            )

        # Main result card
        st.markdown(f"""
        <div class="rbox">
          <div class="rnum">{n2:,}</div>
          <div class="rlbl">buildings within a 2 km radius of <b>{name}</b>
            &nbsp;·&nbsp; confidence &gt;= {conf:.0%}</div>
          <div class="rsrc">
            Source: Google Open Buildings v3 — ML satellite detection, ~2.5B buildings<br>
            Coordinates: {lat:.4f}°, {lon:.4f}° &nbsp;·&nbsp; {dname[:70]}
          </div>
        </div>
        """, unsafe_allow_html=True)

        # Multi-radius snapshot
        st.subheader("Village density profile")
        c1, c2, c5 = st.columns(3)
        c1.metric("1 km radius", f"{n1:,}", help="Dense core")
        c2.metric("2 km radius", f"{n2:,}", help="Village + immediate periphery")
        c5.metric("5 km radius", f"{n5:,}", help="Village + surrounding hamlets")

        if n2 > 0:
            ratio = n5 / n1 if n1 > 0 else 0
            if ratio < 2.5:
                st.markdown("🏘️ **Compact village** — buildings concentrated in core. Simple logistics.")
            elif ratio < 6:
                st.markdown("🏡 **Village with hamlets** — central core + dispersed outskirts. Plan multiple distribution points.")
            else:
                st.markdown("🌳 **Highly dispersed habitat** — few central buildings, many isolated constructions. High logistics cost.")

        # Population estimate
        pop_est = round(n2 * 4.5)
        st.caption(f"Population estimate: ~{pop_est:,} people (based on {n2:,} buildings × 4.5 persons/household — INSTAT Madagascar 2018)")

        # Map
        st.subheader("Satellite map")
        bld2 = snap.get(2.0, (0, []))[1]
        st_folium(make_map(lat, lon, bld2, 2.0, name), width=None, height=500, returned_objects=[])
        st.caption(
            "🔴 Google Open Buildings v3 detections &nbsp;·&nbsp; "
            "🔵 2 km radius circle &nbsp;·&nbsp; "
            f"[Open in Google Maps](https://maps.google.com/?q={lat},{lon}) &nbsp;·&nbsp; "
            "Esri World Imagery &nbsp;·&nbsp; CC BY 4.0"
        )

        st.metric("Satellite tiles used", str(res["tile_count"]),
                  help="Tiles are cached locally — repeat searches are instant.")

# Footer
st.divider()
st.markdown(
    "<small>🛰️ Google Open Buildings v3 &nbsp;·&nbsp; "
    "📍 OpenStreetMap Nominatim &nbsp;·&nbsp; "
    "☀️ WeLight Africa Rooftop Counter v3.0 &nbsp;·&nbsp; "
    "Confidence threshold validated against OSM ground truth (60% = optimal)</small>",
    unsafe_allow_html=True,
)
