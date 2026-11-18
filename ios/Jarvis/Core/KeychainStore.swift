import Foundation
import Security

/// The one place that touches Security.framework. Adapted from the crypto
/// app's port (crypto-signal-engine/ios-port), which is where this was first
/// written and reviewed.
///
/// `kSecAttrAccessibleAfterFirstUnlockThisDevice`: readable once the phone has
/// been unlocked since boot, so a Shortcuts automation can use the passcode
/// while the phone sits locked in a pocket. `ThisDevice` means it never syncs
/// to iCloud Keychain and never reappears on another phone.
///
/// Not the biometry-gated variant on purpose: Siri and automations run with no
/// UI to show a Face ID prompt over, and every one of them would fail.
struct KeychainStore {
    enum KeychainError: Error {
        case unexpectedStatus(OSStatus)
        case unableToEncode
    }

    private let service: String

    init(service: String = "com.dhruvparekh.jarvis") {
        self.service = service
    }

    func save(_ value: String, forKey key: String) throws {
        guard let data = value.data(using: .utf8) else { throw KeychainError.unableToEncode }
        try delete(forKey: key)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDevice,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.unexpectedStatus(status) }
    }

    func load(forKey key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    func delete(forKey key: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key,
        ]
        let status = SecItemDelete(query as CFDictionary)
        // Deleting something already gone is the desired end state either way.
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.unexpectedStatus(status)
        }
    }
}
