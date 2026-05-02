//
//  DebugView.swift
//  NearbyTrains
//

import MapKit
import SwiftUI

// MARK: - Models

struct ServerStats: Decodable {
    let stompMessages: Int
    let caMsgs: Int
    let positionUpdates: Int
    let trustAnchors: Int
    let snapHits: Int
    let trackedTrains: Int
    let trainsWith_coords: Int?
    let learnedBerths: Int
    let railSegments: Int

    enum CodingKeys: String, CodingKey {
        case stompMessages   = "stomp_messages"
        case caMsgs          = "ca_msgs"
        case positionUpdates = "position_updates"
        case trustAnchors    = "trust_anchors"
        case snapHits        = "snap_hits"
        case trackedTrains   = "tracked_trains"
        case trainsWith_coords = "trains_with_coords"
        case learnedBerths   = "learned_berths"
        case railSegments    = "rail_segments"
    }

    var trainsWithCoords: Int { trainsWith_coords ?? 0 }
}

struct SnapCorrection: Identifiable, Decodable {
    let headcode: String
    let area: String
    let berth: String
    let source: String
    let rawLat: Double
    let rawLon: Double
    let snappedLat: Double
    let snappedLon: Double
    let distanceM: Int
    let timestamp: Double

    var id: String { "\(area)-\(berth)-\(timestamp)" }

    enum CodingKeys: String, CodingKey {
        case headcode, area, berth, source, timestamp
        case rawLat      = "raw_lat"
        case rawLon      = "raw_lon"
        case snappedLat  = "snapped_lat"
        case snappedLon  = "snapped_lon"
        case distanceM   = "distance_m"
    }
}

struct SkipEntry: Identifiable, Decodable {
    let area: String
    let berth: String
    let count: Int
    let lastHeadcode: String
    let lastTs: Double
    let reason: String
    let inCache: Bool

    var id: String { "\(area)-\(berth)" }

    enum CodingKeys: String, CodingKey {
        case area, berth, count, reason
        case lastHeadcode = "last_headcode"
        case lastTs       = "last_ts"
        case inCache      = "in_cache"
    }
}

// MARK: - Service

@Observable
class DebugService {
    var stats: ServerStats?
    var snapLog: [SnapCorrection] = []
    var skipLog: [SkipEntry] = []
    var isLoading = false
    var error: String?

    func load() async {
        isLoading = true
        error = nil
        async let statsData = fetch(path: "trains/stats")
        async let snapData  = fetch(path: "trains/snap_log")
        async let skipData  = fetch(path: "trains/skip_log")
        do {
            let (s, n, k) = try await (statsData, snapData, skipData)
            stats   = try JSONDecoder().decode(ServerStats.self, from: s)
            snapLog = try JSONDecoder().decode([SnapCorrection].self, from: n)
                .sorted { $0.distanceM > $1.distanceM }
            skipLog = try JSONDecoder().decode([SkipEntry].self, from: k)
        } catch {
            self.error = error.localizedDescription
        }
        isLoading = false
    }

    private func fetch(path: String) async throws -> Data {
        let url = URL(string: "\(TrainPositionService.serverBase)/\(path)")!
        let (data, _) = try await URLSession.shared.data(from: url)
        return data
    }
}

// MARK: - View

struct DebugView: View {
    @State private var service = DebugService()
    @State private var selectedSnap: SnapCorrection?

