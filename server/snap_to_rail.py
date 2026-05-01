"""
snap_to_rail.py — Snap a lat/lon point onto the nearest Scottish Highland railway linestring.

Downloads Highland railway geometry from OSM Overpass API once at startup and caches
it to highland_rail.json in the server directory. Subsequent startups load from cache.

Only used for TRUST-positioned trains (area_id == "TR") where the position is a station
STANOX coordinate that may be slightly offset from the track. Safe for Highland lines
(single-track, no parallel routes or junctions). Not used for TD berth positions.
"""

import json
import math
import pathlib
import urllib.parse
import urllib.request

CACHE_FILE = pathlib.Path(__file__).parent / "highland_rail.json"
MAX_SNAP_KM = 2.0   # ignore snap if nearest rail is further than this
MIN_SNAP_KM = 0.15  # ignore snap if already within this distance (avoids wrong parallel-track selection)

# Bounding box: covers all Scottish railway lines tracked by this server —
# Highland Mainline, Far North Line, Kyle Line, West Highland Line, and
# the Central Belt (Edinburgh–Glasgow) for TRUST trains without fresh TD positions.
_BBOX = (55.3, -6.5, 58.8, -2.0)   # south, west, north, east

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_OVERPASS_QUERY = (
    f"[out:json][timeout:60];\n"
    f'way["railway"="rail"]({_BBOX[0]},{_BBOX[1]},{_BBOX[2]},{_BBOX[3]});\n'
    f"out geom;"
)

# Populated by load(); list of polylines, each a list of (lat, lon) tuples
_polylines: list[list[tuple[float, float]]] = []


# ── Geometry loading ──────────────────────────────────────────────────────────

def load(force_refresh: bool = False) -> None:
    """Load Highland railway geometry. Call once at server startup."""
    global _polylines
    if not force_refresh and CACHE_FILE.exists():
        try:
            _polylines = _load_cache()
            print(f"[SNAP]  Loaded {len(_polylines)} Highland rail segments from cache "
                  f"({CACHE_FILE.name})")
            return
        except Exception as e:
            print(f"[SNAP]  Cache load failed ({e}), re-fetching …")

    print("[SNAP]  Fetching Highland railway geometry from Overpass API …")
    try:
        _polylines = _fetch()
        _save_cache(_polylines)
        print(f"[SNAP]  Downloaded and cached {len(_polylines)} Highland rail segments")
    except Exception as e:
        print(f"[SNAP]  WARNING: Could not fetch rail geometry ({e}) — snap disabled")
        _polylines = []


def _fetch() -> list[list[tuple[float, float]]]:
    data = urllib.parse.urlencode({"data": _OVERPASS_QUERY}).encode()
    req = urllib.request.Request(
        _OVERPASS_URL, data=data,
        headers={"User-Agent": "NearbyTrains/1.0 (train position display)"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    polylines = []
    for elem in result.get("elements", []):
        geom = elem.get("geometry", [])
        if len(geom) >= 2:
            polylines.append([(g["lat"], g["lon"]) for g in geom])
    return polylines


def _load_cache() -> list[list[tuple[float, float]]]:
    with open(CACHE_FILE) as f:
        raw = json.load(f)
    return [[(pt[0], pt[1]) for pt in line] for line in raw]


def _save_cache(polylines: list[list[tuple[float, float]]]) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(polylines, f)


# ── Snap function ─────────────────────────────────────────────────────────────

def snap(lat: float, lon: float) -> tuple[float, float, float]:
    """Return (snapped_lat, snapped_lon, distance_km).

    Finds the nearest point on any loaded Highland rail linestring.
    If no rail is found within MAX_SNAP_KM, returns the original point unchanged
    with distance = inf.
    """
    if not _polylines:
        return lat, lon, math.inf

    best_lat, best_lon, best_dist = lat, lon, math.inf

    for polyline in _polylines:
        for i in range(len(polyline) - 1):
            a_lat, a_lon = polyline[i]
            b_lat, b_lon = polyline[i + 1]
            s_lat, s_lon = _nearest_on_segment(lat, lon, a_lat, a_lon, b_lat, b_lon)
            d = _haversine_km(lat, lon, s_lat, s_lon)
            if d < best_dist:
                best_dist = d
                best_lat, best_lon = s_lat, s_lon

    if best_dist > MAX_SNAP_KM or best_dist < MIN_SNAP_KM:
        return lat, lon, math.inf

    return best_lat, best_lon, best_dist


def _nearest_on_segment(
    px: float, py: float,
    ax: float, ay: float,
    bx: float, by: float,
) -> tuple[float, float]:
    """Nearest point on segment AB to point P, in degree-space.

    Using degrees directly is accurate enough for short Highland rail segments
    (typical segment < 500 m). The resulting coordinate is fed into haversine
    for the actual distance check.
    """
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    denom = abx * abx + aby * aby
    if denom == 0.0:
        return ax, ay
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / denom))
    return ax + t * abx, ay + t * aby


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
