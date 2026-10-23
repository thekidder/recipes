#if canImport(UIKit)
import Foundation
import UIKit

// Used only by the explicit debug demo. Never intercepts the real client's session.
final class DemoProtocol: URLProtocol, @unchecked Sendable {
    private static let failureLock = NSLock()
    private static var archivedRecipe = false
    private static var injectedFailures = Set<String>()
    private static let statusRecoveryDate = Date().addingTimeInterval(5)
    private static func failOnce(_ flag: String) -> Bool {
        guard ProcessInfo.processInfo.arguments.contains(flag) else { return false }
        failureLock.lock(); defer { failureLock.unlock() }
        return injectedFailures.insert(flag).inserted
    }
    static func session() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [DemoProtocol.self]
        return URLSession(configuration: config)
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        do {
            let path = request.url!.path
            if path == "/api/v1/status", ProcessInfo.processInfo.arguments.contains("--demo-status-failure"), Date() < Self.statusRecoveryDate {
                throw URLError(.cannotConnectToHost)
            }
            if path == "/api/v1/imports", Self.failOnce("--demo-upload-failure") {
                finish(Data("{}".utf8), status: 503, type: "application/json"); return
            }
            var fixture: String
            var status = 200
            switch path {
            case "/api/v1/status": fixture = "status"
            case "/api/v1/recipes": fixture = "library"
            case "/api/v1/recipes/recipe_salad": fixture = "ready_recipe"
            case "/api/v1/recipes/recipe_salad/archive":
                if Self.failOnce("--demo-delete-failure") {
                    finish(Data("{}".utf8), status: 503, type: "application/json"); return
                }
                Self.failureLock.lock(); Self.archivedRecipe = true; Self.failureLock.unlock()
                fixture = "archived"
            case "/api/v1/imports": fixture = "import_text"; status = 202
            case "/api/v1/jobs/job_extract": fixture = "job_extracted"
            case "/api/v1/jobs/job_render": fixture = "job_extracted"
            case "/api/v1/jobs/job_capture": fixture = "job_extracted"
            case "/api/v1/revisions/revision_initial": fixture = "revision"
            case "/api/v1/render-sets/render_initial": fixture = "render_set"
            case "/api/v1/publications": fixture = "publication_selected"; status = 201
            case "/api/v1/device": fixture = "device_selected"
            default:
                if path.hasPrefix("/api/v1/assets/") {
                    let data = try Data(contentsOf: Bundle.main.url(forResource: "demo-preview", withExtension: "png")!)
                    finish(data, status: 200, type: "image/png"); return
                }
                throw ClientError("No demo fixture for this operation.")
            }
            guard let file = Bundle.main.url(forResource: fixture, withExtension: "json") else { throw ClientError("Missing demo fixture.") }
            let envelope = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as! [String: Any]
            var body = (envelope["response"] as! [String: Any])["body"] as! [String: Any]
            Self.failureLock.lock(); let archived = Self.archivedRecipe; Self.failureLock.unlock()
            if path == "/api/v1/recipes", archived { body["items"] = [] }
            if path == "/api/v1/recipes/recipe_salad", archived { body["archived_at"] = "2026-10-22T12:00:00.000Z" }
            if path == "/api/v1/imports" {
                if try importKind() == "url" {
                    body["job_id"] = "job_capture"
                }
            }
            if path == "/api/v1/jobs/job_capture" {
                body["id"] = "job_capture"; body["kind"] = "capture"; body["downstream_job_ids"] = ["job_extract"]
                body["result"] = ["source_capture_id": "capture_initial"]
            }
            if path == "/api/v1/jobs/job_render" {
                body["id"] = "job_render"; body["kind"] = "render"; body["downstream_job_ids"] = []
                body["result"] = ["revision_id": "revision_initial", "render_set_id": "render_initial"]
            }
            if path.hasPrefix("/api/v1/jobs/"), ProcessInfo.processInfo.arguments.contains("--demo-processing-queued") {
                body["status"] = "queued"; body["result"] = [:]; body["downstream_job_ids"] = []
            }
            finish(try JSONSerialization.data(withJSONObject: body), status: status, type: "application/json")
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    private func importKind() throws -> String? {
        guard request.value(forHTTPHeaderField: "Content-Type") == "application/json" else { return nil }
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var bytes = [UInt8](repeating: 0, count: 4096)
            while true {
                let count = stream.read(&bytes, maxLength: bytes.count)
                guard count >= 0 else { throw ClientError("Could not read demo import.") }
                if count == 0 { break }
                data.append(contentsOf: bytes.prefix(count))
                guard data.count <= 1_048_576 else { throw ClientError("Demo import exceeds the text limit.") }
            }
        }
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any])?["kind"] as? String
    }
    private func finish(_ data: Data, status: Int, type: String) {
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
            headerFields: ["Content-Type": type])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}
#endif
