#!/usr/bin/env python3
"""
berth_analysis.py — Detailed analysis of a learned berth.

Usage:
    python3 berth_analysis.py G2:5901
    python3 berth_analysis.py EA P618
"""

import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from berth_lookup import BerthLookup
import snap_to_rail

DB_PATH = Path(__file__).parent / "berth_learned.db"

# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def bar(value, max_value, width=30):
    filled = int(round(value / max_value * width)) if max_value > 0 else 0
    return "█" * filled + "░" * (width - filled)

def fmt_stanox(stanox, lookup):
    if not stanox:
        return "—"
    r = lookup.lookup_by_stanox(stanox)
    name = _stanox_name(stanox, lookup)
    if r:
        return f"{stanox} ({name}, {r[0]:.4f},{r[1]:.4f})"
    return f"{stanox} ({name}, no coords)"

def _stanox_name(stanox, lookup):
    if not stanox:
        return "?"
    entry = lookup._smart_index.get(("", ""))  # unused
    # Search smart index for this stanox
    for (_, _), (s, name) in lookup._smart_index.items():
        if s == stanox and name:
            return name
    return "?"

# ── Main ──────────────────────────────────────────────────────────────────────

def analyse(area_id: str, berth_id: str):
    area_id  = area_id.upper().strip()
    berth_id = berth_id.upper().strip()

    print(f"\n{'═'*60}")
    print(f"  Berth analysis: {area_id}:{berth_id}")
    print(f"{'═'*60}")

    # ── Load data ─────────────────────────────────────────────────────────────
    db = sqlite3.connect(str(DB_PATH))
    lookup = BerthLookup()
    snap_to_rail.load()

    # ── Corpus lookup ─────────────────────────────────────────────────────────
    corpus = lookup.lookup(area_id, berth_id)
    print(f"\n{'─'*60}")
    print("  CORPUS")
    print(f"{'─'*60}")
    if corpus and corpus.lat is not None:
        snap_lat, snap_lon, snap_dist = snap_to_rail.snap(corpus.lat, corpus.lon)
        print(f"  Station:  {corpus.name}  (CRS={corpus.crs}, STANOX={corpus.stanox})")
        print(f"  Position: {corpus.lat:.5f}, {corpus.lon:.5f}")
        print(f"  Snap:     {snap_dist*1000:.0f}m → {snap_lat:.5f}, {snap_lon:.5f}")
    elif corpus:
        print(f"  Station:  {corpus.name}  (CRS={corpus.crs}, STANOX={corpus.stanox})")
        print("  Position: no coordinates resolved")
    else:
        print("  Not in corpus — learned berth only")

    # ── Learned centroid ──────────────────────────────────────────────────────
    coord_row = db.execute(
        "SELECT lat, lon, obs_count, sd_m, iqr_m FROM berth_coords WHERE area_id=? AND berth_id=?",
        (area_id, berth_id)
    ).fetchone()

    print(f"\n{'─'*60}")
    print("  LEARNED CENTROID")
    print(f"{'─'*60}")
    if coord_row:
        c_lat, c_lon, obs_count, sd_m, _ = coord_row
        snap_lat, snap_lon, snap_dist = snap_to_rail.snap(c_lat, c_lon)
        maps_url = f"https://maps.google.com/?q={c_lat:.5f},{c_lon:.5f}"

        # Compute wSD, n_eff, IQR from raw observations
        all_obs = db.execute(
            "SELECT lat, lon, weight FROM berth_observations WHERE area_id=? AND berth_id=? AND weight_version=3",
            (area_id, berth_id)
        ).fetchall()
        lat_m2 = 111_320.0
        lon_m2 = 111_320.0 * math.cos(math.radians(c_lat))
        dists_sq = [(r[0]-c_lat)**2*lat_m2**2 + (r[1]-c_lon)**2*lon_m2**2 for r in all_obs]
        dists = sorted(math.sqrt(d) for d in dists_sq)
        total_w2 = sum(r[2] for r in all_obs)
        wsd_m = math.sqrt(sum(r[2]*d for r, d in zip(all_obs, dists_sq)) / total_w2) if total_w2 > 0 else sd_m
        sum_w2 = sum(r[2]**2 for r in all_obs)
        n_eff = (total_w2**2 / sum_w2) if sum_w2 > 0 else len(all_obs)
        n_obs = len(dists)
        if n_obs >= 4:
            q1, _, q3 = statistics.quantiles(dists, n=4)
            iqr_m = q3 - q1
        elif n_obs >= 2:
            iqr_m = dists[-1] - dists[0]
        else:
            iqr_m = 0
        ratio = iqr_m / sd_m if sd_m > 0 else 0
        verdict = "bimodal" if ratio > 1.0 else "unimodal"

        print(f"  Position:  {c_lat:.5f}, {c_lon:.5f}")
        print(f"  Obs:       n={obs_count}  n_eff={n_eff:.1f}")
        print(f"  SD:        {sd_m:.0f}m   wSD: {wsd_m:.0f}m   IQR: {iqr_m:.0f}m   ratio={ratio:.2f} → {verdict}")
        print(f"  Snap:      {snap_dist*1000:.0f}m → {snap_lat:.5f}, {snap_lon:.5f}")
        print(f"  Maps:      {maps_url}")
    else:
        print("  No centroid — berth not yet learned")
        c_lat = c_lon = sd_m = None

    # ── Observations ──────────────────────────────────────────────────────────
    rows = db.execute(
        "SELECT lat, lon, weight, dt_before, dt_after, "
        "       anc_before_stanox, anc_after_stanox, observed_at "
        "FROM berth_observations "
        "WHERE area_id=? AND berth_id=? AND weight_version=3 "
        "ORDER BY weight DESC",
        (area_id, berth_id)
    ).fetchall()

    if not rows:
        print("\n  No v3 observations.")
        return

    weights = [r[2] for r in rows]
    max_w   = max(weights)

    # ── Anchor pair breakdown ─────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  ANCHOR PAIRS")
    print(f"{'─'*60}")
    by_anchor = defaultdict(list)
    for r in rows:
        key = (r[5] or "?", r[6] or "?")
        by_anchor[key].append(r)
    for (anc_b, anc_a), group in sorted(by_anchor.items(), key=lambda x: -len(x[1])):
        n     = len(group)
        avg_w = sum(r[2] for r in group) / n
        avg_b = sum(r[3] for r in group) / n / 60 if group[0][3] else 0
        avg_a = sum(r[4] for r in group) / n / 60 if group[0][4] else 0
        nb    = _stanox_name(anc_b, lookup)
        na    = _stanox_name(anc_a, lookup)
        print(f"  {anc_b} ({nb}) → {anc_a} ({na})")
        print(f"    n={n}  avg_weight={avg_w:.4f}  avg_dt_before={avg_b:.1f}m  avg_dt_after={avg_a:.1f}m")

    # ── Weight distribution histogram ─────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  WEIGHT DISTRIBUTION  (higher = closer to anchor = more reliable)")
    print(f"{'─'*60}")
    buckets = defaultdict(int)
    bucket_size = max_w / 10 if max_w > 0 else 1
    for w in weights:
        b = min(int(w / bucket_size), 9)
        buckets[b] += 1
    for b in range(10):
        lo = b * bucket_size
        hi = (b+1) * bucket_size
        n  = buckets[b]
        print(f"  {lo:6.3f}–{hi:6.3f}  {bar(n, len(rows), 25)} {n}")

    # ── Top observations by weight ────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("  TOP 20 OBSERVATIONS (by weight)")
    print(f"{'─'*60}")
    print(f"  {'#':>3}  {'lat':>9} {'lon':>9}  {'weight':>8}  {'dtB':>6} {'dtA':>6}  {'dist_m':>7}  anc_before → anc_after")
    for i, (lat, lon, w, dtb, dta, anc_b, anc_a, obs_at) in enumerate(rows[:20]):
        dist = haversine_m(lat, lon, c_lat, c_lon) if c_lat else 0
        dtb_m = dtb/60 if dtb else 0
        dta_m = dta/60 if dta else 0
        flag = " ◀ outlier" if c_lat and dist > 2 * sd_m else ""
        print(f"  {i+1:>3}  {lat:>9.5f} {lon:>9.5f}  {w:>8.4f}  {dtb_m:>5.1f}m {dta_m:>5.1f}m  {dist:>7.0f}m  "
              f"{anc_b or '?'} → {anc_a or '?'}{flag}")

    # ── Outliers ──────────────────────────────────────────────────────────────
    if c_lat and sd_m:
        outliers = [(r, haversine_m(r[0], r[1], c_lat, c_lon))
                    for r in rows if haversine_m(r[0], r[1], c_lat, c_lon) > 2 * sd_m]
        if outliers:
            print(f"\n{'─'*60}")
            print(f"  OUTLIERS (>{2*sd_m:.0f}m from centroid, n={len(outliers)})")
            print(f"{'─'*60}")
            for r, dist in sorted(outliers, key=lambda x: -x[1]):
                nb = _stanox_name(r[5], lookup)
                na = _stanox_name(r[6], lookup)
                print(f"  {dist:7.0f}m  w={r[2]:.4f}  {r[0]:.5f},{r[1]:.5f}  "
                      f"{r[5] or '?'} ({nb}) → {r[6] or '?'} ({na})")

    # ── XY scatter plot ───────────────────────────────────────────────────────
    if rows and c_lat:
        fig, ax = plt.subplots(figsize=(8, 7))

        lons = [r[1] for r in rows]
        lats = [r[0] for r in rows]
        ws   = np.array([r[2] for r in rows])
        log_ws = np.log(ws + 0.001)
        norm_ws = (log_ws - log_ws.min()) / (log_ws.max() - log_ws.min() + 1e-9)

        # Colour by anchor pair
        anchor_keys = list(by_anchor.keys())
        cmap = plt.cm.get_cmap("tab10", len(anchor_keys))
        for idx, (key, group) in enumerate(sorted(by_anchor.items(), key=lambda x: -len(x[1]))):
            glons = [r[1] for r in group]
            glats = [r[0] for r in group]
            gws   = np.array([r[2] for r in group])
            glog  = np.log(gws + 0.001)
            gnorm = (glog - log_ws.min()) / (log_ws.max() - log_ws.min() + 1e-9)
            nb = _stanox_name(key[0], lookup)
            na = _stanox_name(key[1], lookup)
            label = f"{key[0]}({nb})→{key[1]}({na})  n={len(group)}"
            sc = ax.scatter(glons, glats, c=[cmap(idx)]*len(glons),
                            s=20 + gnorm*80, alpha=0.7, label=label, zorder=3)

        # Centroid
        ax.scatter([c_lon], [c_lat], c="red", s=120, marker="*",
                   zorder=5, label=f"centroid ({c_lat:.5f},{c_lon:.5f})")

        # Snapped position
        snap_lat2, snap_lon2, snap_dist2 = snap_to_rail.snap(c_lat, c_lon)
        if snap_dist2 < snap_to_rail.MAX_SNAP_KM:
            ax.scatter([snap_lon2], [snap_lat2], c="green", s=120, marker="^",
                       zorder=5, label=f"snapped ({snap_dist2*1000:.0f}m)")

        # SD circle (approximate)
        lat_m = 111_320.0
        lon_m = 111_320.0 * math.cos(math.radians(c_lat))
        theta = np.linspace(0, 2*math.pi, 100)
        ax.plot(c_lon + sd_m/lon_m * np.cos(theta),
                c_lat + sd_m/lat_m * np.sin(theta),
                "r--", alpha=0.4, linewidth=1, label=f"1SD ({sd_m:.0f}m)")

        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.set_title(f"{area_id}:{berth_id}  —  {len(rows)} v3 obs  (size∝log weight, colour=anchor pair)")
        ax.legend(fontsize=7, loc="best")
        ax.grid(True, alpha=0.3)

        out = f"/tmp/berth_{area_id}_{berth_id}.png"
        plt.tight_layout()
        plt.savefig(out, dpi=150, bbox_inches="tight")
        plt.close()
        print(f"  Plot saved to: {out}")

    print(f"\n{'═'*60}\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 1 and ":" in args[0]:
        area, berth = args[0].split(":", 1)
    elif len(args) == 2:
        area, berth = args
    else:
        sys.exit("Usage: python3 berth_analysis.py G2:5901  OR  python3 berth_analysis.py G2 5901")
    analyse(area, berth)
