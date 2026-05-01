//
//  StationMapView.swift
//  NearbyTrains
//

import SwiftUI
import MapboxMaps
import CoreLocation

// MARK: - Sheet type

private enum ActiveSheet: Identifiable {
    case stationDepartures(StationWithDepartures)
    case forecast
    case trainDetail(LiveTrain)

    var id: String {
        switch self {
        case .stationDepartures(let s): return "station-\(s.id)"
        case .forecast:                 return "forecast"
        case .trainDetail(let t):       return "train-\(t.id)"
        }
    }
}

// MARK: - StationMapView

struct StationMapView: View {
    let viewModel: AppViewModel

    @State private var activeSheet: ActiveSheet?
    @State private var droppedPin: CLLocationCoordinate2D?
    @State private var forecast: [PassingTrain] = []
    @State private var isForecastLoading = false
    @State private var liveTrainService = TrainPositionService()

    private let stationFinder = StationFinder()
    private let trainService = TrainService()

    private var selectedStation: StationWithDepartures? {
        if case .stationDepartures(let s) = activeSheet { return s }
        return nil
    }

    var body: some View {
        MapboxMapRepresentable(
            userLocation: viewModel.userLocation,
            stations: viewModel.stations,
            trains: liveTrainService.trains,
            selectedStation: selectedStation,
            droppedPin: droppedPin,
            onPinDropped: handlePinDrop,
            onStationTapped: { station in
                if case .stationDepartures(let s) = activeSheet, s.id == station.id {
                    activeSheet = nil
                } else {
                    droppedPin = nil
                    activeSheet = .stationDepartures(station)
                }
            },
            onTrainTapped: { train in
                activeSheet = .trainDetail(train)
            },
            onCentreChanged: { centre in
                Task { @MainActor in liveTrainService.updateCentre(centre) }
            }
        )
        .ignoresSafeArea()
        .sheet(item: $activeSheet) { sheet in
            switch sheet {
            case .stationDepartures(let s):
                StationDeparturesSheet(stationWithDepartures: s)
                    .presentationDetents([.fraction(0.4)])
            case .forecast:
                ForecastSheet(forecast: forecast, isLoading: isForecastLoading)
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            case .trainDetail(let t):
                TrainDetailSheet(train: t, trainService: trainService, userLocation: viewModel.userLocation)
                    .presentationDetents([.fraction(0.3)])
            }
        }
    }

    private func handlePinDrop(_ coordinate: CLLocationCoordinate2D) {
        activeSheet = .forecast
        droppedPin = coordinate
        forecast = []
        isForecastLoading = true
        Task { await loadForecast(at: coordinate) }
    }

    private func loadForecast(at coordinate: CLLocationCoordinate2D) async {
        let pinLocation = CLLocation(latitude: coordinate.latitude, longitude: coordinate.longitude)
        do {
            let nearby = try await stationFinder.findStations(near: pinLocation)
            let nearest2 = Array(nearby.prefix(2))

            var allTrains: [PassingTrain] = []
            await withTaskGroup(of: [PassingTrain].self) { group in
                for station in nearest2 {
                    group.addTask {
                        (try? await self.trainService.fetchPassingTrains(for: station.crs, stationName: station.name)) ?? []
                    }
                }
                for await trains in group {
                    allTrains.append(contentsOf: trains)
                }
            }

            var seen = Set<String>()
            forecast = allTrains
                .sorted { $0.scheduledTime < $1.scheduledTime }
                .filter { seen.insert($0.serviceUid).inserted }
        } catch {
            // forecast stays empty; sheet remains open showing empty state
        }

        isForecastLoading = false
    }
}

// MARK: - UIViewRepresentable

