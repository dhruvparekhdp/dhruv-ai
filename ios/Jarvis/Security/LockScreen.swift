import SwiftUI

/// Shown over everything while locked. Adapted from Orbit. Deliberately has
/// exactly one action — there is nothing else useful to do here.
struct LockScreen: View {

    @Environment(AppLockCoordinator.self) private var lock

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: lock.biometry.symbolName)
                .font(.largeTitle)
                .imageScale(.large)
                .foregroundStyle(.tint)
                .accessibilityHidden(true)

            VStack(spacing: 8) {
                Text("Jarvis is locked")
                    .font(.title3.weight(.semibold))
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            .padding(.horizontal)

            Spacer()

            Button {
                Task { await lock.authenticate() }
            } label: {
                Text(unlockTitle).frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .padding(.horizontal, 24)
            .padding(.bottom, 32)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground))
        // One prompt on appear. Every retry after that is user-initiated —
        // an auto-retry loop makes a cancelled prompt impossible to escape.
        .task { await lock.authenticate() }
    }

    private var subtitle: String {
        if lock.state == .lockedOut {
            return BiometricError.lockedOut.userMessage
        }
        if let error = lock.lastError, !error.userMessage.isEmpty {
            return error.userMessage
        }
        return "Unlock with \(lock.biometry.displayName) to continue."
    }

    private var unlockTitle: String {
        lock.state == .lockedOut ? "Enter Passcode" : "Unlock"
    }
}
