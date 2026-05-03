#!/usr/bin/env python3
"""
berth_lookup.py — Map a TD area_id + berth_id to geographic coordinates.

Data chain:
    SMART data   : (area_id, berth_id) → STANOX + station name
    CORPUS data  : STANOX              → CRS code (3ALPHA)
    Overpass API : CRS code            → (lat, lon)

Usage (CLI):
    python3 berth_lookup.py <area_id> <berth_id>
    python3 berth_lookup.py WN 4831

Usage (as a module):
    from berth_lookup import BerthLookup
    bl = BerthLookup()
    result = bl.lookup("WN", "4831")
    print(result)  # {'lat': 51.5, 'lon': -0.12, 'name': 'Paddington', 'crs': 'PAD', 'stanox': '70101'}
"""

import json
import sys
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

SMART_FILE  = Path(__file__).parent / "smart_data.json"
CORPUS_FILE = Path(__file__).parent / "corpus_data.json"
COORD_CACHE = Path(__file__).parent / "coord_cache.json"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# ── Overpass rate limiting (module-level, shared across all instances) ────────

_overpass_semaphore    = threading.Semaphore(3)
_overpass_rate_lock    = threading.Lock()
_overpass_last_call    = [0.0]   # mutable list so we can update in-place
_overpass_min_interval = 2.0     # seconds

# ── Static Scottish station coordinates ──────────────────────────────────────
#
# Pre-seeded into the coord cache on startup so coordinate resolution works
# immediately without any Overpass calls for these stations.
# Coordinates are WGS-84 (lat, lon), sourced from OpenStreetMap / OS data.

