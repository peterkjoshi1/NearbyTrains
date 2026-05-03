//
//  NearbyTrainsApp.swift
//  NearbyTrains
//

import SwiftUI
import MapboxMaps

@main
struct NearbyTrainsApp: App {
    init() {
        // Set your Mapbox token here. Do not commit — use git update-index --assume-unchanged on this file.
        MapboxOptions.accessToken = "YOUR_MAPBOX_TOKEN"
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
