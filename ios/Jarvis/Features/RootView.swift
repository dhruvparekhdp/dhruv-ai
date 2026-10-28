import SwiftUI

struct RootView: View {

    @Environment(Connection.self) private var connection

    var body: some View {
        if connection.isConfigured {
            TabView {
                Tab("Today", systemImage: "sun.max") { TodayView() }
                Tab("Chat", systemImage: "bubble.left.and.bubble.right") { ChatView() }
                Tab("Todos", systemImage: "checklist") { TodosView() }
                Tab("Approvals", systemImage: "checkmark.shield") { ApprovalsView() }
                Tab("Settings", systemImage: "gearshape") {
                    NavigationStack { SettingsView() }
                }
            }
        } else {
            // Nothing works without a server, so setup is the whole app until
            // it's done — not a tab you have to find.
            NavigationStack { SettingsView(isOnboarding: true) }
        }
    }
}
