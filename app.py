"""
WeLight Africa - Village Solar Investment Analyzer
Streamlit web application
"""
import sys, os, math, gzip, csv, json
import requests, folium, s2sphere
import streamlit as st
from streamlit_folium import st_folium

# ── Import analysis pipeline from analyze_village.py ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from analyze_village import (
    load_config, haversine,
    get_s2_tokens, tile_exists, _is_valid_gz, download_tile,
    count_buildings_multi, search_village,
    module_revenue, module_solar_resource, module_sizing,
    module_financials, module_constraints, compute_priority_score,
    _commercial_activity_osm, JIRAMA_NODES, SNAPSHOT_RADII,
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="WeLight — Solar Investment Analyzer",
    page_icon="solar_panels",
    layout="wide",
)

CFG_PATH       = os.path.join(os.path.dirname(__file__), "config.json")
TILE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".tile_cache_google")
GOB_BASE       = "https://storage.googleapis.com/open-buildings-data/v3/points_s2_level_4_gzip"
os.makedirs(TILE_CACHE_DIR, exist_ok=True)

PLACE_PRIORITY = {"city": 0, "town": 1, "village": 2, "hamlet": 3, "suburb": 4, "locality": 5}

# ── Session state ──────────────────────────────────────────────────────────────
for k, v in [("rooftop_result", None), ("full_analysis", None)]:
    if k not in st.session_state:
        st.session_state[k] = v

