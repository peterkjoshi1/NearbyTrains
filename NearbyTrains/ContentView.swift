//
//  ContentView.swift
//  NearbyTrains
//

import SwiftUI
import CoreLocation

@Observable
class AppViewModel {
    var stations: [StationWithDepartures] = []
    var isLoading = false
    var errorMessage: String?
    var userLocation: CLLocationCoordinate2D?

    private let locationManager = LocationManager()
    private let stationFinder = StationFinder()
    private let trainService = TrainService()

    func load() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil

        do {
            let location = try await locationManager.requestLocation()
            userLocation = location.coordinate
            let nearby = try await stationFinder.findStations(near: location)
            let top = Array(nearby.prefix(5))

            var result: [StationWithDepartures] = []
            await withTaskGroup(of: StationWithDepartures.self) { group in
                for station in top {
                    group.addTask {
                        let departures = (try? await self.trainService.fetchDepartures(for: station.crs)) ?? []
                        return StationWithDepartures(station: station, departures: departures)
                    }
                }
                for await item in group {
                    result.append(item)
                }
            }
            result.sort { $0.station.distance < $1.station.distance }
            stations = result
        } catch {
            errorMessage = error.localizedDescription
        }

        isLoading = false
    }
}

// MARK: - Root view

struct ContentView: View {
    @State private var viewModel = AppViewModel()

    var body: some View {
        TabView {
            TrainsListView(viewModel: viewModel)
                .tabItem { Label("Trains", systemImage: "train.side.front.car") }

            StationMapView(viewModel: viewModel)
                .tabItem { Label("Map", systemImage: "map") }

            DebugView()
                .tabItem { Label("Debug", systemImage: "ant") }
        }
        .task {
            await viewModel.load()
        }
    }
}

// MARK: - List tab

struct TrainsListView: View {
    let viewModel: AppViewModel

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.isLoading {
                    ProgressView("Finding nearby stations...")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let error = viewModel.errorMessage {
                    ContentUnavailableView(error, systemImage: "exclamationmark.triangle")
                } else if viewModel.stations.isEmpty {
                    ContentUnavailableView("No stations found nearby", systemImage: "train.side.front.car")
                } else {
                    List {
                        ForEach(viewModel.stations) { item in
                            Section {
                                if item.departures.isEmpty {
                                    Text("No departures found")
                                        .foregroundStyle(.secondary)
                                        .font(.subheadline)
                                } else {
                                    ForEach(item.departures) { departure in
                                        DepartureRow(departure: departure)
                                    }
                                }
                            } header: {
                                HStack {
                                    Text(item.station.name)
                                        .font(.headline)
                                        .foregroundStyle(.primary)
                                    Spacer()
                                    Text(formattedDistance(item.station.distance))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Nearby Trains")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await viewModel.load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(viewModel.isLoading)
                }
            }
        }
    }

    private func formattedDistance(_ metres: Double) -> String {
        metres < 1000
            ? String(format: "%.0fm", metres)
            : String(format: "%.1fkm", metres / 1000)
    }
}

// MARK: - Departure row (shared between list and map popup)

struct DepartureRow: View {
    let departure: Departure

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(departure.destination)
                    .font(.body)
                if let platform = departure.platform {
                    Text("Platform \(platform)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(departure.scheduledDeparture)
                    .font(.system(.body, design: .monospaced).bold())
                if let expected = departure.expectedDeparture {
                    Text(expected)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.orange)
                }
            }
        }
    }
}

#Preview {
    ContentView()
}
