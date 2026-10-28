import Foundation
import Network
import UIKit

/// What the phone can tell Jarvis about itself without asking any permission.
///
/// Everything here is readable with no prompt: battery, power mode, heat, the
/// network type, free space, time zone. Location, Health, Calendar and the
/// rest need a permission each, and arrive one at a time — see ios/README.md,
/// "The access ladder". Each new source is an explicit opt-in, never a bundle.
enum DeviceContext {

    @MainActor
    static func snapshot() async -> [String: MetricValue] {
        var metrics: [String: MetricValue] = [:]

        let device = UIDevice.current
        device.isBatteryMonitoringEnabled = true
        if device.batteryLevel >= 0 {   // -1 means unknown (the simulator)
            metrics["battery_level"] = .number((Double(device.batteryLevel) * 100).rounded() / 100)
        }
        metrics["battery_state"] = .string(batteryState(device.batteryState))

        let process = ProcessInfo.processInfo
        metrics["low_power_mode"] = .bool(process.isLowPowerModeEnabled)
        metrics["thermal_state"] = .string(thermalState(process.thermalState))
        metrics["uptime_hours"] = .number((process.systemUptime / 360).rounded() / 10)

        metrics["timezone"] = .string(TimeZone.current.identifier)
        if let free = freeDiskGB() {
            metrics["disk_free_gb"] = .number(free)
        }

        let network = await currentNetwork()
        metrics["network"] = .string(network.kind)
        // "Expensive" is cellular or a personal hotspot; "constrained" is Low
        // Data Mode. Both say "don't sync anything big right now".
        metrics["network_expensive"] = .bool(network.expensive)
        metrics["network_constrained"] = .bool(network.constrained)

        return metrics
    }

    /// "iPhone17,3" rather than "iPhone": UIDevice.model only ever says the
    /// family, and since iOS 16 UIDevice.name only says "iPhone" too.
    static var modelIdentifier: String {
        var info = utsname()
        uname(&info)
        return withUnsafeBytes(of: &info.machine) { buffer in
            String(decoding: buffer.prefix { $0 != 0 }, as: UTF8.self)
        }
    }

    private static func batteryState(_ state: UIDevice.BatteryState) -> String {
        switch state {
        case .charging:  return "charging"
        case .full:      return "full"
        case .unplugged: return "unplugged"
        default:         return "unknown"
        }
    }

    private static func thermalState(_ state: ProcessInfo.ThermalState) -> String {
        switch state {
        case .nominal:  return "nominal"
        case .fair:     return "fair"
        case .serious:  return "serious"
        case .critical: return "critical"
        @unknown default: return "unknown"
        }
    }

    private static func freeDiskGB() -> Double? {
        let home = URL(fileURLWithPath: NSHomeDirectory())
        guard let values = try? home.resourceValues(
                forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
              let bytes = values.volumeAvailableCapacityForImportantUsage
        else { return nil }
        return (Double(bytes) / 1_000_000_000 * 10).rounded() / 10
    }

    /// NWPathMonitor reports the current path immediately after it starts, so
    /// one callback is all this waits for.
    private static func currentNetwork() async -> (kind: String, expensive: Bool, constrained: Bool) {
        await withCheckedContinuation { continuation in
            let monitor = NWPathMonitor()
            // A class, not a captured `var`: the SDK marks pathUpdateHandler
            // @Sendable, and mutating a captured var inside one is a compile
            // error. Resuming a continuation twice is a crash, so this guards it.
            let once = Once()
            monitor.pathUpdateHandler = { path in
                guard once.claim() else { return }
                monitor.cancel()
                continuation.resume(returning: (
                    DeviceContext.kind(of: path), path.isExpensive, path.isConstrained
                ))
            }
            monitor.start(queue: DispatchQueue(label: "jarvis.network-probe"))
        }
    }

    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var claimed = false

        func claim() -> Bool {
            lock.lock()
            defer { lock.unlock() }
            if claimed { return false }
            claimed = true
            return true
        }
    }

    private static func kind(of path: NWPath) -> String {
        guard path.status == .satisfied else { return "none" }
        if path.usesInterfaceType(.wifi) { return "wifi" }
        if path.usesInterfaceType(.cellular) { return "cellular" }
        if path.usesInterfaceType(.wiredEthernet) { return "wired" }
        return "other"   // e.g. only the Tailscale tunnel is visible
    }
}

/// Sends device reports, and decides when the app opening is worth one.
enum DeviceReporter {
    /// Opening the app ten times in an hour is not ten pieces of news.
    /// Automations and Siri always report; only `app_open` is throttled.
    static let appOpenInterval: TimeInterval = 30 * 60

    private static let lastReportKey = "jarvis.lastReportAt"
    private static let deviceIDKey = "jarvis.deviceID"
    static let deviceNameKey = "jarvis.deviceName"

    static var lastReportAt: Date? {
        let seconds = UserDefaults.standard.double(forKey: lastReportKey)
        return seconds > 0 ? Date(timeIntervalSince1970: seconds) : nil
    }

    /// Stable per install of this app. Not identifierForVendor: that changes if
    /// every app from the vendor is deleted, which on a free team that
    /// reinstalls weekly is more likely than it sounds.
    static var deviceID: String {
        if let existing = UserDefaults.standard.string(forKey: deviceIDKey) { return existing }
        let fresh = "ios-" + UUID().uuidString.lowercased()
        UserDefaults.standard.set(fresh, forKey: deviceIDKey)
        return fresh
    }

    static func reportIfDue(via: String) async {
        if let last = lastReportAt, Date.now.timeIntervalSince(last) < appOpenInterval { return }
        _ = try? await report(via: via)
    }

    /// Returns the metrics that were sent, so the caller can show them.
    @discardableResult
    static func report(via: String) async throws -> [String: MetricValue] {
        let client = try ConnectionStore.requireClient()
        let metrics = await DeviceContext.snapshot()
        let report = await makeReport(via: via, metrics: metrics)
        try await client.checkin(report)
        UserDefaults.standard.set(Date.now.timeIntervalSince1970, forKey: lastReportKey)
        return metrics
    }

    @MainActor
    private static func makeReport(via: String, metrics: [String: MetricValue]) -> DeviceReport {
        let device = UIDevice.current
        let name = UserDefaults.standard.string(forKey: deviceNameKey) ?? ""
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        return DeviceReport(
            deviceID: deviceID,
            name: name.isEmpty ? "iPhone" : name,
            platform: "ios",
            model: DeviceContext.modelIdentifier,
            osVersion: device.systemVersion,
            appVersion: version ?? "",
            via: via,
            metrics: metrics
        )
    }
}
