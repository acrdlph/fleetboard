import Foundation
import Observation

/// Per-account usage.
///
/// **On appear and on an explicit pull, never on a timer.** `GET /api/limits`
/// without `refresh=1` is a cache read and is cheap; the cache behind it is
/// refilled by the server's own collector, so a timer here would only ask a
/// dictionary the same question faster. And `refresh=1` — which this client
/// never sends — shells out to `cclimits` for EVERY account under a 90 s
/// server-side timeout while mutating one global dict. `UX.md` §3.6 is explicit
/// that hiding a 90-second global subprocess behind the cheapest gesture in the
/// app would be a trap; this build does not offer it at all, because a
/// read-only phase has nothing that needs it.
@MainActor
@Observable
public final class LimitsStore {
    public private(set) var report: LimitsReport?
    public private(set) var loading = false
    public private(set) var error: OrchestraError?
    public private(set) var loadedAt: Date?

    private let client: OrchestraClient

    public init(client: OrchestraClient) {
        self.client = client
    }

    public func load() async {
        loading = report == nil
        do {
            report = try await client.limits()
            error = nil
            loadedAt = Date()
        } catch let e as OrchestraError {
            if case .cancelled = e { return }
            error = e
        } catch {
            self.error = ErrnoCause.classify(error)
        }
        loading = false
    }
}
