//
//  TrainService.swift
//  NearbyTrains
//

import CoreLocation
import Foundation

actor TrainService {
    private let refreshToken = Bundle.main.object(forInfoDictionaryKey: "RTTRefreshToken") as? String ?? ""
    private let baseURL = "https://data.rtt.io"

    private var cachedAccessToken: String?
    private var tokenExpiry: Date?

    // MARK: - Public API

    func fetchDepartures(for crs: String) async throws -> [Departure] {
        let token = try await accessToken()

        guard let url = URL(string: "\(baseURL)/gb-nr/location?code=\(crs)") else { return [] }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { return [] }

        let rttResponse = try JSONDecoder().decode(RTTLocationResponse.self, from: data)
        return (rttResponse.services ?? []).prefix(5).compactMap { service in
            guard let dep = service.temporalData.departure,
                  !dep.isCancelled,
                  let destination = service.destination.first?.location.description else { return nil }
            let scheduled = formatTime(dep.scheduleAdvertised)
            let expected: String? = dep.realtimeForecast.map { formatTime($0) }
            let platform = service.locationMetadata?.platform?.planned
            return Departure(
                id: service.scheduleMetadata.uniqueIdentity,
                destination: destination,
                scheduledDeparture: scheduled,
                expectedDeparture: expected != scheduled ? expected : nil,
                platform: platform
            )
        }
    }

    func fetchPassingTrains(for crs: String, stationName: String = "") async throws -> [PassingTrain] {
        let token = try await accessToken()

        guard let url = URL(string: "\(baseURL)/gb-nr/location?code=\(crs)") else { return [] }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { return [] }

        let rttResponse = try JSONDecoder().decode(RTTLocationResponse.self, from: data)
        return (rttResponse.services ?? []).compactMap { service in
            // Prefer departure time; fall back to arrival for terminus calls
            let timing = service.temporalData.departure ?? service.temporalData.arrival
            guard let timing else { return nil }

            let uid = service.scheduleMetadata.uniqueIdentity
            let headcode = service.scheduleMetadata.trainReportingIdentity ?? uid
            let origin = service.origin.first?.location.description ?? "Unknown"
            let dest = service.destination.first?.location.description ?? "Unknown"
            let scheduled = formatTime(timing.scheduleAdvertised)
            let estimated = timing.realtimeForecast.map { formatTime($0) }
            let estimatedIfLate = (estimated != nil && estimated != scheduled) ? estimated : nil

            return PassingTrain(
                id: "\(uid)-\(crs)",
                serviceUid: uid,
                headcode: headcode,
                origin: origin,
                destination: dest,
                scheduledTime: scheduled,
                estimatedTime: estimatedIfLate,
                isCancelled: timing.isCancelled,
                stationName: stationName
            )
        }
    }

    /// Looks up a train's origin and destination by finding stations near the train's
    /// position and searching their passing trains lists for the matching headcode.
    func fetchRoute(headcode: String, lat: Double, lon: Double) async throws -> (origin: String, destination: String)? {
        let location = CLLocation(latitude: lat, longitude: lon)
        let nearby = try await StationFinder().findStations(near: location)
        for station in nearby.prefix(3) {
            let trains = try await fetchPassingTrains(for: station.crs)
            if let match = trains.first(where: { $0.headcode == headcode }) {
                return (match.origin, match.destination)
            }
        }
        return nil
    }

    // MARK: - Token management

    private func accessToken() async throws -> String {
        if let token = cachedAccessToken, let expiry = tokenExpiry, expiry > Date() {
            return token
        }
        return try await refreshAccessToken()
    }

    private func refreshAccessToken() async throws -> String {
        guard let url = URL(string: "\(baseURL)/api/get_access_token") else {
            throw RTTError.invalidURL
        }
        var request = URLRequest(url: url)
        request.setValue("Bearer \(refreshToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw RTTError.tokenRefreshFailed
        }

        let tokenResponse = try JSONDecoder().decode(RTTTokenResponse.self, from: data)
        cachedAccessToken = tokenResponse.token

        // Parse validUntil, fall back to 20 minutes from now
        let formatter = ISO8601DateTimeFormatter()
        tokenExpiry = formatter.date(from: tokenResponse.validUntil) ?? Date().addingTimeInterval(1200)

        return tokenResponse.token
    }

    // MARK: - Helpers

    private func formatTime(_ iso: String) -> String {
        // Input: "2026-04-27T14:24:00" or "2026-04-27T14:24:00+01:00"
        // Extract HH:mm from the time component
        let timePart = iso.components(separatedBy: "T").last ?? iso
        let hhmm = timePart.prefix(5)
        return String(hhmm)
    }

    enum RTTError: LocalizedError {
        case invalidURL, tokenRefreshFailed
        var errorDescription: String? {
            switch self {
            case .invalidURL: return "Invalid RTT API URL."
            case .tokenRefreshFailed: return "Failed to obtain RTT access token."
            }
        }
    }
}

// MARK: - ISO 8601 with offset support

private struct ISO8601DateTimeFormatter {
    private let formatters: [DateFormatter] = {
        let formats = ["yyyy-MM-dd'T'HH:mm:ssZ", "yyyy-MM-dd'T'HH:mm:ss"]
        return formats.map {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = $0
            return f
        }
    }()

    func date(from string: String) -> Date? {
        formatters.lazy.compactMap { $0.date(from: string) }.first
    }
}

// MARK: - Token response

private struct RTTTokenResponse: Decodable {
    let token: String
    let validUntil: String
}

// MARK: - Departures response

private struct RTTLocationResponse: Decodable {
    let services: [RTTService]?
}

private struct RTTService: Decodable {
    let temporalData: RTTTemporalData
    let locationMetadata: RTTLocationMetadata?
    let scheduleMetadata: RTTScheduleMetadata
    let origin: [RTTEndpoint]
    let destination: [RTTEndpoint]
}

private struct RTTTemporalData: Decodable {
    let arrival: RTTTiming?
    let departure: RTTTiming?
}

private struct RTTTiming: Decodable {
    let scheduleAdvertised: String
    let realtimeForecast: String?
    let isCancelled: Bool
}

private struct RTTLocationMetadata: Decodable {
    let platform: RTTPlatform?
}

private struct RTTPlatform: Decodable {
    let planned: String?
}

private struct RTTScheduleMetadata: Decodable {
    let uniqueIdentity: String
    let trainReportingIdentity: String?
}

private struct RTTEndpoint: Decodable {
    let location: RTTEndpointLocation
}

private struct RTTEndpointLocation: Decodable {
    let description: String
}

