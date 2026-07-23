import Foundation
import Observation

/// The one-way channel from a notification tap to the navigation stack.
///
/// A tap is not a tap on a `NavigationLink` — it arrives in the notification
/// delegate, off any view — so it cannot push a route directly. It deposits a
/// `PushDeepLink` here and bumps `generation`; `RootView` watches `generation`
/// to select the Fleet tab, and `FleetView` watches it to resolve the account
/// (which the payload never carried) from the live board and push the exact
/// session. The account resolution is why this holds a `PushDeepLink` and not a
/// `FleetRoute`: the route cannot be built until something can look the board up.
///
/// `generation` rather than just a non-nil value because two taps on
/// notifications about the SAME session must both navigate — an `onChange` on
/// the link alone would ignore the second.
@MainActor
@Observable
public final class PushRouter {
    // `public` because it rides `FleetView`'s public initializer. It lives in the
    // App layer (there is no notification tap to route in the headless test
    // module), so the access level is a build-graph formality, not an API.
    public private(set) var pendingDeepLink: PushDeepLink?
    public private(set) var generation = 0

    public init() {}

    /// A notification asked to open a screen. Deposited for the views to pick up.
    public func navigate(to link: PushDeepLink) {
        pendingDeepLink = link
        generation &+= 1
    }

    /// Take the pending link, clearing it. Called by whichever view is going to
    /// act on it, so it fires once.
    public func consume() -> PushDeepLink? {
        defer { pendingDeepLink = nil }
        return pendingDeepLink
    }
}
