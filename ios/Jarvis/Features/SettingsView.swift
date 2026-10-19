import SwiftUI

struct SettingsView: View {

    var isOnboarding = false

    @Environment(Connection.self) private var connection
    @Environment(AppLockCoordinator.self) private var lock

    @State private var url = ""
    @State private var passcode = ""
    @State private var status: String?
    @State private var testing = false
    @State private var reportStatus: String?
    @AppStorage(DeviceReporter.deviceNameKey) private var deviceName = ""

    var body: some View {
        Form {
            Section {
                TextField("https://dhruv-ai.your-tailnet.ts.net", text: $url)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                SecureField(connection.isConfigured ? "Passcode (blank keeps the saved one)" : "Passcode",
                            text: $passcode)
                Button(testing ? "Testing…" : "Save and test") {
                    Task { await saveAndTest() }
                }
                .disabled(testing || url.trimmingCharacters(in: .whitespaces).isEmpty)
                if let status {
                    Text(status).font(.subheadline)
                }
            } header: {
                Text("Server")
            } footer: {
                Text("Your Funnel URL, or http://dhruv-ai:8000 over Tailscale. The passcode is JARVIS_PASSCODE from the host's .env; it's kept in this phone's Keychain and never leaves it except to Jarvis.")
            }

            if connection.isConfigured {
                Section {
                    TextField("Name", text: $deviceName, prompt: Text("iPhone"))
                    Button("Report now") { Task { await reportNow() } }
                    if let reportStatus {
                        Text(reportStatus).font(.caption.monospaced())
                    }
                    if let last = DeviceReporter.lastReportAt {
                        LabeledContent("Last report", value: last.formatted(date: .abbreviated, time: .shortened))
                    }
                } header: {
                    Text("This phone")
                } footer: {
                    Text("Battery, power mode, network and free space — nothing that needs a permission. Sent when the app opens (at most every 30 minutes) and whenever a Shortcuts automation runs \"Report to Jarvis\".")
                }

                Section {
                    Toggle("Lock with \(lock.biometry.displayName)", isOn: Binding(
                        get: { lock.isEnabled },
                        set: { lock.isEnabled = $0 }
                    ))
                } header: {
                    Text("Security")
                } footer: {
                    Text("Approving a dangerous action always asks for Face ID, whether or not this is on.")
                }

                Section("Siri and Shortcuts") {
                    Text("\"Hey Siri, ask Jarvis\" · \"What's next in Jarvis\" · \"Add a todo to Jarvis\" · \"Log my mood in Jarvis\"")
                        .font(.subheadline)
                    Text("For automations: Shortcuts › Automation › choose a trigger (arrive somewhere, connect a charger, a time of day) › add the \"Report to Jarvis\" action, and turn off \"Ask Before Running\".")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }

                Section {
                    Button("Forget this server", role: .destructive) {
                        connection.forget()
                        url = ""
                        status = nil
                    }
                }
            }
        }
        .navigationTitle(isOnboarding ? "Connect to Jarvis" : "Settings")
        .onAppear { url = connection.serverURL }
    }

    /// Tests the typed values *before* saving them, so a typo never replaces a
    /// working configuration.
    private func saveAndTest() async {
        testing = true
        defer { testing = false }

        guard let base = ConnectionStore.normalise(url) else {
            status = "That doesn't look like an address."
            return
        }
        let typed = passcode.isEmpty ? nil : passcode
        guard let candidate = typed ?? ConnectionStore.storedPasscode else {
            status = "Enter the passcode."
            return
        }

        do {
            let summary = try await JarvisClient(baseURL: base, passcode: candidate).verify()
            try connection.save(serverURL: url, passcode: typed)
            passcode = ""
            url = connection.serverURL
            status = "Connected. \(summary.verdict)"
        } catch {
            status = error.localizedDescription
        }
    }

    private func reportNow() async {
        do {
            let metrics = try await DeviceReporter.report(via: "manual")
            reportStatus = metrics.keys.sorted().joined(separator: ", ")
        } catch {
            reportStatus = error.localizedDescription
        }
    }
}
