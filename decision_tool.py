"""
WeLight Africa — Outil d'Aide à la Décision Solaire
Analyse un village malgache : toits, ressource solaire, réseau, modèle financier.
"""

import streamlit as st
import requests, gzip, csv, math, os, s2sphere, folium, pandas as pd
from streamlit_folium import st_folium

# ── Config ─────────────────────────────────────────────────────────────────────
TILE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".tile_cache")
GOB_BASE       = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip"
NOMINATIM_URL  = "https://nominatim.openstreetmap.org/search"
NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/climatology/point"
SNAPSHOT_RADII = [1.0, 2.0, 5.0]
MIN_CONFIDENCE = 0.6    # Seuil validé sur terrain nord Madagascar — non modifiable
SME_AREA_M2    = 150    # Bâtiments >= 150 m² classifiés PME/commerces

# Villes électrifiées JIRAMA — nord et côte est Madagascar
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
    page_title="WeLight — Outil Décisionnel Solaire",
    page_icon="☀️",
    layout="wide",
)

for k in ("result",):
    if k not in st.session_state:
        st.session_state[k] = None

# ── Charte graphique WeLight ───────────────────────────────────────────────────
st.markdown("""
<style>
  /* ── Typographie de base ── */
  html, body, [class*="css"] {
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
  }

  /* ── Sidebar : titres de section en jaune WeLight, fond par défaut ── */
  [data-testid="stSidebar"] h2,
  [data-testid="stSidebar"] h3 {
    color: #B38600 !important;
    font-weight: 700;
    border-bottom: 2px solid #FFC500;
    padding-bottom: 3px;
    margin-top: 1.2rem !important;
  }
  [data-testid="stSidebar"] small,
  [data-testid="stSidebar"] .stCaption { color: #555 !important; }

  /* ── En-tête ── */
  .wl-header {
    background: #1A1A1A;
    padding: 1.1rem 1.6rem;
    border-radius: 10px;
    margin-bottom: 1.2rem;
    display: flex;
    align-items: center;
    gap: 1.2rem;
  }
  .wl-logo-sun  { font-size: 2.2rem; line-height: 1; }
  .wl-logo-text { font-size: 1.6rem; font-weight: 900; color: #FFC500; letter-spacing: -.5px; }
  .wl-logo-sub  { font-size: .82rem; color: #AAAAAA; margin-top: .15rem; }
  .wl-badge {
    display: inline-block;
    background: #FFC500;
    color: #1A1A1A;
    font-size: .67rem;
    font-weight: 800;
    padding: 3px 10px;
    border-radius: 20px;
    margin-right: 5px;
    text-transform: uppercase;
    letter-spacing: .4px;
  }

  /* ── Cartes de verdict — fond clair, texte foncé, lisible ── */
  .verdict-invest {
    background: #FFFBEA;
    border-left: 6px solid #FFC500;
    border-radius: 10px;
    padding: 1.1rem 1.6rem;
    margin: .8rem 0;
  }
  .verdict-evaluate {
    background: #FFF8E1;
    border-left: 6px solid #FF8F00;
    border-radius: 10px;
    padding: 1.1rem 1.6rem;
    margin: .8rem 0;
  }
  .verdict-no {
    background: #FFF3F3;
    border-left: 6px solid #D32F2F;
    border-radius: 10px;
    padding: 1.1rem 1.6rem;
    margin: .8rem 0;
  }
  .verdict-title {
    font-size: 1.25rem;
    font-weight: 900;
    color: #1A1A1A;
  }
  .verdict-sub {
    font-size: .88rem;
    color: #444444;
    margin-top: .3rem;
  }

  /* ── Barre de score ── */
  .score-bar-bg {
    background: #E0E0E0;
    border-radius: 6px;
    height: 10px;
    margin: 6px 0 14px;
  }
  .score-bar-fill {
    background: #FFC500;
    border-radius: 6px;
    height: 10px;
  }

  /* ── Boîtes info / alerte ── */
  .info-box {
    background: #FFFDE7;
    border: 1px solid #FFC500;
    border-radius: 8px;
    padding: .65rem 1rem;
    font-size: .84rem;
    color: #333;
    margin: .5rem 0;
  }
  .warn-box {
    background: #FFF3E0;
    border: 1px solid #FF8F00;
    border-radius: 8px;
    padding: .65rem 1rem;
    font-size: .84rem;
    color: #333;
    margin: .5rem 0;
  }

  /* ── Métriques ── */
  [data-testid="metric-container"] {
    background: #FAFAFA;
    border: 1px solid #E8E8E8;
    border-radius: 8px;
    padding: .65rem 1rem;
  }
</style>
""", unsafe_allow_html=True)

