#!/usr/bin/env python3
"""
analyse_weights.py — Empirical analysis of observation error vs dt_before/dt_after.

For each v1 observation on a corpus berth, computes the distance from the observation
to the corpus ground-truth position. Then fits and plots error vs dt features to
find the optimal weighting function.

Usage:
    python3 analyse_weights.py

Outputs:
    - Summary statistics to stdout
    - analysis_results.json with raw data for further processing
"""

import json
import math
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict

# ── Paths ──────────────────────────────────────────────────────────────────────
DB_PATH     = Path(__file__).parent / "berth_learned.db"
SMART_PATH  = Path(__file__).parent / "smart_data.json"
CORPUS_PATH = Path(__file__).parent / "corpus_data.json"

# ── Haversine ──────────────────────────────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

COORD_CACHE_PATH = Path(__file__).parent / "coord_cache.json"

# ── Load corpus ground truth ───────────────────────────────────────────────────
def load_lookup_chain():
    """Returns {(area_id, berth_id): (lat, lon)} following the same chain as BerthLookup:
    (area, berth) → stanox  [smart_data]
    stanox → crs            [corpus_data TIPLOCDATA]
    crs → (lat, lon)        [coord_cache]
    """
    # smart: (area, berth) → stanox
    with open(SMART_PATH) as f:
        smart_raw = json.load(f)["BERTHDATA"]
    smart = {}
    for r in smart_raw:
        area   = r.get("TD", "").strip().upper()
        stanox = r.get("STANOX", "").strip()
        if not area or not stanox:
            continue
        for key_field in ("FROMBERTH", "TOBERTH"):
            berth = r.get(key_field, "").strip().upper()
            if berth:
                k = (area, berth)
                if k not in smart:
                    smart[k] = stanox

    # corpus: stanox → crs
    with open(CORPUS_PATH) as f:
        corpus_raw = json.load(f)["TIPLOCDATA"]
    stanox_to_crs = {}
    for r in corpus_raw:
        stanox = r.get("STANOX", "").strip()
        crs    = r.get("3ALPHA", "").strip()
        if stanox and crs:
            stanox_to_crs[stanox] = crs

    # coord_cache: crs → (lat, lon)
    with open(COORD_CACHE_PATH) as f:
        coord_cache = json.load(f)
    crs_to_latlon = {k: tuple(v) for k, v in coord_cache.items() if v and v[0] is not None}

    # Compose: (area, berth) → (lat, lon)
    result = {}
    for (area, berth), stanox in smart.items():
        crs = stanox_to_crs.get(stanox)
        if not crs:
            continue
        latlon = crs_to_latlon.get(crs)
        if latlon and latlon[0] is not None:
            result[(area, berth)] = latlon

    return result

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("Loading corpus/SMART/coord-cache lookup chain...")
    try:
        berth_coords = load_lookup_chain()
    except FileNotFoundError as e:
        sys.exit(f"Error: {e}")

    print(f"  Resolved {len(berth_coords):,} berths to ground-truth coordinates")

    print("Loading observations from DB...")
    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute(
        "SELECT area_id, berth_id, lat, lon, dt_before, dt_after, weight, observed_at "
        "FROM berth_observations WHERE weight_version = 1"
    ).fetchall()

    # Also grab v3 for comparison
    rows_v3 = db.execute(
        "SELECT area_id, berth_id, lat, lon, dt_before, dt_after, weight, observed_at "
        "FROM berth_observations "
        "WHERE weight_version = 3"
    ).fetchall()

    print(f"  v1 obs with dt values: {len(rows):,}")
    print(f"  v3 obs: {len(rows_v3):,}")

    # Match observations to corpus ground truth
    records = []
    skipped_no_stanox = 0
    skipped_no_corpus = 0

    for area_id, berth_id, lat, lon, dt_before, dt_after, weight, obs_at in rows:
        true_pos = berth_coords.get((area_id.upper(), berth_id.upper()))
        if not true_pos:
            skipped_no_corpus += 1
            continue
        dist_m = haversine_m(lat, lon, true_pos[0], true_pos[1])
        rec = {
            "area_id":  area_id,
            "berth_id": berth_id,
            "dist_m":   dist_m,
            "weight":   weight,
            "dt_before": dt_before,
            "dt_after":  dt_after,
        }
        if dt_before is not None and dt_after is not None:
            rec["dt_before_min"] = dt_before / 60.0
            rec["dt_after_min"]  = dt_after  / 60.0
            rec["window_min"]    = (dt_before + dt_after) / 60.0
            rec["frac"]          = dt_before / max(dt_before + dt_after, 1.0)
        records.append(rec)

    print(f"\nMatched {len(records):,} observations to corpus ground truth")
    print(f"  Skipped (no resolved coord): {skipped_no_corpus:,}")

    if not records:
        sys.exit("No matched records — check that v1 observations have dt values stored.")

    dists = [r["dist_m"] for r in records]
    print(f"\nError distance stats (metres):")
    print(f"  Mean:   {sum(dists)/len(dists):.1f}")
    print(f"  Median: {sorted(dists)[len(dists)//2]:.1f}")
    print(f"  p90:    {sorted(dists)[int(len(dists)*0.9)]:.1f}")
    print(f"  p99:    {sorted(dists)[int(len(dists)*0.99)]:.1f}")
    print(f"  Max:    {max(dists):.1f}")

    # v1 weight vs distance (do higher weights correlate with lower error?)
    print("\nMean error by v1 weight bucket (does v1 weight predict accuracy?):")
    w_buckets = defaultdict(list)
    for r in records:
        b = min(int(r["weight"] * 20) / 20, 1.0)  # 0.05-wide buckets
        w_buckets[round(b, 2)].append(r["dist_m"])
    for b in sorted(w_buckets)[:20]:
        vals = w_buckets[b]
        mean = sum(vals)/len(vals)
        bar  = "█" * min(int(mean/200), 30)
        print(f"  w={b:.2f}: n={len(vals):5d}  mean={mean:7.1f}m  {bar}")

    dt_records = [r for r in records if r.get("dt_before_min") is not None]
    print(f"\n{len(dt_records):,} records have dt values (v3+). Skipping dt analysis for v1-only data.")
    if not dt_records:
        print("Restart server and wait for v3 observations to accumulate, then re-run.")
        out_path = Path(__file__).parent / "analysis_results.json"
        with open(out_path, "w") as f:
            json.dump(records, f)
        print(f"\nRaw records saved to {out_path}")
        return

    records = dt_records

    # Bucket by dt_before_min and dt_after_min
    print("\nMean error by dt_before (minutes):")
    buckets_b = defaultdict(list)
    for r in records:
        b = min(int(r["dt_before_min"]), 30)
        buckets_b[b].append(r["dist_m"])
    for b in sorted(buckets_b):
        vals = buckets_b[b]
        mean = sum(vals)/len(vals)
        med  = sorted(vals)[len(vals)//2]
        bar  = "█" * min(int(mean/100), 40)
        print(f"  {b:3d}m: n={len(vals):5d}  mean={mean:7.1f}m  med={med:7.1f}m  {bar}")

    print("\nMean error by dt_after (minutes):")
    buckets_a = defaultdict(list)
    for r in records:
        b = min(int(r["dt_after_min"]), 30)
        buckets_a[b].append(r["dist_m"])
    for b in sorted(buckets_a):
        vals = buckets_a[b]
        mean = sum(vals)/len(vals)
        med  = sorted(vals)[len(vals)//2]
        bar  = "█" * min(int(mean/100), 40)
        print(f"  {b:3d}m: n={len(vals):5d}  mean={mean:7.1f}m  med={med:7.1f}m  {bar}")

    print("\nMean error by min(dt_before, dt_after) — nearest anchor:")
    buckets_min = defaultdict(list)
    for r in records:
        b = min(int(min(r["dt_before_min"], r["dt_after_min"])), 30)
        buckets_min[b].append(r["dist_m"])
    for b in sorted(buckets_min):
        vals = buckets_min[b]
        mean = sum(vals)/len(vals)
        med  = sorted(vals)[len(vals)//2]
        bar  = "█" * min(int(mean/100), 40)
        print(f"  {b:3d}m: n={len(vals):5d}  mean={mean:7.1f}m  med={med:7.1f}m  {bar}")

    print("\nMean error by window (dt_before + dt_after, minutes):")
    buckets_w = defaultdict(list)
    for r in records:
        b = min(int(r["window_min"]), 60)
        b = (b // 5) * 5  # 5-min buckets
        buckets_w[b].append(r["dist_m"])
    for b in sorted(buckets_w):
        vals = buckets_w[b]
        mean = sum(vals)/len(vals)
        med  = sorted(vals)[len(vals)//2]
        bar  = "█" * min(int(mean/100), 40)
        print(f"  {b:3d}m: n={len(vals):5d}  mean={mean:7.1f}m  med={med:7.1f}m  {bar}")

    # Save raw data for further analysis
    out_path = Path(__file__).parent / "analysis_results.json"
    with open(out_path, "w") as f:
        json.dump(records, f)
    print(f"\nRaw records saved to {out_path}")

if __name__ == "__main__":
    main()
