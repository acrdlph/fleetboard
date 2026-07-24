import Foundation
import LocalAuthentication
import Observation

/// The device-owner check that stands in front of the paired app.
///
/// **Policy is `.deviceOwnerAuthentication`, never `.deviceOwnerAuthenticationWithBiometrics`.**
/// The biometrics-only policy *fails to evaluate at all* on a phone with no Face
/// ID / Touch ID enrolled, which would lock the owner out with no recovery.
/// `.deviceOwnerAuthentication` tries biometry first and falls back to the device
/// passcode itself, so every phone with a passcode can pass — the same reason
/// `ARCHITECTURE.md §5.3` makes `.or .devicePasscode` mandatory on the `act`
/// token's ACL.
///
/// **This gate is advisory (threat T10).** A rooted phone defeats it, and it does
/// not touch the token or the wire. The controls that actually hold are
/// server-side — per-device tokens, rate limits, audit, one-tap revoke. So the
/// copy in front of it does not oversell it, and this type gates *visibility and
/// interaction*, not reads: the fleet stream is a `read`, and `§5.3` deliberately
/// keeps reads working with no user present.
@MainActor
@Observable
final class BiometricGate {
    /// `locked` is the resting state before a prompt or after a re-lock;
    /// `failed` carries an already-humanised, non-blaming reason for the last
    /// attempt and is *not* re-prompted automatically (the user asked to stop, or
    /// something needs their attention) — only `locked` auto-prompts.
    enum Phase: Equatable {
        case locked
        case authenticating
        case unlocked
        case failed(String)
    }

    private(set) var phase: Phase = .locked

    /// Bumped by every `lock()`. An evaluation that started under an older
    /// generation must not overwrite the phase — the app was re-locked (a
    /// background transition, another `lock()`) while its system sheet was up.
    @ObservationIgnored private var generation = 0

    var isUnlocked: Bool { phase == .unlocked }
    var isAuthenticating: Bool { phase == .authenticating }

    /// Prompt only from a cold `locked` state. Safe to call from both the lock
    /// view's `.task` (cold launch) and the foreground transition (`.active`)
    /// without stacking a second system sheet — and it will not re-prompt after a
    /// deliberate cancel, which lands in `failed`, not `locked`.
    func authenticateIfNeeded() async {
        guard phase == .locked else { return }
        await authenticate()
    }

    /// Force an evaluation — the explicit "Unlock" / "Try again" button, and the
    /// engine behind `authenticateIfNeeded`.
    func authenticate() async {
        guard phase != .unlocked, phase != .authenticating else { return }
        let gen = generation
        phase = .authenticating

        let context = LAContext()
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) else {
            // Neither a passcode nor biometry is configured, so iOS itself
            // offers no lock to stand behind. Blocking here would strand the
            // owner with no way in; the real controls are server-side (T10).
            // Fail open rather than brick the app — a phone with no device lock
            // is already wide open to whoever holds it.
            if gen == generation { phase = .unlocked }
            return
        }

        let outcome: Outcome = await withCheckedContinuation { continuation in
            context.evaluatePolicy(.deviceOwnerAuthentication,
                                   localizedReason: Self.reason) { success, error in
                continuation.resume(returning: success ? .ok : .denied(Self.message(for: error)))
            }
        }

        // A `lock()` (a background transition) may have fired while the sheet was
        // up. If so, stay locked and let the next foreground re-prompt cleanly.
        guard gen == generation else { return }
        switch outcome {
        case .ok:
            phase = .unlocked
        case .denied(let why):
            phase = .failed(why)
        }
    }

    /// Re-lock so a phone left unlocked and handed over must re-authenticate on
    /// return to the foreground. Invalidates any evaluation currently in flight.
    func lock() {
        generation &+= 1
        phase = .locked
    }

    private enum Outcome: Sendable {
        case ok
        case denied(String)
    }

    private static let reason = "Unlock orchestra to reach the fleet."

    /// LocalAuthentication error → one honest, non-blaming line. `nonisolated`
    /// because it is called from `evaluatePolicy`'s reply, which runs off the
    /// main actor.
    nonisolated static func message(for error: (any Error)?) -> String {
        guard let error = error as? LAError else {
            return "Couldn't confirm it's you. Try again."
        }
        switch error.code {
        case .userCancel, .appCancel, .systemCancel:
            return "Unlock cancelled."
        case .authenticationFailed:
            return "That didn't match. Try again."
        case .biometryLockout:
            return "Too many tries — use your passcode to unlock."
        case .userFallback:
            // `.deviceOwnerAuthentication` presents passcode entry itself, so
            // this is rare; handled for completeness.
            return "Enter your passcode to unlock."
        case .passcodeNotSet, .biometryNotAvailable, .biometryNotEnrolled:
            // `canEvaluatePolicy` should have caught these; if one slips through,
            // stay graceful rather than blaming.
            return "This device can't run the unlock check right now."
        default:
            return "Couldn't confirm it's you. Try again."
        }
    }
}
