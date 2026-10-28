import SwiftUI

/// The whole list, grouped by track. Active tracks first; parked tracks are
/// shown, not hidden — parked work still exists, it's just not today's work.
struct TodosView: View {

    private struct TrackGroup: Identifiable {
        let track: String
        let isActive: Bool
        let items: [Todo]
        var id: String { track }
    }

    @State private var todos: [Todo] = []
    @State private var summary: TrackSummary?
    @State private var errorText: String?
    @State private var adding = false

    private var groups: [TrackGroup] {
        let active = Set(summary?.activeTracks ?? [])
        return Dictionary(grouping: todos, by: \.track)
            .map { TrackGroup(track: $0.key, isActive: active.contains($0.key), items: $0.value) }
            .sorted { lhs, rhs in
                if lhs.isActive != rhs.isActive { return lhs.isActive }
                return lhs.track < rhs.track
            }
    }

    var body: some View {
        NavigationStack {
            List {
                if let summary {
                    Section { Text(summary.verdict).font(.subheadline) }
                }
                if let errorText {
                    Section { Text(errorText).foregroundStyle(.red) }
                }
                ForEach(groups) { group in
                    Section {
                        ForEach(group.items) { todo in
                            row(todo)
                                .opacity(group.isActive ? 1 : 0.6)
                                .swipeActions(edge: .trailing) {
                                    Button("Done") { Task { await complete(todo) } }
                                        .tint(.green)
                                }
                        }
                    } header: {
                        HStack {
                            Text(TrackName.title(for: group.track))
                            Spacer()
                            Text(group.isActive ? "active" : "parked")
                                .font(.caption2)
                                .foregroundStyle(group.isActive ? Color.accentColor : .secondary)
                        }
                    }
                }
            }
            .navigationTitle("Todos")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        adding = true
                    } label: {
                        Image(systemName: "plus")
                    }
                    .accessibilityLabel("Add todo")
                }
            }
            .sheet(isPresented: $adding) {
                AddTodoSheet { await load() }
            }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private func row(_ todo: Todo) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline) {
                if todo.priority == 1 {
                    Image(systemName: "exclamationmark").foregroundStyle(.orange)
                        .accessibilityLabel("High priority")
                }
                Text(todo.title)
            }
            Text("\(todo.status) · \(todo.shortID)")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
        }
    }

    private func load() async {
        do {
            let client = try ConnectionStore.requireClient()
            let list = try await client.todos()
            let overview = try await client.verify()
            todos = list
            summary = overview
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }

    private func complete(_ todo: Todo) async {
        do {
            _ = try await ConnectionStore.requireClient().complete(todo.todoID)
            await load()
        } catch {
            errorText = error.localizedDescription
        }
    }
}

/// Capture is never refused — the same rule as the server's `todos.store.add`.
/// Anything filed here lands in the track's inbox, to be triaged later.
struct AddTodoSheet: View {

    let onAdded: () async -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var track: TrackName = .personal
    @State private var saving = false
    @State private var errorText: String?

    var body: some View {
        NavigationStack {
            Form {
                TextField("What needs doing?", text: $title, axis: .vertical)
                Picker("Track", selection: $track) {
                    ForEach(TrackName.allCases) { Text($0.title).tag($0) }
                }
                if let errorText {
                    Text(errorText).foregroundStyle(.red)
                }
            }
            .navigationTitle("New todo")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") { Task { await save() } }
                        .disabled(saving || title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let text = title.trimmingCharacters(in: .whitespacesAndNewlines)
            _ = try await ConnectionStore.requireClient().addTodo(title: text, track: track.rawValue)
            await onAdded()
            dismiss()
        } catch {
            errorText = error.localizedDescription
        }
    }
}
