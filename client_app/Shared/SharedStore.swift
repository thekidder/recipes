import Foundation
import SQLite3

final class SharedStore: @unchecked Sendable {
    let root: URL
    private let lock = NSRecursiveLock()
    private var db: OpaquePointer?
    private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init(root: URL) throws {
        self.root = root
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        guard sqlite3_open_v2(root.appendingPathComponent("state.sqlite").path, &db,
                              SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE | SQLITE_OPEN_FULLMUTEX, nil) == SQLITE_OK else {
            throw ClientError("Unable to open shared recipe storage.")
        }
        sqlite3_busy_timeout(db, 5000)
        try execute("PRAGMA journal_mode=WAL")
        try execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        #if os(iOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: root.path)
        #endif
    }
    deinit { sqlite3_close(db) }
    private func execute(_ sql: String) throws {
        guard sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK else { throw ClientError("Could not update shared recipe storage.") }
    }
    private func read<T: Decodable>(_ id: String, as: T.Type) throws -> T? {
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT body FROM records WHERE id = ?", -1, &statement, nil) == SQLITE_OK else { throw ClientError("Could not read shared storage.") }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_text(statement, 1, id, -1, transient)
        let result = sqlite3_step(statement)
        if result == SQLITE_DONE { return nil }
        guard result == SQLITE_ROW, let bytes = sqlite3_column_text(statement, 0) else { throw ClientError("Could not read shared storage.") }
        return try JSONDecoder().decode(T.self, from: Data(String(cString: bytes).utf8))
    }
    private func write<T: Encodable>(_ id: String, _ value: T) throws {
        let string = String(decoding: try JSONEncoder().encode(value), as: UTF8.self)
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, "INSERT OR REPLACE INTO records(id, body) VALUES (?, ?)", -1, &statement, nil) == SQLITE_OK else { throw ClientError("Could not save shared storage.") }
        defer { sqlite3_finalize(statement) }
        sqlite3_bind_text(statement, 1, id, -1, transient)
        sqlite3_bind_text(statement, 2, string, -1, transient)
        guard sqlite3_step(statement) == SQLITE_DONE else { throw ClientError("Could not save shared storage.") }
    }
    func connection() throws -> Connection? { lock.lock(); defer { lock.unlock() }; return try read("connection", as: Connection.self) }
    func setConnection(_ value: Connection) throws { lock.lock(); defer { lock.unlock() }; try write("connection", value) }
    func item(_ id: String) throws -> PendingShare? { lock.lock(); defer { lock.unlock() }; return try read(id, as: PendingShare.self) }
    func items(context: String) throws -> [PendingShare] {
        lock.lock(); defer { lock.unlock() }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT body FROM records WHERE id != 'connection'", -1, &statement, nil) == SQLITE_OK else { throw ClientError("Could not list pending shares.") }
        defer { sqlite3_finalize(statement) }
        var result = [PendingShare]()
        while true {
            let step = sqlite3_step(statement)
            if step == SQLITE_DONE { break }
            guard step == SQLITE_ROW, let bytes = sqlite3_column_text(statement, 0) else { throw ClientError("Could not list pending shares.") }
            let item = try JSONDecoder().decode(PendingShare.self, from: Data(String(cString: bytes).utf8))
            if item.context == context { result.append(item) }
        }
        return result.sorted { $0.createdAt > $1.createdAt }
    }
    func bodyURL(_ id: String) -> URL { root.appendingPathComponent(id + ".upload") }
    func create(input: ShareInput, connection: Connection, limits: ImportLimits, autoSend: Bool = true) throws -> PendingShare {
        lock.lock(); defer { lock.unlock() }
        try execute("BEGIN IMMEDIATE")
        do {
            guard try self.connection()?.context == connection.context else { throw ClientError("Registration changed. Reopen this share.") }
            let (body, type) = try input.requestBody(limits: limits)
            let id = UUID().uuidString
            let item = PendingShare(id: id, context: connection.context, createdAt: Date(), summary: input.summary,
                                    contentType: type, importKey: UUID().uuidString, autoSend: autoSend)
            try body.write(to: bodyURL(id), options: .atomic)
            try write(id, item)
            try execute("COMMIT")
            return item
        } catch { try? execute("ROLLBACK"); throw error }
    }

    @discardableResult func update(_ id: String, _ change: (inout PendingShare) throws -> Void) throws -> PendingShare {
        lock.lock(); defer { lock.unlock() }
        try execute("BEGIN IMMEDIATE")
        do {
            guard var value: PendingShare = try read(id, as: PendingShare.self),
                  let connection: Connection = try read("connection", as: Connection.self), value.context == connection.context else {
                throw ClientError("This share belongs to a disconnected account.")
            }
            try change(&value)
            try write(id, value)
            try execute("COMMIT")
            return value
        } catch { try? execute("ROLLBACK"); throw error }
    }
    func clear() throws {
        lock.lock(); defer { lock.unlock() }
        try execute("DELETE FROM records")
        for file in try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) where ["upload", "publication"].contains(file.pathExtension) {
            try FileManager.default.removeItem(at: file)
        }
    }
}
