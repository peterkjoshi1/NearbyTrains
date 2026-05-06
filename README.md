# NearbyTrains

A SwiftUI iOS app showing live Scottish National Rail station departures and train positions on a Mapbox map.

**[Full description →](docs/index.html)**

## Quick start

### What you need

- macOS with Xcode 15 or later
- Python 3.10 or later
- Three free accounts (links below)

### 1. Get accounts and tokens

| Service | Purpose | Link |
|---|---|---|
| Network Rail Open Data | TD + TRUST live feeds | https://publicdatafeeds.networkrail.co.uk |
| Mapbox | Map tiles | https://account.mapbox.com |
| Realtime Trains (RTT) | Station departure boards | https://api-portal.rtt.io |

From Mapbox, copy your **public token** (starts with `pk.`).
From RTT, generate a **refresh token** under API Access.
Network Rail gives you a username and password on registration.

### 2. Clone and install

```bash
git clone https://github.com/peterkjoshi1/NearbyTrains.git
cd NearbyTrains
pip3 install stomp.py
```

### 3. Configure the server

```bash
mkdir -p ~/Library/Application\ Support/NearbyTrains
cp server/credentials.example ~/Library/Application\ Support/NearbyTrains/credentials
```

Edit `~/Library/Application Support/NearbyTrains/credentials`:

```
NR_USERNAME=your_network_rail_username
NR_PASSWORD=your_network_rail_password
```

### 4. Start the server

```bash
python3 server/train_tracker.py
```

You should see `[INIT] Ready.` within a few seconds. Verify it is working:

```bash
curl http://localhost:8080/trains/stats | python3 -m json.tool
```

The server listens on port 8080 and keeps running in that terminal. Leave it open.

### 5. Configure the iOS app

```bash
cp Secrets.xcconfig.example Secrets.xcconfig
```

Edit `Secrets.xcconfig` and fill in your tokens:

```
MAPBOX_TOKEN = pk.your_mapbox_public_token_here
RTT_REFRESH_TOKEN = your_rtt_refresh_token_here
```

### 6. Point the app at localhost

Open `NearbyTrains/TrainPositionService.swift` and change line 12 to:

```swift
static let serverBase = "http://localhost:8080"
```

