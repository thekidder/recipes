import Foundation

struct EnrollmentLink {
    let origin: URL
    let code: String

    init(_ url: URL) throws {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "erecipe", parts.host == "enroll", parts.path.isEmpty,
              parts.user == nil, parts.password == nil, parts.port == nil, parts.fragment == nil,
              let items = parts.queryItems, items.count == 3,
              Set(items.map(\.name)) == Set(["v", "server", "code"]),
              items.first(where: { $0.name == "v" })?.value == "1",
              let server = items.first(where: { $0.name == "server" })?.value,
              let code = items.first(where: { $0.name == "code" })?.value,
              code.range(of: "^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$", options: .regularExpression) != nil else {
            throw ClientError("This registration link is invalid or uses an unsupported version. Request a new QR code.")
        }
        self.origin = try Self.validateOrigin(server)
        self.code = code
    }

    static func validateOrigin(_ string: String) throws -> URL {
        guard !string.contains("\\"), !string.contains(where: { $0.isWhitespace }),
              let parts = URLComponents(string: string), parts.scheme == "https",
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil, parts.path.isEmpty,
              parts.query == nil, parts.fragment == nil,
              parts.port == nil || (1...65535).contains(parts.port!),
              let url = parts.url else { throw ClientError("Registration requires an HTTPS server origin without a path, query, or credentials.") }
        return url
    }
}
