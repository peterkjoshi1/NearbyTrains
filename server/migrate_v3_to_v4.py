#!/usr/bin/env python3
"""
migrate_v3_to_v4.py — Recalculate v3 observations to v4 distance-based weights.

v3 weight: 1/dt_before_min + 1/dt_after_min
v4 weight: v3_weight / speed_km_per_min
         = (1/dt_before_min + 1/dt_after_min) / (segment_km / total_min)

Observations where anchors are too close together (<0.1 km) keep their v3 weight
and are upgraded to v4 version without speed correction.

Run once after deploying berth_learner.py v4 changes:
    python3 server/migrate_v3_to_v4.py
"""

import math
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "berth_learned.db"


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6_371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def migrate():
    db = sqlite3.connect(str(DB_PATH))

    # Register v4 in weight_versions if not already there
    desc = ("Distance-based: time weight divided by estimated speed. "
            "Downweights fast trains (more position uncertainty per unit time). "
            "Formula: (1/dt_before_min + 1/dt_after_min) / (segment_km / total_min)")
    import time
    db.execute(
        "INSERT OR IGNORE INTO weight_versions (version, name, description, created_at) VALUES (?,?,?,?)",
        (4, "dist_anchors", desc, int(time.time()))
    )

    rows = db.execute("""
        SELECT rowid, dt_before, dt_after,
               anc_before_lat, anc_before_lon,
               anc_after_lat,  anc_after_lon
        FROM berth_observations
        WHERE weight_version = 3
          AND dt_before IS NOT NULL AND dt_after IS NOT NULL
          AND anc_before_lat IS NOT NULL AND anc_after_lat IS NOT NULL
    """).fetchall()

    print(f"Migrating {len(rows)} v3 observations to v4...")

    updated = 0
    fallback = 0
    for rowid, dt_before, dt_after, blat, blon, alat, alon in rows:
        dt_before_min = max(dt_before / 60.0, 1.0 / 60.0)
        dt_after_min  = max(dt_after  / 60.0, 1.0 / 60.0)
        time_weight   = 1.0 / dt_before_min + 1.0 / dt_after_min
        total_min     = dt_before_min + dt_after_min
        segment_km    = haversine_km(blat, blon, alat, alon)

        if segment_km > 0.1 and total_min > 0:
            speed = segment_km / total_min
            new_weight = time_weight / speed
        else:
            new_weight = time_weight
            fallback += 1

        db.execute(
            "UPDATE berth_observations SET weight=?, weight_version=4 WHERE rowid=?",
            (new_weight, rowid)
        )
        updated += 1

    db.commit()
    print(f"Done. {updated} observations upgraded to v4 ({fallback} used time fallback).")
    db.close()


if __name__ == "__main__":
    migrate()
