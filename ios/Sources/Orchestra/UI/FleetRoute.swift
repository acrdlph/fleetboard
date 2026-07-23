import Foundation

/// Where the Fleet tab's navigation stack can go.
///
/// A value, not a view, so a destination can be pushed by something that is not
/// a tap — which is what makes every screen in this app reachable from a script
/// on a simulator that has no camera and cannot be typed into. See
/// `DebugRoute`.
public enum FleetRoute: Hashable, Sendable {
    case worktree(String)
    /// The branch map (`UX.md` §5). Pushed from the board's toolbar rather than
    /// spending one of the three permanent tabs on it — the map answers "which
    /// worktree is safe to dispatch into", which is a question you ask FROM the
    /// board, so it lives one push away from it.
    case map
    /// Addressed by `(account, sid)` and NOT by anything positional, for the
    /// same reason every mutation is (ADR 0008): the board re-sorts under you,
    /// so "the second session on ConfidAI2" names a different agent a second
    /// later.
    case chat(worktree: String, account: String, sid: String)
}

/// Which sheet a pushed worktree screen should present on appear.
///
/// A value for the same reason `FleetRoute` is one: **phase 3's whole surface is
/// sheets, and `xcrun simctl` cannot tap.** A sheet that compiles and renders
/// blank is precisely the silent failure this project keeps finding, and the only
/// way to look at one from a script is to have something other than a finger
/// press the button. This presents the SAME sheet, from the same state, that the
/// tap presents — it is a way to press the button, not a second way to act.
public enum WorktreeSheet: Hashable, Sendable {
    case finish
    case resume(sid: String)
}
