#!/usr/bin/env python3
"""
berth_learner.py — Self-populating berth coordinate database.

Records TD berth step timestamps bracketed by TRUST position fixes,
interpolates coordinates linearly, and persists them in SQLite.
Provides a coordinate lookup fallback for berths not resolved by SMART/CORPUS.

How it works:
    1. TRUST MOVEMENT events give us exact times at known locations (TIPLOCs).
       These are stored as "anchors" per headcode.
    2. When a TD berth step arrives for the same train, if two TRUST anchors
       bracket its timestamp (one before, one after), we interpolate a
       coordinate proportionally from the time elapsed.
    3. Each interpolated observation is stored in SQLite. Once a berth has
       accumulated MIN_OBSERVATIONS samples, its average coordinate is used
       as a live fallback in the position lookup chain.

Limitations:
    - Interpolation is linear (straight line between anchors). For curved
      track the estimated position drifts away from the actual line.
      Snap-to-track correction (Step 2) would fix this.
    - Assumes constant speed between anchors. Trains that stop at signals
      between two TRUST points will have biased coordinates near stations.
    - Cold start: no learned data until trains have been observed crossing
      bracketing TRUST points. Coverage builds over days/weeks of traffic.
"""

import math
import sqlite3
import statistics
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

DB_PATH            = Path(__file__).parent / "berth_learned.db"
_DEBUG_LOG_PATH    = Path(__file__).parent / "debug.log"
_debug_file        = open(_DEBUG_LOG_PATH, "a", buffering=1)