# ── En-tête ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="wl-header">
  <div class="wl-logo-sun">&#9728;</div>
  <div>
    <div class="wl-logo-text">WeLight Africa</div>
    <div class="wl-logo-sub">Outil d'aide à la décision — Mini-réseaux solaires Madagascar</div>
  </div>
  <div style="margin-left:auto; text-align:right">
    <span class="wl-badge">Google Open Buildings v3</span>
    <span class="wl-badge">NASA POWER GHI</span>
    <span class="wl-badge">172 villages opérés</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Fonctions S2 / GOB ─────────────────────────────────────────────────────────

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
        raise IOError(f"Tuile {token} corrompue — réessayez.")
    os.replace(tmp, path)
    return path

def count_buildings_detailed(lat, lon, tile_paths, radii, sme_threshold=SME_AREA_M2):
    max_r = max(radii); BB = max_r / 111.0 * 1.3
    candidates = []
    for path in tile_paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    if float(row.get("confidence", 1)) < MIN_CONFIDENCE: continue
                    blat = float(row["latitude"]); blon = float(row["longitude"])
                    if abs(blat - lat) > BB or abs(blon - lon) > BB: continue
                    d = haversine(lat, lon, blat, blon)
                    if d > max_r: continue
                    area = float(row.get("area_in_meters", 0) or 0)
                    candidates.append((d, blat, blon, area, area >= sme_threshold))
                except Exception:
                    pass
    result = {}
    for r in radii:
        in_r = [c for c in candidates if c[0] <= r]
        result[r] = {
            "total":      len(in_r),
            "residential": len([c for c in in_r if not c[4]]),
            "sme":         len([c for c in in_r if c[4]]),
            "res_coords":  [(c[1], c[2]) for c in in_r if not c[4]],
            "sme_coords":  [(c[1], c[2]) for c in in_r if c[4]],
        }
    return result

@st.cache_data(show_spinner=False)
def geocode(name):
    PRIO = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 4, "locality": 5}
    try:
        r = requests.get(NOMINATIM_URL,
                         params={"q": f"{name}, Madagascar", "format": "json",
                                 "limit": 8, "addressdetails": 1},
                         headers={"User-Agent": "WeLight-DecisionTool/2.0"},
                         timeout=10)
        results = r.json()
    except Exception:
        return []
    return sorted(results,
                  key=lambda x: (PRIO.get(x.get("type", ""), 99), x.get("place_rank", 99)))

@st.cache_data(show_spinner=False)
def get_ghi(lat, lon):
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
            ann = sum(v for v in monthly if v) / len([v for v in monthly if v])
        return (ann if ann > 0 else 5.5), monthly
    except Exception:
        return 5.5, [None] * 12

def get_grid_distance(lat, lon):
    best_d, best_name = None, None
    for tlat, tlon, tname in JIRAMA_TOWNS:
        d = haversine(lat, lon, tlat, tlon)
        if best_d is None or d < best_d:
            best_d, best_name = d, tname
    return round(best_d, 1), best_name

