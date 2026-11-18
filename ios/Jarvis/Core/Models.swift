import AppIntents
import Foundation

// Shapes of what the Jarvis API returns. Every type spells out its CodingKeys
// rather than using `.convertFromSnakeCase`: that strategy also rewrites the
// keys *inside* dictionaries, so an approval's `max_bytes` argument would show
// up as `maxBytes` — a different name from what the agent actually sent.
//
// Timestamps stay `String`. Python's isoformat() drops the fractional seconds
// entirely when they happen to be zero, so a strict Date decoder would fail on
// roughly one response in a million. Parse at display time instead; see
// `ServerTime`.

struct Todo: Decodable, Identifiable, Hashable {
    let todoID: String
    let title: String
    let track: String
    let status: String
    let priority: Int
    let notes: String
    let dueAt: String?

    var id: String { todoID }

    /// The six characters Telegram's `/done` accepts — same id, same shorthand.
    var shortID: String { String(todoID.replacingOccurrences(of: "todo_", with: "").prefix(6)) }

    var isDone: Bool { status == "done" }

    enum CodingKeys: String, CodingKey {
        case todoID = "todo_id"
        case title, track, status, priority, notes
        case dueAt = "due_at"
    }
}

struct NextTodoResponse: Decodable {
    let todo: Todo?
    let activeTracks: [String]
    /// Why there is nothing to do, when `todo` is nil.
    let reason: String

    enum CodingKeys: String, CodingKey {
        case todo
        case activeTracks = "active_tracks"
        case reason
    }
}

struct TodoListResponse: Decodable {
    let todos: [Todo]
}

struct TodoResponse: Decodable {
    let todo: Todo
}

struct TrackSummary: Decodable {
    let activeTracks: [String]
    let verdict: String
    let openTotal: Int

    enum CodingKeys: String, CodingKey {
        case activeTracks = "active_tracks"
        case verdict
        case openTotal = "open_total"
    }
}

struct SessionResponse: Decodable {
    let sessionID: String

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
    }
}

struct CommandResponse: Decodable {
    let response: String
    let provider: String
    /// True when no model key is configured and the echo engine answered.
    let degraded: Bool

    enum CodingKeys: String, CodingKey {
        case response, provider, degraded
    }
}

struct MoodResponse: Decodable {
    let score: Int
    let label: String
    let response: String
}

struct Approval: Decodable, Identifiable {
    let approvalID: String
    let missionID: String
    let agentID: String
    let toolName: String
    let toolArguments: [String: JSONValue]
    let risk: String
    let createdAt: String

    var id: String { approvalID }

    enum CodingKeys: String, CodingKey {
        case approvalID = "approval_id"
        case missionID = "mission_id"
        case agentID = "agent_id"
        case toolName = "tool_name"
        case toolArguments = "tool_arguments"
        case risk
        case createdAt = "created_at"
    }
}

struct ApprovalListResponse: Decodable {
    let approvals: [Approval]
}

/// For endpoints whose body is ignored. Any JSON object decodes into this.
struct Ignored: Decodable {}

// MARK: - Request bodies

struct CommandBody: Encodable {
    let sessionID: String
    let prompt: String

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case prompt
    }
}

struct MoodBody: Encodable {
    let sessionID: String
    let score: Int
    let note: String

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case score, note
    }
}

struct NewTodoBody: Encodable {
    let title: String
    let track: String
    let status: String
}

struct StatusBody: Encodable {
    let status: String
}

struct DecisionBody: Encodable {
    let approve: Bool
}

struct SessionBody: Encodable {
    let clientType: String

    enum CodingKeys: String, CodingKey {
        case clientType = "client_type"
    }
}

/// A phone reporting on itself — `POST /api/v1/devices/checkin`.
struct DeviceReport: Encodable {
    let deviceID: String
    let name: String
    let platform: String
    let model: String
    let osVersion: String
    let appVersion: String
    /// What produced the report: "app_open", "siri", "shortcut:arrived_office".
    let via: String
    let metrics: [String: MetricValue]

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case name, platform, model
        case osVersion = "os_version"
        case appVersion = "app_version"
        case via, metrics
    }
}

