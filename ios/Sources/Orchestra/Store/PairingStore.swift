import Foundation
import Observation

/// Owns "which server, and do we have a credential for it".
///
/// The whole app is gated on this: with no token there is nothing to show and no
/// request worth making, so `RootView` renders pairing until `isPaired` and the
/// fleet after.
@MainActor
@Observable
public final class PairingStore {
    public enum Step: Sendable, Equatable {
        case unpaired
        case pairing
        case paired
        case failed(OrchestraError)
    }

    public private(set) var step: Step = .unpaired
    public private(set) var profile: ServerProfile?
    /// Surfaced rather than swallowed. A Keychain write that failed leaves an
    /// app that works until it is relaunched and then mysteriously does not —
    /// exactly the silent failure this project keeps finding.
    public private(set) var keychainWarning: String?

    private let client: OrchestraClient
    private let tokens: TokenStore
    private let defaults: UserDefaults

    private static let profileKey = "sh.orchestra.server-profile"

    public init(client: OrchestraClient,
                tokens: TokenStore = TokenStore(),
                defaults: UserDefaults = .standard) {
        self.client = client
        self.tokens = tokens
        self.defaults = defaults
    }

    public var isPaired: Bool {
        if case .paired = step { return true }
        return false
    }

    /// Reload a profile persisted by an earlier launch and hand it to the
    /// transport. Called once, before the first view appears.
    public func restore() async {
        guard let data = defaults.data(forKey: Self.profileKey),
              let saved = try? JSONDecoder().decode(ServerProfile.self, from: data)
        else { return }
        guard tokens.token(for: saved.host) != nil else {
            // A profile with no token is not a paired server; it is a leftover.
            // Clearing it is what makes "re-pair" the obvious next screen rather
            // than a permanent 401 loop.
            defaults.removeObject(forKey: Self.profileKey)
            return
        }
        profile = saved
        await client.configure(profile: saved,
                               tokens: KeychainTokenSource(store: tokens, account: saved.host))
        step = .paired
    }

    /// Claim a pairing code. The ticket carries the address the SERVER
    /// advertised for itself, so this never depends on the user typing one.
    public func pair(with ticket: PairingTicket, label: String) async {
        step = .pairing
        keychainWarning = nil
        do {
            let response = try await client.pair(ticket, label: label)
            let saved = ServerProfile(response.server, deviceID: response.deviceID)
            // The server reports the address it is BOUND to, which is the one
            // worth keeping; but if it reports something empty, fall back to the
            // address that just worked.
            let profile = saved.host.isEmpty
                ? ServerProfile(host: ticket.host, port: ticket.port,
                                hostname: saved.hostname, deviceID: saved.deviceID)
                : saved
            do {
                try tokens.save(token: response.token, for: profile.host)
            } catch {
                keychainWarning = "The token could not be written to the Keychain "
                    + "(\(error)). This pairing will be lost when the app quits."
            }
            if let data = try? JSONEncoder().encode(profile) {
                defaults.set(data, forKey: Self.profileKey)
            }
            self.profile = profile
            // Hand the transport the live token directly as well as the Keychain
            // source, so a Keychain that refused the write still produces a
            // working session for as long as the process lives.
            let source: any TokenSource = keychainWarning == nil
                ? KeychainTokenSource(store: tokens, account: profile.host)
                : StaticTokenSource(response.token)
            await client.configure(profile: profile, tokens: source)
            step = .paired
        } catch let error as OrchestraError {
            step = .failed(error)
        } catch {
            step = .failed(ErrnoCause.classify(error))
        }
    }

    public func unpair() async {
        if let profile { tokens.delete(for: profile.host) }
        defaults.removeObject(forKey: Self.profileKey)
        profile = nil
        await client.configure(profile: nil, tokens: StaticTokenSource(nil))
        step = .unpaired
    }
}
