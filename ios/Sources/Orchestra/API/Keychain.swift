import Foundation
import Security

/// The device token's only home.
///
/// It is `orc1_<devid>_<43 base64url chars>`, minted once by
/// `POST /api/v1/pair`, and it is a permanent credential to a server that can
/// type into terminals running `--dangerously-skip-permissions`. So:
///
/// * **`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`.** `AfterFirstUnlock`
///   because a background refresh and a notification-service extension both have
///   to read it while the phone is locked. `ThisDeviceOnly` because the item
///   must not ride an encrypted backup onto a second device — the whole point of
///   per-device tokens is that revoking one revokes one.
/// * **Never `UserDefaults`, never a file, never a log line.** `auth.audit` was
///   fixed on the server side for exactly this: a token that arrived in a query
///   string was being written to `audit.log.jsonl` in full.
///
/// `SecItem` is thread-safe and this type holds no state, so it is a
/// `nonisolated` value rather than an actor. Making it an actor would serialise
/// a keychain read behind whatever else the actor was doing, for no safety it
/// does not already have.
public struct TokenStore: Sendable {
    public static let service = "sh.orchestra.device-token"

    private let service: String

    public init(service: String = TokenStore.service) {
        self.service = service
    }

    /// Keyed by the server's host so two Macs can be paired at once. `port` is
    /// deliberately NOT part of the key: a server restarted on a different port
    /// is the same server holding the same device registry.
    private func query(_ account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    public func save(token: String, for account: String) throws {
        guard let data = token.data(using: .utf8) else {
            throw KeychainError.encoding
        }
        var q = query(account)
        SecItemDelete(q as CFDictionary)          // upsert; a stale token must never win
        q[kSecValueData as String] = data
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(q as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.status(status) }
    }

    public func token(for account: String) -> String? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        guard status == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    @discardableResult
    public func delete(for account: String) -> Bool {
        SecItemDelete(query(account) as CFDictionary) == errSecSuccess
    }

    public enum KeychainError: Error, Equatable {
        case encoding
        case status(OSStatus)
    }
}

/// A token holder that does not touch the Keychain — for `swift test`, where
/// there is no keychain entitlement and `SecItemAdd` returns `errSecMissingEntitlement`.
public actor InMemoryTokenStore {
    private var tokens: [String: String] = [:]
    public init() {}
    public func save(token: String, for account: String) { tokens[account] = token }
    public func token(for account: String) -> String? { tokens[account] }
    public func delete(for account: String) { tokens[account] = nil }
}

/// What the transport needs from wherever the token lives.
public protocol TokenSource: Sendable {
    func bearerToken() async -> String?
}

/// The real one: Keychain, keyed by host.
public struct KeychainTokenSource: TokenSource {
    private let store: TokenStore
    private let account: String

    public init(store: TokenStore = TokenStore(), account: String) {
        self.store = store
        self.account = account
    }

    public func bearerToken() async -> String? { store.token(for: account) }
}

/// A fixed token, for tests and for the moment between pairing and persisting.
public struct StaticTokenSource: TokenSource {
    private let value: String?
    public init(_ value: String?) { self.value = value }
    public func bearerToken() async -> String? { value }
}