struct MapboxMapRepresentable: UIViewRepresentable {
    let userLocation: CLLocationCoordinate2D?
    let stations: [StationWithDepartures]
    let trains: [LiveTrain]
    let selectedStation: StationWithDepartures?
    let droppedPin: CLLocationCoordinate2D?
    let onPinDropped: (CLLocationCoordinate2D) -> Void
    let onStationTapped: (StationWithDepartures) -> Void
    let onTrainTapped: (LiveTrain) -> Void
    let onCentreChanged: (CLLocationCoordinate2D) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(
            onPinDropped: onPinDropped,
            onStationTapped: onStationTapped,
            onTrainTapped: onTrainTapped,
            onCentreChanged: onCentreChanged
        )
    }

    func makeUIView(context: Context) -> MapView {
        let mapView = MapView(frame: .zero, mapInitOptions: MapInitOptions(styleURI: .streets))
        mapView.autoresizingMask = [.flexibleWidth, .flexibleHeight]

        // Open on Scotland immediately, before GPS location arrives
        mapView.mapboxMap.setCamera(to: CameraOptions(
            center: CLLocationCoordinate2D(latitude: 57.0, longitude: -4.0),
            zoom: 7
        ))

        let stationMgr = mapView.annotations.makeCircleAnnotationManager(id: "stations")
        stationMgr.delegate = context.coordinator
        context.coordinator.stationAnnotationManager = stationMgr

        let pinMgr = mapView.annotations.makeCircleAnnotationManager(id: "pin")
        context.coordinator.pinAnnotationManager = pinMgr

        let trainMgr = mapView.annotations.makePointAnnotationManager(id: "live-trains")
        trainMgr.delegate = context.coordinator
        trainMgr.iconRotationAlignment = .map
        context.coordinator.trainAnnotationManager = trainMgr

        let confirmedMgr = mapView.annotations.makePointAnnotationManager(id: "confirmed-trains")
        confirmedMgr.iconRotationAlignment = .map
        context.coordinator.confirmedAnnotationManager = confirmedMgr

        let longPress = UILongPressGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.handleLongPress(_:))
        )
        longPress.minimumPressDuration = 0.5
        mapView.addGestureRecognizer(longPress)

        context.coordinator.mapView = mapView
        context.coordinator.startCentreTimer()

        // Add railway layers and pre-register train images once the style is fully ready
        context.coordinator.styleLoadedCancellable = mapView.mapboxMap.onStyleLoaded.observe { [weak mapView, weak coord = context.coordinator] _ in
            guard let mapView, let coord else { return }
            coord.railLayersAdded = false
            coord.addRailLayers(to: mapView)
            coord.registerTrainImages(to: mapView)
        }

        return mapView
    }

    func updateUIView(_ mapView: MapView, context: Context) {
        context.coordinator.onPinDropped    = onPinDropped
        context.coordinator.onStationTapped = onStationTapped
        context.coordinator.onTrainTapped   = onTrainTapped
        context.coordinator.onCentreChanged = onCentreChanged
        context.coordinator.stations = stations
        context.coordinator.trains   = trains
        context.coordinator.syncStationAnnotations(selectedStation: selectedStation)
        context.coordinator.syncTrainAnnotations()
        context.coordinator.syncPin(coordinate: droppedPin)

        if let loc = userLocation, !context.coordinator.hasSetInitialCamera {
            context.coordinator.hasSetInitialCamera = true
            mapView.camera.ease(
                to: CameraOptions(center: loc, zoom: 8),
                duration: 1.0
            )
        }
    }

    static func dismantleUIView(_ uiView: MapView, coordinator: Coordinator) {
        coordinator.centreTimer?.invalidate()
        coordinator.centreTimer = nil
    }
}

// MARK: - Coordinator

final class Coordinator: NSObject, AnnotationInteractionDelegate {
    var stations: [StationWithDepartures] = []
    var trains: [LiveTrain] = []
    var onPinDropped: (CLLocationCoordinate2D) -> Void
    var onStationTapped: (StationWithDepartures) -> Void
    var onTrainTapped: (LiveTrain) -> Void
    var onCentreChanged: (CLLocationCoordinate2D) -> Void

    var stationAnnotationManager: CircleAnnotationManager?
    var pinAnnotationManager: CircleAnnotationManager?
    var trainAnnotationManager: PointAnnotationManager?
    var confirmedAnnotationManager: PointAnnotationManager?
    var mapView: MapView?

