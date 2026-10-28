import Foundation

/// Everything that can go wrong talking to Jarvis, named specifically enough
/// that the UI can say something useful. A 401 means "fix the passcode in
/// Settings", which is a different instruction from "the host is off".
enum JarvisError: LocalizedError, Equatable {
    case notConfigured
    case transport(String)
    case unauthorized
    case notFound(String)
    case server(status: Int, detail: String)
    case decode(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Jarvis isn't set up yet. Add the server address and passcode in Settings."
        case .transport(let detail):
            return "Couldn't reach Jarvis (\(detail)). Is the host on, and is Tailscale connected?"
        case .unauthorized:
            return "Jarvis refused the passcode. Check it in Settings."
        case .notFound(let detail):
            return detail.isEmpty ? "Not found." : detail
        case .server(let status, let detail):
            return "Jarvis returned \(status)\(detail.isEmpty ? "" : ": \(detail)")"
        case .decode(let detail):
            return "Jarvis sent something this app doesn't understand yet (\(detail))."
        }
    }
}

/// A client for the Jarvis HTTP API.
///
/// The app is a thin client on purpose. The todo rules (two active tracks, one
/// next item), the approval gate and the memory all live server-side, tested,
/// in one place. Re-implementing any of them in Swift would make two versions
/// that drift — the same reasoning as the crypto app's port.
actor JarvisClient {
    private let baseURL: URL
    private let passcode: String
    private let session: URLSession

    /// The chat session id, reused across launches so a conversation from the
    /// phone is one thread in Jarvis's history rather than hundreds of
    /// one-message sessions.
    static let sessionKey = "jarvis.sessionID"

    init(baseURL: URL, passcode: String) {
        self.baseURL = baseURL
        self.passcode = passcode
        let configuration = URLSessionConfiguration.default
        // Chat goes through an LLM; Groq is fast, the fallback chain may not be.
        configuration.timeoutIntervalForRequest = 60
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
    }

    // MARK: - Todos

    func verify() async throws -> TrackSummary {
        try await send("GET", "/api/v1/todos/summary")
    }

    func nextTodo() async throws -> NextTodoResponse {
        try await send("GET", "/api/v1/todos/next")
    }

    func todos() async throws -> [Todo] {
        let response: TodoListResponse = try await send("GET", "/api/v1/todos")
        return response.todos
    }

    func addTodo(title: String, track: String) async throws -> Todo {
        // "inbox": captured, not yet triaged — the same status Telegram's /add
        // uses. Triage happens on the full list, not at the moment of capture.
        let response: TodoResponse = try await send(
            "POST", "/api/v1/todos", body: NewTodoBody(title: title, track: track, status: "inbox"))
        return response.todo
    }

    func complete(_ todoID: String) async throws -> Todo {
        let response: TodoResponse = try await send(
            "PATCH", "/api/v1/todos/\(todoID)", body: StatusBody(status: "done"))
        return response.todo
    }

    // MARK: - Conversation

    func ask(_ prompt: String) async throws -> CommandResponse {
        try await withSession { (sessionID: String) async throws -> CommandResponse in
            try await self.send("POST", "/api/v1/command",
                                body: CommandBody(sessionID: sessionID, prompt: prompt))
        }
    }

    func logMood(score: Int, note: String = "") async throws -> MoodResponse {
        try await withSession { (sessionID: String) async throws -> MoodResponse in
            try await self.send("POST", "/api/v1/mood",
                                body: MoodBody(sessionID: sessionID, score: score, note: note))
        }
    }

    // MARK: - Approvals

    func approvals() async throws -> [Approval] {
        let response: ApprovalListResponse = try await send("GET", "/api/v1/approvals")
        return response.approvals
    }

    func decide(_ approvalID: String, approve: Bool) async throws {
        let _: Ignored = try await send(
            "POST", "/api/v1/approvals/\(approvalID)", body: DecisionBody(approve: approve))
    }

    // MARK: - Device

    func checkin(_ report: DeviceReport) async throws {
        let _: Ignored = try await send("POST", "/api/v1/devices/checkin", body: report)
    }

    // MARK: - Sessions

    /// Runs `body` with a valid session id. If the stored one is gone — the
    /// host's database was reset — start a fresh session and retry exactly once.
    private func withSession<T>(_ body: (String) async throws -> T) async throws -> T {
        let sessionID = try await currentSession()
        do {
            return try await body(sessionID)
        } catch JarvisError.notFound(_) {
            UserDefaults.standard.removeObject(forKey: Self.sessionKey)
            return try await body(try await currentSession())
        }
    }

    private func currentSession() async throws -> String {
        if let existing = UserDefaults.standard.string(forKey: Self.sessionKey), !existing.isEmpty {
            return existing
        }
        let response: SessionResponse = try await send(
            "POST", "/api/v1/session/start", body: SessionBody(clientType: "IOS_APP"))
        UserDefaults.standard.set(response.sessionID, forKey: Self.sessionKey)
        return response.sessionID
    }

    // MARK: - Plumbing

    private func send<T: Decodable>(
        _ method: String, _ path: String, body: (any Encodable)? = nil
    ) async throws -> T {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        request.setValue(passcode, forHTTPHeaderField: "X-Jarvis-Passcode")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }

        let result: (Data, URLResponse)
        do {
            result = try await session.data(for: request)
        } catch {
            throw JarvisError.transport(error.localizedDescription)
        }
        let (data, response) = result

        guard let http = response as? HTTPURLResponse else {
            throw JarvisError.transport("no HTTP response")
        }
        switch http.statusCode {
        case 200..<300:
            do {
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                // A shape mismatch, not a transient fault: retrying would fail
                // the same way, so it is reported, loudly, straight away.
                throw JarvisError.decode(String(describing: error))
            }
        case 401:
            throw JarvisError.unauthorized
        case 404:
            throw JarvisError.notFound(Self.detail(from: data))
        default:
            throw JarvisError.server(status: http.statusCode, detail: Self.detail(from: data))
        }
    }

    /// FastAPI puts the human-readable reason in {"detail": "..."}.
    static func detail(from data: Data) -> String {
        struct ErrorBody: Decodable { let detail: String }
        if let body = try? JSONDecoder().decode(ErrorBody.self, from: data) {
            return body.detail
        }
        return String(data: data.prefix(300), encoding: .utf8) ?? ""
    }
}