def compute_financials(n_res, n_sme, ghi, cfg):
    sub_r = round(n_res * cfg["pen_r"])
    sub_s = round(n_sme * cfg["pen_s"])
    daily_kwh = sub_r * cfg["kwh_r"] + sub_s * cfg["kwh_s"]
    if daily_kwh <= 0 or ghi <= 0:
        return None
    peak_kwp = daily_kwh / (ghi * cfg["eff"])
    batt_kwh = daily_kwh * cfg["batt"]
    capex    = peak_kwp * cfg["cpkwp"] + batt_kwh * cfg["cpbatt"]
    ann_rev  = (sub_r * cfg["tar_r"] + sub_s * cfg["tar_s"]) * 12
    opex     = capex * cfg["opex_p"]
    net_ann  = ann_rev - opex
    payback  = capex / net_ann if net_ann > 0 else 999
    npv = -capex + sum(net_ann / (1 + cfg["dr"]) ** t for t in range(1, cfg["life"] + 1))
    # IRR Newton-Raphson
    irr = None
    try:
        cf = [-capex] + [net_ann] * cfg["life"]
        r  = 0.1
        for _ in range(200):
            f  = sum(cf[t] / (1 + r) ** t for t in range(cfg["life"] + 1))
            df = sum(-t * cf[t] / (1 + r) ** (t + 1) for t in range(1, cfg["life"] + 1))
            if df == 0: break
            r2 = r - f / df
            if abs(r2 - r) < 1e-7: irr = r2; break
            r = r2
    except Exception:
        pass
    # Tarif résidentiel minimum pour payback 7 ans
    # Revenus annuels PME déjà acquis déduits — on cherche uniquement le complément résidentiel
    sme_ann_rev  = sub_s * cfg["tar_s"] * 12
    rev_needed   = max(0.0, capex / 7 + opex - sme_ann_rev)
    req_tar_r    = rev_needed / (sub_r * 12) if sub_r > 0 else None

    return {
        "sub_r": sub_r, "sub_s": sub_s,
        "peak_kwp": round(peak_kwp, 1),
        "batt_kwh": round(batt_kwh, 1),
        "capex":    round(capex),
        "ann_rev":  round(ann_rev),
        "opex":     round(opex),
        "net_ann":  round(net_ann),
        "payback":  round(payback, 1),
        "npv":      round(npv),
        "irr":      round(irr * 100, 1) if irr else None,
        "req_tar_r": round(req_tar_r, 2),
    }

def priority_score(fin, ghi, grid_km):
    """
    Score /80 pts :
      - Finance  40 pts  (payback <= 5 = 40, <=7 = 30, <=10 = 15, >10 = 0)
      - Solaire  20 pts  (GHI / 6.5 * 20, plafonné à 20)
      - Réseau   20 pts  (>50 km = 20, 20-50 km = 12, 10-20 km = 6, <10 km = 0)
      NOTE: distance à vol d'oiseau — présence réelle du réseau peut différer.
    """
    pb = fin["payback"] if fin else 999
    f  = 40 if pb <= 5 else 30 if pb <= 7 else 15 if pb <= 10 else 0
    s  = min(20, round(ghi / 6.5 * 20))
    g  = 20 if grid_km > 50 else 12 if grid_km > 20 else 6 if grid_km > 10 else 0
    return {"total": f + s + g, "finance": f, "solaire": s, "reseau": g}

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
                  color="#FFC500", fill=True, fill_opacity=0.07, weight=2).add_to(m)
    folium.Marker(location=[lat, lon],
                  popup=f"<b>{name}</b>",
                  icon=folium.Icon(color="orange", icon="home", prefix="fa")).add_to(m)
    for blat, blon in res_blds[:2500]:
        folium.CircleMarker(location=[blat, blon], radius=3,
                            color="#FF4500", fill=True, fill_opacity=0.85, weight=0).add_to(m)
    for blat, blon in sme_blds[:500]:
        folium.CircleMarker(location=[blat, blon], radius=6,
                            color="#FFC500", fill=True, fill_opacity=0.9, weight=1,
                            tooltip="Commerce / PME").add_to(m)
    folium.LayerControl().add_to(m)
    return m

# ── Recherche principale ───────────────────────────────────────────────────────

