//
//  DebugView.swift
//  NearbyTrains
//

import MapboxMaps
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
    let berthsWithObservations: Int?
    let totalObservations: Int?
    let v4Observations: Int?

    enum CodingKeys: String, CodingKey {
        case stompMessages         = "stomp_messages"
        case caMsgs                = "ca_msgs"
        case positionUpdates       = "position_updates"
        case trustAnchors          = "trust_anchors"
        case snapHits              = "snap_hits"
        case trackedTrains         = "tracked_trains"
        case trainsWith_coords     = "trains_with_coords"
        case learnedBerths         = "learned_berths"
        case railSegments          = "rail_segments"
        case berthsWithObservations = "berths_with_observations"
        case totalObservations     = "total_observations"
        case v4Observations        = "v4_observations"
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
    var weightVersions: [WeightVersion] = []
    var isLoading = false
    var error: String?

    func load() async {
        isLoading = true
        error = nil
        async let statsData    = fetch(path: "trains/stats")
        async let snapData     = fetch(path: "trains/snap_log")
        async let skipData     = fetch(path: "trains/skip_log")
        async let versionsData = fetch(path: "trains/weight_versions")
        do {
            let (s, n, k, v) = try await (statsData, snapData, skipData, versionsData)
            stats          = try JSONDecoder().decode(ServerStats.self, from: s)
            snapLog        = try JSONDecoder().decode([SnapCorrection].self, from: n)
                .sorted { $0.distanceM > $1.distanceM }
            skipLog        = try JSONDecoder().decode([SkipEntry].self, from: k)
            weightVersions = try JSONDecoder().decode([WeightVersion].self, from: v)
        } catch {
            self.error = error.localizedDescription
        }
        isLoading = false
    }

    func rebuildPositions() async {
        _ = try? await fetch(path: "trains/rebuild_positions")
        await load()
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
                    HStack {
                        Button {
                            Task { await service.rebuildPositions() }
                        } label: {
                            Image(systemName: "wand.and.stars")
                        }
                        .disabled(service.isLoading)
                        Button {
                            Task { await service.load() }
                        } label: {
                            Image(systemName: "arrow.clockwise")
                        }
                        .disabled(service.isLoading)
                    }
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
            if let n = s.berthsWithObservations { stat("Berths with observations", "\(n)") }
            if let n = s.totalObservations { stat("Total observations", "\(n)") }
            if let n = s.v4Observations { stat("v4 observations", "\(n)") }
        }
        if !service.weightVersions.isEmpty {
            Section("Weight versions") {
                ForEach(service.weightVersions) { v in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text("v\(v.version)").font(.system(.caption, design: .monospaced).weight(.semibold))
                            Text(v.name).font(.system(.caption, design: .monospaced))
                        }
                        if let desc = v.description {
                            Text(desc).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
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
        snapSection("corpus",  label: "Bad NR source data")
        snapSection("learned", label: "Bad interpolation")
        snapSection("trust",   label: "Bad TRUST position")
    }

    @ViewBuilder
    private func snapSection(_ source: String, label: String) -> some View {
        let items = service.snapLog
            .filter { $0.source == source }
            .prefix(20)
        Section("\(label) — \(items.count) (top 20 by distance)") {
            if items.isEmpty {
                Text("None recorded yet")
                    .foregroundStyle(.secondary).font(.subheadline)
            } else {
                ForEach(items) { correction in
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
    let weight: Double?
    let weightVersion: Int?
    let dtBefore: Double?
    let dtAfter: Double?
    let ancBeforeLat: Double?
    let ancBeforeLon: Double?
    let ancBeforeStanox: String?
    let ancAfterLat: Double?
    let ancAfterLon: Double?
    let ancAfterStanox: String?
    enum CodingKeys: String, CodingKey {
        case lat, lon, weight
        case observedAt       = "observed_at"
        case weightVersion    = "weight_version"
        case dtBefore         = "dt_before"
        case dtAfter          = "dt_after"
        case ancBeforeLat     = "anc_before_lat"
        case ancBeforeLon     = "anc_before_lon"
        case ancBeforeStanox  = "anc_before_stanox"
        case ancAfterLat      = "anc_after_lat"
        case ancAfterLon      = "anc_after_lon"
        case ancAfterStanox   = "anc_after_stanox"
    }
}

struct WeightVersion: Decodable, Identifiable {
    let version: Int
    let name: String
    let description: String?
    var id: Int { version }
}

struct SnapMapView: View {
    let correction: SnapCorrection
    @State private var observations: [BerthObservation] = []
    @State private var showData = false

    var body: some View {
        NavigationStack {
            Group {
                if showData {
                    dataView
                } else {
                    SnapDebugMapView(correction: correction, observations: observations)
                        .ignoresSafeArea()
                }
            }
            .navigationTitle("\(correction.area):\(correction.berth) · \(correction.distanceM)m")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(showData ? "Map" : "Data") { showData.toggle() }
                        .font(.caption)
                }
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
            let urlStr = "\(TrainPositionService.serverBase)/trains/berth_observations?area=\(correction.area)&berth=\(correction.berth)"
            guard let url = URL(string: urlStr),
                  let (data, _) = try? await URLSession.shared.data(from: url) else { return }
            observations = (try? JSONDecoder().decode([BerthObservation].self, from: data)) ?? []
        }
    }

    private var dataView: some View {
        List {
            Section("Snap correction") {
                row("Raw",     String(format: "%.5f, %.5f", correction.rawLat, correction.rawLon))
                row("Snapped", String(format: "%.5f, %.5f", correction.snappedLat, correction.snappedLon))
                row("Distance", "\(correction.distanceM) m")
                row("Source",  correction.source)
            }
            Section("Observations (\(observations.count) total)") {
                if observations.isEmpty {
                    Text("None (source: \(correction.source))")
                        .foregroundStyle(.secondary).font(.caption)
                } else {
                    ForEach(Array(observations.reversed().prefix(20).enumerated()), id: \.offset) { i, obs in
                        VStack(alignment: .leading, spacing: 2) {
                            row(String(format: "#%d  v%d  w=%.4f", i + 1, obs.weightVersion ?? 1, obs.weight ?? 1.0),
                                String(format: "%.5f, %.5f", obs.lat, obs.lon))
                            if let dtb = obs.dtBefore, let dta = obs.dtAfter {
                                Text(String(format: "dt_before=%.0fs  dt_after=%.0fs", dtb, dta))
                                    .font(.system(.caption2, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                            if let bs = obs.ancBeforeStanox, let as_ = obs.ancAfterStanox, !bs.isEmpty {
                                Text("anchors: \(bs) → \(as_)")
                                    .font(.system(.caption2, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
        .font(.system(.caption, design: .monospaced))
    }

    private func row(_ label: String, _ value: String) -> some View {
        HStack {
            Text(label).foregroundStyle(.secondary)
            Spacer()
            Text(value)
        }
        .font(.system(.caption, design: .monospaced))
    }
}

// MARK: - Mapbox snap debug map

private struct SnapDebugMapView: UIViewRepresentable {
    let correction: SnapCorrection
    let observations: [BerthObservation]

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> MapView {
        let initialCamera = CameraOptions(
            center: CLLocationCoordinate2D(latitude: correction.rawLat, longitude: correction.rawLon),
            zoom: 13)
        let mapView = MapView(frame: .zero, mapInitOptions: MapInitOptions(cameraOptions: initialCamera, styleURI: .streets))
        mapView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        let coord = context.coordinator
        coord.circleMgr    = mapView.annotations.makeCircleAnnotationManager(id: "debug-circles")
        coord.polylineMgr  = mapView.annotations.makePolylineAnnotationManager(id: "debug-line")
        coord.anchorMgr    = mapView.annotations.makePolylineAnnotationManager(id: "debug-anchors")
        coord.styleToken  = mapView.mapboxMap.onStyleLoaded.observe { [weak mapView] _ in
            guard let mapView else { return }
            coord.addRailLayers(to: mapView)
        }
        return mapView
    }

    func updateUIView(_ mapView: MapView, context: Context) {
        context.coordinator.sync(correction: correction, observations: observations, mapView: mapView)
    }

    // MARK: Coordinator

    final class Coordinator: NSObject {
        var circleMgr:   CircleAnnotationManager?
        var polylineMgr: PolylineAnnotationManager?
        var anchorMgr:   PolylineAnnotationManager?
        var styleToken:  AnyCancelable?
        var lastObsCount = -1

        func addRailLayers(to mapView: MapView) {
            let railColor = StyleColor(UIColor(red: 0.8, green: 0.0, blue: 0.0, alpha: 1))
            for id in ["road-rail", "bridge-rail"] {
                try? mapView.mapboxMap.updateLayer(withId: id, type: LineLayer.self) { layer in
                    layer.lineColor   = .constant(railColor)
                    layer.lineWidth   = .constant(3.0)
                    layer.lineOpacity = .constant(1.0)
                    layer.minZoom     = 5
                }
            }
        }

        func sync(correction: SnapCorrection, observations: [BerthObservation], mapView: MapView) {
            let rawCoord     = CLLocationCoordinate2D(latitude: correction.rawLat,     longitude: correction.rawLon)
            let snappedCoord = CLLocationCoordinate2D(latitude: correction.snappedLat, longitude: correction.snappedLon)

            // Blue dots, log-normalised by 1/weight (dark = high 1/w = low quality)
            let invWeights = observations.map { 1.0 / max($0.weight ?? 1.0, 1e-6) }
            let logMin = log((invWeights.min() ?? 0.01) + 0.001)
            let logMax = log((invWeights.max() ?? 1.0) + 0.001)
            let logRange = logMax > logMin ? logMax - logMin : 1

            var circles: [CircleAnnotation] = []

            for (i, obs) in observations.enumerated() {
                let invW = 1.0 / max(obs.weight ?? 1.0, 1e-6)
                let t = (log(invW + 0.001) - logMin) / logRange  // 0=good, 1=bad
                let darkness = t * 0.8
                var a = CircleAnnotation(id: "obs-\(i)",
                    centerCoordinate: CLLocationCoordinate2D(latitude: obs.lat, longitude: obs.lon))
                a.circleColor       = StyleColor(UIColor(red: 0.3 * (1 - darkness), green: 0.6 - 0.5 * darkness, blue: 1.0 - 0.7 * darkness, alpha: 0.9))
                a.circleRadius      = 5
                a.circleStrokeColor = StyleColor(UIColor(white: 0.2, alpha: 0.6))
                a.circleStrokeWidth = 1
                circles.append(a)
            }

            var rawA = CircleAnnotation(id: "raw", centerCoordinate: rawCoord)
            rawA.circleColor       = StyleColor(UIColor.red)
            rawA.circleRadius      = 9
            rawA.circleStrokeColor = StyleColor(UIColor.white)
            rawA.circleStrokeWidth = 2
            circles.append(rawA)

            var snapA = CircleAnnotation(id: "snapped", centerCoordinate: snappedCoord)
            snapA.circleColor       = StyleColor(UIColor(red: 0.2, green: 0.8, blue: 0.2, alpha: 1))
            snapA.circleRadius      = 9
            snapA.circleStrokeColor = StyleColor(UIColor.white)
            snapA.circleStrokeWidth = 2
            circles.append(snapA)

            circleMgr?.annotations = circles

            // Orange line raw → snapped
            var line = PolylineAnnotation(id: "snap-line", lineCoordinates: [rawCoord, snappedCoord])
            line.lineColor = StyleColor(UIColor.orange)
            line.lineWidth = 2.5
            polylineMgr?.annotations = [line]

            // Purple lines: anchor A → observation → anchor B (first 20)
            var anchorLines: [PolylineAnnotation] = []
            for (i, obs) in observations.prefix(20).enumerated() {
                guard let bLat = obs.ancBeforeLat, let bLon = obs.ancBeforeLon,
                      let aLat = obs.ancAfterLat,  let aLon = obs.ancAfterLon else { continue }
                let obsCoord    = CLLocationCoordinate2D(latitude: obs.lat, longitude: obs.lon)
                let beforeCoord = CLLocationCoordinate2D(latitude: bLat,   longitude: bLon)
                let afterCoord  = CLLocationCoordinate2D(latitude: aLat,   longitude: aLon)
                var al = PolylineAnnotation(id: "anc-\(i)", lineCoordinates: [beforeCoord, obsCoord, afterCoord])
                al.lineColor = StyleColor(UIColor(red: 0.6, green: 0.0, blue: 0.8, alpha: 0.5))
                al.lineWidth = 1.5
                anchorLines.append(al)
            }
            anchorMgr?.annotations = anchorLines

            // Fit camera whenever obs count changes (fires for initial render and again when obs load)
            guard observations.count != lastObsCount else { return }
            lastObsCount = observations.count
            let fitMax = observations.compactMap { $0.weight }.max() ?? 1.0
            let threshold = fitMax * 0.05
            let fittedObs = observations.filter { ($0.weight ?? 1.0) >= threshold }
            let allCoords = [rawCoord, snappedCoord] + fittedObs.map {
                CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lon)
            }
            let midLat = allCoords.map(\.latitude).reduce(0, +)  / Double(allCoords.count)
            let midLon = allCoords.map(\.longitude).reduce(0, +) / Double(allCoords.count)
            var camera = mapView.mapboxMap.camera(
                for: allCoords,
                padding: UIEdgeInsets(top: 80, left: 60, bottom: 80, right: 60),
                bearing: nil, pitch: nil
            )
            if camera.center == nil {
                camera.center = CLLocationCoordinate2D(latitude: midLat, longitude: midLon)
                camera.zoom   = 13
            }
            mapView.camera.ease(to: camera, duration: observations.isEmpty ? 0 : 0.5)
        }
    }
}