    // Passenger trains (headcodes 1–2): dark→mid→faded blue for live→recent→stale
    private lazy var arrowLive:    UIImage = makeArrowImage(color: UIColor(red: 0.0,  green: 0.18, blue: 0.65, alpha: 1))
    private lazy var arrowRecent:  UIImage = makeArrowImage(color: UIColor(red: 0.20, green: 0.47, blue: 0.85, alpha: 1))
    private lazy var arrowStale:   UIImage = makeArrowImage(color: UIColor(red: 0.53, green: 0.68, blue: 0.82, alpha: 1))
    private lazy var dotLive:      UIImage = makeDotImage(color: UIColor(red: 0.0,  green: 0.18, blue: 0.65, alpha: 1))
    private lazy var dotRecent:    UIImage = makeDotImage(color: UIColor(red: 0.20, green: 0.47, blue: 0.85, alpha: 1))
    private lazy var dotStale:     UIImage = makeDotImage(color: UIColor(red: 0.53, green: 0.68, blue: 0.82, alpha: 1))
    // Freight / ECS trains (headcodes 4–9): red
    private lazy var arrowFreight: UIImage = makeArrowImage(color: UIColor(red: 0.87, green: 0.17, blue: 0.17, alpha: 1))
    private lazy var dotFreight:   UIImage = makeDotImage(color: UIColor(red: 0.87, green: 0.17, blue: 0.17, alpha: 1))

    private func makeArrowImage(color: UIColor) -> UIImage {
        let size = CGSize(width: 20, height: 20)
        return UIGraphicsImageRenderer(size: size).image { ctx in
            let c = ctx.cgContext
            c.setFillColor(color.cgColor)
            c.setStrokeColor(UIColor.white.cgColor)
            c.setLineWidth(1.5)
            let path = CGMutablePath()
            path.move(to: CGPoint(x: 10, y: 1))
            path.addLine(to: CGPoint(x: 17, y: 18))
            path.addLine(to: CGPoint(x: 10, y: 14))
            path.addLine(to: CGPoint(x: 3, y: 18))
            path.closeSubpath()
            c.addPath(path)
            c.drawPath(using: .fillStroke)
        }
    }

    private func makeDotImage(color: UIColor) -> UIImage {
        let size = CGSize(width: 16, height: 16)
        return UIGraphicsImageRenderer(size: size).image { ctx in
            let c = ctx.cgContext
            c.setFillColor(color.cgColor)
            c.setStrokeColor(UIColor.white.cgColor)
            c.setLineWidth(1.5)
            c.fillEllipse(in: CGRect(x: 1.75, y: 1.75, width: 12.5, height: 12.5))
            c.strokeEllipse(in: CGRect(x: 1.75, y: 1.75, width: 12.5, height: 12.5))
        }
    }

    private func freshnessTag(_ age: Int) -> String {
        if age < 90  { return "live" }
        if age < 300 { return "recent" }
        return "stale"
    }

    private func arrowImage(for freshness: String) -> UIImage {
        switch freshness {
        case "live":   return arrowLive
        case "recent": return arrowRecent
        default:       return arrowStale
        }
    }

    private func dotImage(for freshness: String) -> UIImage {
        switch freshness {
        case "live":   return dotLive
        case "recent": return dotRecent
        default:       return dotStale
        }
    }
    var hasSetInitialCamera = false
    var centreTimer: Timer?
    var railLayersAdded = false
    var styleLoadedCancellable: AnyCancelable?

    init(
        onPinDropped: @escaping (CLLocationCoordinate2D) -> Void,
        onStationTapped: @escaping (StationWithDepartures) -> Void,
        onTrainTapped: @escaping (LiveTrain) -> Void,
        onCentreChanged: @escaping (CLLocationCoordinate2D) -> Void
    ) {
        self.onPinDropped    = onPinDropped
        self.onStationTapped = onStationTapped
        self.onTrainTapped   = onTrainTapped
        self.onCentreChanged = onCentreChanged
    }

