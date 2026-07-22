import Foundation
import Observation

/// The board's state, on the main actor.
///
/// `@MainActor` is not decoration here. Every property is read by SwiftUI during
/// a layout pass, and `@Observable`'s change tracking has no isolation of its
/// own — a write from a background task is a data race that shows up as a
/// corrupted diff rather than as a crash. The rule for the whole app is: values
/// cross from the transport actor, stores mutate on the main actor, views read.
@MainActor
@Observable
public final class FleetStore {
    public enum Phase: Sendable, Equatable {
        /// Nothing has ever loaded. This is the ONLY state that may show a
        /// skeleton — every later failure keeps the last good board.
        case cold
        case loading
        case loaded
        case failed(OrchestraError)
    }

    public private(set) var phase: Phase = .cold
    public private(set) var state: FleetState?
    /// Retained across failures on purpose: `UX.md` §3.1.5 — "skeletons never
    /// replace live data". A board that is 40 s old and says so beats a spinner.
    public private(set) var lastGoodAt: Date?
    /// The last transport failure, even when a stale board is still on screen.
    public private(set) var lastError: OrchestraError?
    /// Decode surprises, counted rather than swallowed. An enum widened to
    /// `.unknown` is a server change nobody told the client about, and it should
    /// be visible somewhere other than a pixel.
    public private(set) var unknownStatuses: Int = 0

    private let client: OrchestraClient

    public init(client: OrchestraClient) {
        self.client = client
    }

    public var groups: [Triage.Group] {
        Triage.groups(state?.worktrees ?? [])
    }

    public var headline: Triage.Headline {
        Triage.headline(state?.worktrees ?? [])
    }

    public func refresh() async {
        if state == nil { phase = .loading }
        do {
            let fresh = try await client.fleetState()
            apply(fresh)
        } catch let error as OrchestraError {
            if case .cancelled = error { return }
            lastError = error
            // A board already on screen stays on screen. Only "we have nothing
            // and cannot get anything" is a failure state the user must look at.
            if state == nil || !error.keepsLastGoodData {
                if !error.keepsLastGoodData { state = nil }
                phase = .failed(error)
            } else {
                phase = .loaded
            }
        } catch {
            lastError = ErrnoCause.classify(error)
            phase = state == nil ? .failed(ErrnoCause.classify(error)) : .loaded
        }
    }

    /// Split out so a test can drive it with a fixture and never touch a socket.
    public func apply(_ fresh: FleetState) {
        state = fresh
        lastGoodAt = Date()
        lastError = nil
        phase = .loaded
        unknownStatuses = fresh.worktrees.reduce(0) { total, card in
            total + card.sessions.filter { $0.status == .unknown }.count
        }
    }
}