# ══════════════════════════════════════════════════════════════════════════════
# INVESTMENT RECOMMENDATION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def generate_recommendation(village, households, score, fin, solar, sizing,
                             constraints, cfg):
    """
    Returns a structured recommendation dict with verdict and French justifications.
    """
    payback   = fin["payback_years"] or 999
    ghi       = solar["ghi_annual_kwh_m2_day"]
    grid_km   = constraints["grid_distance_km"] or 0
    max_pb    = cfg["max_acceptable_payback_years"]
    ins       = constraints["instability_score"]
    road      = constraints["road_score"]
    total     = score["total"]
    irr       = fin.get("irr_pct") or 0
    npv       = fin["npv_eur"]
    capex     = sizing["capex_eur"]
    pen       = cfg["penetration_rate"]
    tariff    = cfg["monthly_tariff_eur"]
    lifetime  = cfg["project_lifetime_years"]

    # ── Verdict ────────────────────────────────────────────────────────────────
    if total >= 60 and payback <= max_pb:
        verdict       = "INVESTIR"
        verdict_color = "#166534"
        verdict_bg    = "#DCFCE7"
        verdict_icon  = "green"
    elif total >= 35 or (payback <= max_pb * 1.3):
        verdict       = "A ETUDIER"
        verdict_color = "#92400E"
        verdict_bg    = "#FEF3C7"
        verdict_icon  = "orange"
    else:
        verdict       = "NE PAS INVESTIR"
        verdict_color = "#991B1B"
        verdict_bg    = "#FEE2E2"
        verdict_icon  = "red"

    # ── Summary sentence ───────────────────────────────────────────────────────
    if verdict == "INVESTIR":
        summary = (
            f"**{village}** presente un profil solide pour un mini-reseau WeLight. "
            f"Le remboursement en **{payback:.1f} ans** est inferieur au seuil de {max_pb} ans, "
            f"le village est a **{grid_km:.0f} km du reseau JIRAMA** (pas de concurrence immediate) "
            f"et l'ensoleillement de **{ghi:.2f} kWh/m2/j** garantit une production fiable toute l'annee."
        )
    elif verdict == "A ETUDIER":
        summary = (
            f"**{village}** necessite une analyse complementaire avant decision. "
            f"Le score global de **{total:.0f}/100** et le remboursement de **{payback:.1f} ans** "
            f"sont {'proches du seuil acceptable' if payback <= max_pb * 1.3 else 'au-dela du seuil de ' + str(max_pb) + ' ans'}. "
            f"Des ajustements de parametres ou une validation terrain pourraient changer la conclusion."
        )
    else:
        summary = (
            f"**{village}** ne repond pas aux criteres d'investissement WeLight dans les conditions actuelles. "
            f"Le remboursement de **{payback:.1f} ans** depasse significativement le seuil de {max_pb} ans "
            f"et la VAN de **EUR {npv:,.0f}** est negative sur {lifetime} ans."
        )

    # ── Arguments POUR ────────────────────────────────────────────────────────
    pros = []
    if grid_km > 50:
        pros.append(f"Isolement du reseau JIRAMA ({grid_km:.0f} km) : marche captif, pas de concurrence de l'electricite publique a court terme.")
    elif grid_km > 20:
        pros.append(f"Distance reseau moderee ({grid_km:.0f} km) : risque d'electrification JIRAMA faible a moyen terme.")
    if ghi >= 5.5:
        pros.append(f"Ensoleillement favorable ({ghi:.2f} kWh/m2/j) : production solaire optimale toute l'annee, factor de charge eleve.")
    if road >= 8:
        pros.append(f"Acces routier bon ({constraints['road_type']}) : logistique d'installation et de maintenance simplifiee.")
    if ins <= 3:
        pros.append(f"Environnement securitaire stable (score ACLED {ins:.1f}/10) : risque operationnel faible.")
    if constraints.get("has_commercial"):
        pros.append(f"Activite commerciale detectee ({constraints['commercial_count']} points OSM) : facteur d'usage productif applicable, revenus potentiellement superieurs.")
    if households >= 500:
        pros.append(f"Taille de marche adequate ({households:,} menages) : economie d'echelle favorable sur le projet.")
    if payback <= max_pb:
        pros.append(f"Remboursement en {payback:.1f} ans (< seuil {max_pb} ans) : projet autoportant sans subvention.")
    if irr > 0:
        pros.append(f"TRI positif ({irr:.1f}%) : le projet cree de la valeur sur {lifetime} ans.")
    if npv > 0:
        pros.append(f"VAN positive (EUR {npv:,.0f}) : flux nets actualises favorables a {cfg['discount_rate']:.0%} de taux d'actualisation.")

    # ── Arguments CONTRE ──────────────────────────────────────────────────────
    cons = []
    if payback > max_pb:
        cons.append(f"Remboursement de {payback:.1f} ans depasse le seuil WeLight de {max_pb} ans : le projet n'est pas autoportant aux tarifs actuels.")
    if npv < 0:
        cons.append(f"VAN negative (EUR {npv:,.0f}) : destruction de valeur nette sur {lifetime} ans au taux d'actualisation de {cfg['discount_rate']:.0%}.")
    if grid_km < 20:
        cons.append(f"Proximite du reseau JIRAMA ({grid_km:.0f} km) : risque fort d'electrification publique a court/moyen terme, rendant le mini-reseau obsolete.")
    if ins > 6:
        cons.append(f"Risque securitaire eleve (ACLED {ins:.1f}/10) : incidents frequents, equipements exposes, operateurs en danger.")
    elif ins > 3:
        cons.append(f"Risque securitaire modere (ACLED {ins:.1f}/10) : surveiller l'evolution du contexte avant engagement.")
    if road <= 3:
        cons.append(f"Acces routier difficile ({constraints['road_type']}) : cout logistique eleve pour installation et maintenance corrective.")
    if households < 200:
        cons.append(f"Village de petite taille ({households} menages) : CAPEX par menage eleve, pas d'economie d'echelle, risque de non-rentabilite si penetration reelle < estimee.")
    if ghi < 5.0:
        cons.append(f"Ensoleillement limite ({ghi:.2f} kWh/m2/j) : necessite un systeme surdimensionne, augmente le CAPEX.")
    if irr is not None and irr < 0:
        cons.append(f"TRI negatif ({irr:.1f}%) : meme sur {lifetime} ans, le projet ne couvre pas le cout du capital.")

    # ── Conditions pour rendre le projet viable ────────────────────────────────
    conditions = []
    if payback > max_pb:
        # Required tariff for target payback
        kwh_per_hh    = cfg["consumption_per_household_kwh_day"] * cfg["productive_use_factor"]
        capex_per_hh  = kwh_per_hh / (ghi * cfg["system_efficiency"]) * cfg["capex_per_kwp_eur"]
        opex_per_hh   = capex_per_hh * cfg["opex_pct_capex"]
        req_rev_hh    = capex_per_hh / max_pb + opex_per_hh
        req_tariff    = req_rev_hh / (pen * 12)
        conditions.append(
            f"Relever le tarif a **EUR {req_tariff:.2f}/mois** (actuellement EUR {tariff:.2f}) "
            f"pour atteindre un remboursement de {max_pb} ans — soit +{(req_tariff/tariff - 1):.0%}."
        )
        # Required penetration rate
        req_pen = req_rev_hh / (tariff * 12)
        if req_pen <= 0.95:
            conditions.append(
                f"Ou augmenter le taux de penetration a **{req_pen:.0%}** (actuellement {pen:.0%}) "
                f"via un effort commercial intensif pre-lancement."
            )
        # Required capex reduction
        target_capex_hh = (tariff * pen * 12 - opex_per_hh) * max_pb
        req_kwp_cost    = target_capex_hh / (kwh_per_hh / (ghi * cfg["system_efficiency"]))
        if req_kwp_cost > 400:
            conditions.append(
                f"Ou reduire le CAPEX a **EUR {req_kwp_cost:,.0f}/kWp** (actuellement EUR {cfg['capex_per_kwp_eur']:,}/kWp) "
                f"par negociation fournisseur ou economie d'echelle multi-sites."
            )
        # Subsidy bridge
        gap_eur = capex - (fin["annual_margin_eur"] * max_pb)
        if gap_eur > 0:
            conditions.append(
                f"Ou obtenir une **subvention/don de EUR {gap_eur:,.0f}** "
                f"({gap_eur/capex:.0%} du CAPEX) pour combler l'ecart de viabilite — "
                f"eligible aux fonds BERD, AFD, SEFA ou WeLight Foundation."
            )
    if grid_km < 20:
        conditions.append(
            f"Obtenir une **confirmation officielle JIRAMA** que ce village n'est pas dans "
            f"le plan d'electrification a 5 ans avant tout engagement."
        )
    if road <= 3:
        conditions.append(
            f"Realiser une **etude logistique** (cout transport, accessibilite saison des pluies) "
            f"avant validation du budget CAPEX."
        )

    return {
        "verdict":        verdict,
        "verdict_color":  verdict_color,
        "verdict_bg":     verdict_bg,
        "verdict_icon":   verdict_icon,
        "summary":        summary,
        "pros":           pros,
        "cons":           cons,
        "conditions":     conditions,
    }