    var body: some View {
        NavigationStack {
            Group {
                if service.isLoading && service.stats == nil {
                    ProgressView("Loading…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let error = service.error {
                    ContentUnavailableView(error, systemImage: "exclamationmark.triangle")
                } else {
                    List {
                        if let stats = service.stats {
                            statsSection(stats)
                        }
                        snapLogSection
                        skipLogSection
                    }
                }
            }
            .navigationTitle("Debug")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await service.load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(service.isLoading)
                }
            }
            .sheet(item: $selectedSnap) { snap in
                SnapMapView(correction: snap)
            }
        }
        .task { await service.load() }
    }

    // MARK: Stats

    @ViewBuilder
    private func statsSection(_ s: ServerStats) -> some View {
        Section("Server") {
            stat("Tracked trains",    "\(s.trackedTrains) (\(s.trainsWithCoords) located)")
            stat("STOMP messages",    "\(s.stompMessages)")
            stat("CA messages",       "\(s.caMsgs)")
            stat("Position updates",  "\(s.positionUpdates)")
            stat("TRUST anchors",     "\(s.trustAnchors)")
            stat("Snap corrections",  "\(s.snapHits)")
            stat("Learned berths",    "\(s.learnedBerths)")
            stat("Rail segments",     "\(s.railSegments)")
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(.system(.subheadline, design: .monospaced))
        }
        .font(.subheadline)
    }

    // MARK: Snap log

    @ViewBuilder
    private var snapLogSection: some View {
        Section("Snap corrections — \(service.snapLog.count) (sorted by distance)") {
            if service.snapLog.isEmpty {
                Text("None recorded yet")
                    .foregroundStyle(.secondary).font(.subheadline)
            } else {
                ForEach(service.snapLog) { correction in
                    Button { selectedSnap = correction } label: {
                        SnapRow(correction: correction)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: Skip log

    @ViewBuilder
    private var skipLogSection: some View {
        Section("Unresolved berths — \(service.skipLog.count) (≥3 skips)") {
            if service.skipLog.isEmpty {
                Text("None yet")
                    .foregroundStyle(.secondary).font(.subheadline)
            } else {
                ForEach(service.skipLog) { entry in
                    SkipRow(entry: entry)
                }
            }
        }
    }
}

// MARK: - Snap row

private struct SnapRow: View {
    let correction: SnapCorrection

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("\(correction.area):\(correction.berth)")
                    .font(.system(.subheadline, design: .monospaced).weight(.semibold))
                Spacer()
                Text("\(correction.distanceM) m")
                    .font(.system(.subheadline, design: .monospaced))
                    .foregroundStyle(distanceColor)
            }
            HStack {
                Text(correction.headcode)
                    .font(.system(.caption, design: .monospaced))
                sourceTag
                Spacer()
                Text(timeAgo(correction.timestamp))
                    .font(.caption)
            }
            .foregroundStyle(.secondary)
            Text(String(format: "raw (%.4f, %.4f)", correction.rawLat, correction.rawLon))
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    private var sourceTag: some View {
        Text(correction.source)
            .font(.system(.caption2, design: .monospaced))
            .padding(.horizontal, 4).padding(.vertical, 1)
            .background(sourceColor.opacity(0.15))
            .foregroundStyle(sourceColor)
            .clipShape(RoundedRectangle(cornerRadius: 3))
    }

    private var sourceColor: Color {
        switch correction.source {
        case "corpus":  return .red
        case "learned": return .orange
        case "trust":   return .blue
        default:        return .secondary
        }
    }

    private var distanceColor: Color {
        if correction.distanceM > 500 { return .red }
        if correction.distanceM > 250 { return .orange }
        return .secondary
    }

    private func timeAgo(_ ts: Double) -> String {
        let secs = Int(Date().timeIntervalSince1970 - ts)
        if secs < 60   { return "\(secs)s ago" }
        if secs < 3600 { return "\(secs / 60)m ago" }
        return "\(secs / 3600)h ago"
    }
}

// MARK: - Skip row

private struct SkipRow: View {
    let entry: SkipEntry

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text("\(entry.area):\(entry.berth)")
                    .font(.system(.subheadline, design: .monospaced).weight(.semibold))
                HStack(spacing: 6) {
                    Text(entry.lastHeadcode)
                        .font(.system(.caption, design: .monospaced))
                    Text(entry.reason.replacingOccurrences(of: "_", with: " "))
                        .font(.caption)
                    if entry.inCache {
                        Text("learned")
                            .font(.caption2)
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(Color.green.opacity(0.15))
                            .foregroundStyle(.green)
                            .clipShape(RoundedRectangle(cornerRadius: 3))
                    }
                }
                .foregroundStyle(.secondary)
            }
            Spacer()
            Text("×\(entry.count)")
                .font(.system(.subheadline, design: .monospaced))
                .foregroundStyle(entry.count > 20 ? .red : .secondary)
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Snap map

private struct BerthObservation: Decodable {
    let lat: Double
    let lon: Double
    let observedAt: Int
    enum CodingKeys: String, CodingKey {
        case lat, lon
        case observedAt = "observed_at"
    }
}

struct SnapMapView: View {
    let correction: SnapCorrection

    @State private var observations: [BerthObservation] = []

    private var rawCoord: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: correction.rawLat, longitude: correction.rawLon)
    }
    private var snappedCoord: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: correction.snappedLat, longitude: correction.snappedLon)
    }
    private var region: MKCoordinateRegion {
        var lats = [correction.rawLat, correction.snappedLat] + observations.map(\.lat)
        var lons = [correction.rawLon, correction.snappedLon] + observations.map(\.lon)
        let minLat = lats.min()!, maxLat = lats.max()!
        let minLon = lons.min()!, maxLon = lons.max()!
        return MKCoordinateRegion(
            center: CLLocationCoordinate2D(
                latitude: (minLat + maxLat) / 2,
                longitude: (minLon + maxLon) / 2
            ),
            span: MKCoordinateSpan(
                latitudeDelta: max(maxLat - minLat, 0.004) * 4,
                longitudeDelta: max(maxLon - minLon, 0.004) * 4
            )
        )
    }

    var body: some View {
        NavigationStack {
            Map(initialPosition: .region(region)) {
                // Observation scatter (learned only)
                ForEach(Array(observations.enumerated()), id: \.offset) { _, obs in
                    Annotation("", coordinate: CLLocationCoordinate2D(latitude: obs.lat, longitude: obs.lon), anchor: .center) {
                        Circle()
                            .fill(.white.opacity(0.7))
                            .frame(width: 8, height: 8)
                            .overlay(Circle().stroke(.gray, lineWidth: 1))
                    }
                }
                // Dashed line: raw → snapped
                MapPolyline(coordinates: [rawCoord, snappedCoord])
                    .stroke(.orange, style: StrokeStyle(lineWidth: 2, dash: [6, 4]))
                // Raw (wrong) position
                Annotation("Raw", coordinate: rawCoord, anchor: .bottom) {
                    VStack(spacing: 2) {
                        Text(correction.source).font(.caption2).foregroundStyle(.white)
                            .padding(.horizontal, 4).padding(.vertical, 2)
                            .background(.red).clipShape(RoundedRectangle(cornerRadius: 3))
                        Image(systemName: "circle.fill").foregroundStyle(.red).font(.caption)
                    }
                }
                // Snapped (correct) position
                Annotation("Snapped", coordinate: snappedCoord, anchor: .bottom) {
                    VStack(spacing: 2) {
                        Text("rail").font(.caption2).foregroundStyle(.white)
                            .padding(.horizontal, 4).padding(.vertical, 2)
                            .background(.green).clipShape(RoundedRectangle(cornerRadius: 3))
                        Image(systemName: "circle.fill").foregroundStyle(.green).font(.caption)
                    }
                }
            }
            .mapStyle(.hybrid)
            .navigationTitle("\(correction.area):\(correction.berth) · \(correction.distanceM)m")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(correction.headcode)
                            .font(.system(.caption, design: .monospaced).weight(.semibold))
                        Text(observations.isEmpty ? correction.source : "\(correction.source) · \(observations.count) obs")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .task {
            guard correction.source == "learned" else { return }
            let urlStr = "\(TrainPositionService.serverBase)/trains/berth_observations?area=\(correction.area)&berth=\(correction.berth)"
            guard let url = URL(string: urlStr),
                  let (data, _) = try? await URLSession.shared.data(from: url) else { return }
            observations = (try? JSONDecoder().decode([BerthObservation].self, from: data)) ?? []
        }
    }
}