    func addRailLayers(to mapView: MapView) {
        guard !railLayersAdded else { return }

        let layerCount = mapView.mapboxMap.allLayerIdentifiers.count
        guard layerCount > 3 else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self, weak mapView] in
                guard let self, let mapView else { return }
                self.addRailLayers(to: mapView)
            }
            return
        }

        let railColor = StyleColor(UIColor(red: 0.8, green: 0.0, blue: 0.0, alpha: 1))
        for id in ["road-rail", "bridge-rail"] {
            do {
                try mapView.mapboxMap.updateLayer(withId: id, type: LineLayer.self) { layer in
                    layer.lineColor = .constant(railColor)
                    layer.lineWidth = .expression(Exp(.interpolate) {
                        Exp(.linear)
                        Exp(.zoom)
                        5; 1.0
                        10; 2.0
                        14; 4.0
                    })
                    layer.lineOpacity = .constant(1.0)
                    layer.minZoom = 5
                }
                print("[MAP] Updated \(id) OK")
            } catch {
                print("[MAP] Failed to update \(id): \(error)")
            }
        }
        railLayersAdded = true

        let motorwayIds = mapView.mapboxMap.allLayerIdentifiers
            .map { $0.id }
            .filter { $0.localizedCaseInsensitiveContains("motor") || $0.localizedCaseInsensitiveContains("trunk") }
        print("[MAP] Rail updated. Motorway-related layers: \(motorwayIds)")
    }

    func registerTrainImages(to mapView: MapView) {
        let images: [(UIImage, String)] = [
            (arrowLive,    "train-arrow-live"),
            (arrowRecent,  "train-arrow-recent"),
            (arrowStale,   "train-arrow-stale"),
            (dotLive,      "train-dot-live"),
            (dotRecent,    "train-dot-recent"),
            (dotStale,     "train-dot-stale"),
            (arrowFreight, "train-arrow-freight"),
            (dotFreight,   "train-dot-freight"),
        ]
        for (image, name) in images {
            try? mapView.mapboxMap.addImage(image, id: name)
        }
    }

    func startCentreTimer() {
        centreTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self, let mapView = self.mapView else { return }
            self.onCentreChanged(mapView.mapboxMap.cameraState.center)
        }
    }

    // MARK: Station annotations

    func syncStationAnnotations(selectedStation: StationWithDepartures?) {
        stationAnnotationManager?.annotations = stations.map { item in
            let isSelected = selectedStation?.id == item.id
            var a = CircleAnnotation(id: "s-\(item.station.id)", centerCoordinate: item.station.coordinate)
            a.circleColor = StyleColor(isSelected ? UIColor.systemOrange : UIColor.systemBlue)
            a.circleRadius = isSelected ? 14 : 10
            a.circleStrokeColor = StyleColor(.white)
            a.circleStrokeWidth = 2
            return a
        }
    }

    // MARK: Train annotations

    func syncTrainAnnotations() {
        trainAnnotationManager?.annotations = trains.map { train in
            let isFreight = train.headcode.first.map { $0 >= "4" && $0 <= "9" } ?? false
            var a = PointAnnotation(
                id: train.id,
                coordinate: CLLocationCoordinate2D(latitude: train.lat, longitude: train.lon)
            )
            if isFreight {
                if let bearing = train.bearing {
                    a.image = .init(image: arrowFreight, name: "train-arrow-freight")
                    a.iconRotate = bearing
                } else {
                    a.image = .init(image: dotFreight, name: "train-dot-freight")
                }
            } else {
                let freshness = freshnessTag(train.ageSeconds)
                if let bearing = train.bearing {
                    a.image = .init(image: arrowImage(for: freshness), name: "train-arrow-\(freshness)")
                    a.iconRotate = bearing
                } else {
                    a.image = .init(image: dotImage(for: freshness), name: "train-dot-\(freshness)")
                }
            }
            return a
        }
        confirmedAnnotationManager?.annotations = []
    }

    // MARK: Pin annotation

    func syncPin(coordinate: CLLocationCoordinate2D?) {
        guard let coordinate else {
            pinAnnotationManager?.annotations = []
            return
        }
        var a = CircleAnnotation(id: "dropped-pin", centerCoordinate: coordinate)
        a.circleColor = StyleColor(.systemRed)
        a.circleRadius = 14
        a.circleStrokeColor = StyleColor(.white)
        a.circleStrokeWidth = 3
        pinAnnotationManager?.annotations = [a]
    }

    // MARK: Tap handling

    func annotationManager(_ manager: any AnnotationManager, didDetectTappedAnnotations annotations: [any Annotation]) {
        DispatchQueue.main.async {
            if manager.id == "stations" {
                guard let first = annotations.first as? CircleAnnotation,
                      let tapped = self.stations.first(where: { "s-\($0.station.id)" == first.id }) else { return }
                self.onStationTapped(tapped)
            } else if manager.id == "live-trains" {
                guard let first = annotations.first as? PointAnnotation,
                      let tapped = self.trains.first(where: { $0.id == first.id }) else { return }
                self.onTrainTapped(tapped)
            }
        }
    }

    // MARK: Long press

    @objc func handleLongPress(_ gesture: UILongPressGestureRecognizer) {
        guard gesture.state == .began, let mapView else { return }
        let point = gesture.location(in: mapView)
        let coordinate = mapView.mapboxMap.coordinate(for: point)
        onPinDropped(coordinate)
    }
}