(The iOS Simulator on your Mac can reach the server at `localhost`. On a physical device, use your Mac's LAN IP instead, e.g. `http://192.168.1.x:8080`.)

### 7. Set simulator location

The simulator has no GPS. Set it to somewhere in Scotland:

In the running simulator: **Features → Location → Custom Location**
Enter latitude `55.8609`, longitude `-4.2514` (Glasgow Central).

> If you skip this step the app falls back to Glasgow Central automatically, but setting it explicitly means the OS location permission prompt works correctly.

### 8. Build and run

Open `NearbyTrains.xcodeproj` in Xcode. Select an iPhone simulator from the device picker, then press **Run** (⌘R). The map should appear and trains should start populating within a minute or so as berth-step messages arrive from Network Rail.

> **Note:** The berth learner starts with a pre-built database of observed positions (`server/berth_learned.db`), so most Scottish central-belt berths will resolve immediately. The learned positions improve passively as more trains are observed.

**API**

```
GET /trains?lat=57.06&lon=-4.12&radius=50   — trains within radius (km)
GET /trains/stats                            — connection/tracking stats, observation counts
GET /trains/snap_log                         — recent 200 snap-to-rail corrections (sorted by distance in app)
GET /trains/weight_versions                  — descriptions of each observation weight formula version
GET /trains/rebuild_positions                   — recompute all learned berth centroids from stored observations
GET /trains/debug_state?headcode=1A23        — internal anchor/interpolation state for a specific train
```

**Useful curl one-liners**

```bash
# Check server health
curl http://localhost:8080/trains/stats | python3 -m json.tool

# Recompute all learned berth positions from stored observations
curl http://localhost:8080/trains/rebuild_positions

# Show worst snap corrections (pipe through jq or python for readability)
curl http://localhost:8080/trains/snap_log | python3 -c "
import json,sys
rows=json.load(sys.stdin)
rows.sort(key=lambda r: r.get('distance_m',0), reverse=True)
for r in rows[:20]: print(f\"{r['distance_m']:5d}m  {r['area']}:{r['berth']}  {r['source']}  {r['headcode']}\")
"

# Inspect a specific train's interpolation state
curl "http://localhost:8080/trains/debug_state?headcode=1A23" | python3 -m json.tool
```

**Analysis scripts**

```bash
# Detailed berth analysis with scatter plot saved to /tmp/
python3 server/berth_analysis.py G2:5901

# Empirical weight validation against corpus ground truth
python3 server/analyse_weights.py
```

## Geographic coverage

Train positions come from two distinct data sources with very different coverage:

### Central belt — continuous TD tracking

The Scottish central belt (Edinburgh, Glasgow, Fife, Dundee, Aberdeen corridor, and most lines south of the Highland boundary) is covered by the Network Rail Train Describer (TD) STOMP feed. The server subscribes to `TD_SE_SIG_AREA` and `TD_SW_SIG_AREA`. These carry berth-step messages for TD areas EA, EB, EC, ED, G1, G2, YO, SM and others. Trains move continuously between berths as they travel, giving near-real-time position updates every few seconds.

### Highland and remote lines — no TD coverage

The following routes have **no TD berth coverage** in the public Network Rail feed:

| Route | Reason |
|---|---|
| Highland Mainline (Stirling–Perth–Aviemore–Inverness) | RETB signalling — no track circuit berths |
| Far North Line (Inverness–Wick/Thurso) | RETB |
| West Highland Line (Glasgow–Fort William–Mallaig) | RETB |
| Kyle of Lochalsh Line | RETB |
| Oban branch | RETB |

RETB (Radio Electronic Token Block) is a radio-based single-line token system used on remote Scottish routes. Trains on these lines do not generate TD berth messages, so continuous position tracking is not possible via the TD feed.

The TD area codes for these routes (IH, IN, IR, CV, AD) exist in the Network Rail SMART reference data but do not appear in any public STOMP topic, including `TD_ALL_SIG_AREA`.

### Planned: TRUST snapshot positions for Highland trains

The TRUST feed (`TRAIN_MVT_ALL_TOC`) does cover Highland trains — it fires a movement event whenever a train passes a timing point, which corresponds to a station or major junction. The server receives these events but currently uses them only to help the berth learner interpolate positions in the central belt.

The next planned improvement is to use TRUST events directly to place Highland trains on the map at station coordinates as they pass through. A train would appear at Stirling, then Perth, then Pitlochry, then Aviemore, and so on — not moving smoothly between stations, but showing the correct last-reported location.

## How train positions are determined

Train positions come from combining three data sources:

### 1. SMART/CORPUS reference data (most central belt trains)
Network Rail publishes a reference database that maps each TD berth ID to a geographical coordinate. When a train enters a berth, we look up its position directly from this table. The coordinates are generally accurate but occasionally wrong by hundreds of metres — the snap-to-rail step corrects these onto the nearest railway line. About 60% of berths have corpus entries.

### 2. Berth learning (remaining central belt trains)
Around 40% of berths have no corpus entry. For these, the server learns positions over time by watching TRUST movement events. When a train passes two timing points (e.g. departs Glasgow Central, arrives Motherwell), we know its position at both ends and the time it took. When a TD berth step fires between those two events, we can estimate where the train was at that moment by interpolating along the time axis. The more trains pass through a berth, the more confident the learned position becomes.

Learned positions are stored in a SQLite database and survive server restarts. Each observation is weighted by how confident the interpolation was — observations near the midpoint of a long journey segment are given low weight; observations close to a known timing point are given higher weight. An outlier-rejection algorithm (IRLS) periodically down-weights observations that are far from the cluster centroid, handling cases where the interpolation went wrong.

**Important:** the learner only operates on berths that have no corpus entry. Corpus positions are treated as ground truth and are never overwritten by learning.

### 3. Snap-to-rail
All positions (corpus and learned) are snapped to the nearest railway line geometry downloaded from OpenStreetMap. This corrects small positional errors from the reference data and ensures trains appear on the track rather than a few hundred metres to one side. Snaps of more than 2 km are rejected as likely errors.

## Debug screen

The iOS app includes a Debug tab (ant icon) showing:

- **Server stats** — message counts, tracked trains, learned berths
- **Bad NR source data** — corpus berths that snapped a long way, suggesting the NR reference coordinate is wrong
- **Bad interpolation** — learned berths whose position snapped a long way, suggesting the learning produced a poor estimate
- **Bad TRUST position** — TRUST-fed positions that snapped a long way
- **Unresolved berths** — berths seen repeatedly that neither corpus nor the learner can place

Tapping any snap-correction entry opens a map showing the raw position, the snapped position, and (for learned berths) the individual observations underlying the estimate, with darker dots indicating higher-confidence observations.

## Potential improvements

### OSM routing graph (foundation)

Build a routable graph from the existing OSM rail geometry using `networkx`. Nodes at junctions, edges are track segments with full polyline geometry. This is the prerequisite for path interpolation, speed correction, and Highland positioning.

### Track-curve-aware interpolation

Replace straight-line lat/lon interpolation between TRUST anchors with arc-length interpolation along the OSM track path. The interpolated position stays on the rail by construction — no snap correction needed. Biggest win on curved routes (Glasgow approaches, coastal lines).

### ML speed-correction model

Trains don't travel at constant speed between timing points — they accelerate out of stations and brake into them. The time-linear interpolation fraction `t` is therefore wrong, especially near anchors.

- **Shadow-learn corpus berths**: run the learner on berths that already have a known corpus position (currently skipped), recording observations without serving them as positions
- **Training data**: for each shadow-learned berth, the interpolated centroid is the prediction and the corpus position is the label; compute the true arc-length fraction along the OSM path
- **Model**: learn `t → t′` correction (isotonic regression or polynomial); features include segment length, dt_before, dt_after, time of day
- **Apply**: correct the interpolation fraction for non-corpus berths before computing weighted centroid

### GPX ground-truth collection

Sitting on a train with a GPX logger running and cross-referencing GPS timestamps against TD berth step timestamps (server already records `observed_at`) gives near-exact ground truth for each berth traversed — no interpolation, no corpus approximation.

- One Edinburgh–Glasgow run ≈ 50–100 ground-truthed berths in 50 minutes
- Better training labels than corpus positions (which are STANOX-level, not berth-level)
- Also validates OSM geometry — a large snap distance means the OSM track is drawn wrong at that point
- Note: berth step fires at circuit entry, not midpoint — record entry and exit times and take the midpoint position

### Route context for path selection at junctions

A berth is one fixed physical location, but may be bracketed by different TRUST anchor pairs depending on which route a train came from. The sequence of recent TRUST STANOXes identifies which branch of a junction the train is on, allowing the correct OSM path to be chosen for interpolation.

### Real-time continuous position predictor

Once path interpolation and speed correction exist, position can be predicted at any moment — not just when a berth step fires. Given last known TRUST position, elapsed time, and a learned speed profile, the train can be walked along the track path continuously. Trains would appear to glide rather than jump between berth steps.

### Highland and RETB lines

No TD berths exist on the Highland Mainline, Far North Line, West Highland Line, or Kyle/Oban branches (RETB signalling). With path interpolation between TRUST timing points and a speed model, trains on these routes could be shown moving between stations. Speed profiles trained on central belt data may generalise to Highland segments with similar characteristics.

**Suggested build order**: OSM routing graph → path interpolation → shadow-learn corpus berths → ML speed correction → GPX validation → continuous predictor → Highland lines

### Expand TD coverage

The server currently subscribes to the Scottish TD areas. Coverage could be extended to other regions of Great Britain by subscribing to additional STOMP topics and expanding the snap-to-rail geometry bounding box.
