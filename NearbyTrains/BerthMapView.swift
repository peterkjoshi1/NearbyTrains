//
//  BerthMapView.swift
//  NearbyTrains
//

import MapboxMaps
import SwiftUI

// MARK: - Model

struct BerthSummary: Identifiable, Decodable {
    let area: String
    let berth: String
    let lat: Double?
    let lon: Double?
    let obsCount: Int?
    let sdM: Double?
    let inCache: Bool
    let source: String   // "learned" | "corpus" | "skip"
    let snapLat: Double?
    let snapLon: Double?
    let snapDistM: Int?
    let skipCount: Int?
    let skipReason: String?

    var id: String { "\(area):\(berth)" }

    enum CodingKeys: String, CodingKey {
        case area, berth, lat, lon
        case obsCount   = "obs_count"
        case sdM        = "sd_m"
        case inCache    = "in_cache"
        case source
        case snapLat    = "snap_lat"
        case snapLon    = "snap_lon"
        case snapDistM  = "snap_dist_m"
        case skipCount  = "skip_count"
        case skipReason = "skip_reason"
    }

    /// Synthesise a SnapCorrection so we can reuse SnapMapView for the detail screen.
    func asSnapCorrection() -> SnapCorrection? {
        guard let lat, let lon else { return nil }
        return SnapCorrection(
            headcode: "—",
            area: area, berth: berth,
            source: inCache ? "learned" : "unlearned",
            rawLat: lat, rawLon: lon,
            snappedLat: snapLat ?? lat, snappedLon: snapLon ?? lon,
            distanceM: snapDistM ?? Int(sdM ?? 0),
            timestamp: 0
        )
    }
}

// MARK: - Screen

struct BerthMapScreen: View {
    @State private var berths: [BerthSummary] = []
    @State private var selected: BerthSummary?
    @State private var isLoading = true

    private var learnedCount: Int  { berths.filter { $0.source == "learned" }.count }
    private var corpusCount: Int   { berths.filter { $0.source == "corpus"  }.count }
    private var skipCount: Int     { berths.filter { $0.source == "skip"    }.count }

    var body: some View {
        BerthMapbox(berths: berths, onSelect: { selected = $0 })
            .ignoresSafeArea()
            .overlay(alignment: .bottom) {
                if !berths.isEmpty {
                    HStack(spacing: 12) {
                        Label("\(learnedCount)", systemImage: "circle.fill").foregroundStyle(.blue)
                        Label("\(corpusCount)",  systemImage: "circle.fill").foregroundStyle(.black)
                        Label("\(skipCount) skipped", systemImage: "circle.fill").foregroundStyle(.orange)
                    }
                    .font(.caption)
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(.regularMaterial, in: Capsule())
                    .padding(.bottom, 30)
                }
            }
            .overlay(alignment: .center) {
                if isLoading { ProgressView() }
            }
            .navigationTitle("Berth Map")
            .navigationBarTitleDisplayMode(.inline)
            .sheet(item: $selected) { b in
                if let corr = b.asSnapCorrection() {
                    SnapMapView(correction: corr)
                }
            }
            .task { await load() }
    }

    private func load() async {
        guard let url = URL(string: "\(TrainPositionService.serverBase)/trains/berths"),
              let (data, _) = try? await URLSession.shared.data(from: url) else {
            isLoading = false; return
        }
        berths = (try? JSONDecoder().decode([BerthSummary].self, from: data)) ?? []
        isLoading = false
    }
}

// MARK: - Mapbox UIViewRepresentable