# ══════════════════════════════════════════════════════════════════════════════
# GOB TILE HELPERS (for rooftop tab)
# ══════════════════════════════════════════════════════════════════════════════

def get_tile_paths(lat, lon, writer):
    tokens  = get_s2_tokens(lat, lon, 4)
    ll      = s2sphere.LatLng.from_degrees(lat, lon)
    pri_tok = s2sphere.CellId.from_lat_lng(ll).parent(4).to_token()
    present = [(t, sz) for t in tokens
               for (ok, sz) in [tile_exists(t)] if ok
               if t == pri_tok or sz <= 20e6]
    if not present:
        return []
    tile_paths = []
    for tok, sz in present:
        cached = os.path.join(TILE_CACHE_DIR, f"{tok}_buildings.csv.gz")
        if os.path.exists(cached):
            writer(f"   Cache {tok} ({sz/1e6:.1f} MB)")
        else:
            writer(f"   Download {tok} ({sz/1e6:.1f} MB)...")
        try:
            tile_paths.append(download_tile(tok))
        except Exception as e:
            writer(f"   Warning: {tok} skipped ({e})")
    return tile_paths


def make_rooftop_map(lat, lon, buildings, radius_km, name):
    m = folium.Map(
        location=[lat, lon], zoom_start=15,
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery",
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
                html=f'<div style="font-size:11px;color:#fff;background:rgba(0,0,0,.6);padding:2px 7px;border-radius:3px">Shown: 3,000 / {len(buildings):,}</div>',
                icon_size=(200, 22)),
        ).add_to(m)
    folium.LayerControl().add_to(m)
    return m