def _debug(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{ts}  {msg}", file=_debug_file)

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS-84 points."""
    R = 6_371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))

MAX_WINDOW_SECONDS  = 1800   # reject interpolation windows wider than 30 min
MIN_OBSERVATIONS    = 5      # observations needed before using a learned coord
MAX_ANCHORS         = 20     # max TRUST anchors kept per headcode in memory
ANCHOR_JUMP_KM      = 100    # geographic jump that signals headcode reuse


class BerthLearner:
    """
    Learns berth→coordinate mappings from live TRUST + TD observations.

    Thread-safe. All DB writes are dispatched to daemon threads so the
    calling STOMP thread is never blocked.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # headcode → deque of (timestamp_float, lat, lon) — recent TRUST fixes
        self._anchors: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MAX_ANCHORS)
        )
        # headcode → deque of (timestamp_float, area_id, berth_id) — recent unresolved berth steps
        self._pending: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=50)
        )
        self._db = self._init_db()
        # In-memory lookup cache: (area_id, berth_id) → (lat, lon)
        self._cache: dict[tuple, tuple] = {}
        self._load_cache()
        # Skip tracking: (area_id, berth_id) → {count, last_headcode, last_ts, reason}
        self._skips: dict[tuple, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def add_anchor(self, headcode: str, timestamp: float,
                   lat: float, lon: float, stanox: str = "") -> None:
        """Record a TRUST position fix and retrospectively bracket any pending berth steps."""
        with self._lock:
            existing = list(self._anchors[headcode])
            if existing:
                last = existing[-1]
                jump_km = _haversine_km(last[1], last[2], lat, lon)
                if jump_km > ANCHOR_JUMP_KM:
                    print(f"[LEARN]  {headcode} — anchor jump {jump_km:.0f} km "
                          f"({last[1]:.2f},{last[2]:.2f} → {lat:.2f},{lon:.2f}) "
                          f"— deque cleared, headcode reuse suspected")
                    self._anchors[headcode].clear()
                    self._pending[headcode].clear()
            self._anchors[headcode].append((timestamp, lat, lon, stanox))
            pending = list(self._pending.get(headcode, []))
            anchors = list(self._anchors[headcode])

        cutoff = timestamp - MAX_WINDOW_SECONDS
        matched = [s for s in pending if s[0] >= cutoff]
        if matched:
            _debug(f"[LEARN]  {headcode} — {len(matched)} pending steps to check")
        for step_ts, area_id, berth_id in matched:
            self._try_interpolate(headcode, area_id, berth_id, step_ts, anchors)

    def observe_berth_step(self, headcode: str, area_id: str,
                           berth_id: str, timestamp: float) -> None:
        """
        Called for every TD berth step. Stores the step as pending and attempts
        immediate interpolation if two TRUST anchors already bracket it.
        """
        with self._lock:
            self._pending[headcode].append((timestamp, area_id.upper(), berth_id.upper()))
            anchors = list(self._anchors.get(headcode, []))

        self._try_interpolate(headcode, area_id.upper(), berth_id.upper(), timestamp, anchors)

    def _try_interpolate(self, headcode: str, area_id: str, berth_id: str,
                         timestamp: float, anchors: list) -> None:
        """Attempt to interpolate a coordinate for a berth step given a list of anchors."""
        import time as _time
        def _fmt(ts): return _time.strftime("%H:%M:%S", _time.localtime(ts))
        key = (area_id, berth_id)
        if len(anchors) < 2:
            _debug(f"[INTERP] {headcode} {area_id}:{berth_id} step={_fmt(timestamp)} — only {len(anchors)} anchor(s), skipping")
            self._record_skip(key, headcode, timestamp, "few_anchors")
            return
        before = after = None
        for a in anchors:
            if a[0] <= timestamp:
                if before is None or a[0] > before[0]:
                    before = a
            else:
                if after is None or a[0] < after[0]:
                    after = a
        anchor_times = [_fmt(a[0]) for a in anchors]
        if before is None or after is None:
            _debug(f"[INTERP] {headcode} {area_id}:{berth_id} step={_fmt(timestamp)} — no bracket "
                   f"(before={'none' if before is None else _fmt(before[0])}, "
                   f"after={'none' if after is None else _fmt(after[0])}) "
                   f"anchors={anchor_times}")
            self._record_skip(key, headcode, timestamp, "no_bracket")
            return
        window = after[0] - before[0]
        if window <= 0 or window > MAX_WINDOW_SECONDS:
            self._record_skip(key, headcode, timestamp, "window_too_wide")
            return
        frac      = (timestamp - before[0]) / window
        lat       = before[1] + frac * (after[1] - before[1])
        lon       = before[2] + frac * (after[2] - before[2])
        dt_before = timestamp - before[0]
        dt_after  = after[0] - timestamp
        # Weight = 1/dt_before_min + 1/dt_after_min
        # Linear inverse of minutes to nearest TRUST anchor on each side.
        # High when berth step fires close to either anchor (position well-constrained).
        dt_before_min = max(dt_before / 60.0, 1.0 / 60.0)
        dt_after_min  = max(dt_after  / 60.0, 1.0 / 60.0)
        weight = 1.0 / dt_before_min + 1.0 / dt_after_min
        anc_b_lat, anc_b_lon = before[1], before[2]
        anc_a_lat, anc_a_lon = after[1],  after[2]
        anc_b_stanox = before[3] if len(before) > 3 else ""
        anc_a_stanox = after[3]  if len(after)  > 3 else ""
        threading.Thread(
            target=self._record,
            args=(area_id, berth_id, lat, lon, None, int(timestamp),
                  weight, 3, dt_before, dt_after,
                  anc_b_lat, anc_b_lon, anc_b_stanox,
                  anc_a_lat, anc_a_lon, anc_a_stanox),
            daemon=True,
            name="learner-write",
        ).start()

    def lookup(self, area_id: str,
               berth_id: str) -> Optional[tuple[float, float]]:
        """Return learned (lat, lon) if enough observations exist, else None."""
        return self._cache.get((area_id.upper(), berth_id.upper()))

    @property
    def learned_count(self) -> int:
        return len(self._cache)

    def debug_state(self, headcode: str = None) -> dict:
        """Return internal anchor/pending state for diagnosis via HTTP."""
        def fmt(ts):
            return time.strftime("%H:%M:%S", time.localtime(ts))
        with self._lock:
            if headcode:
                hcs = [headcode.upper()]
            else:
                # all headcodes that have anchors or pending steps
                hcs = sorted(set(self._anchors) | set(self._pending))
            out = {}
            for hc in hcs:
                anchors  = [(fmt(ts), round(lat,4), round(lon,4))
                            for ts, lat, lon in self._anchors.get(hc, [])]
                pending  = [(fmt(ts), area, berth)
                            for ts, area, berth in self._pending.get(hc, [])]
                out[hc] = {"anchors": anchors, "pending": pending}
        return out

    def _record_skip(self, key: tuple, headcode: str, ts: float, reason: str) -> None:
        entry = self._skips.get(key)
        if entry is None:
            self._skips[key] = {"count": 1, "last_headcode": headcode,
                                 "last_ts": ts, "reason": reason}
        else:
            entry["count"] += 1
            entry["last_headcode"] = headcode
            entry["last_ts"] = ts
            entry["reason"] = reason

    def recalc_weights(self) -> dict:
        """Recompute weights for all berths using IRLS (iterative reweighted least squares).

        Observations near the converged centroid get high weight; outliers get low weight.
        Runs for every berth that has any observation with weight=1.0 (legacy/uncomputed).
        Returns stats.
        """
        rows = self._db.execute(
            "SELECT DISTINCT area_id, berth_id FROM berth_observations WHERE weight = 1.0"
        ).fetchall()

        updated = 0
        for area_id, berth_id in rows:
            obs = self._db.execute(
                "SELECT rowid, lat, lon FROM berth_observations WHERE area_id=? AND berth_id=?",
                (area_id, berth_id)
            ).fetchall()
            if len(obs) < 2:
                continue
            lats = [r[1] for r in obs]
            lons = [r[2] for r in obs]
            weights = self._irls_weights(lats, lons)
            with self._lock:
                for i, row in enumerate(obs):
                    self._db.execute(
                        "UPDATE berth_observations SET weight=? WHERE rowid=?",
                        (weights[i], row[0])
                    )
                self._db.commit()
                self._recompute(area_id, berth_id)
            updated += 1

        print(f"[LEARN]  recalc_weights: updated {updated} / {len(rows)} berths")
        return {"berths_updated": updated, "total_berths": len(rows)}

    def _irls_weights(self, lats: list[float], lons: list[float],
                      iterations: int = 5, epsilon_m: float = 50.0) -> list[float]:
        """Return IRLS weights: high near centroid, low for outliers. Normalised to max=1."""
        lat_m = 111_320.0
        c_lat = sum(lats) / len(lats)
        c_lon = sum(lons) / len(lons)
        weights = [1.0] * len(lats)
        for _ in range(iterations):
            lon_m = 111_320.0 * math.cos(math.radians(c_lat))
            dists = [
                math.sqrt((lats[i] - c_lat) ** 2 * lat_m ** 2 +
                          (lons[i] - c_lon) ** 2 * lon_m ** 2)
                for i in range(len(lats))
            ]
            weights = [1.0 / (d ** 2 + epsilon_m) for d in dists]
            total_w = sum(weights)
            c_lat = sum(lats[i] * weights[i] for i in range(len(lats))) / total_w
            c_lon = sum(lons[i] * weights[i] for i in range(len(lons))) / total_w
        max_w = max(weights)
        return [w / max_w for w in weights]

    def observations_for(self, area_id: str, berth_id: str) -> list[dict]:
        """Return all raw observations for a berth from the DB."""
        rows = self._db.execute(
            "SELECT lat, lon, observed_at, weight, weight_version, dt_before, dt_after, "
            "anc_before_lat, anc_before_lon, anc_before_stanox, "
            "anc_after_lat, anc_after_lon, anc_after_stanox "
            "FROM berth_observations "
            "WHERE area_id=? AND berth_id=? ORDER BY observed_at",
            (area_id.upper(), berth_id.upper())
        ).fetchall()
        return [{"lat": r[0], "lon": r[1], "observed_at": r[2], "weight": r[3],
                 "weight_version": r[4], "dt_before": r[5], "dt_after": r[6],
                 "anc_before_lat": r[7], "anc_before_lon": r[8], "anc_before_stanox": r[9],
                 "anc_after_lat": r[10], "anc_after_lon": r[11], "anc_after_stanox": r[12]}
                for r in rows]

    def weight_versions(self) -> list[dict]:
        """Return all weight version definitions."""
        rows = self._db.execute(
            "SELECT version, name, description FROM weight_versions ORDER BY version"
        ).fetchall()
        return [{"version": r[0], "name": r[1], "description": r[2]} for r in rows]

    def skip_list(self, min_count: int = 3) -> list[dict]:
        """Return berths with repeated skip count >= min_count, sorted by count desc."""
        result = []
        for (area_id, berth_id), entry in self._skips.items():
            if entry["count"] >= min_count:
                result.append({
                    "area":          area_id,
                    "berth":         berth_id,
                    "count":         entry["count"],
                    "last_headcode": entry["last_headcode"],
                    "last_ts":       entry["last_ts"],
                    "reason":        entry["reason"],
                    "in_cache":      (area_id, berth_id) in self._cache,
                })
        return sorted(result, key=lambda x: x["count"], reverse=True)

    # ── Database ──────────────────────────────────────────────────────────────

    def _init_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weight_versions (
                version     INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL,
                description TEXT,
                created_at  INTEGER NOT NULL
            )
        """)
        conn.executemany(
            "INSERT OR IGNORE INTO weight_versions VALUES (?,?,?,?)",
            [
                (1, "frac_window",
                 "Confidence peaks at midpoint between anchors, divided by window duration. "
                 "Formula: (1 - 2*|frac - 0.5|) / window_seconds",
                 0),
                (2, "dt_anchors_sq",
                 "Inverse squared seconds to nearest TRUST anchor on each side. "
                 "Formula: 1/dt_before^2 + 1/dt_after^2",
                 0),
                (3, "dt_anchors_min",
                 "Inverse minutes to nearest TRUST anchor on each side (linear, not squared). "
                 "Formula: 1/dt_before_min + 1/dt_after_min",
                 0),
            ]
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS berth_observations (
                area_id         TEXT    NOT NULL,
                berth_id        TEXT    NOT NULL,
                lat             REAL    NOT NULL,
                lon             REAL    NOT NULL,
                bearing         REAL,
                observed_at     INTEGER NOT NULL,
                weight          REAL    NOT NULL DEFAULT 1.0,
                weight_version  INTEGER NOT NULL DEFAULT 1,
                dt_before       REAL,
                dt_after        REAL,
                anc_before_lat  REAL,
                anc_before_lon  REAL,
                anc_before_stanox TEXT,
                anc_after_lat   REAL,
                anc_after_lon   REAL,
                anc_after_stanox TEXT,
                PRIMARY KEY (area_id, berth_id, observed_at)
            )
        """)
        # Migrate existing databases
        for stmt in (
            "ALTER TABLE berth_observations ADD COLUMN bearing REAL",
            "ALTER TABLE berth_observations ADD COLUMN weight REAL NOT NULL DEFAULT 1.0",
            "ALTER TABLE berth_observations ADD COLUMN weight_version INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE berth_observations ADD COLUMN dt_before REAL",
            "ALTER TABLE berth_observations ADD COLUMN dt_after REAL",
            "ALTER TABLE berth_observations ADD COLUMN anc_before_lat REAL",
            "ALTER TABLE berth_observations ADD COLUMN anc_before_lon REAL",
            "ALTER TABLE berth_observations ADD COLUMN anc_before_stanox TEXT",
            "ALTER TABLE berth_observations ADD COLUMN anc_after_lat REAL",
            "ALTER TABLE berth_observations ADD COLUMN anc_after_lon REAL",
            "ALTER TABLE berth_observations ADD COLUMN anc_after_stanox TEXT",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS berth_coords (
                area_id    TEXT    NOT NULL,
                berth_id   TEXT    NOT NULL,
                lat        REAL    NOT NULL,
                lon        REAL    NOT NULL,
                obs_count  INTEGER NOT NULL DEFAULT 0,
                sd_m       REAL    NOT NULL DEFAULT 0,
                iqr_m      REAL    NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (area_id, berth_id)
            )
        """)
        # Migrate existing databases
        for stmt in (
            "ALTER TABLE berth_coords RENAME COLUMN variance TO sd_m",
            "ALTER TABLE berth_coords ADD COLUMN iqr_m REAL NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass
        conn.commit()
        return conn

    def _load_cache(self) -> None:
        """Load converged learned coords into memory on startup."""
        rows = self._db.execute(
            "SELECT area_id, berth_id, lat, lon FROM berth_coords "
            "WHERE obs_count >= ?",
            (MIN_OBSERVATIONS,)
        ).fetchall()
        self._cache = {(r[0], r[1]): (r[2], r[3]) for r in rows}
        print(f"[LEARN]  Loaded {len(self._cache):,} learned berth coordinates")

    def _record(self, area_id: str, berth_id: str,
                lat: float, lon: float, bearing: Optional[float], ts: int,
                weight: float = 1.0, weight_version: int = 1,
                dt_before: Optional[float] = None, dt_after: Optional[float] = None,
                anc_before_lat: Optional[float] = None, anc_before_lon: Optional[float] = None,
                anc_before_stanox: Optional[str] = None,
                anc_after_lat: Optional[float] = None, anc_after_lon: Optional[float] = None,
                anc_after_stanox: Optional[str] = None) -> None:
        """Persist one observation and recompute the summary for this berth."""
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO berth_observations "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (area_id, berth_id, lat, lon, bearing, ts,
                     weight, weight_version, dt_before, dt_after,
                     anc_before_lat, anc_before_lon, anc_before_stanox,
                     anc_after_lat, anc_after_lon, anc_after_stanox)
                )
                self._db.commit()
                self._recompute(area_id, berth_id)
        except Exception as exc:
            print(f"[LEARN]  DB error: {exc}")

    # β multiplier: v2 observations count this many times more than v1 in the blend
    _V2_BETA = 5

    def _recompute(self, area_id: str, berth_id: str) -> None:
        """Recompute position as a blend of v1 and v2 centroids weighted by n, with v2 scaled by β.
        Each version's centroid uses its own weighted mean. Once enough v2 data accumulates,
        v1 becomes negligible. Falls back to v1-only if no v2 observations exist."""
        v1_rows = self._db.execute(
            "SELECT lat, lon, weight FROM berth_observations "
            "WHERE area_id=? AND berth_id=? AND weight_version=1",
            (area_id, berth_id)
        ).fetchall()
        v3_rows = self._db.execute(
            "SELECT lat, lon, weight FROM berth_observations "
            "WHERE area_id=? AND berth_id=? AND weight_version=3",
            (area_id, berth_id)
        ).fetchall()

        if not v1_rows and not v3_rows:
            return

        def weighted_centroid(rows):
            total_w = sum(r[2] for r in rows)
            if total_w > 0:
                return (sum(r[0] * r[2] for r in rows) / total_w,
                        sum(r[1] * r[2] for r in rows) / total_w)
            n = len(rows)
            return (sum(r[0] for r in rows) / n, sum(r[1] for r in rows) / n)

        n1 = len(v1_rows)
        n3 = len(v3_rows)
        effective_n3 = n3 * self._V2_BETA

        if n3 == 0:
            avg_lat, avg_lon = weighted_centroid(v1_rows)
        elif n1 == 0:
            avg_lat, avg_lon = weighted_centroid(v3_rows)
        else:
            c1_lat, c1_lon = weighted_centroid(v1_rows)
            c3_lat, c3_lon = weighted_centroid(v3_rows)
            total = n1 + effective_n3
            avg_lat = (n1 * c1_lat + effective_n3 * c3_lat) / total
            avg_lon = (n1 * c1_lon + effective_n3 * c3_lon) / total

        n = n1 + n3
        rows = v1_rows + v3_rows

        # Use weighted mean as the canonical coordinate
        med_lat, med_lon = avg_lat, avg_lon

        lat_m = 111_320.0
        lon_m = 111_320.0 * math.cos(math.radians(med_lat))

        sd_m = math.sqrt(
            sum((r[0] - avg_lat) ** 2 * lat_m ** 2 +
                (r[1] - avg_lon) ** 2 * lon_m ** 2
                for r in rows) / n
        )

        # IQR of per-observation distances from the mean position
        dists = sorted(
            math.sqrt((r[0] - med_lat) ** 2 * lat_m ** 2 +
                      (r[1] - med_lon) ** 2 * lon_m ** 2)
            for r in rows
        )
        if n >= 4:
            q1, _, q3 = statistics.quantiles(dists, n=4)
            iqr_m = q3 - q1
        else:
            iqr_m = dists[-1] - dists[0]

        self._db.execute(
            "INSERT OR REPLACE INTO berth_coords VALUES (?,?,?,?,?,?,?,?)",
            (area_id, berth_id, med_lat, med_lon, n, sd_m, iqr_m, int(time.time()))
        )
        self._db.commit()

        if n >= MIN_OBSERVATIONS:
            ratio = iqr_m / sd_m if sd_m > 0 else 0
            if ratio > 1.0:
                label = "poisoned" if sd_m > 50_000 else "bimodal"
                # Keep last valid position in cache rather than evicting —
                # an approximate location is better than none.
                prev = self._cache.get((area_id, berth_id))
                kept = f" — keeping ({prev[0]:.4f},{prev[1]:.4f})" if prev else " — no prior position"
                print(f"[LEARN]  {area_id}:{berth_id} — suppressed  "
                      f"n={n}  sd={sd_m:.0f}m  iqr={iqr_m:.0f}m  "
                      f"(ratio={ratio:.2f} — {label}){kept}")
            else:
                self._cache[(area_id, berth_id)] = (med_lat, med_lon)
                print(f"[LEARN]  {area_id}:{berth_id} → "
                      f"({med_lat:.4f}, {med_lon:.4f})  "
                      f"n={n}  sd={sd_m:.0f}m  iqr={iqr_m:.0f}m")