def run_search(village_name, sme_threshold):
    res = {"village": village_name, "error": None, "snapshot": {},
           "lat": None, "lon": None, "display_name": None, "candidates": [],
           "tile_count": 0, "ghi_annual": None, "ghi_monthly": None,
           "grid_km": None, "grid_town": None}

    with st.status(f"Analyse de **{village_name}**...", expanded=True) as status:
        st.write("Localisation via OpenStreetMap...")
        candidates = geocode(village_name)
        mada = ([c for c in candidates
                 if "Madagascar" in c.get("display_name", "")
                 or c.get("address", {}).get("country_code") == "mg"]
                or candidates)
        if not mada:
            status.update(label="Village introuvable", state="error")
            res["error"] = f"Aucun résultat pour « {village_name} » à Madagascar."
            st.session_state["result"] = res; return

        res["candidates"] = mada
        chosen = mada[0]
        lat, lon = float(chosen["lat"]), float(chosen["lon"])
        res["lat"], res["lon"] = lat, lon
        res["display_name"] = chosen.get("display_name", village_name)
        st.write(f"Trouvé : ({lat:.4f}, {lon:.4f})")

        st.write("Ressource solaire — NASA POWER...")
        ghi_ann, ghi_monthly = get_ghi(lat, lon)
        res["ghi_annual"] = ghi_ann; res["ghi_monthly"] = ghi_monthly
        st.write(f"GHI annuel : {ghi_ann:.2f} kWh/m²/jour")

        st.write("Distance réseau JIRAMA...")
        grid_km, grid_town = get_grid_distance(lat, lon)
        res["grid_km"] = grid_km; res["grid_town"] = grid_town
        st.write(f"Ville JIRAMA la plus proche : {grid_town} ({grid_km} km à vol d'oiseau)")

        st.write("Identification des tuiles satellitaires...")
        tokens  = get_s2_tokens(lat, lon, 4)
        ll      = s2sphere.LatLng.from_degrees(lat, lon)
        pri_tok = s2sphere.CellId.from_lat_lng(ll).parent(4).to_token()
        needed  = [(t, sz) for t in tokens
                   for (ok, sz) in [tile_exists(t)] if ok
                   if t == pri_tok or sz <= 20e6]
        if not needed:
            status.update(label="Aucune donnée satellite disponible", state="error")
            res["error"] = "Pas de tuile Google Open Buildings pour cette zone."
            st.session_state["result"] = res; return
        st.write(f"{len(needed)} tuile(s) à traiter")

        tile_paths = []
        for tok, sz in needed:
            cached = os.path.join(TILE_CACHE_DIR, f"{tok}_buildings.csv.gz")
            label  = f"Cache ({sz/1e6:.1f} Mo)" if os.path.exists(cached) else f"Téléchargement ({sz/1e6:.1f} Mo)..."
            st.write(f"   {tok} : {label}")
            try:
                tile_paths.append(download_tile(tok))
            except Exception as e:
                st.warning(f"   Ignoré {tok} : {e}")
        if not tile_paths:
            status.update(label="Échec du téléchargement", state="error")
            res["error"] = "Impossible de télécharger les tuiles."
            st.session_state["result"] = res; return

        st.write(f"Comptage des bâtiments (seuil confiance {MIN_CONFIDENCE:.0%}, PME >= {sme_threshold} m²)...")
        snapshot = count_buildings_detailed(lat, lon, tile_paths, SNAPSHOT_RADII, sme_threshold)
        res["snapshot"] = snapshot; res["tile_count"] = len(tile_paths)
        n2 = snapshot.get(2.0, {}).get("total", 0)
        status.update(label=f"Terminé — {n2:,} bâtiments dans un rayon de 2 km",
                      state="complete", expanded=False)

    st.session_state["result"] = res

