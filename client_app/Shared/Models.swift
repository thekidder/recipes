import Foundation

struct Person: Codable, Equatable { let id: String; let displayName: String }
struct Credential: Codable { let id: String }
struct EnrollmentReceipt: Decodable {
    let user: Person
    let credential: Credential
    let accessToken: String
    let tokenType: String
    let serverId: String
}
struct Connection: Codable, Equatable {
    let origin: URL
    let serverId: String
    let user: Person
    let credentialId: String
    var isDemo = false
    var context: String { "\(serverId):\(user.id):\(credentialId)" }
}
struct ImportLimits: Codable {
    let textCharacters: Int
    let jsonBodyBytes: Int
    let urlCharacters: Int
    let imageCount: Int
    let imageFileBytes: Int
    let imageTotalBytes: Int
    let multipartBodyBytes: Int
    let imagePixels: Int
    let imageMediaTypes: [String]
}
struct Capabilities: Codable {
    let importKinds: [String]
    let publication: Bool
    let renderProfileIds: [String]
    let limits: ImportLimits
}
struct Readiness: Codable {
    let worker: String
    let extraction: String
    let rendering: String
    let issues: [ErrorDetail]
    var isReady: Bool { worker == "ready" && extraction == "ready" && rendering == "ready" }
}
struct ServerStatus: Codable {
    let apiVersion: String
    let contractVersion: String
    let serverId: String
    let user: Person
    let capabilities: Capabilities
    let readiness: Readiness
}
struct ErrorDetail: Codable { let code: String; let message: String; let retryable: Bool }
struct APIError: Decodable, LocalizedError {
    let error: ErrorDetail
    let requestId: String
    var errorDescription: String? { "\(error.message) (\(error.code); request \(requestId))" }
}
struct ClientError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
struct ImportReceipt: Codable { let recipeId: String; let jobId: String }
struct JobResult: Codable { let revisionId: String?; let renderSetId: String? }
struct Job: Codable {
    let id: String
    let kind: String
    let status: String
    let retryAllowed: Bool
    let error: ErrorDetail?
    let result: JobResult
    let downstreamJobIds: [String]
    var sourceCaptureId: String? = nil
}
struct Revision: Codable { let id: String; let title: String; let warnings: [String] }
struct Asset: Codable { let id: String; let href: String; let mediaType: String; let byteSize: Int }
struct RenderPage: Codable, Identifiable { let pageNumber: Int; let asset: Asset; var id: Int { pageNumber } }
struct RenderSet: Codable { let id: String; let revisionId: String; let pageCount: Int; let pages: [RenderPage] }
struct Publication: Codable { let id: String; let renderSetId: String?; let pageNumber: Int? }
struct PublicationReceipt: Codable { let status: String; let publication: Publication }
struct Device: Decodable { let paired: Bool; let selectedPublication: Publication?; let selectedAssetFetchedAt: String? }

