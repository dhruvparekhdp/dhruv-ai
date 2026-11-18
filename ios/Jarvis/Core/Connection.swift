import Foundation
import Observation

/// Where Jarvis lives and the passcode that proves it's you.
///
/// The address goes in UserDefaults — it isn't secret, it's on your tailnet.
/// The passcode goes in the Keychain. Both are read through `ConnectionStore`
/// so App Intents, which run without any SwiftUI environment, see exactly what
/// the app sees.
enum ConnectionStore {
    private static let urlKey = "jarvis.serverURL"
    private static let passcodeKey = "jarvis.passcode"
    private static let keychain = KeychainStore()

    static var serverURL: String? {
        UserDefaults.standard.string(forKey: urlKey)
    }

    static var storedPasscode: String? {
        keychain.load(forKey: passcodeKey)
    }

    /// A ready client, or nil if setup isn't finished.
    static func client() -> JarvisClient? {
        guard let raw = serverURL, let url = normalise(raw),
              let passcode = keychain.load(forKey: passcodeKey), !passcode.isEmpty
        else { return nil }
        return JarvisClient(baseURL: url, passcode: passcode)
    }

    static func requireClient() throws -> JarvisClient {
        guard let client = client() else { throw JarvisError.notConfigured }
        return client
    }

    static func save(serverURL raw: String, passcode: String?) throws {
        guard let url = normalise(raw) else { throw JarvisError.notConfigured }
        UserDefaults.standard.set(url.absoluteString, forKey: urlKey)
        if let passcode, !passcode.isEmpty {
            try keychain.save(passcode, forKey: passcodeKey)
        }
        // A new server means the old chat session id means nothing there.
        UserDefaults.standard.removeObject(forKey: JarvisClient.sessionKey)
    }

    static func forget() {
        UserDefaults.standard.removeObject(forKey: urlKey)
        UserDefaults.standard.removeObject(forKey: JarvisClient.sessionKey)
        try? keychain.delete(forKey: passcodeKey)
    }

    /// Accepts what people actually type: "dhruv-ai:8000", "https://x.ts.net/",
    /// " https://x.ts.net/api ". Returns nil for anything that isn't http(s).
    ///
    /// No scheme defaults to https, because the normal address is the Funnel
    /// URL. The exception is a bare tailnet name or IP *with a port* —
    /// "dhruv-ai:8000", "100.64.0.7:8000" — which is the host itself,
    /// serving plain http on your private tailnet.
    static func normalise(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if !text.contains("://") {
            let authority = text.split(separator: "/").first.map(String.init) ?? text
            let name = authority.split(separator: ":").first.map(String.init) ?? authority
            let hasPort = authority.contains(":")
            let isIPv4 = !name.isEmpty && name.allSatisfy { $0.isNumber || $0 == "." }
            let isPrivateHost = hasPort && (!name.contains(".") || isIPv4)
            text = (isPrivateHost ? "http://" : "https://") + text
        }
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text),
              let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = url.host(), !host.isEmpty
        else { return nil }
        return url
    }
}

/// The observable face of `ConnectionStore`, for views.
@MainActor
@Observable
final class Connection {
    private(set) var isConfigured: Bool
    private(set) var serverURL: String

    init() {
        serverURL = ConnectionStore.serverURL ?? ""
        isConfigured = ConnectionStore.client() != nil
    }

    func save(serverURL: String, passcode: String?) throws {
        try ConnectionStore.save(serverURL: serverURL, passcode: passcode)
        refresh()
    }

    func forget() {
        ConnectionStore.forget()
        refresh()
    }

    private func refresh() {
        serverURL = ConnectionStore.serverURL ?? ""
        isConfigured = ConnectionStore.client() != nil
    }
}