# ── Sidebar : paramètres ───────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Paramètres")
    st.caption(
        "Valeurs par défaut calibrées sur le portefeuille WeLight "
        "(172 villages opérés à Madagascar). "
        "Modifiez selon votre contexte terrain."
    )

    st.markdown("### Détection PME")
    sme_threshold = st.number_input(
        "Surface min. bâtiment commercial (m²)", 50, 1000, 150, 25,
        help="Bâtiments >= cette surface = PME/commerce. "
             "Maison rurale malgache : 30-60 m² en moyenne."
    )

    st.markdown("### Clients résidentiels")
    pen_r = st.slider("Taux de pénétration résidentiel", 0.10, 1.0, 0.70, 0.05,
                      format="%.0f%%",
                      help="Part des ménages abonnés. WeLight atteint ~70% en pratique.")
    tar_r = st.number_input("Tarif mensuel résidentiel (EUR)", 0.5, 20.0, 2.50, 0.10,
                             format="%.2f",
                             help="WeLight pratique ~2,50 EUR/mois dans le nord de Madagascar.")
    kwh_r = st.number_input("Consommation résidentielle (kWh/jour)", 0.1, 3.0, 0.30, 0.05,
                             format="%.2f")

    st.markdown("### Clients PME / Commerces")
    pen_s = st.slider("Taux de pénétration PME", 0.10, 1.0, 0.60, 0.05, format="%.0f%%")
    tar_s = st.number_input("Tarif mensuel PME (EUR)", 1.0, 100.0, 12.00, 0.50,
                             format="%.2f",
                             help="Les PME paient généralement 10-15 EUR/mois pour une alimentation fiable.")
    kwh_s = st.number_input("Consommation PME (kWh/jour)", 0.5, 20.0, 2.00, 0.25,
                             format="%.2f")

    st.markdown("### Système & finance")
    capex_kwp  = st.number_input("CAPEX par kWp (EUR)", 500, 3000, 900, 50,
                                  help="900-1 000 EUR/kWp pour un opérateur expérimenté. "
                                       "1 200+ EUR pour un premier projet.")
    capex_batt = st.number_input("Coût batterie (EUR/kWh)", 100, 400, 200, 10,
                                  help="LFP Afrique 2025 : EUR 180-250/kWh. "
                                       "EUR 150/kWh = optimiste, EUR 250/kWh = prudent.")
    eff       = st.slider("Efficacité système", 0.50, 0.90, 0.75, 0.01, format="%.0f%%")
    batt_days = st.slider("Autonomie batterie (jours)", 0.5, 3.0, 1.5, 0.25)
    opex_pct  = st.slider("OPEX (% du CAPEX / an)", 0.01, 0.10, 0.04, 0.005,
                          format="%.1f%%")
    dr        = st.slider("Taux d'actualisation", 0.05, 0.25, 0.08, 0.01, format="%.0f%%",
                          help="8% pour financement DFI (BEI/Triodos). "
                               "12-15% pour capital commercial.")
    life      = st.number_input("Durée du projet (ans)", 5, 30, 15, 1)

    cfg = {
        "pen_r": pen_r, "pen_s": pen_s,
        "tar_r": tar_r, "tar_s": tar_s,
        "kwh_r": kwh_r, "kwh_s": kwh_s,
        "cpkwp": capex_kwp, "cpbatt": capex_batt, "eff": eff,
        "batt":  batt_days, "opex_p": opex_pct,
        "dr": dr, "life": int(life),
    }

# ── Barre de recherche ─────────────────────────────────────────────────────────
col_v, col_b = st.columns([5, 1])
with col_v:
    village_input = st.text_input(
        "Village", label_visibility="collapsed",
        placeholder="Ex. : Betsiaka, Tsarabaria, Mahavanona, Farahalana..."
    )
with col_b:
    search = st.button("🔍 Analyser", type="primary", use_container_width=True)

if search:
    if village_input.strip():
        run_search(village_input.strip(), sme_threshold)
    else:
        st.warning("Veuillez saisir un nom de village.")