SCOTLAND_COORDS: dict[str, tuple[float, float]] = {
    # ── Edinburgh area ───────────────────────────────────────────────────
    "EDB": (55.95191, -3.19087),   # Edinburgh Waverley
    "HYM": (55.94648, -3.21803),   # Edinburgh Haymarket
    "EDP": (55.92743, -3.30674),   # Edinburgh Park
    "INK": (56.02963, -3.39714),   # Inverkeithing
    "KDY": (56.11295, -3.16283),   # Kirkcaldy
    "LDY": (56.19952, -3.00264),   # Ladybank
    "CUP": (56.31872, -2.90195),   # Cupar
    "LEU": (56.36060, -2.83100),   # Leuchars
    "DEE": (56.45654, -2.97023),   # Dundee
    # ── Glasgow area ─────────────────────────────────────────────────────
    "GLC": (55.85777, -4.25742),   # Glasgow Central
    "GLQ": (55.86372, -4.25064),   # Glasgow Queen Street
    "PTK": (55.86784, -4.30965),   # Partick
    "DLR": (55.87610, -4.36050),   # Dalmuir
    "HLC": (55.98037, -4.73218),   # Helensburgh Central
    "HLU": (56.00120, -4.72050),   # Helensburgh Upper
    # ── Highland Main Line: Glasgow/Edinburgh – Inverness ─────────────────
    "STG": (56.11919, -3.93582),   # Stirling
    "DBL": (56.18628, -3.96744),   # Dunblane
    "PTH": (56.39459, -3.42442),   # Perth
    "DCF": (56.55960, -3.57760),   # Dunkeld & Birnam
    "PIT": (56.70590, -3.73240),   # Pitlochry
    "BLA": (56.76963, -3.84878),   # Blair Atholl
    "DLW": (56.93850, -4.24340),   # Dalwhinnie
    "NWR": (57.05970, -4.13350),   # Newtonmore
    "KGS": (57.08023, -4.05351),   # Kingussie
    "KIN": (57.07256, -4.03238),   # Kingussie (alt)
    "AVM": (57.19300, -3.82720),   # Aviemore
    "CAG": (57.28290, -3.83400),   # Carrbridge
    "INV": (57.47729, -4.22474),   # Inverness
    # ── Aberdeen line ────────────────────────────────────────────────────
    "ARB": (56.55860, -2.58700),   # Arbroath
    "MNF": (56.71080, -2.46280),   # Montrose
    "STO": (56.96290, -2.21050),   # Stonehaven
    "ABD": (57.14371, -2.09807),   # Aberdeen
    # ── West Highland Line ───────────────────────────────────────────────
    "ARS": (56.20890, -4.72380),   # Arrochar & Tarbet
    "CNR": (56.38748, -4.61820),   # Crianlarich
    "TYL": (56.42972, -4.72989),   # Tyndrum Lower
    "TYU": (56.43437, -4.72439),   # Tyndrum Upper
    "BOC": (56.52649, -4.76195),   # Bridge of Orchy
    "RAN": (56.67671, -4.58335),   # Rannoch
    "CRR": (56.76558, -4.68380),   # Corrour
    "TUL": (56.86490, -4.71260),   # Tulloch
    "RYB": (56.88610, -4.94190),   # Roy Bridge
    "SBR": (56.91570, -4.91130),   # Spean Bridge
    "FTW": (56.81937, -5.10485),   # Fort William
    "MLG": (57.00720, -5.82800),   # Mallaig
    # ── Oban branch ──────────────────────────────────────────────────────
    "LCA": (56.37820, -5.10560),   # Loch Awe
    "OBN": (56.41470, -5.47360),   # Oban
    # ── Kyle of Lochalsh line ────────────────────────────────────────────
    "ACH": (57.61290, -4.43040),   # Achnasheen
    "STR": (57.59300, -5.46520),   # Strathcarron
    "KYL": (57.27690, -5.71390),   # Kyle of Lochalsh
    # ── Far North line ───────────────────────────────────────────────────
    "DNG": (57.58790, -4.02220),   # Dingwall
    "TAI": (57.81550, -4.04390),   # Tain
    "GLS": (57.97530, -3.97900),   # Golspie
    "BRO": (58.01170, -3.84340),   # Brora
    "HLP": (58.30110, -3.55220),   # Helmsdale
    "WCK": (58.44050, -3.09690),   # Wick
    "THB": (58.58870, -3.52400),   # Thurso
    # ── Ayrshire / Southwest ─────────────────────────────────────────────
    "AYR": (55.46289, -4.62874),   # Ayr
    "KMK": (55.61157, -4.49596),   # Kilmarnock
    "GRK": (55.94490, -4.75700),   # Greenock Central
    "DMF": (55.06770, -3.60150),   # Dumfries
}


@dataclass
class BerthLocation:
    stanox: str
    name: str
    crs: Optional[str]
    lat: Optional[float]
    lon: Optional[float]


