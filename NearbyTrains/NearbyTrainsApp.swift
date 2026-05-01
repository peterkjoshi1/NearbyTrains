//
//  NearbyTrainsApp.swift
//  NearbyTrains
//

import SwiftUI
import MapboxMaps

@main
struct NearbyTrainsApp: App {
    init() {
        MapboxOptions.accessToken = "YOUR_MAPBOX_TOKEN"
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
