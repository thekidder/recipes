import Foundation
import Security

struct TokenVault {
    let group: String
    private func query(_ connection: Connection) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "eRecipe",
         kSecAttrAccount as String: connection.context, kSecAttrAccessGroup as String: group]
    }
    func save(_ token: String, for connection: Connection) throws {
        var value = query(connection)
        SecItemDelete(value as CFDictionary)
        value[kSecValueData as String] = Data(token.utf8)
        value[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        guard SecItemAdd(value as CFDictionary, nil) == errSecSuccess else {
            throw ClientError("Could not save the credential. Check Keychain Sharing entitlements. If the code was consumed, revoke its credential in the dashboard and create a new QR.")
        }
    }
    func token(for connection: Connection) throws -> String {
        if connection.isDemo { return "demo" }
        var value = query(connection)
        value[kSecReturnData as String] = true; value[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(value as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data, let token = String(data: data, encoding: .utf8) else {
            throw ClientError("Unlock your phone, or register again if this credential is no longer available.")
        }
        return token
    }
    func remove(_ connection: Connection) throws {
        guard !connection.isDemo else { return }
        let status = SecItemDelete(query(connection) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw ClientError("Could not remove the saved credential.") }
    }
}

#if canImport(UIKit)
import UIKit

final class ClientEnvironment {
    let store: SharedStore
    let vault: TokenVault
    let group: String
    let demo: Bool
    lazy var uploads = UploadManager(environment: self)

    init(demo: Bool = false) throws {
        self.demo = demo
        guard let group = Bundle.main.object(forInfoDictionaryKey: "RecipeAppGroup") as? String,
              let keychain = Bundle.main.object(forInfoDictionaryKey: "RecipeKeychainGroup") as? String else {
            throw ClientError("App sharing configuration is missing.")
        }
        self.group = group; self.vault = TokenVault(group: keychain)
        let root: URL
        if demo {
            root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Demo")
        } else {
            guard let container = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group) else {
                throw ClientError("Shared storage is unavailable. Configure the same App Group on the app and Share extension.")
            }
            root = container.appendingPathComponent("eRecipe")
        }
        store = try SharedStore(root: root)
        if demo {
            try store.setConnection(Connection(origin: URL(string: "https://recipes.example.invalid")!, serverId: "server_demo",
                user: Person(id: "user_alex", displayName: "Alex"), credentialId: "credential_phone", isDemo: true))
        }
    }
    func api(for connection: Connection) throws -> APIClient {
        #if DEBUG
        if connection.isDemo { return APIClient(origin: connection.origin, token: "demo", session: DemoProtocol.session()) }
        #endif
        return APIClient(origin: connection.origin, token: try vault.token(for: connection))
    }
    func currentConnection() throws -> Connection {
        guard let connection = try store.connection() else { throw ClientError("Open eRecipe and register using the dashboard QR code first.") }
        return connection
    }
}
#endif