def make_investment_map(lat, lon, village_name, grid_km, nearest_jirama):
    m = folium.Map(location=[lat, lon], zoom_start=8, tiles="CartoDB positron")
    # Village
    folium.Marker(location=[lat, lon],
                  popup=f"<b>{village_name}</b>",
                  icon=folium.Icon(color="blue", icon="bolt", prefix="fa")).add_to(m)
    # 50km radius
    folium.Circle(location=[lat, lon], radius=50_000,
                  color="#94A3B8", fill=False, weight=1.5, dash_array="6",
                  tooltip="50 km radius").add_to(m)
    # Nearest JIRAMA
    best = min(JIRAMA_NODES, key=lambda t: haversine(lat, lon, t[1], t[2]))
    folium.Marker(location=[best[1], best[2]],
                  tooltip=f"JIRAMA: {best[0]} ({grid_km:.0f} km)",
                  icon=folium.Icon(color="red", icon="plug", prefix="fa")).add_to(m)
    folium.PolyLine(locations=[[lat, lon], [best[1], best[2]]],
                    color="#EF4444", weight=1.5, dash_array="4",
                    tooltip=f"Grid distance: {grid_km:.0f} km").add_to(m)
    return m


# ══════════════════════════════════════════════════════════════════════════════
# RUNNERS
# ══════════════════════════════════════════════════════════════════════════════

def run_rooftop(village_name, min_conf):
    res = {"village": village_name, "error": None, "snapshot": {},
           "lat": None, "lon": None, "display_name": None,
           "candidates": [], "tile_count": 0}
    with st.status(f"Searching **{village_name}**...", expanded=True) as status:
        cands = search_village(village_name)
        mada  = [c for c in cands
                 if "Madagascar" in c.get("display_name", "")
                 or c.get("address", {}).get("country_code") == "mg"] or cands
        if not mada:
            status.update(label="Village not found", state="error")
            res["error"] = f"No result for '{village_name}'."; st.session_state["rooftop_result"] = res; return
        chosen = mada[0]
        lat = float(chosen["lat"]); lon = float(chosen["lon"])
        res.update(lat=lat, lon=lon, display_name=chosen.get("display_name", village_name), candidates=mada)
        st.write(f"Coordinates: ({lat:.4f}, {lon:.4f})")
        tile_paths = get_tile_paths(lat, lon, st.write)
        if not tile_paths:
            status.update(label="No GOB tiles for this area", state="error")
            res["error"] = "No Google Open Buildings tiles available."; st.session_state["rooftop_result"] = res; return
        snapshot = count_buildings_multi(lat, lon, tile_paths, SNAPSHOT_RADII, min_conf)
        res["snapshot"] = snapshot; res["tile_count"] = len(tile_paths)
        n2 = snapshot.get(2.0, (0, []))[0]
        status.update(label=f"Done - {n2:,} buildings (2 km)", state="complete", expanded=False)
    st.session_state["rooftop_result"] = res


