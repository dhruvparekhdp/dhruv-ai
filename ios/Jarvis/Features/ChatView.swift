import SwiftUI

/// A conversation with Jarvis. The history lives on the host (every turn is a
/// trajectory there); this screen only shows what was said since it opened.
struct ChatView: View {

    private struct Line: Identifiable {
        let id = UUID()
        let fromMe: Bool
        let text: String
    }

    @State private var lines: [Line] = []
    @State private var draft = ""
    @State private var sending = false

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        if lines.isEmpty {
                            Text("Ask Jarvis anything. The conversation is kept on your host, not on this phone.")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .padding(.top, 40)
                        }
                        ForEach(lines) { line in
                            bubble(line).id(line.id)
                        }
                        if sending {
                            ProgressView().padding(.leading, 8)
                        }
                    }
                    .padding()
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: lines.count) { _, _ in
                    guard let last = lines.last else { return }
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
            .safeAreaInset(edge: .bottom) { inputBar }
            .navigationTitle("Chat")
        }
    }

    private func bubble(_ line: Line) -> some View {
        HStack {
            if line.fromMe { Spacer(minLength: 40) }
            Text(line.text)
                .textSelection(.enabled)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(
                    line.fromMe ? Color.accentColor.opacity(0.2) : Color(.secondarySystemBackground),
                    in: RoundedRectangle(cornerRadius: 14)
                )
            if !line.fromMe { Spacer(minLength: 40) }
        }
    }

    private var inputBar: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Message", text: $draft, axis: .vertical)
                .lineLimit(1...5)
                .textFieldStyle(.roundedBorder)
            Button {
                Task { await send() }
            } label: {
                Image(systemName: "arrow.up.circle.fill").font(.title2)
            }
            .disabled(sending || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel("Send")
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        lines.append(Line(fromMe: true, text: text))
        sending = true
        defer { sending = false }
        do {
            let reply = try await ConnectionStore.requireClient().ask(text)
            lines.append(Line(fromMe: false, text: reply.response))
        } catch {
            lines.append(Line(fromMe: false, text: "⚠️ \(error.localizedDescription)"))
        }
    }
}
