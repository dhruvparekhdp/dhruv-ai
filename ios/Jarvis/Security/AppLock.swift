import SwiftUI

/// The UX curtain, from Orbit: scene-phase driven, with a grace period.
///
/// Deliberately *not* security at rest — anyone with the device passcode gets
/// in. It stops someone holding your unlocked phone from reading Jarvis or
/// approving an action. The passcode itself is in the Keychain, and Siri
/// requests that read anything require the phone to be unlocked (see
/// JarvisIntents.swift).
@MainActor
@Observable
final class AppLockCoordinator {

    enum State: Equatable { case unlocked, locked, lockedOut }

    private(set) var state: State
    private(set) var lastError: BiometricError?

    /// Off by default. An unexpected Face ID prompt on first launch reads as
    /// malware — let the user opt in from Settings.
    var isEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isEnabled, forKey: Keys.enabled)
            state = isEnabled ? .locked : .unlocked
        }
    }

    /// Measured from `.background`, never `.inactive` — Control Centre and the
    /// notification shade fire `.inactive`, and locking on those is maddening.
    var graceInterval: TimeInterval {
        didSet { UserDefaults.standard.set(graceInterval, forKey: Keys.grace) }
    }

    var biometry: BiometryKind { gate.availableBiometry() }

    private let gate: BiometricGate
    private var backgroundedAt: Date?

    private enum Keys {
        static let enabled = "jarvis.appLock.enabled"
        static let grace   = "jarvis.appLock.grace"
    }

    init(gate: BiometricGate = BiometricGate()) {
        self.gate = gate
        let enabled = UserDefaults.standard.bool(forKey: Keys.enabled)
        self.isEnabled = enabled
        self.graceInterval =
            UserDefaults.standard.object(forKey: Keys.grace) as? TimeInterval ?? 60
        self.state = enabled ? .locked : .unlocked
    }

    // MARK: - Scene phase

    func onScenePhase(_ phase: ScenePhase) {
        guard isEnabled else { return }
        switch phase {
        case .background:
            backgroundedAt = .now
        case .active:
            defer { backgroundedAt = nil }
            guard let at = backgroundedAt else { return }
            if Date.now.timeIntervalSince(at) >= graceInterval { state = .locked }
        default:
            break
        }
    }

    func lockNow() {
        guard isEnabled else { return }
        state = .locked
    }

    // MARK: - Unlock

    func authenticate() async {
        guard state != .unlocked else { return }
        lastError = nil
        do {
            let ok = try await gate.authenticate(reason: "Unlock Jarvis")
            state = ok ? .unlocked : .locked
        } catch let error as BiometricError {
            lastError = error
            switch error {
            case .notAvailable, .notEnrolled, .passcodeNotSet:
                // Never trap someone in a dead app because biometry went away.
                isEnabled = false
                state = .unlocked
            case .lockedOut:
                state = .lockedOut
            case .cancelledByUser, .cancelledBySystem, .failed:
                // Stay locked and show the Unlock button. Re-prompting in a loop
                // makes the app impossible to escape.
                state = .locked
            }
        } catch {
            lastError = .failed(error.localizedDescription)
            state = .locked
        }
    }
}

// MARK: - Privacy snapshot

extension View {
    /// Covers content while locked *and* during `.inactive`, so the app-switcher
    /// snapshot never shows a conversation. It must be `.inactive`: by `.background`
    /// iOS has already taken the screenshot.
    func privacyBlur(active: Bool) -> some View {
        modifier(PrivacyBlurModifier(active: active))
    }
}

private struct PrivacyBlurModifier: ViewModifier {
    let active: Bool
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var shouldCover: Bool { active || scenePhase == .inactive }

    func body(content: Content) -> some View {
        content
            .overlay {
                if shouldCover {
                    ZStack {
                        Rectangle().fill(.ultraThinMaterial)
                        Image(systemName: "lock.fill")
                            .font(.largeTitle)
                            .foregroundStyle(.secondary)
                    }
                    .ignoresSafeArea()
                    .transition(.opacity)
                }
            }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.15), value: shouldCover)
    }
}
