import AppIntents

// Siri, Shortcuts, the Action button and Spotlight, all from one set of types.
//
// These run in the app's own process — no extension — because a free Personal
// Team allows three free-signed apps on the phone and every extension counts
// as one (ios/README.md).
//
// Authentication policy is set per intent, on one rule: anything whose answer
// can reveal something about you requires the phone to be unlocked. Intents
// that only *send* (report the device, file a todo) are always allowed, because
// automations run while the phone is locked in a pocket and there is nothing
// to read back.

struct AskJarvisIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Jarvis"
    static var description = IntentDescription(
        "Ask Jarvis a question and get the answer back.",
        categoryName: "Conversation",
        searchKeywords: ["ask", "chat", "question", "assistant"]
    )
    static var openAppWhenRun = false
    /// Answers can quote memory, todos and history.
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    @Parameter(title: "Question", requestValueDialog: "What do you want to ask?")
    var question: String

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let reply = try await ConnectionStore.requireClient().ask(question)
        return .result(value: reply.response, dialog: "\(reply.response)")
    }
}

struct NextTodoIntent: AppIntent {
    static var title: LocalizedStringResource = "What's Next"
    static var description = IntentDescription(
        "The one thing to work on now, from your active tracks.",
        categoryName: "Todos",
        searchKeywords: ["todo", "next", "focus", "task"]
    )
    static var openAppWhenRun = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    @MainActor
    func perform() async throws -> some IntentResult & ReturnsValue<String> & ProvidesDialog {
        let next = try await ConnectionStore.requireClient().nextTodo()
        let answer: String
        if let todo = next.todo {
            answer = "\(todo.title) — \(TrackName.title(for: todo.track))."
        } else {
            answer = next.reason.isEmpty ? "Nothing to do right now." : next.reason
        }
        return .result(value: answer, dialog: "\(answer)")
    }
}

struct AddTodoIntent: AppIntent {
    static var title: LocalizedStringResource = "Add Todo"
    static var description = IntentDescription(
        "File a todo. Capture is never refused; it lands in the track's inbox.",
        categoryName: "Todos",
        searchKeywords: ["todo", "add", "remember", "capture"]
    )
    static var openAppWhenRun = false
    /// Writing reveals nothing, and capture should work from the lock screen.
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(title: "Todo", requestValueDialog: "What needs doing?")
    var text: String

    @Parameter(title: "Track", default: .personal)
    var track: TrackName

    static var parameterSummary: some ParameterSummary {
        Summary("Add \(\.$text) to \(\.$track)")
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let todo = try await ConnectionStore.requireClient().addTodo(title: text, track: track.rawValue)
        return .result(dialog: "Filed to \(TrackName.title(for: todo.track)): \(todo.title)")
    }
}

struct LogMoodIntent: AppIntent {
    static var title: LocalizedStringResource = "Log Mood"
    static var description = IntentDescription(
        "Tell Jarvis how you're doing. It shapes how Jarvis talks to you.",
        categoryName: "Mood",
        searchKeywords: ["mood", "feeling", "check in"]
    )
    static var openAppWhenRun = false
    /// The reply is personal.
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    @Parameter(title: "Mood", requestValueDialog: "How are you feeling?")
    var mood: MoodLevel

    @Parameter(title: "Note")
    var note: String?

    static var parameterSummary: some ParameterSummary {
        Summary("Log mood \(\.$mood)") {
            \.$note
        }
    }

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let reply = try await ConnectionStore.requireClient()
            .logMood(score: mood.score, note: note ?? "")
        return .result(dialog: "\(reply.response)")
    }
}

struct ReportDeviceIntent: AppIntent {
    static var title: LocalizedStringResource = "Report to Jarvis"
    static var description = IntentDescription(
        "Send this phone's battery, power, network and storage state to Jarvis. Built for Shortcuts automations: arriving somewhere, plugging in, a time of day.",
        categoryName: "Device",
        searchKeywords: ["report", "battery", "device", "status"]
    )
    static var openAppWhenRun = false
    /// Sends only; reads nothing back. Must run with the phone locked.
    static var authenticationPolicy: IntentAuthenticationPolicy = .alwaysAllowed

    @Parameter(
        title: "Reason",
        description: "Saved with the report so Jarvis knows what triggered it, e.g. arrived_office."
    )
    var reason: String?

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let trimmed = (reason ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        try await DeviceReporter.report(via: trimmed.isEmpty ? "shortcut" : "shortcut:\(trimmed)")
        return .result(dialog: "Sent to Jarvis.")
    }
}

struct JarvisShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskJarvisIntent(),
            phrases: ["Ask \(.applicationName)", "Talk to \(.applicationName)"],
            shortTitle: "Ask Jarvis",
            systemImageName: "bubble.left.and.bubble.right"
        )
        AppShortcut(
            intent: NextTodoIntent(),
            phrases: ["What's next in \(.applicationName)", "Ask \(.applicationName) what's next"],
            shortTitle: "What's Next",
            systemImageName: "arrow.right.circle"
        )
        AppShortcut(
            intent: AddTodoIntent(),
            phrases: ["Add a todo to \(.applicationName)", "Capture something in \(.applicationName)"],
            shortTitle: "Add Todo",
            systemImageName: "plus.circle"
        )
        AppShortcut(
            intent: LogMoodIntent(),
            phrases: ["Log my mood in \(.applicationName)", "Tell \(.applicationName) how I feel"],
            shortTitle: "Log Mood",
            systemImageName: "face.smiling"
        )
        AppShortcut(
            intent: ReportDeviceIntent(),
            phrases: ["Report to \(.applicationName)"],
            shortTitle: "Report Device",
            systemImageName: "iphone"
        )
    }
}
