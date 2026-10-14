import XCTest
import UniformTypeIdentifiers
@testable import ERecipeCore

extension CoreTests {
    func testURLProviderAndTextLinkPreserveQueryAndFragment() async throws {
        let link = "https://example.com/salad?q=a%20b&x=1#recipe"
        let provider = NSItemProvider(item: URL(string: link)! as NSURL, typeIdentifier: UTType.url.identifier)
        let shared = try await SharedInputLoader.loadProviders([provider])
        XCTAssertEqual(shared.kind, "url"); XCTAssertEqual(shared.url, link)
        XCTAssertEqual(ShareInput.fromText("  \(link)\n").url, link)
        XCTAssertEqual(ShareInput.fromText("Recipe: \(link)\nMix chickpeas.").kind, "text")
        let saved = try store.create(input: shared, connection: connection, limits: status().capabilities.limits)
        let payload = try JSONSerialization.jsonObject(with: Data(contentsOf: store.bodyURL(saved.id))) as! [String: String]
        XCTAssertEqual(payload, ["kind": "url", "url": link])
        for invalid in ["https://user:pass@example.com/", "http://example.com:8000/", "https://example.com/a b", "https://example.com/\nsecret"] {
            XCTAssertThrowsError(try ShareInput(kind: "url", url: invalid).requestBody(limits: status().capabilities.limits))
        }
    }

    func testAutomaticURLDeliveryFollowsCaptureBeforeExtraction() throws {
        let item = try store.create(input: ShareInput(kind: "url", url: "https://example.com/recipe"), connection: connection, limits: status().capabilities.limits)
        try store.update(item.id) { $0.recipeId = "recipe_initial"; $0.jobId = "job_capture" }
        let delivery = AutomaticDelivery(store: store, connection: connection)
        var step = try XCTUnwrap(delivery.initial(item.id))
        step = try XCTUnwrap(delivery.consume(step, data: fixture("status")))
        var capture = try JSONSerialization.jsonObject(with: fixture("job_extracted")) as! [String: Any]
        capture["id"] = "job_capture"; capture["kind"] = "capture"; capture["status"] = "running"
        capture["downstream_job_ids"] = []; capture["result"] = ["source_capture_id": "capture_initial"]
        step = try XCTUnwrap(delivery.consume(step, data: JSONSerialization.data(withJSONObject: capture)))
        XCTAssertGreaterThan(step.delay, 0); XCTAssertNil(try store.item(item.id)?.publishKey)
        capture["status"] = "succeeded"; capture["downstream_job_ids"] = ["job_extract"]
        step = try XCTUnwrap(delivery.consume(step, data: JSONSerialization.data(withJSONObject: capture)))
        XCTAssertEqual(try step.path, "/api/v1/jobs/job_extract")
        step = try XCTUnwrap(delivery.consume(step, data: fixture("job_extracted")))
        XCTAssertEqual(try step.path, "/api/v1/jobs/job_render")
        XCTAssertNil(try store.item(item.id)?.publishKey)
    }

    func testCaptureRefreshReusesKeyAfterLostResponseAndKeepsRecipe() async throws {
        let item = try store.create(input: ShareInput(kind: "url", url: "https://example.com/recipe"), connection: connection, limits: status().capabilities.limits)
        try store.update(item.id) { $0.recipeId = "recipe_initial"; $0.jobId = "job_capture" }
        var failure = try JSONSerialization.jsonObject(with: fixture("job_configuration_missing")) as! [String: Any]
        failure["id"] = "job_capture"; failure["kind"] = "capture"; failure["source_capture_id"] = "capture_failed"; failure["retry_allowed"] = false
        let delivery = AutomaticDelivery(store: store, connection: connection)
        XCTAssertNil(try delivery.consume(DeliveryTransfer(itemId: item.id, step: .job("job_capture", remaining: [], visited: [])), data: JSONSerialization.data(withJSONObject: failure)))
        XCTAssertNil(try store.item(item.id)?.retryJobId)
        XCTAssertEqual(try store.item(item.id)?.retryCaptureId, "capture_failed")
        var keys = [String]()
        StubProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/api/v1/recipes/recipe_initial/refresh")
            var data = request.httpBody ?? Data()
            if let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 4096)
                while true {
                    let count = stream.read(&bytes, maxLength: bytes.count)
                    guard count > 0 else { break }
                    data.append(contentsOf: bytes.prefix(count))
                }
            }
            let body = try JSONSerialization.jsonObject(with: data) as! [String: String]
            XCTAssertEqual(body["source_capture_id"], "capture_failed")
            keys.append(request.value(forHTTPHeaderField: "Idempotency-Key")!)
            if keys.count == 1 { throw URLError(.networkConnectionLost) }
            return (202, try JSONSerialization.data(withJSONObject: ["recipe_id": "recipe_initial", "job_id": "capture_retry_job"]))
        }
        let workflow = Workflow(store: store, api: api(), connection: connection)
        do { try await workflow.retry(item.id); XCTFail("Expected lost response") } catch {}
        XCTAssertNotNil(try store.item(item.id)?.retryKey)
        try await workflow.retry(item.id)
        XCTAssertEqual(keys.count, 2); XCTAssertEqual(keys[0], keys[1])
        XCTAssertEqual(try store.item(item.id)?.jobId, "capture_retry_job")
        XCTAssertNil(try store.item(item.id)?.retryCaptureId)
        XCTAssertEqual(try store.item(item.id)?.recipeId, "recipe_initial")
        XCTAssertNotNil(try delivery.initial(item.id))
    }
}
