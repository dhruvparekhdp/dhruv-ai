import SwiftUI

/// Dangerous actions waiting on you (invariant 3: they always park).
///
/// Approving asks for Face ID every time, even with the app unlocked. Denying
/// does not — saying no is always safe, and it should never be harder than
/// saying yes.
struct ApprovalsView: View {

    @State private var approvals: [Approval] = []
    @State private var errorText: String?
    @State private var deciding: String?
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            List {
                if let errorText {
                    Section { Text(errorText).foregroundStyle(.red) }
                }
                if loaded && approvals.isEmpty && errorText == nil {
                    ContentUnavailableView(
                        "Nothing waiting",
                        systemImage: "checkmark.shield",
                        description: Text("When an agent wants to do something dangerous, it stops and waits here for you.")
                    )
                }
                ForEach(approvals) { approval in
                    card(approval)
                }
            }
            .navigationTitle("Approvals")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private func card(_ approval: Approval) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(approval.toolName).font(.headline.monospaced())
            Text("\(approval.agentID) · \(approval.risk)")
                .font(.caption)
                .foregroundStyle(.secondary)
            ForEach(approval.toolArguments.keys.sorted(), id: \.self) { key in
                Text("\(key): \(approval.toolArguments[key]?.description ?? "")")
                    .font(.caption.monospaced())
                    .textSelection(.enabled)
            }
            HStack {
                Button("Deny", role: .destructive) {
                    Task { await decide(approval, approve: false) }
                }
                .buttonStyle(.bordered)
                Spacer()
                Button("Approve") {
                    Task { await decide(approval, approve: true) }
                }
                .buttonStyle(.borderedProminent)
            }
            .disabled(deciding == approval.id)
        }
        .padding(.vertical, 4)
    }

    private func load() async {
        do {
            approvals = try await ConnectionStore.requireClient().approvals()
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
        loaded = true
    }

    private func decide(_ approval: Approval, approve: Bool) async {
        deciding = approval.id
        defer { deciding = nil }

        if approve {
            do {
                let ok = try await BiometricGate().authenticate(
                    reason: "Approve \(approval.toolName) for \(approval.agentID)")
                guard ok else { return }
            } catch let error as BiometricError {
                // No passcode on the phone means no way to prove it's you. The
                // approval stays parked; it can still be approved from the PWA.
                let message = error.userMessage
                if !message.isEmpty { errorText = message }
                return
            } catch {
                errorText = error.localizedDescription
                return
            }
        }

        do {
            try await ConnectionStore.requireClient().decide(approval.approvalID, approve: approve)
            await load()
        } catch {
            errorText = error.localizedDescription
        }
    }
}
