#!/usr/bin/env python3
"""
train_tracker.py — Real-time Scotland train position tracker.

Connects to the Network Rail TD STOMP feed, filters to Scottish TD areas only,
resolves berth IDs to coordinates via SMART/CORPUS data, and serves a REST API
for querying nearby trains.

Credentials:
    Loaded from ~/Library/Application Support/NearbyTrains/credentials (see server/credentials.example).
    Falls back to NR_USERNAME / NR_PASSWORD environment variables if the file is absent.

Usage:
    python3 train_tracker.py

API:
    GET /trains?lat=57.06&lon=-4.12&radius=50
    GET /trains/stats
"""

import collections
import json
import math
import os
import pathlib
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Logging ───────────────────────────────────────────────────────────────────
# High-level events go to stdout (redirect to server.log).
# Per-message noise goes to debug.log in the server directory.

_DEBUG_LOG_PATH = pathlib.Path(__file__).parent / "debug.log"
_debug_file = open(_DEBUG_LOG_PATH, "a", buffering=1)  # line-buffered

def _debug(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{ts}  {msg}", file=_debug_file)


def _load_credentials():
    """Load NR_USERNAME / NR_PASSWORD from ~/Library/Application Support/NearbyTrains/credentials
    if not already set in the environment."""
    creds_path = pathlib.Path.home() / "Library" / "Application Support" / "NearbyTrains" / "credentials"
    try:
        with open(creds_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass  # fall back to environment variables already set


_load_credentials()

import stomp

from berth_lookup import BerthLookup
from berth_learner import BerthLearner
import snap_to_rail

# ── Config ──────────────────────────────────────────────────────────────────

STOMP_HOST    = "publicdatafeeds.networkrail.co.uk"
STOMP_PORT    = 61618
TD_TOPICS     = [
    "/topic/TD_SE_SIG_AREA",   # Scotland East
    "/topic/TD_SW_SIG_AREA",   # Scotland West
]
TRUST_TOPICS  = ["/topic/TRAIN_MVT_ALL_TOC"]
HTTP_PORT     = 8080
STALE_SECONDS = 3600   # drop trains not updated for 1 hour
LOOKUP_WORKERS = 8     # thread pool size for berth→coord lookups
RECONNECT_DELAY = 60   # seconds to wait before reconnecting

# ── Shared state ─────────────────────────────────────────────────────────────
#
# positions: headcode → {lat, lon, area_id, berth, timestamp}
#
positions: dict[str, dict] = {}
_pos_lock  = threading.Lock()

stats = {"stomp_messages": 0, "ca_msgs": 0, "position_updates": 0, "trust_anchors": 0,
         "snap_hits": 0}
_stats_lock = threading.Lock()

# Rolling log of significant snap corrections (>150 m), newest first
_snap_log: collections.deque = collections.deque(maxlen=200)
_snap_log_lock = threading.Lock()

_last_message_time = time.time()   # updated on every STOMP message received
WATCHDOG_TIMEOUT = 120             # reconnect if silent for this many seconds
_learner: "BerthLearner | None" = None  # set in main()


def _inc(key: str, n: int = 1):
    with _stats_lock:
        stats[key] += n


# ── Position store ────────────────────────────────────────────────────────────

def set_position(headcode: str, area_id: str, berth: str,
                 lat: float | None, lon: float | None,
                 source: str = "unknown") -> None:
    with _pos_lock:
        prev = positions.get(headcode)
        now = time.time()
        # Compute bearing and speed from previous position
        brg = None
        speed_kmh = None
        if lat is not None and prev and prev["lat"] is not None:
            if (prev["lat"], prev["lon"]) != (lat, lon):
                brg = bearing_degrees(prev["lat"], prev["lon"], lat, lon)
                dt = now - prev["timestamp"]
                if 30 < dt < 7200:   # 30 s – 2 h gap is plausible
                    dist = haversine_km(prev["lat"], prev["lon"], lat, lon)
                    if dist >= 0.2:  # at least 200 m of movement
                        spd = dist / (dt / 3600)
                        if spd <= 200:   # sanity cap
                            speed_kmh = round(spd, 1)
            else:
                brg = prev.get("bearing")
                speed_kmh = prev.get("speed_kmh")
        elif prev:
            brg = prev.get("bearing")
            speed_kmh = prev.get("speed_kmh")
        if lat is not None:
            raw_lat, raw_lon = lat, lon
            snapped_lat, snapped_lon, snap_dist = snap_to_rail.snap(lat, lon)
            if snap_dist < snap_to_rail.MAX_SNAP_KM:  # inf when no snap needed/possible
                _debug(f"[SNAP]   {headcode} {area_id}:{berth} snapped {snap_dist*1000:.0f}m onto rail")
                lat, lon = snapped_lat, snapped_lon
                _inc("snap_hits")
                with _snap_log_lock:
                    _snap_log.appendleft({
                        "headcode":    headcode,
                        "area":        area_id,
                        "berth":       berth,
                        "source":      source,
                        "raw_lat":     raw_lat,
                        "raw_lon":     raw_lon,
                        "snapped_lat": lat,
                        "snapped_lon": lon,
                        "distance_m":  round(snap_dist * 1000),
                        "timestamp":   now,
                    })
        positions[headcode] = {
            "lat": lat,
            "lon": lon,
            "area_id": area_id,
            "berth": berth,
            "bearing": brg,
            "speed_kmh": speed_kmh,
            "timestamp": now,
        }
    if lat is not None:
        _inc("position_updates")


def prune_stale() -> int:
    cutoff = time.time() - STALE_SECONDS
    with _pos_lock:
        stale = [k for k, v in positions.items() if v["timestamp"] < cutoff]
        for k in stale:
            del positions[k]
    return len(stale)


# ── Geo ───────────────────────────────────────────────────────────────────────

def bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from (lat1,lon1) to (lat2,lon2), 0 = north, clockwise."""
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def query_radius(lat: float, lon: float, radius_km: float) -> list[dict]:
    now = time.time()
    result = []
    with _pos_lock:
        for headcode, pos in positions.items():
            if pos["lat"] is None or pos["lon"] is None:
                continue
            dist = haversine_km(lat, lon, pos["lat"], pos["lon"])
            if dist <= radius_km:
                result.append({
                    "headcode":    headcode,
                    "lat":         pos["lat"],
                    "lon":         pos["lon"],
                    "area_id":     pos["area_id"],
                    "berth":       pos["berth"],
                    "bearing":     pos.get("bearing"),
                    "speed_kmh":   pos.get("speed_kmh"),
                    "age_seconds": int(now - pos["timestamp"]),
                    "distance_km": round(dist, 3),
                })
    result.sort(key=lambda t: t["distance_km"])
    return result


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/trains":
            self._trains(parsed.query)
        elif parsed.path == "/trains/stats":
            self._train_stats()
        elif parsed.path == "/trains/snap_log":
            self._snap_log()
        elif parsed.path == "/trains/skip_log":
            self._skip_log()
        elif parsed.path == "/trains/berth_observations":
            self._berth_observations(parsed.query)
        elif parsed.path == "/trains/debug":
            self._train_debug(parsed.query)
        else:
            self.send_error(404)

    def _trains(self, qs: str):
        params = urllib.parse.parse_qs(qs)
        try:
            lat    = float(params["lat"][0])
            lon    = float(params["lon"][0])
            radius = float(params.get("radius", ["50"])[0])
        except (KeyError, ValueError, IndexError):
            self.send_error(400, "Required params: lat, lon  Optional: radius (km, default 50)")
            return
        self._json(query_radius(lat, lon, radius))

    def _train_stats(self):
        with _pos_lock:
            total      = len(positions)
            with_coords = sum(1 for p in positions.values() if p["lat"] is not None)
        with _stats_lock:
            s = dict(stats)
        self._json({
            **s,
            "tracked_trains":     total,
            "trains_with_coords": with_coords,
            "learned_berths":     _learner.learned_count if _learner else 0,
            "rail_segments":      len(snap_to_rail._polylines),
        })

    def _snap_log(self):
        with _snap_log_lock:
            self._json(list(_snap_log))

    def _skip_log(self):
        self._json(_learner.skip_list() if _learner else [])

    def _berth_observations(self, qs: str):
        params = urllib.parse.parse_qs(qs)
        area  = params.get("area",  [None])[0]
        berth = params.get("berth", [None])[0]
        if not area or not berth or not _learner:
            self.send_error(400, "Required params: area, berth")
            return
        self._json(_learner.observations_for(area, berth))

    def _train_debug(self, qs: str):
        params = urllib.parse.parse_qs(qs)
        headcode = params.get("hc", [None])[0]
        if not _learner:
            self._json({"error": "learner not ready"})
            return
        self._json(_learner.debug_state(headcode))

    def _json(self, obj):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass   # suppress per-request access logs


# ── Combined TD + TRUST listener ─────────────────────────────────────────────

class CombinedListener(stomp.ConnectionListener):
    """Single STOMP connection handling both TD and TRUST topics."""

    def __init__(self, conn, lookup: BerthLookup, learner: BerthLearner,
                 executor: ThreadPoolExecutor, username: str, password: str):
        self.conn          = conn
        self.lookup        = lookup
        self.learner       = learner
        self.executor      = executor
        self.username      = username
        self.password      = password
        self._reconnecting = False

    def on_connected(self, frame):
        all_topics = TD_TOPICS + TRUST_TOPICS
        print(f"[STOMP] Connected — subscribing to {len(all_topics)} topics")
        for sub_id, topic in enumerate(all_topics, start=1):
            self.conn.subscribe(destination=topic, id=sub_id, ack="auto")
            print(f"[STOMP]   {topic}")

    def on_message(self, frame):
        global _last_message_time
        _last_message_time = time.time()
        _inc("stomp_messages")
        try:
            batch = json.loads(frame.body)
        except (json.JSONDecodeError, TypeError):
            return
        for item in batch:
            if "CA_MSG" in item:
                self._handle_td(item["CA_MSG"])
            elif "header" in item:
                self._handle_trust(item)

    def _handle_td(self, ca: dict):
        headcode = ca.get("descr", "").strip()
        area_id  = ca.get("area_id", "").strip().upper()
        to_berth = ca.get("to", "").strip()
        if not (headcode and area_id and to_berth):
            return
        _inc("ca_msgs")
        try:
            ts = int(ca.get("time", 0)) / 1000.0 or time.time()
        except (ValueError, TypeError):
            ts = time.time()
        _debug(f"[CA_MSG] {headcode}  area={area_id}  berth={to_berth}")
        self.learner.observe_berth_step(headcode, area_id, to_berth, ts)
        self.executor.submit(self._resolve, headcode, area_id, to_berth)

    def _handle_trust(self, item: dict):
        if item.get("header", {}).get("msg_type") != "0003":
            return
        body     = item.get("body", {})
        train_id = body.get("train_id", "")
        headcode = (body.get("train_description") or
                    (train_id[2:6] if len(train_id) >= 6 else "")).strip()
        stanox   = body.get("loc_stanox", "").strip()
        ts_str = body.get("actual_timestamp", "").strip()
        if not ts_str or ts_str == "0":
            return   # pre-declaration/forecast — no actual time yet
        if not (headcode and stanox):
            return
        if stanox[:2] not in ("01", "03", "04", "05", "06", "07", "08", "09"):
            return
        coords = self.lookup.lookup_by_stanox(stanox)
        if coords is None:
            _debug(f"[TRUST]  {headcode} stanox={stanox}  NO COORDS")
            return
        lat, lon = coords
        # Use wall-clock time as anchor — TRUST actual_timestamp contains
        # predicted future times, not actual past times, so it can't be used
        # for bracketing against TD berth step timestamps.
        self.learner.add_anchor(headcode, time.time(), lat, lon)
        _inc("trust_anchors")
        _debug(f"[TRUST]  {headcode} stanox={stanox}  ({lat:.4f},{lon:.4f})")

        # Use TRUST position directly if no fresh TD position exists.
        # This gives Highland trains (RETB lines with no TD berth coverage)
        # a snapshot position each time they pass a timing point.
        # Central belt trains with a recent TD berth position are left alone.
        with _pos_lock:
            existing = positions.get(headcode)
            has_fresh_td = (existing is not None and
                            existing.get("lat") is not None and
                            time.time() - existing["timestamp"] < 60)
        if not has_fresh_td:
            set_position(headcode, "TR", stanox, lat, lon, source="trust")
            _debug(f"[TRUST]  {headcode} → position set from TRUST ({lat:.4f},{lon:.4f})")

    def _resolve(self, headcode: str, area_id: str, berth: str):
        loc = self.lookup.lookup(area_id, berth)
        if loc is not None and loc.lat is not None:
            set_position(headcode, area_id, berth, loc.lat, loc.lon, source="corpus")
            return
        learned = self.learner.lookup(area_id, berth)
        if learned is not None:
            lat, lon = learned
            set_position(headcode, area_id, berth, lat, lon, source="learned")

    def on_error(self, frame):
        body = frame.body or ""
        headers = dict(frame.headers)
        msg_header = headers.get("message", "")
        # Only log body if it contains something useful
        if body.strip():
            print(f"[STOMP] ERROR — body: {body.strip()}", file=sys.stderr)
        print(f"[STOMP] ERROR — {msg_header}", file=sys.stderr)
        combined = (body + msg_header).lower()
        if any(kw in combined for kw in ("auth", "security", "credential", "login", "password")):
            print("[STOMP] Auth/security error — NOT reconnecting. "
                  "Check credentials and wait 15 min for any IP block to expire.",
                  file=sys.stderr)
            raise SystemExit(1)
        if "amq339009" in combined:
            print(f"[STOMP] Broker session still open — will wait {SESSION_WAIT} s before reconnecting.",
                  file=sys.stderr)

    def on_disconnected(self):
        if self._reconnecting:
            return
        self._reconnecting = True
        print(f"[STOMP] Disconnected — reconnecting in {RECONNECT_DELAY} s …",
              file=sys.stderr)
        threading.Timer(RECONNECT_DELAY, self._reconnect).start()

    def _reconnect(self):
        _connect_with_backoff(self.conn, self.username, self.password, "[STOMP]")
        self._reconnecting = False


# ── Background maintenance ────────────────────────────────────────────────────

def background_loop(conn, username: str, password: str):
    while True:
        time.sleep(30)   # check every 30 seconds

        # ── Watchdog ──────────────────────────────────────────────────────
        silence = time.time() - _last_message_time
        if silence > WATCHDOG_TIMEOUT:
            print(f"[WATCHDOG] No STOMP message for {int(silence)}s — reconnecting …",
                  file=sys.stderr)
            try:
                conn.disconnect()
            except Exception:
                pass
            _connect_with_backoff(conn, username, password, "[WATCHDOG]")
            print("[WATCHDOG] Reconnected.", file=sys.stderr)
            continue

        # ── Stats + prune (every ~5 min) ──────────────────────────────────
        if int(time.time()) % 300 < 30:
            removed = prune_stale()
            with _pos_lock:
                total = len(positions)
                with_coords = sum(1 for p in positions.values() if p["lat"] is not None)
            with _stats_lock:
                s = dict(stats)
            if removed:
                print(f"[PRUNE] Removed {removed} stale trains")
            learned = _learner.learned_count if _learner else 0
            print(f"[STATS] trains={total} ({with_coords} located) | "
                  f"STOMP msgs={s['stomp_messages']} | "
                  f"CA_MSGs={s['ca_msgs']} | "
                  f"position updates={s['position_updates']} | "
                  f"TRUST anchors={s['trust_anchors']} | "
                  f"learned berths={learned}")


# ── Helpers ───────────────────────────────────────────────────────────────────

SESSION_WAIT = 300   # seconds to wait when broker reports an existing session (AMQ339009)

def _connect_with_backoff(conn, username: str, password: str, label: str) -> None:
    """Connect with exponential backoff — doubles delay each attempt, caps at 1800 s."""
    delay = RECONNECT_DELAY
    while True:
        # Clean disconnect before each attempt so the broker sees a fresh start
        try:
            conn.disconnect()
        except Exception:
            pass
        try:
            conn.connect(username, password, wait=True)
            return
        except Exception as exc:
            msg = str(exc).lower()
            print(f"{label} Connect failed ({type(exc).__name__}): {exc}", file=sys.stderr)
            if any(kw in msg for kw in ("auth", "security", "credential", "login", "password")):
                raise SystemExit(
                    f"{label} Auth/security error — NOT retrying. "
                    "Check credentials and wait 15 min for any IP block to expire."
                )
            # AMQ339009 = broker still has our previous session open; wait for it to expire
            if "amq339009" in msg or "amq339009" in str(exc):
                wait = SESSION_WAIT
                print(f"{label} Broker session still active (AMQ339009) — waiting {wait} s for it to expire …",
                      file=sys.stderr)
                time.sleep(wait)
                continue   # don't increase backoff — just retry at same interval
            print(f"{label} Retrying in {delay} s …", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 1800)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _learner

    username = os.environ.get("NR_USERNAME")
    password = os.environ.get("NR_PASSWORD")
    if not username or not password:
        raise SystemExit("Set NR_USERNAME and NR_PASSWORD before running.")

    print("[INIT] Loading SMART + CORPUS data …")
    lookup = BerthLookup()
    print("[INIT] Loading berth learner …")
    _learner = BerthLearner()
    print("[INIT] Loading Highland rail geometry …")
    snap_to_rail.load()
    print("[INIT] Ready.\n")

    executor = ThreadPoolExecutor(max_workers=LOOKUP_WORKERS, thread_name_prefix="lookup")

    # ── Single connection for both TD and TRUST ───────────────────────────
    td_conn = stomp.Connection(
        host_and_ports=[(STOMP_HOST, STOMP_PORT)],
        heartbeats=(10000, 10000),  # 10s keepalive — prevents AMQ229014 TTL drops
    )
    td_conn.set_listener("", CombinedListener(td_conn, lookup, _learner, executor, username, password))
    _connect_with_backoff(td_conn, username, password, "[STOMP]")
    trust_conn = None

    threading.Thread(target=background_loop, args=(td_conn, username, password),
                     daemon=True, name="bg").start()

    server = HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[HTTP]  http://localhost:{HTTP_PORT}/trains?lat=57.06&lon=-4.12&radius=50")
    print(f"[HTTP]  http://localhost:{HTTP_PORT}/trains/stats")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping …")
        td_conn.disconnect()
        if trust_conn:
            trust_conn.disconnect()
        executor.shutdown(wait=False)


if __name__ == "__main__":
    main()
