import Foundation
import Testing
@testable import Jarvis

// The JSON below was captured from the real API (FastAPI TestClient against
// app/main.py), not written from memory. If the server's shape changes, the
// fix is to re-capture it — not to edit these until they pass.

struct DecodingTests {

    @Test func nextTodoWithAnItem() throws {
        let json = """
        {"status":"success","todo":{"todo_id":"todo_378e2e02b99a","title":"Make dhruv-ai public",
        "track":"jarvis","status":"next","priority":1,"notes":"Check the history first.",
        "source":"claude","created_at":"2026-10-19T14:47:53.119551+00:00",
        "updated_at":"2026-10-19T14:47:53.119551+00:00","due_at":null,"remind_at":null,
        "completed_at":null},"active_tracks":["jarvis","job_switch"],"reason":""}
        """
        let next = try JSONDecoder().decode(NextTodoResponse.self, from: Data(json.utf8))

        #expect(next.todo?.title == "Make dhruv-ai public")
        #expect(next.todo?.shortID == "378e2e")
        #expect(next.todo?.dueAt == nil)
        #expect(next.activeTracks == ["jarvis", "job_switch"])
    }

    @Test func nextTodoWhenThereIsNothing() throws {
        let json = """
        {"status":"success","todo":null,"active_tracks":[],
        "reason":"No active track. Nothing will be offered by next_todo until you activate one."}
        """
        let next = try JSONDecoder().decode(NextTodoResponse.self, from: Data(json.utf8))

        #expect(next.todo == nil)
        #expect(next.reason.hasPrefix("No active track"))
    }

    @Test func todoWithAnOffsetDueDate() throws {
        let json = """
        {"status":"success","todo":{"todo_id":"todo_743c2d09fcb4","title":"Wire Telegram",
        "track":"jarvis","status":"done","priority":1,"notes":"","source":"me",
        "created_at":"2026-10-19T14:47:53.196207+00:00","updated_at":"2026-10-19T14:47:53.236391+00:00",
        "due_at":"2026-10-20T09:00:00+05:30","remind_at":null,
        "completed_at":"2026-10-19T14:47:53.236378+00:00"}}
        """
        let todo = try JSONDecoder().decode(TodoResponse.self, from: Data(json.utf8)).todo

        #expect(todo.isDone)
        #expect(todo.dueAt == "2026-10-20T09:00:00+05:30")
        #expect(ServerTime.parse(todo.dueAt!) != nil)
    }

    @Test func approvalKeepsArgumentNamesExactlyAsSent() throws {
        let json = """
        {"status":"success","count":1,"approvals":[{"approval_id":"appr_e2a4","mission_id":"mis_1",
        "task_id":"task_1","created_at":"2026-10-19T14:48:01.273863+00:00","decided_at":null,
        "agent_id":"operator","tool_name":"fs.delete",
        "tool_arguments":{"path":"~/ws/old.log","recursive":false,"max_bytes":10},
        "risk":"DANGEROUS","decision":"PENDING","decided_by":""}]}
        """
        let approval = try #require(
            try JSONDecoder().decode(ApprovalListResponse.self, from: Data(json.utf8)).approvals.first)

        #expect(approval.toolName == "fs.delete")
        // Not "maxBytes": what you approve must be what the agent actually sent.
        #expect(approval.toolArguments["max_bytes"]?.description == "10")
        #expect(approval.toolArguments["recursive"]?.description == "false")
        #expect(approval.toolArguments["path"]?.description == "~/ws/old.log")
    }

    @Test func degradedChatReply() throws {
        let json = """
        {"status":"success","trajectory_id":"traj_73c2","intent":"PERSONAL_CHAT",
        "response":"Hey. Jarvis here, running on local fallback.","provider":"ECHO",
        "model":"jarvis-local-echo","latency_ms":0,"degraded":true}
        """
        let reply = try JSONDecoder().decode(CommandResponse.self, from: Data(json.utf8))

        #expect(reply.degraded)
        #expect(reply.provider == "ECHO")
    }

    @Test func fastAPIErrorDetailIsExtracted() {
        let body = Data(#"{"detail":"Invalid or missing passcode."}"#.utf8)
        #expect(JarvisClient.detail(from: body) == "Invalid or missing passcode.")
    }
}

struct EncodingTests {

    @Test func deviceReportUsesTheServersFieldNames() throws {
        let report = DeviceReport(
            deviceID: "ios-1", name: "iPhone", platform: "ios", model: "iPhone17,3",
            osVersion: "26.6.1", appVersion: "0.1", via: "shortcut:arrived_office",
            metrics: ["battery_level": .number(0.82), "low_power_mode": .bool(false),
                      "network": .string("wifi")]
        )
        let data = try JSONEncoder().encode(report)
        let object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])

        #expect(object["device_id"] as? String == "ios-1")
        #expect(object["os_version"] as? String == "26.6.1")
        #expect(object["via"] as? String == "shortcut:arrived_office")
        let metrics = try #require(object["metrics"] as? [String: Any])
        #expect(metrics["battery_level"] as? Double == 0.82)
        #expect(metrics["low_power_mode"] as? Bool == false)
        #expect(metrics["network"] as? String == "wifi")
    }

    @Test func commandBodyUsesSnakeCase() throws {
        let data = try JSONEncoder().encode(CommandBody(sessionID: "sess_1", prompt: "hi"))
        let object = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(object["session_id"] as? String == "sess_1")
    }
}

struct ConnectionTests {

    @Test func funnelURLIsKeptAsHTTPS() {
        #expect(ConnectionStore.normalise("https://dhruv-ai.tail1234.ts.net/")?.absoluteString
                == "https://dhruv-ai.tail1234.ts.net")
    }

    @Test func noSchemeMeansHTTPS() {
        #expect(ConnectionStore.normalise("dhruv-ai.tail1234.ts.net")?.absoluteString
                == "https://dhruv-ai.tail1234.ts.net")
    }

    @Test func bareTailnetNameWithPortIsTheHostOverHTTP() {
        #expect(ConnectionStore.normalise("dhruv-ai:8000")?.absoluteString == "http://dhruv-ai:8000")
        #expect(ConnectionStore.normalise(" 100.64.0.7:8000 ")?.absoluteString
                == "http://100.64.0.7:8000")
    }

    @Test func rejectsWhatIsNotAnAddress() {
        #expect(ConnectionStore.normalise("") == nil)
        #expect(ConnectionStore.normalise("   ") == nil)
        #expect(ConnectionStore.normalise("ftp://dhruv-ai") == nil)
    }
}

struct TimeTests {

    @Test func parsesPythonIsoformatWithAndWithoutMicroseconds() {
        #expect(ServerTime.parse("2026-10-19T14:47:53.119551+00:00") != nil)
        // isoformat() drops the fraction entirely when microseconds are zero.
        #expect(ServerTime.parse("2026-10-19T14:47:53+00:00") != nil)
        #expect(ServerTime.parse("yesterday") == nil)
    }
}

struct MoodTests {

    @Test func scaleMatchesTheServer() {
        // MOOD_SCALE in app/agent/prompts.py is 1...5.
        #expect(MoodLevel.allCases.map(\.score) == [1, 2, 3, 4, 5])
    }
}
