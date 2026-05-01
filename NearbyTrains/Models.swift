//
//  Models.swift
//  NearbyTrains
//

import Foundation
import CoreLocation

struct Station: Identifiable {
    let id: Int  // OSM element ID
    let name: String
    let crs: String
    let coordinate: CLLocationCoordinate2D
    let distance: Double  // metres
}

struct Departure: Identifiable {
    let id: String  // serviceUid
    let destination: String
    let scheduledDeparture: String  // "HH:mm"
    let expectedDeparture: String?
    let platform: String?
}

struct StationWithDepartures: Identifiable {
    let station: Station
    var departures: [Departure]
    var id: Int { station.id }
}

struct PassingTrain: Identifiable {
    let id: String          // uniqueIdentity + "-" + crs, for SwiftUI list identity
    let serviceUid: String  // uniqueIdentity only, used for cross-station deduplication
    let headcode: String
    let origin: String
    let destination: String
    let scheduledTime: String  // "HH:mm"
    let estimatedTime: String? // "HH:mm", only present when different from scheduled
    let isCancelled: Bool
    let stationName: String

    var isDelayed: Bool { estimatedTime != nil && !isCancelled }
}

struct LiveTrain: Identifiable, Decodable {
    let headcode: String
    let lat: Double
    let lon: Double
    let areaId: String      // snake_case → camelCase via .convertFromSnakeCase
    let berth: String
    let bearing: Double?    // nil = only one position known, no direction yet
    let ageSeconds: Int
    let distanceKm: Double

    var id: String { "\(headcode)-\(areaId)-\(berth)" }
}
