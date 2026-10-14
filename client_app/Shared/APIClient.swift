import Foundation

final class NoRedirects: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

final class APIClient: @unchecked Sendable {
    let origin: URL
    private let token: String?
    private let session: URLSession
    init(origin: URL, token: String? = nil, session: URLSession? = nil) {
        self.origin = origin; self.token = token
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        configuration.timeoutIntervalForRequest = 30
        self.session = session ?? URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }

    deinit { session.invalidateAndCancel() }

    func request(_ path: String, method: String = "GET", body: Data? = nil, key: String? = nil,
                 contentType: String = "application/json") throws -> URLRequest {
        guard path.hasPrefix("/api/v1/"), !path.contains("\\"),
              let url = URL(string: path, relativeTo: origin)?.absoluteURL,
              url.scheme == origin.scheme, url.host == origin.host, url.port == origin.port,
              url.user == nil, url.password == nil, url.fragment == nil,
              url.path.hasPrefix("/api/v1/"), !url.path.contains("..") else {
            throw ClientError("The server returned an unsafe resource URL.")
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData)
        request.httpMethod = method; request.httpBody = body
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let key { request.setValue(key, forHTTPHeaderField: "Idempotency-Key") }
        if method != "GET" { request.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        return request
    }

    static func checked(_ data: Data, _ response: URLResponse?) throws -> Data {
        guard let response = response as? HTTPURLResponse else { throw ClientError("No response from the server. Check Tailscale and retry.") }
        guard (200..<300).contains(response.statusCode) else {
            if let error = try? Wire.decoder.decode(APIError.self, from: data) { throw error }
            throw ClientError("Server returned HTTP \(response.statusCode). Check that the application API is deployed.")
        }
        return data
    }
    func data(_ path: String, method: String = "GET", body: Data? = nil, key: String? = nil) async throws -> Data {
        let (data, response) = try await session.data(for: request(path, method: method, body: body, key: key))
        return try Self.checked(data, response)
    }
    func get<T: Decodable>(_ path: String, as type: T.Type = T.self) async throws -> T {
        try Wire.decoder.decode(T.self, from: await data(path))
    }
    func post<T: Decodable>(_ path: String, body: [String: Any]? = nil, key: String? = nil, as type: T.Type = T.self) async throws -> T {
        let encoded = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        return try Wire.decoder.decode(T.self, from: await data(path, method: "POST", body: encoded, key: key))
    }
    func status(for connection: Connection) async throws -> ServerStatus {
        let result: ServerStatus = try await get("/api/v1/status")
        guard result.apiVersion == "1", result.serverId == connection.serverId, result.user.id == connection.user.id else {
            throw ClientError("The server or account identity changed. Disconnect and register again.")
        }
        return result
    }
    static func segment(_ id: String) throws -> String {
        guard id.range(of: "^[A-Za-z0-9_-]{1,128}$", options: .regularExpression) != nil else { throw ClientError("Invalid resource identifier from server.") }
        return id
    }
}