# ── Affichage des résultats ────────────────────────────────────────────────────
res = st.session_state["result"]
if res:
    if res["error"]:
        st.error(res["error"])
    else:
        # Sélecteur si plusieurs résultats OSM
        if len(res["candidates"]) > 1:
            opts = {
                f"{c['display_name'][:80]}  ({float(c['lat']):.3f}, {float(c['lon']):.3f})": i
                for i, c in enumerate(res["candidates"][:5])
            }
            idx = opts[st.selectbox("Plusieurs résultats OSM — choisissez le bon :", list(opts.keys()))]
            ch  = res["candidates"][idx]
            res["lat"], res["lon"] = float(ch["lat"]), float(ch["lon"])
            res["display_name"] = ch.get("display_name", res["village"])

        lat   = res["lat"];  lon  = res["lon"]
        name  = res["village"]
        snap  = res["snapshot"]
        ghi   = res["ghi_annual"] or 5.5
        ghim  = res["ghi_monthly"] or [None] * 12
        gkm   = res["grid_km"] or 0
        gtown = res["grid_town"] or "inconnue"
        dname = res["display_name"] or name

        d2    = snap.get(2.0, {})
        n_res = d2.get("residential", 0)
        n_sme = d2.get("sme", 0)
        n_tot = d2.get("total", 0)

        fin   = compute_financials(n_res, n_sme, ghi, cfg)
        sc    = priority_score(fin, ghi, gkm)

        # ── Alerte coordonnées suspectes ──────────────────────────────────────
        if n_tot < 30:
            if any(t in dname.lower() for t in ["district", "province", "region", "diana", "sava", "sofia"]):
                st.markdown(
                    f'<div class="warn-box">⚠️ Les coordonnées semblent pointer sur une limite administrative, '
                    f'pas sur le centre du village. '
                    f'<a href="https://maps.google.com/?q={lat},{lon}" target="_blank">Vérifier sur Google Maps</a></div>',
                    unsafe_allow_html=True,
                )

        # ── Verdict ───────────────────────────────────────────────────────────
        if fin:
            pb = fin["payback"]; total_sc = sc["total"]
            if pb <= 7 and total_sc >= 42:
                vcls  = "verdict-invest"
                vicon = "✅"
                vtit  = "INVESTIR"
                vsub  = f"Bonne viabilité : remboursement en {pb} ans, score {total_sc}/80."
            elif pb <= 12 and total_sc >= 27:
                vcls  = "verdict-evaluate"
                vicon = "🔶"
                vtit  = "À ÉVALUER"
                vsub  = f"Signaux mixtes — remboursement {pb} ans, score {total_sc}/80. Visite terrain recommandée."
            else:
                vcls  = "verdict-no"
                vicon = "❌"
                vtit  = "NE PAS INVESTIR"
                vsub  = f"Remboursement trop long ({pb} ans) ou score insuffisant ({total_sc}/80) avec les paramètres actuels."

            st.markdown(f"""
            <div class="{vcls}">
              <div class="verdict-title">{vicon}&nbsp; {name.upper()} — {vtit}</div>
              <div class="verdict-sub">{vsub}</div>
            </div>""", unsafe_allow_html=True)

            # Barre de score
            pct = int(total_sc / 80 * 100)
            st.markdown(f"""
            <div style="font-size:.8rem;color:#888;margin-bottom:2px">
              Score global : <b style="color:#1A1A1A">{total_sc} / 80 pts</b>
              &nbsp;·&nbsp; Finance : {sc['finance']}/40
              &nbsp;·&nbsp; Solaire : {sc['solaire']}/20
              &nbsp;·&nbsp; Réseau : {sc['reseau']}/20
            </div>
            <div class="score-bar-bg">
              <div class="score-bar-fill" style="width:{pct}%"></div>
            </div>""", unsafe_allow_html=True)

        # ── Métriques clés ────────────────────────────────────────────────────
        st.markdown("---")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("🏠 Bâtiments résidentiels", f"{n_res:,}",
                  help="Rayon 2 km, confiance ≥ 60%")
        c2.metric("🏪 PME / Commerces", f"{n_sme:,}",
                  help=f"Bâtiments ≥ {sme_threshold} m²")
        c3.metric("☀️ GHI annuel moyen", f"{ghi:.2f} kWh/m²/j")
        c4.metric("🔌 Distance réseau", f"{gkm} km",
                  help=f"Ville JIRAMA la plus proche : {gtown} (distance à vol d'oiseau — pas la présence réelle du réseau)")
        c5.metric("👥 Population estimée", f"{round(n_tot * 4.5):,}",
                  help="Bâtiments totaux × 4,5 pers./ménage (INSTAT Madagascar 2018)")

        # Note distance réseau
        if gkm <= 25:
            st.markdown(
                f'<div class="info-box">ℹ️ <b>Distance réseau :</b> {gtown} est à {gkm} km à vol d\'oiseau, '
                f'mais la présence réelle du réseau JIRAMA dans ce village est à vérifier sur le terrain. '
                f'Des villages WeLight confirmés (ex. Mahavanona, 15 km d\'Antsiranana) sont dans cette configuration et restent viables.</div>',
                unsafe_allow_html=True,
            )

        # ── Onglets ───────────────────────────────────────────────────────────
        tab1, tab2, tab3 = st.tabs(["💶 Modèle financier", "🗺️ Carte satellite", "☀️ Profil solaire"])

        # ── Onglet 1 : Finance ────────────────────────────────────────────────
        with tab1:
            if fin:
                st.subheader("Projection revenus & investissement")
                fc1, fc2, fc3 = st.columns(3)
                fc1.metric("Puissance crête", f"{fin['peak_kwp']} kWc")
                fc1.metric("Stockage batterie", f"{fin['batt_kwh']} kWh")
                fc2.metric("CAPEX total", f"EUR {fin['capex']:,}")
                fc2.metric("Revenus annuels", f"EUR {fin['ann_rev']:,}")
                fc3.metric("Remboursement", f"{fin['payback']} ans",
                           delta="OK" if fin["payback"] <= 7 else "Trop long",
                           delta_color="normal" if fin["payback"] <= 7 else "inverse")
                fc3.metric(f"VAN ({life} ans)", f"EUR {fin['npv']:,}",
                           delta_color="normal" if fin["npv"] > 0 else "inverse")
                if fin["irr"]:
                    fc3.metric("TRI", f"{fin['irr']}%")

                st.markdown("#### Répartition abonnés")
                sc1, sc2 = st.columns(2)
                sc1.metric("Abonnés résidentiels", f"{fin['sub_r']:,}",
                           help=f"{pen_r:.0%} de {n_res:,} bâtiments résidentiels")
                sc2.metric("Abonnés PME", f"{fin['sub_s']:,}",
                           help=f"{pen_s:.0%} de {n_sme:,} PME détectées")

                rev_res = fin["sub_r"] * tar_r * 12
                rev_sme = fin["sub_s"] * tar_s * 12
                if rev_res + rev_sme > 0:
                    sme_share = rev_sme / (rev_res + rev_sme) * 100
                    st.caption(
                        f"Part PME dans les revenus : **{sme_share:.0f}%** "
                        f"(EUR {rev_sme:,.0f}/an) vs résidentiel EUR {rev_res:,.0f}/an"
                    )

                # Tarif résidentiel minimum (net des revenus PME)
                req = fin["req_tar_r"]
                if req is None:
                    st.markdown(
                        '<div class="info-box">ℹ️ Aucun abonné résidentiel — les revenus PME seuls ne permettent pas de calculer un tarif résidentiel minimum.</div>',
                        unsafe_allow_html=True,
                    )
                elif req > tar_r:
                    st.markdown(
                        f'<div class="warn-box">⚠️ Pour un remboursement en 7 ans, le tarif résidentiel '
                        f'doit atteindre <b>EUR {req:.2f}/mois</b> (actuellement EUR {tar_r:.2f}). '
                        f'Essayez d\'augmenter le tarif PME ou le taux de pénétration.</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f'<div class="info-box">✅ Le tarif actuel (EUR {tar_r:.2f}/mois) est supérieur au minimum '
                        f'requis (EUR {req:.2f}/mois, déduction faite des revenus PME) '
                        f'pour un remboursement en 7 ans.</div>',
                        unsafe_allow_html=True,
                    )

                # Cashflow cumulé
                st.markdown(f"#### Flux de trésorerie cumulé ({life} ans)")
                ann_net = fin["net_ann"]
                cf  = [-fin["capex"]] + [ann_net] * int(life)
                cum = []; s = 0
                for v in cf: s += v; cum.append(s)
                df_cf = pd.DataFrame({
                    "Année": list(range(0, int(life) + 1)),
                    "Flux cumulé (EUR)": cum
                }).set_index("Année")
                st.line_chart(df_cf, color="#FFC500")

            else:
                st.info("Aucun abonné avec les paramètres actuels — ajustez les taux de pénétration.")

        # ── Onglet 2 : Carte ──────────────────────────────────────────────────
        with tab2:
            bld_res = d2.get("res_coords", [])
            bld_sme = d2.get("sme_coords", [])
            st_folium(make_map(lat, lon, bld_res, bld_sme, 2.0, name),
                      width=None, height=520, returned_objects=[])
            st.caption(
                "🔴 Bâtiments résidentiels &nbsp;·&nbsp; 🟡 PME / Commerces (survol = label) &nbsp;·&nbsp; "
                f"Cercle jaune = rayon 2 km &nbsp;·&nbsp; "
                f"[Ouvrir dans Google Maps](https://maps.google.com/?q={lat},{lon})"
            )

            # Tableau multi-rayons
            st.markdown("#### Bâtiments par rayon")
            rows = []
            for r in SNAPSHOT_RADII:
                d = snap.get(r, {})
                rows.append({
                    "Rayon": f"{r} km",
                    "Total": d.get("total", 0),
                    "Résidentiels": d.get("residential", 0),
                    "PME / Commerces": d.get("sme", 0),
                    "Pop. estimée": round(d.get("total", 0) * 4.5),
                })
            st.dataframe(pd.DataFrame(rows).set_index("Rayon"), use_container_width=True)

        # ── Onglet 3 : Solaire ────────────────────────────────────────────────
        with tab3:
            st.subheader("Profil GHI mensuel")
            mois = ["Jan","Fév","Mar","Avr","Mai","Jun","Jul","Aoû","Sep","Oct","Nov","Déc"]
            if any(v for v in ghim if v):
                df_ghi = pd.DataFrame({
                    "Mois": mois,
                    "GHI (kWh/m²/jour)": [v if v else 0 for v in ghim],
                }).set_index("Mois")
                st.bar_chart(df_ghi, color="#FFC500")
                min_m = min((v for v in ghim if v), default=0)
                max_m = max((v for v in ghim if v), default=0)
                st.caption(
                    f"Moyenne annuelle : **{ghi:.2f}** kWh/m²/j &nbsp;·&nbsp; "
                    f"Mois min : **{min_m:.2f}** &nbsp;·&nbsp; Mois max : **{max_m:.2f}** &nbsp;·&nbsp; "
                    f"Source : NASA POWER climatologie"
                )
                # Alerte si mois le plus faible < 75% de la moyenne (sous-dimensionnement potentiel)
                if ghi > 0 and min_m < 0.75 * ghi:
                    kwp_avg   = round(1 / (ghi * cfg["eff"]), 3)   # kWc par kWh/jour sur moy.
                    kwp_worst = round(1 / (min_m * cfg["eff"]), 3)  # kWc par kWh/jour sur mois min
                    pct_extra = round((kwp_worst / kwp_avg - 1) * 100)
                    st.markdown(
                        f'<div class="warn-box">⚠️ <b>Risque de sous-dimensionnement :</b> '
                        f'le mois le plus faible ({min_m:.2f} kWh/m²/j) représente '
                        f'{min_m/ghi*100:.0f}% de la moyenne annuelle. '
                        f'Un système dimensionné sur la moyenne annuelle sera déficitaire '
                        f'en saison défavorable ({pct_extra}% de puissance manquante). '
                        f'Augmentez l\'autonomie batterie ou la puissance crête en conséquence.</div>',
                        unsafe_allow_html=True,
                    )
                elif min_m < 4.0:
                    st.markdown(
                        f'<div class="warn-box">⚠️ Mois à faible ensoleillement ({min_m:.2f} kWh/m²/j). '
                        f'Augmentez l\'autonomie batterie pour une alimentation fiable toute l\'année.</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.info(f"Données mensuelles indisponibles. Moyenne annuelle utilisée : {ghi:.2f} kWh/m²/j")

            st.markdown("#### Contexte réseau électrique")
            gcol1, gcol2 = st.columns(2)
            gcol1.metric("Distance réseau JIRAMA", f"{gkm} km",
                         help=f"Ville électrifiée la plus proche : {gtown}")
            if gkm > 50:
                gcol2.markdown(
                    '<div class="info-box">🟢 <b>Zone très isolée</b> — risque d\'extension du réseau '
                    'dans les 10 ans très faible. Fort avantage compétitif pour le mini-réseau.</div>',
                    unsafe_allow_html=True,
                )
            elif gkm > 20:
                gcol2.markdown(
                    '<div class="warn-box">🟡 <b>Isolation modérée</b> — extension possible '
                    'à 5-10 ans. À intégrer dans l\'horizon du projet.</div>',
                    unsafe_allow_html=True,
                )
            else:
                gcol2.markdown(
                    '<div class="info-box">ℹ️ <b>Proche d\'une ville électrifiée</b> — '
                    'mais la distance à vol d\'oiseau ne signifie pas que le réseau JIRAMA '
                    'atteint physiquement le village. Vérification terrain requise.</div>',
                    unsafe_allow_html=True,
                )

# ── Pied de page ───────────────────────────────────────────────────────────────
st.divider()
st.markdown(
    "<small style='color:#999'>☀️ WeLight Africa — Outil Décisionnel Solaire v2.0 &nbsp;·&nbsp; "
    "Google Open Buildings v3 (confiance ≥ 60%) &nbsp;·&nbsp; "
    "NASA POWER GHI &nbsp;·&nbsp; "
    "OpenStreetMap Nominatim &nbsp;·&nbsp; "
    "Calibré sur 172 villages opérés à Madagascar</small>",
    unsafe_allow_html=True,
)