class BerthLookup:
    def __init__(self):
        self._smart_index: dict[tuple[str, str], tuple[str, str]] = {}   # (area, berth) → (stanox, name)
        self._corpus_index: dict[str, str] = {}                           # stanox → crs
        self._coord_cache: dict[str, tuple[float, float]] = {}            # crs → (lat, lon)
        self._cache_lock = threading.Lock()
        self._retry_queue: list[tuple[str, float]] = []                   # (crs, retry_after)
        self._retry_lock = threading.Lock()
        self._load_smart()
        self._load_corpus()
        self._load_coord_cache()
        self._preload_known_coords()
        threading.Thread(target=self._retry_loop, daemon=True,
                         name="overpass-retry").start()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def lookup(self, area_id: str, berth_id: str) -> Optional[BerthLocation]:
        """Return location for a (area_id, berth_id) pair, or None if unknown."""
        key = (area_id.upper(), berth_id.upper())
        entry = self._smart_index.get(key)
        if entry is None:
            print(f"[SMART]  MISS  area={area_id}  berth={berth_id}", file=sys.stderr)
            return None

        stanox, name = entry
        crs = self._corpus_index.get(stanox)
        print(f"[SMART]  HIT   area={area_id}  berth={berth_id}  stanox={stanox}  name={name!r}  crs={crs}", file=sys.stderr)
        lat, lon = self._resolve_coordinates(crs) if crs else (None, None)

        return BerthLocation(stanox=stanox, name=name, crs=crs, lat=lat, lon=lon)

    def lookup_dict(self, area_id: str, berth_id: str) -> Optional[dict]:
        result = self.lookup(area_id, berth_id)
        return asdict(result) if result else None

    def lookup_by_stanox(self, stanox: str) -> Optional[tuple[float, float]]:
        """Return (lat, lon) for a STANOX code, or None if not resolvable."""
        stanox = stanox.strip()
        crs = self._corpus_index.get(stanox)
        if not crs:
            print(f"[TRUST]  MISS stanox={stanox}  (no CORPUS entry)", file=sys.stderr)
            return None
        lat, lon = self._resolve_coordinates(crs)
        if lat is None:
            print(f"[TRUST]  MISS stanox={stanox}  crs={crs}  (no coords)", file=sys.stderr)
            return None
        return lat, lon

    def all_corpus_berths(self):
        """Yield (area_id, berth_id, lat, lon) for every berth resolvable via corpus."""
        for (area, berth) in self._smart_index:
            result = self.lookup(area, berth)
            if result is not None:
                yield area, berth, result.lat, result.lon

    # ------------------------------------------------------------------ #
    # Data loading                                                         #
    # ------------------------------------------------------------------ #

    def _load_smart(self):
        with open(SMART_FILE) as f:
            records = json.load(f)["BERTHDATA"]

        for r in records:
            area    = r.get("TD", "").strip().upper()
            stanox  = r.get("STANOX", "").strip()
            name    = r.get("STANME", "").strip()
            if not area or not stanox:
                continue
            for berth_key in ("FROMBERTH", "TOBERTH"):
                berth = r.get(berth_key, "").strip().upper()
                if berth:
                    key = (area, berth)
                    # Don't overwrite an existing entry that already has a name
                    if key not in self._smart_index or not self._smart_index[key][1]:
                        self._smart_index[key] = (stanox, name)

        print(f"[SMART]  Loaded {len(self._smart_index):,} berth entries", file=sys.stderr)

    def _load_corpus(self):
        with open(CORPUS_FILE) as f:
            records = json.load(f)["TIPLOCDATA"]

        for r in records:
            stanox = r.get("STANOX", "").strip()
            crs    = r.get("3ALPHA", "").strip()
            if stanox and crs:
                self._corpus_index[stanox] = crs

        print(f"[CORPUS] Loaded {len(self._corpus_index):,} STANOX→CRS mappings", file=sys.stderr)

    def _load_coord_cache(self):
        if COORD_CACHE.exists():
            with open(COORD_CACHE) as f:
                raw = json.load(f)
            self._coord_cache = {k: tuple(v) for k, v in raw.items()
                                  if v and v[0] is not None}
            print(f"[CACHE]  Loaded {len(self._coord_cache):,} cached coordinates", file=sys.stderr)

    def _preload_known_coords(self):
        """Seed coord cache with static Scottish station coordinates.

        Only fills in missing entries so any corrected values already in the
        persisted cache file are not overwritten.
        """
        added = 0
        with self._cache_lock:
            for crs, latlon in SCOTLAND_COORDS.items():
                existing = self._coord_cache.get(crs)
                if existing is None or existing == (None, None):
                    self._coord_cache[crs] = latlon
                    added += 1
            if added:
                self._save_coord_cache()
        print(f"[CACHE]  Pre-loaded {added} static Scottish station coordinates",
              file=sys.stderr)

    def _save_coord_cache(self):
        with open(COORD_CACHE, "w") as f:
            json.dump(self._coord_cache, f)

    # ------------------------------------------------------------------ #
    # Coordinate resolution via Overpass                                  #
    # ------------------------------------------------------------------ #

    def _resolve_coordinates(self, crs: str) -> tuple[Optional[float], Optional[float]]:
        crs = crs.upper()
        with self._cache_lock:
            if crs in self._coord_cache:
                return self._coord_cache[crs]

        # Throttled Overpass call — outside the cache lock so other threads
        # can still serve cached results while this one waits.
        result = self._throttled_overpass_call(crs)

        with self._cache_lock:
            self._coord_cache[crs] = result
            self._save_coord_cache()
        return result

    def _throttled_overpass_call(self, crs: str) -> tuple[Optional[float], Optional[float]]:
        """Acquire semaphore (≤3 concurrent) then enforce 2 s minimum spacing."""
        with _overpass_semaphore:
            with _overpass_rate_lock:
                wait = _overpass_min_interval - (time.time() - _overpass_last_call[0])
                if wait > 0:
                    time.sleep(wait)
                _overpass_last_call[0] = time.time()
            return self._overpass_lookup(crs)

    def _overpass_lookup(self, crs: str) -> tuple[Optional[float], Optional[float]]:
        query = f"""
[out:json][timeout:10];
node["railway"="station"]["ref:crs"="{crs}"];
out body 1;
"""
        body = ("data=" + urllib.parse.quote(query)).encode()
        req  = urllib.request.Request(OVERPASS_URL, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "NearbyTrains/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
            elements = data.get("elements", [])
            if elements:
                e = elements[0]
                lat, lon = float(e["lat"]), float(e["lon"])
                print(f"[OVERPASS] {crs} → ({lat:.5f}, {lon:.5f})", file=sys.stderr)
                return lat, lon
            # CRS not found in OSM — no benefit in retrying
            return None, None
        except Exception as exc:
            print(f"[OVERPASS] Failed for {crs}: {exc}", file=sys.stderr)
            self._queue_retry(crs)
            return None, None

    def _queue_retry(self, crs: str) -> None:
        """Add crs to the retry queue if not already present."""
        with self._retry_lock:
            if not any(c == crs for c, _ in self._retry_queue):
                self._retry_queue.append((crs, time.time() + 300))
                print(f"[RETRY]  Queued {crs} (retry in 5 min)", file=sys.stderr)

    def _retry_loop(self) -> None:
        """Background thread: retry failed Overpass lookups every 5 minutes."""
        while True:
            time.sleep(60)   # check once per minute
            now = time.time()

            to_retry: list[str] = []
            with self._retry_lock:
                pending = []
                for crs, retry_after in self._retry_queue:
                    if retry_after <= now:
                        to_retry.append(crs)
                    else:
                        pending.append((crs, retry_after))
                self._retry_queue = pending

            for crs in to_retry:
                # Skip if already resolved successfully since queuing
                with self._cache_lock:
                    cached = self._coord_cache.get(crs)
                    if cached is not None and cached != (None, None):
                        continue
                    # Remove stale None so the fresh result can be stored
                    self._coord_cache.pop(crs, None)

                print(f"[RETRY]  Retrying {crs} …", file=sys.stderr)
                result = self._throttled_overpass_call(crs)

                with self._cache_lock:
                    self._coord_cache[crs] = result
                    self._save_coord_cache()

                if result != (None, None):
                    print(f"[RETRY]  {crs} → ({result[0]:.5f}, {result[1]:.5f})",
                          file=sys.stderr)
                # If still failed, _overpass_lookup called _queue_retry again,
                # so this CRS will be retried in another 5 minutes automatically.


# ------------------------------------------------------------------ #
# CLI                                                                 #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 berth_lookup.py <area_id> <berth_id>")
        print("  e.g. python3 berth_lookup.py WN 4831")
        sys.exit(1)

    area_id, berth_id = sys.argv[1], sys.argv[2]
    bl = BerthLookup()

    result = bl.lookup(area_id, berth_id)
    if result is None:
        print(f"No entry found for area={area_id!r} berth={berth_id!r}")
        sys.exit(1)

    print(json.dumps(asdict(result), indent=2))
