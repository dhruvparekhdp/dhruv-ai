import Foundation
import LocalAuthentication

/// Thin wrapper over LocalAuthentication. Copied from Orbit (dhruv-ios),
/// where it is built and running on the iPhone; see its docs/04-security-faceid.md.
///
/// `NSFaceIDUsageDescription` must be in Info.plist (it is, via project.yml).
/// Without it the app does not get a denied permission — it hard crashes.
enum BiometryKind {
    case none, touchID, faceID, opticID

    var displayName: String {
        switch self {
        case .none:    "Passcode"
        case .touchID: "Touch ID"
        case .faceID:  "Face ID"
        case .opticID: "Optic ID"
        }
    }

    var symbolName: String {
        switch self {
        case .none:    "lock.fill"
        case .touchID: "touchid"
        case .faceID:  "faceid"
        case .opticID: "opticid"
        }
    }
}

enum BiometricError: Error, Equatable {
    case notAvailable          // no hardware, or the user denied permission
    case notEnrolled           // hardware present, nothing enrolled
    case lockedOut             // too many failures; passcode required
    case passcodeNotSet        // device has no passcode at all
    case cancelledByUser
    case cancelledBySystem     // call, Siri, backgrounded — safe to retry once
    case failed(String)

    /// Never show a raw LAError to a user.
    var userMessage: String {
        switch self {
        case .notAvailable:
            "Face ID isn't available on this device. Jarvis works fine without app lock."
        case .notEnrolled:
            "Face ID isn't set up yet. Add it in Settings ▸ Face ID & Passcode."
        case .lockedOut:
            "Too many attempts. Enter your device passcode to continue."
        case .passcodeNotSet:
            "App Lock needs a device passcode. Set one in Settings ▸ Face ID & Passcode."
        case .cancelledByUser, .cancelledBySystem:
            ""   // Silent: stay locked and show the Unlock button.
        case .failed(let message):
            message
        }
    }
}

struct BiometricGate {

    func availableBiometry() -> BiometryKind {
        let context = LAContext()
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics,
                                        error: nil) else { return .none }
        return switch context.biometryType {
        case .faceID:  .faceID
        case .touchID: .touchID
        case .opticID: .opticID
        default:       .none
        }
    }

    /// Unlock the app.
    ///
    /// `.deviceOwnerAuthentication` is biometrics *with automatic passcode
    /// fallback*. Do not swap it for `.deviceOwnerAuthenticationWithBiometrics`:
    /// that variant strands the user in a dead app after three failures with no
    /// way back in.
    func authenticate(reason: String) async throws -> Bool {
        let context = LAContext()
        context.localizedCancelTitle = "Cancel"

        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            throw Self.map(error)
        }

        do {
            return try await context.evaluatePolicy(.deviceOwnerAuthentication,
                                                    localizedReason: reason)
        } catch {
            throw Self.map(error as NSError)
        }
    }

    private static func map(_ error: NSError?) -> BiometricError {
        guard let code = error.map({ LAError.Code(rawValue: $0.code) }) ?? nil else {
            return .failed(error?.localizedDescription ?? "Unknown error")
        }
        return switch code {
        case .biometryNotAvailable, .biometryDisconnected: .notAvailable
        case .biometryNotEnrolled:                         .notEnrolled
        case .biometryLockout:                             .lockedOut
        case .passcodeNotSet:                              .passcodeNotSet
        case .userCancel, .userFallback:                   .cancelledByUser
        case .appCancel, .systemCancel:                    .cancelledBySystem
        default: .failed(error?.localizedDescription ?? "Authentication failed")
        }
    }
}
