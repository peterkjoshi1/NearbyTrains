# NearbyTrains

A SwiftUI iOS app showing live Scottish National Rail station departures and train positions on a Mapbox map.

**[Full description →](docs/index.html)**

## Setup

### iOS App

1. Copy `Secrets.xcconfig.example` to `Secrets.xcconfig` in the project root
2. Fill in your Mapbox public token from https://account.mapbox.com
3. Fill in your RTT refresh token from https://api-portal.rtt.io
4. Open `NearbyTrains.xcodeproj` and build

### Train Position Server

The server connects to the Network Rail TD STOMP feed and serves a REST API for live train positions.

**Requirements**

```bash
pip install stomp.py
```

**Credentials**

Register for a free account at https://publicdatafeeds.networkrail.co.uk, then:

```bash
mkdir -p ~/Library/Application\ Support/NearbyTrains
cp server/credentials.example ~/Library/Application\ Support/NearbyTrains/credentials
```

Edit `~/Library/Application Support/NearbyTrains/credentials` and fill in your username and password.

**Running**

```bash
cd /path/to/NearbyTrains
python3 server/train_tracker.py
```

The server listens on `http://localhost:8080`. On a physical device, update the IP address in `NearbyTrains/TrainPositionService.swift` to your Mac's local IP.

**API**

```
GET /trains?lat=57.06&lon=-4.12&radius=50   — trains within radius (km)
GET /trains/stats                            — connection and tracking stats
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

### Use corpus to seed or constrain learning
Currently the learner ignores berths that have corpus entries. A better approach would be to use corpus positions as a strong prior — if a berth has a corpus entry and observations are accumulating nearby, the learned centroid should be anchored to the corpus position rather than free-floating. This would catch cases where interpolation is consistently going wrong and producing a learned position far from the known corpus location.

### Smooth interpolated positions between updates
Positions jump discretely each time a new berth step fires (every few seconds in busy areas). A Kalman filter or simple dead-reckoning model could smooth motion between updates using the train's last known speed and heading.

### Cluster-aware learning for shared berth IDs
Some berth IDs appear on multiple routes that pass through the same signalling area. Observations for these berths can form two geographic clusters. The current outlier-rejection approach picks the dominant cluster and suppresses the other, but a better approach would be to detect bimodal distributions and maintain two separate learned positions, choosing between them based on the train's recent history.

### Expand TD coverage
The server currently subscribes to the Scottish TD areas. Coverage could be extended to other regions of Great Britain by subscribing to additional STOMP topics and expanding the snap-to-rail geometry bounding box.

### Highland interpolation using public timetable
For RETB lines with no TD coverage, trains could be interpolated between TRUST timing points using the public timetable. If a train is scheduled to take 45 minutes from Pitlochry to Blair Atholl and a TRUST event confirms it departed Pitlochry on time, its position at any moment can be estimated by interpolating along the known route geometry.
