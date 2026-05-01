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