struct ImageInput: Codable { let filename: String; let mediaType: String; let data: Data }
struct ShareInput: Codable {
    let kind: String
    var text: String? = nil
    var url: String? = nil
    var images: [ImageInput] = []
    static func fromText(_ text: String) -> ShareInput {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if let parts = URLComponents(string: trimmed), ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
           parts.host != nil, trimmed.rangeOfCharacter(from: .whitespacesAndNewlines) == nil {
            return ShareInput(kind: "url", url: trimmed)
        }
        return ShareInput(kind: "text", text: text)
    }
    var summary: String {
        switch kind {
        case "url": return url ?? "Website"
        case "images": return "\(images.count) recipe photo\(images.count == 1 ? "" : "s")"
        default: return String((text ?? "Recipe").prefix(100))
        }
    }
    func requestBody(limits: ImportLimits) throws -> (Data, String) {
        if kind == "text" {
            guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  text.unicodeScalars.count <= limits.textCharacters else {
                throw ClientError("Share nonempty recipe text, up to \(limits.textCharacters) characters.")
            }
            let data = try JSONSerialization.data(withJSONObject: ["kind": "text", "text": text])
            guard data.count <= limits.jsonBodyBytes else { throw ClientError("Encoded text exceeds the server's upload limit.") }
            return (data, "application/json")
        }
        if kind == "url" {
            guard let url, url.unicodeScalars.count <= limits.urlCharacters,
                  url.rangeOfCharacter(from: .whitespacesAndNewlines.union(.controlCharacters)) == nil, !url.contains("\\"),
                  let parts = URLComponents(string: url), ["https", "http"].contains(parts.scheme?.lowercased() ?? ""),
                  let host = parts.host, !host.isEmpty, parts.user == nil, parts.password == nil,
                  parts.port == nil || parts.port == (parts.scheme?.lowercased() == "https" ? 443 : 80) else { throw ClientError("Share one public HTTP or HTTPS recipe URL without credentials, using its standard port.") }
            let data = try JSONSerialization.data(withJSONObject: ["kind": "url", "url": url])
            guard data.count <= limits.jsonBodyBytes else { throw ClientError("The URL exceeds the upload limit.") }
            return (data, "application/json")
        }
        guard kind == "images", !images.isEmpty, images.count <= limits.imageCount else {
            throw ClientError("Share up to \(limits.imageCount) JPEG or PNG images.")
        }
        guard images.allSatisfy({ limits.imageMediaTypes.contains($0.mediaType) && $0.data.count <= limits.imageFileBytes }),
              images.reduce(0, { $0 + $1.data.count }) <= limits.imageTotalBytes else {
            throw ClientError("Images exceed the server's file size or media type limits.")
        }
        for image in images { try ImagePreparation.validate(image, pixelLimit: limits.imagePixels) }
        let boundary = "eRecipe-\(UUID().uuidString)"
        let metadata: [String: Any] = ["kind": "images", "attachments": images.enumerated().map { i, image in
            ["part_name": "image_\(i + 1)", "filename": image.filename]
        }]
        let manifest = try JSONSerialization.data(withJSONObject: metadata)
        guard manifest.count <= 16_384 else { throw ClientError("Image metadata is too large.") }
        var body = Data()
        func append(_ string: String) { body.append(Data(string.utf8)) }
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"metadata\"\r\nContent-Type: application/json\r\n\r\n")
        body.append(manifest)
        for (i, image) in images.enumerated() {
            // Transport names are fixed; the original display name lives in the JSON manifest.
            append("\r\n--\(boundary)\r\nContent-Disposition: form-data; name=\"image_\(i + 1)\"; filename=\"image_\(i + 1)\"\r\nContent-Type: \(image.mediaType)\r\n\r\n")
            body.append(image.data)
        }
        append("\r\n--\(boundary)--\r\n")
        guard body.count <= limits.multipartBodyBytes else { throw ClientError("The combined upload is too large.") }
        return (body, "multipart/form-data; boundary=\(boundary)")
    }
}

struct PendingShare: Codable, Identifiable {
    let id: String
    let context: String
    let createdAt: Date
    let summary: String
    let contentType: String
    let importKey: String
    // Missing on older records: never send a previously review-only import implicitly.
    var autoSend: Bool?
    var phase = "saved"
    var recipeId: String?
    var jobId: String?
    var renderSet: RenderSet?
    var revision: Revision?
    var publication: PublicationReceipt?
    var publishKey: String?
    var publishPage: Int?
    var retryKey: String?
    var retryJobId: String?
    var retryCaptureId: String?
    var message: String?
}

enum Wire {
    static var decoder: JSONDecoder { let value = JSONDecoder(); value.keyDecodingStrategy = .convertFromSnakeCase; return value }
    static var encoder: JSONEncoder { let value = JSONEncoder(); value.keyEncodingStrategy = .convertToSnakeCase; return value }
}
