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
                   lat: float, lon: float) -> None:
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
            self._anchors[headcode].append((timestamp, lat, lon))
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
        frac = (timestamp - before[0]) / window
        lat  = before[1] + frac * (after[1] - before[1])
        lon  = before[2] + frac * (after[2] - before[2])
        threading.Thread(
            target=self._record,
            args=(area_id, berth_id, lat, lon, None, int(timestamp)),
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

    def observations_for(self, area_id: str, berth_id: str) -> list[dict]:
        """Return all raw observations for a berth from the DB."""
        rows = self._db.execute(
            "SELECT lat, lon, observed_at FROM berth_observations "
            "WHERE area_id=? AND berth_id=? ORDER BY observed_at",
            (area_id.upper(), berth_id.upper())
        ).fetchall()
        return [{"lat": r[0], "lon": r[1], "observed_at": r[2]} for r in rows]

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
            CREATE TABLE IF NOT EXISTS berth_observations (
                area_id     TEXT    NOT NULL,
                berth_id    TEXT    NOT NULL,
                lat         REAL    NOT NULL,
                lon         REAL    NOT NULL,
                bearing     REAL,
                observed_at INTEGER NOT NULL,
                PRIMARY KEY (area_id, berth_id, observed_at)
            )
        """)
        # Migrate existing databases that predate the bearing column
        try:
            conn.execute("ALTER TABLE berth_observations ADD COLUMN bearing REAL")
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
                lat: float, lon: float, bearing: Optional[float], ts: int) -> None:
        """Persist one observation and recompute the summary for this berth."""
        try:
            with self._lock:
                self._db.execute(
                    "INSERT OR IGNORE INTO berth_observations VALUES (?,?,?,?,?,?)",
                    (area_id, berth_id, lat, lon, bearing, ts)
                )
                self._db.commit()
                self._recompute(area_id, berth_id)
        except Exception as exc:
            print(f"[LEARN]  DB error: {exc}")

    def _recompute(self, area_id: str, berth_id: str) -> None:
        """Recompute mean coordinate and variance; update cache if converged."""
        rows = self._db.execute(
            "SELECT lat, lon FROM berth_observations "
            "WHERE area_id=? AND berth_id=?",
            (area_id, berth_id)
        ).fetchall()
        if not rows:
            return

        n       = len(rows)
        lats    = [r[0] for r in rows]
        lons    = [r[1] for r in rows]
        med_lat = statistics.median(lats)
        med_lon = statistics.median(lons)

        lat_m   = 111_320.0
        lon_m   = 111_320.0 * math.cos(math.radians(med_lat))

        # SD around the mean (kept as-is for now)
        avg_lat = sum(lats) / n
        avg_lon = sum(lons) / n
        sd_m    = math.sqrt(
            sum((r[0] - avg_lat) ** 2 * lat_m ** 2 +
                (r[1] - avg_lon) ** 2 * lon_m ** 2
                for r in rows) / n
        )

        # IQR of per-observation distances from the median position
        dists = sorted(
            math.sqrt((r[0] - med_lat) ** 2 * lat_m ** 2 +
                      (r[1] - med_lon) ** 2 * lon_m ** 2)
            for r in rows
        )
        if n >= 4:
            q1, _, q3 = statistics.quantiles(dists, n=4)
            iqr_m = q3 - q1
        else:
            iqr_m = dists[-1] - dists[0]  # fallback for n < 4: full range

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