def run_investment(village_name, households, min_conf, cfg):
    an = {"village": village_name, "error": None, "snapshot": {},
          "lat": None, "lon": None, "display_name": None, "candidates": [],
          "ghi": None, "grid_dist": None, "nearest_jirama": None,
          "score": None, "revenue": None, "sizing": None,
          "fin": None, "constraints": None, "recommendation": None}
    with st.status(f"Full analysis of **{village_name}**...", expanded=True) as status:
        # Geocode
        cands = search_village(village_name)
        mada  = [c for c in cands
                 if "Madagascar" in c.get("display_name", "")
                 or c.get("address", {}).get("country_code") == "mg"] or cands
        if not mada:
            status.update(label="Village not found", state="error")
            an["error"] = f"No result for '{village_name}'."; st.session_state["full_analysis"] = an; return
        chosen = mada[0]
        lat = float(chosen["lat"]); lon = float(chosen["lon"])
        an.update(lat=lat, lon=lon, display_name=chosen.get("display_name", village_name), candidates=mada)
        st.write(f"1/5 - Coordinates: ({lat:.4f}, {lon:.4f})")

        # GOB (optional - user may have provided household count)
        if households == 0:
            st.write("2/5 - Building count (Google Open Buildings)...")
            tile_paths = get_tile_paths(lat, lon, st.write)
            if tile_paths:
                snapshot = count_buildings_multi(lat, lon, tile_paths, SNAPSHOT_RADII, min_conf)
                households = snapshot.get(2.0, (0, []))[0]
                an["snapshot"] = snapshot
            if households == 0:
                status.update(label="No buildings found", state="error")
                an["error"] = "No buildings found."; st.session_state["full_analysis"] = an; return
        st.write(f"2/5 - Households: {households:,}")

        # Solar
        st.write("3/5 - Solar resource (NASA POWER)...")
        solar = module_solar_resource(lat, lon)
        an["ghi"] = solar["ghi_annual_kwh_m2_day"]
        st.write(f"   GHI = {solar['ghi_annual_kwh_m2_day']:.2f} kWh/m2/day ({solar['source']})")

        # Constraints
        st.write("4/5 - Constraints (grid, roads, security)...")
        constraints = module_constraints(lat, lon, cfg)
        an["grid_dist"]       = constraints["grid_distance_km"]
        an["nearest_jirama"]  = constraints["nearest_jirama"]

        # Revenue + Sizing + Financials
        st.write("5/5 - Financial model...")
        revenue    = module_revenue(households, cfg, constraints.get("has_commercial", False))
        sizing     = module_sizing(households, solar["ghi_annual_kwh_m2_day"], cfg)
        fin        = module_financials(sizing["capex_eur"], revenue["annual_revenue_eur"], cfg)
        score      = compute_priority_score(fin, solar, constraints)
        rec        = generate_recommendation(village_name, households, score,
                                              fin, solar, sizing, constraints, cfg)
        an.update(revenue=revenue, sizing=sizing, fin=fin, constraints=constraints,
                  score=score, recommendation=rec)

        badge = rec["verdict"]
        status.update(
            label=f"{badge} | Score {score['total']:.0f}/100 | Payback {fin['payback_years']:.1f if fin['payback_years'] else 'N/A'} yrs | CAPEX EUR {sizing['capex_eur']:,.0f}",
            state="complete", expanded=False,
        )
    st.session_state["full_analysis"] = an