// MARK: - Station departures sheet

private struct StationDeparturesSheet: View {
    let stationWithDepartures: StationWithDepartures

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            dragHandle
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(stationWithDepartures.station.name).font(.title2.bold())
                    Text(stationWithDepartures.station.crs).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Text(formattedDistance(stationWithDepartures.station.distance))
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            .padding(.horizontal)
            Divider().padding(.vertical, 12)
            if stationWithDepartures.departures.isEmpty {
                Text("No departures found").foregroundStyle(.secondary).padding(.horizontal)
            } else {
                let next2 = Array(stationWithDepartures.departures.prefix(2))
                ForEach(next2) { departure in
                    DepartureRow(departure: departure).padding(.horizontal).padding(.vertical, 8)
                    if departure.id != next2.last?.id { Divider().padding(.leading) }
                }
            }
            Spacer()
        }
    }

    private var dragHandle: some View {
        RoundedRectangle(cornerRadius: 2)
            .fill(Color.secondary.opacity(0.4))
            .frame(width: 36, height: 4)
            .frame(maxWidth: .infinity)
            .padding(.top, 8).padding(.bottom, 16)
    }

    private func formattedDistance(_ metres: Double) -> String {
        metres < 1000 ? String(format: "%.0fm", metres) : String(format: "%.1fkm", metres / 1000)
    }
}

// MARK: - Passing forecast sheet

struct ForecastSheet: View {
    let forecast: [PassingTrain]
    let isLoading: Bool

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Finding passing trains…")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if forecast.isEmpty {
                    ContentUnavailableView(
                        "No trains found",
                        systemImage: "train.side.front.car",
                        description: Text("No trains are scheduled to pass this location in the next hour.")
                    )
                } else {
                    List(forecast) { train in
                        PassingTrainRow(train: train)
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Passing forecast")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct PassingTrainRow: View {
    let train: PassingTrain

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .trailing, spacing: 2) {
                Text(train.scheduledTime)
                    .font(.system(.body, design: .monospaced).bold())
                    .strikethrough(train.isCancelled, color: .red)
                if train.isCancelled {
                    Text("Cancelled").font(.system(.caption2, design: .monospaced)).foregroundStyle(.red)
                } else if let est = train.estimatedTime {
                    Text(est).font(.system(.caption, design: .monospaced)).foregroundStyle(.orange)
                }
                Text("@ \(train.stationName)")
                    .font(.system(.caption2, design: .default))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            .frame(width: 80, alignment: .trailing)

            Divider().frame(height: 36)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Link(destination: URL(string: "https://www.realtimetrains.co.uk/search/handler?qsearch=\(train.headcode)")!) {
                        HStack(spacing: 3) {
                            Text(train.headcode)
                            Image(systemName: "arrow.up.right.square")
                        }
                        .font(.system(.caption, design: .monospaced).weight(.semibold))
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(Color.secondary.opacity(0.15))
                        .clipShape(RoundedRectangle(cornerRadius: 4))
                    }
                    if train.isDelayed {
                        Image(systemName: "clock.badge.exclamationmark.fill")
                            .foregroundStyle(.orange).font(.caption)
                    }
                }
                Text("\(train.origin) → \(train.destination)")
                    .font(.subheadline)
                    .foregroundStyle(train.isCancelled ? .secondary : .primary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 4)
        .opacity(train.isCancelled ? 0.5 : 1)
    }
}