/// The server accepts flat scalars only (app/devices/store.py), so the type
/// system enforces the same rule here: there is no case for a nested object.
enum MetricValue: Encodable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value):   try container.encode(value)
        }
    }
}

/// Arbitrary JSON, for tool arguments whose shape depends on the tool.
enum JSONValue: Decodable, Hashable, CustomStringConvertible {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        // Bool before Double: JSONDecoder will not read `true` as a number, but
        // checking in this order keeps that guarantee explicit.
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    var description: String {
        switch self {
        case .string(let value):
            return value
        case .number(let value):
            let isWhole = value.rounded() == value && abs(value) < 1e15
            return isWhole ? String(Int(value)) : String(value)
        case .bool(let value):
            return value ? "true" : "false"
        case .array(let values):
            let items = values.map { $0.description }.joined(separator: ", ")
            return "[\(items)]"
        case .object(let values):
            let pairs = values.keys.sorted().map { key in
                "\(key): \(values[key]?.description ?? "null")"
            }
            return "{\(pairs.joined(separator: ", "))}"
        case .null:
            return "null"
        }
    }
}

/// The closed set of tracks, mirroring `Track` in app/todos/models.py. Adding a
/// track there is a deliberate code change; it is one here too.
///
/// An AppEnum so Siri and Shortcuts offer the same choices as the app.
enum TrackName: String, AppEnum, Identifiable {
    case jobSwitch = "job_switch"
    case saloni
    case jarvis
    case crypto
    case docsTool = "docs_tool"
    case content
    case personal

    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Track"
    static var caseDisplayRepresentations: [TrackName: DisplayRepresentation] = [
        .jobSwitch: "Job switch",
        .saloni: "Saloni",
        .jarvis: "Jarvis",
        .crypto: "Crypto",
        .docsTool: "Docs tool",
        .content: "Content",
        .personal: "Personal",
    ]

    var id: String { rawValue }

    var title: String {
        switch self {
        case .jobSwitch: "Job switch"
        case .saloni:    "Saloni"
        case .jarvis:    "Jarvis"
        case .crypto:    "Crypto"
        case .docsTool:  "Docs tool"
        case .content:   "Content"
        case .personal:  "Personal"
        }
    }

    static func title(for raw: String) -> String {
        TrackName(rawValue: raw)?.title ?? raw
    }
}

/// The mood scale, mirroring MOOD_SCALE in app/agent/prompts.py (1–5).
enum MoodLevel: String, AppEnum, Identifiable {
    case rough, low, okay, good, great

    static var typeDisplayRepresentation: TypeDisplayRepresentation = "Mood"
    static var caseDisplayRepresentations: [MoodLevel: DisplayRepresentation] = [
        .rough: "Rough",
        .low: "Low",
        .okay: "Okay",
        .good: "Good",
        .great: "Great",
    ]

    var id: String { rawValue }

    var score: Int {
        switch self {
        case .rough: 1
        case .low:   2
        case .okay:  3
        case .good:  4
        case .great: 5
        }
    }

    var emoji: String {
        switch self {
        case .rough: "😞"
        case .low:   "🙁"
        case .okay:  "😐"
        case .good:  "🙂"
        case .great: "😄"
        }
    }

    var label: String { rawValue.capitalized }
}

/// Server timestamps, parsed for display only.
enum ServerTime {
    /// Python's isoformat(): "2026-10-19T14:47:53.119551+00:00", or without the
    /// fraction when microseconds are zero. ISO8601DateFormatter expects
    /// milliseconds and misreads six fractional digits, hence DateFormatter.
    static func parse(_ raw: String) -> Date? {
        for pattern in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSSxxxxx", "yyyy-MM-dd'T'HH:mm:ssxxxxx"] {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.dateFormat = pattern
            if let date = formatter.date(from: raw) { return date }
        }
        return nil
    }
}
