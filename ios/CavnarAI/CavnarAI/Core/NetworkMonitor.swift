import Network
import Observation

/// The app's single source of connectivity truth.
///
/// Before this, every transport failure collapsed into one string ("Couldn't
/// reach the server") whether the device was in a walk-in cooler or the
/// backend was down, and there was nothing to trigger a retry on (audit 6.3).
/// This is also what wakes PendingWriteQueue: deferred writes need a
/// reconnect signal or they never drain.
@Observable
@MainActor
final class NetworkMonitor {
    static let shared = NetworkMonitor()

    private(set) var isOnline = true
    /// Low Data Mode, a personal hotspot, or a metered link — worth knowing
    /// before kicking off a large refresh in a restaurant on tethered data.
    private(set) var isConstrained = false

    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "ai.cavnar.network-monitor")

    init() {
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            let constrained = path.isConstrained
            Task { @MainActor [weak self] in
                guard let self else { return }
                let cameBackOnline = !self.isOnline && online
                self.isOnline = online
                self.isConstrained = constrained
                if cameBackOnline {
                    await PendingWriteQueue.shared.drain()
                }
            }
        }
        monitor.start(queue: queue)
    }

    deinit { monitor.cancel() }
}
