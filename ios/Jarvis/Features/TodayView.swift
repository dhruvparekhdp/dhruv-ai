import SwiftUI

/// The one next thing, and how you're doing. Deliberately not a dashboard:
/// a screen of ten things is a decision to make, and removing that decision
/// is what the todo rules are for.
struct TodayView: View {

    @State private var next: NextTodoResponse?
    @State private var errorText: String?
    @State private var moodReply: String?
    @State private var working = false

    var body: some View {
        NavigationStack {
            List {
                Section("Now") {
                    nowContent
                }

                Section {
                    HStack {
                        ForEach(MoodLevel.allCases) { mood in
                            Button {
                                Task { await log(mood) }
                            } label: {
                                VStack(spacing: 4) {
                                    Text(mood.emoji).font(.title)
                                    Text(mood.label).font(.caption2).foregroundStyle(.secondary)
                                }
                                .frame(maxWidth: .infinity)
                            }
                            // Borderless, or a tap anywhere in the row fires
                            // every button in it.
                            .buttonStyle(.borderless)
                            .disabled(working)
                            .accessibilityLabel("Mood: \(mood.label)")
                        }
                    }
                    if let moodReply {
                        Text(moodReply).font(.subheadline)
                    }
                } header: {
                    Text("How are you?")
                }

                if let errorText {
                    Section { Text(errorText).foregroundStyle(.red) }
                }
            }
            .navigationTitle("Today")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    @ViewBuilder
    private var nowContent: some View {
        if let todo = next?.todo {
            VStack(alignment: .leading, spacing: 6) {
                Text(todo.title).font(.headline)
                Text(TrackName.title(for: todo.track))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if !todo.notes.isEmpty {
                    Text(todo.notes)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(4)
                }
            }
            .padding(.vertical, 4)

            Button("Mark done") {
                Task { await complete(todo) }
            }
            .disabled(working)
        } else if let next {
            Text(next.reason.isEmpty ? "Nothing to do." : next.reason)
                .foregroundStyle(.secondary)
        } else if errorText == nil {
            ProgressView()
        }
    }

    private func load() async {
        do {
            next = try await ConnectionStore.requireClient().nextTodo()
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }

    private func complete(_ todo: Todo) async {
        working = true
        defer { working = false }
        do {
            _ = try await ConnectionStore.requireClient().complete(todo.todoID)
            await load()
        } catch {
            errorText = error.localizedDescription
        }
    }

    private func log(_ mood: MoodLevel) async {
        working = true
        defer { working = false }
        do {
            let reply = try await ConnectionStore.requireClient().logMood(score: mood.score)
            moodReply = reply.response
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }
}