// MARK: - Live train detail sheet

private struct TrainDetailSheet: View {
    let train: LiveTrain
    let trainService: TrainService
    let userLocation: CLLocationCoordinate2D?

    @State private var route: (origin: String, destination: String)? = nil
    @State private var isLoadingRoute = true

    var body: some View {
        VStack(spacing: 0) {
            dragHandle
            VStack(spacing: 16) {
                Text(train.headcode)
                    .font(.system(.largeTitle, design: .monospaced).bold())

                if isLoadingRoute {
                    ProgressView().tint(.secondary)
                } else if let route {
                    VStack(spacing: 4) {
                        Text("\(route.origin) → \(route.destination)")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                        Link(destination: URL(string: "https://www.realtimetrains.co.uk/search/handler?qsearch=\(train.headcode)")!) {
                            Label("RTT", systemImage: "arrow.up.right.square")
                                .font(.caption)
                        }
                    }
                } else if isFreight {
                    Text("Freight")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                } else {
                    Link(destination: URL(string: "https://www.realtimetrains.co.uk/search/handler?qsearch=\(train.headcode)")!) {
                        Label("Look up on RTT", systemImage: "arrow.up.right.square")
                            .font(.subheadline)
                    }
                }

                Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 8) {
                    GridRow {
                        label("Area / Berth")
                        value("\(train.areaId) · \(train.berth)")
                    }
                    if let loc = userLocation {
                        GridRow {
                            label("Distance")
                            value(distanceFromUser(to: train, from: loc))
                        }
                    }
                    GridRow {
                        label("Last seen")
                        value(ageString(train.ageSeconds))
                    }
                }
            }
            .padding()
            Spacer()
        }
        .task {
            route = try? await trainService.fetchRoute(headcode: train.headcode, lat: train.lat, lon: train.lon)
            isLoadingRoute = false
        }
    }

    private var isFreight: Bool {
        train.headcode.first.map { $0 >= "4" && $0 <= "9" } ?? false
    }

    private var dragHandle: some View {
        RoundedRectangle(cornerRadius: 2)
            .fill(Color.secondary.opacity(0.4))
            .frame(width: 36, height: 4)
            .frame(maxWidth: .infinity)
            .padding(.top, 8).padding(.bottom, 16)
    }

    private func label(_ text: String) -> some View {
        Text(text).font(.caption).foregroundStyle(.secondary)
    }

    private func value(_ text: String) -> some View {
        Text(text).font(.subheadline.weight(.medium))
    }

    private func ageString(_ seconds: Int) -> String {
        seconds < 60 ? "\(seconds)s ago" : "\(seconds / 60)m ago"
    }

    private func distanceFromUser(to train: LiveTrain, from loc: CLLocationCoordinate2D) -> String {
        let userLoc = CLLocation(latitude: loc.latitude, longitude: loc.longitude)
        let trainLoc = CLLocation(latitude: train.lat, longitude: train.lon)
        let metres = userLoc.distance(from: trainLoc)
        return metres < 1000 ? String(format: "%.0f m", metres) : String(format: "%.1f km", metres / 1000)
    }
}
