//
//  StationFinder.swift
//  NearbyTrains
//

import CoreLocation
import Foundation

struct StationFinder {
    private let overpassURL = URL(string: "https://overpass-api.de/api/interpreter")!

    func findStations(near location: CLLocation) async throws -> [Station] {
        let lat = location.coordinate.latitude
        let lon = location.coordinate.longitude

        // Union of stations tagged network=National Rail OR stations that have a CRS code
        let query = """
        [out:json][timeout:25];
        (
          node["railway"="station"]["network"="National Rail"](around:10000,\(lat),\(lon));
          node["railway"="station"]["ref:crs"](around:10000,\(lat),\(lon));
        );
        out body;
        """

        var request = URLRequest(url: overpassURL)
        request.httpMethod = "POST"
        let body = "data=" + (query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")
        request.httpBody = body.data(using: .utf8)
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")

        let (data, _) = try await URLSession.shared.data(for: request)
        let response = try JSONDecoder().decode(OverpassResponse.self, from: data)

        // Deduplicate by OSM id, require a CRS code
        var seen = Set<Int>()
        var stations: [Station] = []
        for element in response.elements {
            guard !seen.contains(element.id),
                  let name = element.tags["name"],
                  let crs = element.tags["ref:crs"] else { continue }
            seen.insert(element.id)
            let stationLocation = CLLocation(latitude: element.lat, longitude: element.lon)
            let distance = location.distance(from: stationLocation)
            stations.append(Station(
                id: element.id,
                name: name,
                crs: crs,
                coordinate: CLLocationCoordinate2D(latitude: element.lat, longitude: element.lon),
                distance: distance
            ))
        }
        return stations.sorted { $0.distance < $1.distance }
    }
}

private struct OverpassResponse: Decodable {
    let elements: [OverpassElement]
}

private struct OverpassElement: Decodable {
    let id: Int
    let lat: Double
    let lon: Double
    let tags: [String: String]
}
