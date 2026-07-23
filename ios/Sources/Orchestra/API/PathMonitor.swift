import Foundation
import Network

/// Whether this device has a usable network path *at all* — the question
/// `URLSession` can only answer by trying and timing out.
///
/// This exists for one use: the traveller in a dead zone. Without it the stream
/// loop learns it is offline the slow way — open a socket, wait out a timeout,
/// back off, do it three more times before it will say the word. `NWPathMonitor`
/// knows the instant the radio drops and the instant it returns, so the loop can
/// know instantly, wait quietly, and resume the moment a path comes back —
/// which also stops it hammering a radio that has nothing to talk to, the thing
/// that drains a battery fastest exactly when it is scarcest.
///
/// It reports the DEVICE's path, not the server's reachability. A satisfied path
/// with a stream that still fails is the useful distinction: the phone has
/// network, but the tailnet or the server is not answering — two different
/// sentences for the person standing in a field trying to figure out why.
///
/// No entitlement. `NWPathMonitor` reads the same path the OS shows in the
/// status bar; it is not Wi-Fi Aware, not a network extension, not slicing —
/// none of which would help here and all of which cost a provisioning line.
@MainActor
@Observable
public final class PathMonitor {
    /// There is a usable path off this device. The default is `true` so a client
    /// created before the first callback does not spuriously read as offline and
    /// refuse to even try.
    public private(set) var isSatisfied = true

    /// The path is cellular or a personal hotspot — metered. Surfaced so a future
    /// policy could hold large side-fetches for Wi-Fi; nothing acts on it yet.
    public private(set) var isExpensive = false

    /// Low Data Mode, or a constrained interface. Same status as `isExpensive`:
    /// observed, not yet acted on.
    public private(set) var isConstrained = false

    private let monitor = NWPathMonitor()
    private let queue = DispatchQueue(label: "sh.orchestra.path")
    private var waiters: [UUID: CheckedContinuation<Void, Never>] = [:]
    private var started = false

    public init() {}

    /// Begin watching. Idempotent. The handler fires on `queue`; the Sendable
    /// primitives are lifted out there and hopped to the main actor, because
    /// `NWPath` itself is not `Sendable` and the state it feeds is read by
    /// SwiftUI during layout.
    public func start() {
        guard !started else { return }
        started = true
        monitor.pathUpdateHandler = { [weak self] path in
            let satisfied = path.status == .satisfied
            let expensive = path.isExpensive
            let constrained = path.isConstrained
            Task { @MainActor [weak self] in
                self?.apply(satisfied: satisfied, expensive: expensive,
                            constrained: constrained)
            }
        }
        monitor.start(queue: queue)
    }

    public func stop() {
        monitor.cancel()
        started = false
        // A monitor that is going away must not strand a loop parked in
        // `waitUntilSatisfied` — release them; the caller re-checks `isSatisfied`.
        let parked = waiters.values
        waiters.removeAll()
        for w in parked { w.resume() }
    }

    /// Return immediately if there is a path; otherwise suspend until one
    /// appears. Respects cancellation: a cancelled task is released and its
    /// continuation cleaned up, so the stream loop's own `Task.isCancelled`
    /// check does the rest.
    public func waitUntilSatisfied() async {
        if isSatisfied { return }
        let id = UUID()
        await withTaskCancellationHandler {
            await withCheckedContinuation { cont in
                if isSatisfied { cont.resume(); return }
                waiters[id] = cont
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.waiters.removeValue(forKey: id)?.resume()
            }
        }
    }

    private func apply(satisfied: Bool, expensive: Bool, constrained: Bool) {
        isExpensive = expensive
        isConstrained = constrained
        guard satisfied != isSatisfied else { return }
        isSatisfied = satisfied
        guard satisfied else { return }
        // Rose from unsatisfied to satisfied: wake everyone waiting on a path.
        let parked = waiters.values
        waiters.removeAll()
        for w in parked { w.resume() }
    }
}
