import Foundation
import Observation

/// What the phone can afford right now (iOS parity audit 10/7/26 #83) —
/// the one place every ambient animation and every optional network read
/// asks, rather than each screen reading ProcessInfo on its own.
///
/// Two answers:
///  - `reducedActivity`: Low Power Mode is on, or the device is running
///    hot (thermal state serious or critical). Ambient motion — Home's
///    30 fps obsidian field, the swipe-hint chevron, the empty state's
///    breath — freezes on its resting frame. Motion that reports progress
///    (a "working" indicator) keeps going: a frozen one reads as hung.
///  - `reducedNetwork`: the link is constrained (Low Data Mode, a personal
///    hotspot — NetworkMonitor.isConstrained). Reads nobody asked for
///    wait: the forced widget refresh, warming the tabs not yet opened.
///    What the owner opened always loads.
///
/// Observable, so a view reading `CavnarEnvironment.shared.reducedActivity`
/// in its body re-renders when the owner turns Low Power Mode on or off.
@Observable
@MainActor
final class CavnarEnvironment {
    static let shared = CavnarEnvironment()

    private(set) var lowPowerMode: Bool
    private(set) var thermalState: ProcessInfo.ThermalState

    /// Low Power Mode, or serious-or-worse heat: ambient motion freezes.
    var reducedActivity: Bool { Self.reduces(lowPower: lowPowerMode, thermal: thermalState) }

    /// A constrained link: optional, unasked-for reads are skipped.
    var reducedNetwork: Bool { NetworkMonitor.shared.isConstrained }

    static var reducedActivity: Bool { shared.reducedActivity }
    static var reducedNetwork: Bool { shared.reducedNetwork }

    @ObservationIgnored private var observers: [NSObjectProtocol] = []

    init(center: NotificationCenter = .default) {
        lowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled
        thermalState = ProcessInfo.processInfo.thermalState
        observers.append(center.addObserver(forName: .NSProcessInfoPowerStateDidChange, object: nil,
                                            queue: .main) { [weak self] _ in
            Task { @MainActor in self?.lowPowerMode = ProcessInfo.processInfo.isLowPowerModeEnabled }
        })
        observers.append(center.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            Task { @MainActor in self?.thermalState = ProcessInfo.processInfo.thermalState }
        })
    }

    /// The rule itself, for the tests: Low Power Mode, or `.serious` and
    /// `.critical` heat. `.fair` is ordinary warmth under load.
    nonisolated static func reduces(lowPower: Bool, thermal: ProcessInfo.ThermalState) -> Bool {
        lowPower || thermal == .serious || thermal == .critical
    }
}
