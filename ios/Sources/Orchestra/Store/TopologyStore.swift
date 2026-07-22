import Foundation
import Observation

/// The branch map's data, on the main actor. Same discipline as `FleetStore`:
/// the transport actor returns a value, this store mutates on the main actor,
/// the view reads.
///
/// **It has no stream and no timer** — and that is the whole design (§5.11).
/// `GET /api/topology` is ~90 git subprocesses behind a 30 s server cache; a
/// phone polling it is the measured desktop pathology. So this fetches exactly
/// twice: on appear, and on an explicit pull-to-refresh. The tips' status colours
/// do NOT come from here — they ride the board's state stream, joined by
/// worktree name, so a live status change recolours a tip with no topology fetch.
@MainActor
@Observable
public final class TopologyStore {
    public enum Phase: Sendable, Equatable {
        case cold
        case loading
        case loaded
        case failed(OrchestraError)
    }

    public private(set) var phase: Phase = .cold
    public private(set) var topology: Topology?
    /// When the payload arrived, on the device clock — the map ages against this.
    public private(set) var loadedAt: Date?
    public private(set) var lastError: OrchestraError?

    private let client: OrchestraClient
    private var inFlight: Task<Void, Never>?

    public init(client: OrchestraClient) {
        self.client = client
    }

    /// Fetch once, on appear. Idempotent: a second appear while one is in flight,
    /// or after data already loaded, does nothing — that is what keeps a
    /// re-entered screen from re-paying the git sweep.
    public func load() {
        guard topology == nil, inFlight == nil else { return }
        fetch()
    }

    /// Pull-to-refresh. Always fetches; awaited so `.refreshable` can hold the
    /// spinner until the answer lands.
    public func refresh() async {
        inFlight?.cancel()
        inFlight = nil
        await fetchAwaiting()
    }

    private func fetch() {
        inFlight = Task { [weak self] in await self?.fetchAwaiting() }
    }

    private func fetchAwaiting() async {
        if topology == nil { phase = .loading }
        defer { inFlight = nil }
        do {
            let fresh = try await client.topology()
            topology = fresh
            loadedAt = Date()
            lastError = nil
            phase = .loaded
        } catch let error as OrchestraError {
            if case .cancelled = error { return }
            note(error)
        } catch {
            note(ErrnoCause.classify(error))
        }
    }

    private func note(_ error: OrchestraError) {
        lastError = error
        // Keep the last good map on screen if we have one; only a cold failure is
        // a screen the user must look at. Same rule as the board.
        phase = topology == nil ? .failed(error) : .loaded
    }

    /// Worktree names the board knows about that the topology could not place —
    /// the silent drop `gitrepo.branch_topology` `continue`s past (no base ref, no
    /// merge-base, bad timestamps). Surfaced by DIFFERENCE because the legacy
    /// endpoint ships no `dropped[]` (§5.10 unmapped).
    public func unmapped(boardWorktrees: [String]) -> [String] {
        guard let topology else { return [] }
        let mapped = topology.mappedWorktrees
        return boardWorktrees.filter { !mapped.contains($0) }
    }
}
