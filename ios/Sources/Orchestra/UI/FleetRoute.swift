import Foundation

/// Where the Fleet tab's navigation stack can go.
///
/// A value, not a view, so a destination can be pushed by something that is not
/// a tap — which is what makes every screen in this app reachable from a script
/// on a simulator that has no camera and cannot be typed into. See
/// `DebugRoute`.
public enum FleetRoute: Hashable, Sendable {
    case worktree(String)
    /// Addressed by `(account, sid)` and NOT by anything positional, for the
    /// same reason every mutation is (ADR 0008): the board re-sorts under you,
    /// so "the second session on ConfidAI2" names a different agent a second
    /// later.
    case chat(worktree: String, account: String, sid: String)
}
