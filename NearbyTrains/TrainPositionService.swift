//
//  TrainPositionService.swift
//  NearbyTrains
//

import Foundation
import CoreLocation

@Observable
@MainActor
final class TrainPositionService {
    static let serverBase = "http://192.168.1.102:8080"

    var trains: [LiveTrain] = []

    private var centre: CLLocationCoordinate2D?
    private var pollingTask: Task<Void, Never>?

    func updateCentre(_ newCentre: CLLocationCoordinate2D) {
        centre = newCentre
        if pollingTask == nil {
            startPolling()
        }
    }

    func stop() {
        pollingTask?.cancel()
        pollingTask = nil
    }

    private func startPolling() {
        pollingTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                await self.fetch()
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    private func fetch() async {
        guard let centre else { return }
        let urlString = "\(Self.serverBase)/trains?lat=\(centre.latitude)&lon=\(centre.longitude)&radius=300"
        guard let url = URL(string: urlString) else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            trains = try decoder.decode([LiveTrain].self, from: data)
        } catch {
            // keep stale data; server may be offline
        }
    }
}
