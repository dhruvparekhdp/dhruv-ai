import SwiftUI

@main
struct JarvisApp: App {

    @Environment(\.scenePhase) private var scenePhase
    @State private var lock = AppLockCoordinator()
    @State private var connection = Connection()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(lock)
                .environment(connection)
                .privacyBlur(active: lock.state != .unlocked)
                .fullScreenCover(isPresented: .constant(lock.state != .unlocked)) {
                    LockScreen()
                        .environment(lock)
                        .interactiveDismissDisabled()
                }
        }
        .onChange(of: scenePhase) { _, phase in
            lock.onScenePhase(phase)
            // Opening the app is itself a signal ("he's looking at his phone"),
            // throttled so that ten opens an hour aren't ten reports.
            if phase == .active {
                Task { await DeviceReporter.reportIfDue(via: "app_open") }
            }
        }
    }
}