# ══════════════════════════════════════════════════════════════════════════════
# CSS
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<style>
  .main-title { font-size:1.7rem; font-weight:700; color:#1F4E79; margin:0 }
  .sub-title  { font-size:.85rem; color:#6B7280; margin:.1rem 0 1rem }
  .badge      { display:inline-block; padding:2px 10px; border-radius:10px;
                font-size:.72rem; font-weight:700; margin-right:4px }
  .result-box { background:#EBF5FB; border-left:5px solid #2563EB;
                padding:.9rem 1.4rem; border-radius:6px; margin:.8rem 0 }
  .result-num { font-size:2.8rem; font-weight:800; color:#1E3A5F }
  .verdict-box { padding:1.2rem 1.6rem; border-radius:8px; margin:1rem 0 }
  .verdict-num { font-size:2.6rem; font-weight:800; line-height:1 }
  .pro-item   { padding:5px 0; border-bottom:1px solid #F0FFF4;
                font-size:.88rem; color:#166534 }
  .con-item   { padding:5px 0; border-bottom:1px solid #FFF1F2;
                font-size:.88rem; color:#9F1239 }
  .cond-item  { padding:6px 0; border-bottom:1px solid #F5F3FF;
                font-size:.88rem; color:#5B21B6 }
  .kpi-card   { background:#F8FAFC; border:1px solid #E2E8F0; border-radius:6px;
                padding:10px 14px; text-align:center }
  .kpi-val    { font-size:1.5rem; font-weight:700; color:#1F4E79 }
  .kpi-lbl    { font-size:.72rem; color:#9CA3AF }
</style>
""", unsafe_allow_html=True)

# ── Header ──────────────────────────────────────────────────────────────────────
st.markdown('<p class="main-title">⚡ WeLight Africa — Solar Investment Analyzer</p>',
            unsafe_allow_html=True)
st.markdown(
    '<p class="sub-title">Madagascar off-grid solar mini-grid prioritization tool'
    '&nbsp;<span class="badge" style="background:#DBEAFE;color:#1E40AF">Google Open Buildings v3</span>'
    '<span class="badge" style="background:#D1FAE5;color:#065F46">NASA POWER</span>'
    '<span class="badge" style="background:#EDE9FE;color:#5B21B6">JIRAMA Grid Proxy</span>'
    '<span class="badge" style="background:#FEF3C7;color:#92400E">ACLED Security</span>'
    '</p>',
    unsafe_allow_html=True,
)

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════

tab_rooftop, tab_invest = st.tabs(
    ["🏘️  Building Counter", "📊  Investment Analysis"]
)

# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — ROOFTOP COUNTER
# ─────────────────────────────────────────────────────────────────────────────
with tab_rooftop:
    c1, c2 = st.columns([4, 1])
    with c1:
        v1 = st.text_input("Village name", placeholder="e.g. Betsiaka, Ambilobe, Farahalana...",
                           label_visibility="collapsed", key="v1")
    with c2:
        cf1 = st.selectbox("Confidence", [0.0, 0.5, 0.6, 0.7, 0.8], index=2,
                           format_func=lambda x: "All" if x == 0.0 else f">={x:.0%}", key="cf1")
    if st.button("🔍 Count buildings", type="primary", use_container_width=True, key="btn1"):
        if v1.strip(): run_rooftop(v1.strip(), cf1)
        else: st.warning("Enter a village name.")

    rr = st.session_state["rooftop_result"]
    if rr:
        if rr["error"]: st.error(rr["error"])
        else:
            snap = rr["snapshot"]; lat = rr["lat"]; lon = rr["lon"]
            n1 = snap.get(1.0, (0, []))[0]; n2 = snap.get(2.0, (0, []))[0]; n5 = snap.get(5.0, (0, []))[0]
            st.markdown(f"""
            <div class="result-box">
              <div class="result-num">{n2:,}</div>
              <div style="font-size:.88rem;color:#555">buildings within 2 km of <b>{rr['village']}</b>
                &nbsp;·&nbsp; confidence &gt;= {rr.get('min_conf', cf1):.0%}</div>
              <div style="font-size:.70rem;color:#888">{lat:.4f}, {lon:.4f} &nbsp;·&nbsp; {rr['display_name'][:70]}</div>
            </div>
            """, unsafe_allow_html=True)
            c_1, c_2, c_5 = st.columns(3)
            c_1.metric("1 km radius", f"{n1:,}"); c_2.metric("2 km radius", f"{n2:,}"); c_5.metric("5 km radius", f"{n5:,}")
            if n2 > 0:
                ratio = n5 / n1 if n1 > 0 else 0
                if ratio < 2.5: st.markdown("🏘️ **Compact village** — simple logistics.")
                elif ratio < 6: st.markdown("🏡 **Village with hamlets** — plan multiple distribution points.")
                else: st.markdown("🌳 **Dispersed habitat** — high logistics cost.")
            st.subheader("Map (2 km radius)")
            bld2 = snap.get(2.0, (0, []))[1]
            st_folium(make_rooftop_map(lat, lon, bld2, 2.0, rr["village"]),
                      width=None, height=450, returned_objects=[])
            st.caption(f"Esri satellite · Red dots = Google Open Buildings v3 · [Google Maps](https://maps.google.com/?q={lat},{lon})")

# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — INVESTMENT ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
with tab_invest:
    ci1, ci2, ci3 = st.columns([3, 1, 1])
    with ci1:
        v2 = st.text_input("Village name", placeholder="e.g. Betsiaka, Andrafiabe...",
                           label_visibility="collapsed", key="v2")
    with ci2:
        hh = st.number_input("Households", min_value=0, value=0,
                              help="Leave 0 to auto-count from Google Open Buildings", key="hh")
    with ci3:
        cf2 = st.selectbox("GOB Confidence", [0.0, 0.5, 0.6, 0.7, 0.8], index=2,
                            format_func=lambda x: "All" if x == 0.0 else f">={x:.0%}", key="cf2")

    with st.expander("⚙️ Model parameters", expanded=False):
        cc1, cc2, cc3, cc4 = st.columns(4)
        with cc1:
            pen   = st.slider("Penetration rate", 0.10, 0.90, 0.60, 0.05, format="%.0f%%")
        with cc2:
            tarif = st.number_input("Monthly tariff (EUR)", value=1.60, step=0.10, format="%.2f")
        with cc3:
            capkwp = st.number_input("CAPEX per kWp (EUR)", value=1200, step=100)
        with cc4:
            max_pb = st.number_input("Max payback (years)", value=7, step=1)

        cc5, cc6, cc7, cc8 = st.columns(4)
        with cc5: dr   = st.slider("Discount rate", 0.05, 0.25, 0.10, 0.01, format="%.0f%%")
        with cc6: life = st.slider("Project lifetime (years)", 10, 25, 15)
        with cc7: puf  = st.slider("Productive use factor", 1.0, 2.0, 1.3, 0.1)
        with cc8: opex = st.slider("Annual OPEX (% CAPEX)", 0.02, 0.10, 0.04, 0.01, format="%.0f%%")

    # Build config from UI
    cfg = load_config(CFG_PATH)
    cfg.update({
        "penetration_rate": pen,
        "monthly_tariff_eur": tarif,
        "capex_per_kwp_eur": capkwp,
        "max_acceptable_payback_years": max_pb,
        "discount_rate": dr,
        "project_lifetime_years": life,
        "productive_use_factor": puf,
        "opex_pct_capex": opex,
    })

    if st.button("🔬 Analyze investment", type="primary", use_container_width=True, key="btn2"):
        if v2.strip(): run_investment(v2.strip(), int(hh), cf2, cfg)
        else: st.warning("Enter a village name.")

    an = st.session_state["full_analysis"]
    if an:
        if an["error"]: st.error(an["error"])
        else:
            rec   = an["recommendation"]
            score = an["score"]
            fin   = an["fin"]
            sizing = an["sizing"]
            solar = an.get("ghi")
            constraints = an["constraints"]
            revenue = an["revenue"]
            lat2 = an["lat"]; lon2 = an["lon"]

            # ── Verdict banner ────────────────────────────────────────────────
            payback_str = f"{fin['payback_years']:.1f} yrs" if fin["payback_years"] else "N/A"
            irr_str     = f"{fin['irr_pct']:.1f}%" if fin["irr_pct"] is not None else "N/A"

            st.markdown(f"""
            <div class="verdict-box" style="background:{rec['verdict_bg']};border-left:6px solid {rec['verdict_color']}">
              <div class="verdict-num" style="color:{rec['verdict_color']}">{rec['verdict']}</div>
              <div style="font-size:1rem;font-weight:600;color:{rec['verdict_color']};margin:.3rem 0">
                Score {score['total']:.0f}/100 &nbsp;·&nbsp; Payback {payback_str} &nbsp;·&nbsp; IRR {irr_str}
              </div>
            </div>
            """, unsafe_allow_html=True)

            st.markdown(rec["summary"])

            # ── KPI cards ─────────────────────────────────────────────────────
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            def kpi(col, val, lbl):
                col.markdown(f'<div class="kpi-card"><div class="kpi-val">{val}</div><div class="kpi-lbl">{lbl}</div></div>',
                             unsafe_allow_html=True)
            kpi(k1, f"{score['total']:.0f}/100",            "Priority score")
            kpi(k2, payback_str,                             "Payback")
            kpi(k3, irr_str,                                 "IRR")
            kpi(k4, f"EUR {sizing['capex_eur']:,.0f}",       "CAPEX")
            kpi(k5, f"EUR {revenue['annual_revenue_eur']:,.0f}", "Annual revenue")
            kpi(k6, f"{an['grid_dist']:.0f} km",             "Grid distance")

            st.divider()

            # ── Pros / Cons / Conditions ───────────────────────────────────────
            col_pro, col_con = st.columns(2)

            with col_pro:
                st.markdown("### ✅ Arguments for investment")
                if rec["pros"]:
                    for p in rec["pros"]:
                        st.markdown(f'<div class="pro-item">✔ {p}</div>', unsafe_allow_html=True)
                else:
                    st.info("No significant positive factors identified.")

            with col_con:
                st.markdown("### ❌ Arguments against")
                if rec["cons"]:
                    for c in rec["cons"]:
                        st.markdown(f'<div class="con-item">✘ {c}</div>', unsafe_allow_html=True)
                else:
                    st.success("No significant risk factors identified.")

            # ── Conditions / Levers ───────────────────────────────────────────
            if rec["conditions"]:
                st.markdown("### 🔧 Conditions to make this project viable")
                for cond in rec["conditions"]:
                    st.markdown(f'<div class="cond-item">◆ {cond}</div>', unsafe_allow_html=True)

            st.divider()

            # ── Score breakdown ───────────────────────────────────────────────
            st.subheader("Score breakdown")
            sc1, sc2, sc3, sc4, sc5 = st.columns(5)
            for col, lbl, pts, mx in [
                (sc1, "Financial",  score["financial"], 40),
                (sc2, "Solar",      score["solar"],     20),
                (sc3, "Grid",       score["grid"],      20),
                (sc4, "Road",       score["road"],      10),
                (sc5, "Security",   score["security"],  10),
            ]:
                pct  = pts / mx * 100
                c    = "#22C55E" if pct >= 60 else "#F59E0B" if pct >= 30 else "#EF4444"
                col.markdown(
                    f'<div class="kpi-card"><div class="kpi-val" style="color:{c}">'
                    f'{pts:.0f}<span style="font-size:.9rem;color:#9CA3AF">/{mx}</span></div>'
                    f'<div class="kpi-lbl">{lbl}</div></div>',
                    unsafe_allow_html=True,
                )

            st.divider()

            # ── Cashflow chart ────────────────────────────────────────────────
            st.subheader("Cumulative cashflow (EUR)")
            import pandas as pd
            table = fin["cashflow_table"]
            df = pd.DataFrame([{"Year": 0, "Cumulative CF (EUR)": -sizing["capex_eur"]}] +
                               [{"Year": r["year"], "Cumulative CF (EUR)": r["cumulative_cf_eur"]}
                                for r in table])
            df = df.set_index("Year")
            st.line_chart(df)
            pb = fin["payback_years"]
            if pb:
                st.caption(f"Payback reached at year {pb:.1f}. "
                           f"NPV over {cfg['project_lifetime_years']} years: EUR {fin['npv_eur']:,.0f}.")

            # ── System details ────────────────────────────────────────────────
            with st.expander("System sizing details"):
                sd1, sd2, sd3, sd4 = st.columns(4)
                sd1.metric("Daily consumption", f"{sizing['daily_consumption_kwh']:.1f} kWh")
                sd2.metric("Peak PV power",     f"{sizing['peak_power_kwp']:.1f} kWp")
                sd3.metric("Battery",           f"{sizing['battery_capacity_kwh']:.1f} kWh")
                sd4.metric("GHI",               f"{an['ghi']:.2f} kWh/m2/day")
                sa1, sa2, sa3, sa4 = st.columns(4)
                sa1.metric("Subscribers",        f"{revenue['subscribers']:.0f}")
                sa2.metric("Annual revenue",     f"EUR {revenue['annual_revenue_eur']:,.0f}")
                sa3.metric("Annual OPEX",        f"EUR {fin['annual_opex_eur']:,.0f}")
                sa4.metric("Annual net margin",  f"EUR {fin['annual_margin_eur']:,.0f}")

            # ── Constraints ───────────────────────────────────────────────────
            with st.expander("Constraint details"):
                ct1, ct2, ct3 = st.columns(3)
                ct1.metric("Grid distance",   f"{an['grid_dist']:.0f} km",   help=constraints.get("grid_source",""))
                ct2.metric("Security score",  f"{constraints['instability_score']:.1f}/10",
                           help=f"{constraints['acled_events_count']} ACLED events — {constraints['security_source']}")
                ct3.metric("Road quality",    f"{constraints['road_score']}/10",
                           help=f"{constraints['road_type']} — {constraints['road_source']}")

            # ── Location map ──────────────────────────────────────────────────
            st.subheader("Location map")
            m2 = make_investment_map(lat2, lon2, an["village"],
                                     an["grid_dist"], an["nearest_jirama"])
            st_folium(m2, width=None, height=400, returned_objects=[])
            st.caption(
                f"Blue = {an['village']} · Red = nearest JIRAMA node ({an['nearest_jirama']}, {an['grid_dist']:.0f} km) · "
                f"[Google Maps](https://maps.google.com/?q={lat2},{lon2})"
            )

# ── Footer ──────────────────────────────────────────────────────────────────────
st.divider()
st.markdown(
    "<small>WeLight Africa Solar Investment Analyzer v1.0 · "
    "Google Open Buildings v3 · NASA POWER · OSM Nominatim · ACLED</small>",
    unsafe_allow_html=True,
)