struct BerthMapbox: UIViewRepresentable {
    let berths: [BerthSummary]
    let onSelect: (BerthSummary) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(onSelect: onSelect) }

    func makeUIView(context: Context) -> MapView {
        let mapView = MapView(frame: .zero, mapInitOptions: MapInitOptions(
            cameraOptions: CameraOptions(
                center: CLLocationCoordinate2D(latitude: 56.0, longitude: -4.0),
                zoom: 7),
            styleURI: .streets))
        mapView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        let lineMgr = mapView.annotations.makePolylineAnnotationManager(id: "snap-lines")
        context.coordinator.lineManager = lineMgr
        let mgr = mapView.annotations.makeCircleAnnotationManager(id: "berths")
        mgr.delegate = context.coordinator
        context.coordinator.manager = mgr
        let snapMgr = mapView.annotations.makeCircleAnnotationManager(id: "snaps")
        context.coordinator.snapManager = snapMgr
        let coord = context.coordinator
        coord.styleToken = mapView.mapboxMap.onStyleLoaded.observe { [weak mapView, weak coord] _ in
            guard let mapView, let coord else { return }
            coord.addRailLayers(to: mapView)
        }
        return mapView
    }

    func updateUIView(_ mapView: MapView, context: Context) {
        context.coordinator.sync(berths: berths)
    }

    // MARK: Coordinator

    class Coordinator: NSObject, AnnotationInteractionDelegate {
        let onSelect: (BerthSummary) -> Void
        var manager: CircleAnnotationManager?
        var snapManager: CircleAnnotationManager?
        var lineManager: PolylineAnnotationManager?
        var annotationMap: [String: BerthSummary] = [:]
        var styleToken: AnyCancelable?
        var railLayersAdded = false

        init(onSelect: @escaping (BerthSummary) -> Void) {
            self.onSelect = onSelect
        }

        func addRailLayers(to mapView: MapView) {
            guard !railLayersAdded else { return }
            guard mapView.mapboxMap.allLayerIdentifiers.count > 3 else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self, weak mapView] in
                    guard let self, let mapView else { return }
                    self.addRailLayers(to: mapView)
                }
                return
            }
            let railColor = StyleColor(UIColor(red: 0.8, green: 0.0, blue: 0.0, alpha: 1))
            for id in ["road-rail", "bridge-rail"] {
                try? mapView.mapboxMap.updateLayer(withId: id, type: LineLayer.self) { layer in
                    layer.lineColor = .constant(railColor)
                    layer.lineWidth = .expression(Exp(.interpolate) {
                        Exp(.linear); Exp(.zoom)
                        5; 1.0; 10; 2.0; 14; 4.0
                    })
                    layer.lineOpacity = .constant(1.0)
                    layer.minZoom = 5
                }
            }
            railLayersAdded = true
        }

        func sync(berths: [BerthSummary]) {
            render(berths)
        }

        private func render(_ berths: [BerthSummary]) {
            annotationMap = [:]
            var circles: [CircleAnnotation] = []
            var snaps:   [CircleAnnotation] = []
            var lines:   [PolylineAnnotation] = []

            for b in berths {
                guard let lat = b.lat, let lon = b.lon else { continue }

                // Centroid — small dot, colour by source
                var a = CircleAnnotation(
                    id: b.id,
                    centerCoordinate: CLLocationCoordinate2D(latitude: lat, longitude: lon))
                switch b.source {
                case "learned":
                    a.circleColor = StyleColor(UIColor(red: 0.2, green: 0.5, blue: 1.0, alpha: 0.7))
                case "corpus":
                    a.circleColor = StyleColor(UIColor(white: 0.0, alpha: 0.6))
                default:
                    a.circleColor = StyleColor(UIColor(red: 1.0, green: 0.5, blue: 0.1, alpha: 0.7))
                }
                a.circleRadius      = 3
                a.circleStrokeColor = StyleColor(UIColor(white: 1.0, alpha: 0.4))
                a.circleStrokeWidth = 1
                circles.append(a)
                annotationMap[b.id] = b

                // Snap — large prominent dot + line from centroid
                if let sLat = b.snapLat, let sLon = b.snapLon {
                    var s = CircleAnnotation(
                        id: "\(b.id):snap",
                        centerCoordinate: CLLocationCoordinate2D(latitude: sLat, longitude: sLon))
                    switch b.source {
                    case "learned":
                        s.circleColor = StyleColor(UIColor(red: 0.1, green: 0.75, blue: 0.3, alpha: 0.9))
                    case "corpus":
                        s.circleColor = StyleColor(UIColor(white: 0.2, alpha: 0.8))
                    default:
                        s.circleColor = StyleColor(UIColor(red: 1.0, green: 0.5, blue: 0.1, alpha: 0.9))
                    }
                    s.circleRadius      = 5
                    s.circleStrokeColor = StyleColor(UIColor(white: 1.0, alpha: 0.6))
                    s.circleStrokeWidth = 1
                    snaps.append(s)

                    var line = PolylineAnnotation(id: "\(b.id):line", lineCoordinates: [
                        CLLocationCoordinate2D(latitude: lat,  longitude: lon),
                        CLLocationCoordinate2D(latitude: sLat, longitude: sLon),
                    ])
                    line.lineColor = StyleColor(UIColor(white: 0.4, alpha: 0.4))
                    line.lineWidth = 1
                    lines.append(line)
                }
            }
            lineManager?.annotations  = lines
            snapManager?.annotations  = snaps
            manager?.annotations      = circles
        }

        func annotationManager(_ manager: AnnotationManager,
                               didDetectTappedAnnotations annotations: [Annotation]) {
            guard let first = annotations.first, let b = annotationMap[first.id] else { return }
            onSelect(b)
        }
    }
}
